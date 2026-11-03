# Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
#
# Python port of the rtldavis receiver, Copyright (C) 2015 Douglas Hall,
# modified by Luc Heijst 2019-2020 (multi-transmitter hopping, EU and NZ
# frequencies) and Steve Wormley 2020 (device selection).
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <http://www.gnu.org/licenses/>.

"""Follow Davis transmitters through their frequency hops with an RTL-SDR dongle.

``Receiver`` holds the hopping logic and is independent of the hardware: it is
fed sample blocks with the time of their last sample and says which frequency to
tune next. ``Radio`` runs it in a thread against a dongle through librtlsdr.

The hop schedule follows Luc Heijst's rtldavis: listen on the first channel of
the hop sequence until every configured transmitter was heard once, then
predict each transmitter's next channel and time from its fixed period.

Dongles without a temperature-compensated oscillator can be tens of kHz off at
868 to 928 MHz. The band tables are not certain either: they differ by 10 to
37 kHz from the tables of the DavisRFM69 receiver, and in the EU band the
channel spacing differs as well. Until the first frame is decoded, the receiver
therefore searches several tuning offsets. Afterwards it corrects the tuning per
transmitter and channel from the frequency error of each received frame.
"""

import ctypes
import ctypes.util
import grp
import importlib
import logging
import os
import pwd
import queue
import sys
import threading
import time

log = logging.getLogger(__name__)

SAMPLE_RATE = 268800

# Channel frequencies in Hz, in ascending order, and the order in which the
# transmitters visit them. Both are taken unchanged from rtldavis 0.16.
BANDS = {
    'EU': (
        (868077250, 868197250, 868317250, 868437250, 868557250),
        (0, 2, 4, 1, 3)),
    'US': (
        (902419338, 902921088, 903422839, 903924589, 904426340, 904928090,
         905429841, 905931591, 906433342, 906935092, 907436843, 907938593,
         908440344, 908942094, 909443845, 909945595, 910447346, 910949096,
         911450847, 911952597, 912454348, 912956099, 913457849, 913959599,
         914461350, 914963100, 915464850, 915966601, 916468351, 916970102,
         917471852, 917973603, 918475353, 918977104, 919478854, 919980605,
         920482355, 920984106, 921485856, 921987607, 922489357, 922991108,
         923492858, 923994609, 924496359, 924998110, 925499860, 926001611,
         926503361, 927005112, 927506862),
        (0, 19, 41, 25, 8, 47, 32, 13, 36, 22, 3, 29, 44, 16, 5, 27, 38,
         10, 49, 21, 2, 30, 42, 14, 48, 7, 24, 34, 45, 1, 17, 39, 26, 9,
         31, 50, 37, 12, 20, 33, 4, 43, 28, 15, 35, 6, 40, 11, 23, 46, 18)),
    'NZ': (
        (921070709, 921207977, 921345642, 921482910, 921620178, 921757446,
         921894714, 922031586, 922168854, 922306519, 922443787, 922581055,
         922718323, 922855591, 922992859, 923130127, 923267395, 923404266,
         923541931, 923678802, 923816467, 923953735, 924091003, 924228271,
         924365143, 924502411, 924638885, 924776550, 924913818, 925051086,
         925188354, 925325623, 925462494, 925600159, 925737030, 925874695,
         926011963, 926149231, 926286499, 926423370, 926561035, 926697906,
         926835571, 926972839, 927110107, 927246979, 927384644, 927521515,
         927658783, 927796448, 927933716),
        (0, 19, 41, 25, 8, 47, 32, 13, 36, 22, 3, 29, 44, 16, 5, 27, 38,
         10, 49, 21, 2, 30, 42, 14, 48, 7, 24, 34, 45, 1, 17, 39, 26, 9,
         31, 50, 37, 12, 20, 33, 4, 43, 28, 15, 35, 6, 40, 11, 23, 46, 18)),
}

# Transmitter ID 0 (channel 1) sends every 2.5625 s, each higher ID 62.5 ms later.
BASE_PERIOD = 2.5625
PERIOD_STEP = 0.0625
# How long after the predicted time a frame may still arrive.
RECEIVE_WINDOW = PERIOD_STEP + 0.3
# Frames of one transmitter closer together than this are decodes of one burst.
BURST_GUARD = 2.0
# Missed frames in a row after which a transmitter is searched for again.
MAX_MISSED = 51

# Tuning offsets in Hz searched one after another until the first frame. Each is
# also searched at three digital shifts 20 kHz apart, and each shift decodes
# about 10 kHz either side, so one step covers 60 kHz and the steps together
# cover +-150 kHz. That is +-170 ppm at 868 MHz.
#
# The search listens on the lowest channel of the band. A transmitter on the
# next channel up appears there when the dongle's error is about minus one
# channel spacing, and would be taken for the lowest channel with a small error.
# Negative steps therefore come before positive ones, so that a dongle reading
# low is found on the right channel first. In the EU band, with 120 kHz between
# channels, errors below about -90 kHz cannot be told apart from the next channel.
SEARCH_STEPS = (0, -60000, 60000, 120000, -120000)
WIDE_SHIFTS = (-20000, 0, 20000)
# Share of each new frequency error added to a learned offset. Errors are
# measured from 16 noisy samples; halving them keeps one outlier from moving
# the tuning out of range.
CORRECTION_GAIN = 0.5
# Without any frame in this many synchronization periods, the frequency is
# searched again from the last known offset.
LOST_LOCK_PERIODS = 2
# A transmitter on a neighbouring channel, taken for the lowest one, still
# matches some predictions by chance. After the first frame, the receiver
# therefore checks this many predictions. If fewer than a quarter of them arrive,
# the frame is taken for another channel and the search continues.
PROBATION = 20
PROBATION_MINIMUM = 8


def transmitter_period(transmitter):
    """Return the transmission interval in seconds of a zero-based transmitter ID."""
    return BASE_PERIOD + transmitter * PERIOD_STEP


class Frame:
    """A frame from a configured transmitter, accepted by the receiver.

    Attributes:
        transmitter (int): Zero-based transmitter ID; the console channel minus 1.
        payload (bytes): Eight bytes including the CRC.
        time (float): time.monotonic() of the sync word, in the clock the
            receiver was fed with.
        counts (tuple[int, ...]): Frames received from each configured
            transmitter since the receiver started, ordered by transmitter ID.
        channel (int): Index of the channel frequency in its band.
        frequency_offset (float): Learned offset in Hz of this transmitter on
            this channel from the band table.
        frequency_error (float): Carrier offset in Hz from the frequency the
            receiver was tuned to. Near zero once the offset is learned.
    """

    def __init__(self, transmitter, payload, time, counts, channel, frequency_offset,
                 frequency_error=0.0):
        self.transmitter = transmitter
        self.payload = payload
        self.time = time
        self.counts = counts
        self.channel = channel
        self.frequency_offset = frequency_offset
        self.frequency_error = frequency_error


class Receiver:
    """Hop with the configured transmitters and return their frames.

    Call ``start`` once, then ``feed`` every sample block in order. After each
    call, tune the dongle to ``frequency``.

    Args:
        band (str): 'EU', 'US' or 'NZ'.
        transmitters (list[int]): Zero-based IDs of the transmitters to follow.
        ppm (float): Expected frequency error of the dongle in ppm. The search
            starts there. Default is 0.
    """

    def __init__(self, band, transmitters, ppm=0.0):
        self.frequencies, self.pattern = BANDS[band]
        self.band = band
        self.sequence_of = {channel: position for position, channel in enumerate(self.pattern)}
        self.transmitters = sorted(set(transmitters))
        self.index = {transmitter: i for i, transmitter in enumerate(self.transmitters)}
        self.sync_period = (len(self.frequencies) + 2) * transmitter_period(self.transmitters[-1])
        self.offset = ppm * 1e-6 * self.frequencies[0]
        self.locked = False
        self.search_base = self.offset
        self.search_step = 0
        self.learned = {}
        self.counts = [0] * len(self.transmitters)
        self.frequency = None
        self.channel = None
        self.shifts = WIDE_SHIFTS
        self._demodulator = None
        self._missed_channels = set()
        self._reported = set()
        self._probation = None

    def start(self, now):
        """Start listening from a new sample stream.

        Learned offsets and counters are kept, so a restart after a USB error
        resumes without a new frequency search.

        Args:
            now (float): Time of the first sample.
        """
        dsp = importlib.import_module('user.rtldavis_dsp')
        self._demodulator = dsp.Demodulator()
        self._accepted = {}
        self._empty_periods = 0
        self._tunings = []
        self._synchronize(now)

    def feed(self, raw, end_time):
        """Demodulate a sample block and advance the hop schedule.

        Args:
            raw (bytes): Interleaved unsigned 8-bit IQ samples at SAMPLE_RATE.
            end_time (float): Time of the last sample in the block.

        Returns:
            list[Frame]: Frames from configured transmitters, in time order.
        """
        # The dongle is retuned after each call, so this block was sampled at
        # the tuning in effect now. A burst can be returned a block after it
        # was sampled, and one frame can retune before the next is handled, so
        # each frame looks up the tuning in effect at its own first sample.
        if not self._tunings or self._tunings[-1][1:] != (self.channel, self.frequency):
            self._tunings.append((self._demodulator.sample_count, self.channel, self.frequency))
            del self._tunings[:-8]
        candidates = self._demodulator.feed(raw, self.shifts)
        end_index = self._demodulator.sample_count
        frames = []
        for candidate in candidates:
            frame_time = end_time - (end_index - candidate.index) / SAMPLE_RATE
            channel, frequency = next(
                ((channel, frequency) for index, channel, frequency in reversed(self._tunings)
                 if index <= candidate.index), self._tunings[0][1:])
            frame = self._accept(candidate, frame_time, channel, frequency)
            if frame is not None:
                frames.append(frame)
        # The demodulator returns a burst a few milliseconds after it ends. A
        # deadline in that gap waits, so that a late frame still counts for
        # the channel it was sent on.
        settled = end_time - (end_index - self._demodulator.settled) / SAMPLE_RATE
        while settled >= self._deadline:
            self._timeout(self._deadline)
        return frames

    def _accept(self, candidate, now, channel, frequency):
        """Apply one decoded frame to the schedule; return a Frame or None.

        Args:
            candidate (user.rtldavis_dsp.Candidate): The decoded frame.
            now (float): Time of its sync word.
            channel (int): Index of the channel it was received on.
            frequency (int): Frequency in Hz the dongle was tuned to.
        """
        transmitter = candidate.payload[0] & 7
        previous = self._accepted.get(transmitter)
        # A noisy burst can decode twice, even with two different CRC-valid
        # payloads. A transmitter never sends twice within 2.5 s.
        if previous is not None and now - previous < BURST_GUARD:
            log.debug("ignoring second decode of a burst: %s", candidate.payload.hex())
            return None
        self._accepted[transmitter] = now
        # Another station's transmitter may be off by a different amount, and
        # its frames would keep the search from moving on.
        if transmitter not in self.index:
            self._report_unconfigured(transmitter)
            return None
        self._heard = True
        offset = self._learn(transmitter, channel,
                             frequency - self.frequencies[channel] + candidate.frequency_error)
        i = self.index[transmitter]
        self.counts[i] += 1
        self._missed[i] = 0
        hop = False
        if self._syncing:
            if self._last_visit[i] is None:
                log.info("Receiving transmitter on channel %d", transmitter + 1)
                self._last_hop[i] = self.sequence_of[channel]
                self._last_visit[i] = now
                if all(visit is not None for visit in self._last_visit):
                    if len(self.transmitters) > 1:
                        log.info("Receiving all configured transmitters")
                    self._syncing = False
                    hop = True
            else:
                self._last_visit[i] = now
        else:
            self._last_hop[i] = self.sequence_of[channel]
            self._last_visit[i] = now
            self._check_probation(now, hit=True)
            hop = True
        frame = Frame(transmitter, candidate.payload, now, tuple(self.counts),
                      channel, offset, candidate.frequency_error)
        log.debug("frame %s from channel %d on %d Hz, error %+.0f Hz",
                  candidate.payload.hex(), transmitter + 1,
                  frequency, candidate.frequency_error)
        if hop:
            self._hop_next(now)
        return frame

    def _learn(self, transmitter, channel, offset):
        """Record a measured offset from the band table and return the learned one.

        Args:
            transmitter (int): Zero-based ID of the sender.
            channel (int): Index of the channel it was received on.
            offset (float): Carrier minus table frequency in Hz, as measured.

        Returns:
            float: The offset now used for this transmitter on this channel.
        """
        key = (transmitter, channel)
        if not self.locked:
            self.locked = True
            self._probation = [0, 0]
            self.offset = offset
            log.info("Found a Davis transmitter %+.1f kHz (%+.0f ppm) from the table "
                     "frequency. The receiver corrects this automatically.",
                     offset / 1000, offset / self.frequencies[channel] * 1e6)
        if key in self.learned:
            self.learned[key] += CORRECTION_GAIN * (offset - self.learned[key])
        else:
            self.learned[key] = offset
        self.offset += CORRECTION_GAIN * (offset - self.offset)
        self._missed_channels.discard(key)
        return self.learned[key]

    def _check_probation(self, now, hit):
        """Count a prediction after the first frame; return True if the lock was false."""
        if self._probation is None:
            return False
        self._probation[0 if hit else 1] += 1
        hits, misses = self._probation
        if hits + misses >= PROBATION_MINIMUM and hits * 3 < misses:
            log.info("Few frames arrive at %+.1f kHz; the first one probably came "
                     "from a neighbouring channel. Searching on.", self.offset / 1000)
            self._probation = None
            self.locked = False
            self.learned.clear()
            self.search_step = (self.search_step + 1) % len(SEARCH_STEPS)
            self._synchronize(now)
            return True
        if hits + misses >= PROBATION:
            self._probation = None
        return False

    def _report_unconfigured(self, transmitter):
        if transmitter in self._reported:
            return
        self._reported.add(transmitter)
        configured = ', '.join(str(t + 1) for t in self.transmitters)
        log.info("Heard a Davis transmitter on channel %d, but only channel %s is "
                 "configured. If this transmitter belongs to your station, set its "
                 "channel in [Rtldavis], for example iss_channel = %d.",
                 transmitter + 1, configured, transmitter + 1)

    def _synchronize(self, now):
        """Listen on the first channel of the sequence until each transmitter sends."""
        self._syncing = True
        self._heard = False
        self._last_visit = [None] * len(self.transmitters)
        self._last_hop = [0] * len(self.transmitters)
        self._missed = [0] * len(self.transmitters)
        self._tune(0, self.transmitters[0])
        self._deadline = now + self.sync_period
        if not self.locked:
            log.info("Searching for transmitter(s) on channel %s at %+.0f kHz "
                     "(step %d of %d), up to %.0f s",
                     ', '.join(str(t + 1) for t in self.transmitters),
                     (self.search_base + SEARCH_STEPS[self.search_step]) / 1000,
                     self.search_step + 1, len(SEARCH_STEPS), self.sync_period)

    def _tune(self, sequence, transmitter):
        """Select the channel at a sequence position and its corrected frequency."""
        self.channel = self.pattern[sequence % len(self.pattern)]
        key = (transmitter, self.channel)
        if not self.locked:
            offset = self.search_base + SEARCH_STEPS[self.search_step]
        else:
            offset = self.learned.get(key, self.offset)
        self.frequency = int(round(self.frequencies[self.channel] + offset))
        # A channel without a learned offset can be off by the table error, and
        # one that was just missed may have drifted. Search those more widely.
        wide = not self.locked or key not in self.learned or key in self._missed_channels
        self.shifts = WIDE_SHIFTS if wide else (0,)
        self._expected = key

    def _hop_next(self, now):
        """Tune to the transmitter that sends next and set the deadline for it."""
        best = None
        for i, transmitter in enumerate(self.transmitters):
            visit = self._last_visit[i]
            if visit is None:
                visit = self._last_visit[i] = now
            hop = self._last_hop[i]
            period = transmitter_period(transmitter)
            while visit <= now:
                visit += period
                hop = (hop + 1) % len(self.pattern)
            if best is None or visit < best[0]:
                best = (visit, hop, transmitter, i)
        visit, hop, transmitter, i = best
        self._next = i
        self._tune(hop, transmitter)
        self._deadline = visit + RECEIVE_WINDOW
        log.debug("expecting channel %d in %.2f s on %d Hz", transmitter + 1,
                  visit - now, self.frequency)

    def _timeout(self, now):
        """Handle a deadline: a missed frame, or a synchronization period without one."""
        if self._syncing:
            if not self.locked:
                self.search_step = (self.search_step + 1) % len(SEARCH_STEPS)
            elif self._heard:
                self._empty_periods = 0
            else:
                self._empty_periods += 1
                if self._empty_periods >= LOST_LOCK_PERIODS:
                    log.info("No Davis transmitter heard for %.0f s, searching the "
                             "frequency again", self._empty_periods * self.sync_period)
                    self.locked = False
                    self.learned.clear()
                    self.search_base = self.offset
                    self.search_step = 0
                    self._empty_periods = 0
            self._synchronize(now)
            return
        i = self._next
        transmitter = self.transmitters[i]
        self._last_visit[i] += transmitter_period(transmitter)
        self._last_hop[i] = (self._last_hop[i] + 1) % len(self.pattern)
        self._missed[i] += 1
        self._missed_channels.add(self._expected)
        log.debug("missed frame %d in a row from channel %d", self._missed[i], transmitter + 1)
        if self._check_probation(now, hit=False):
            return
        if self._missed[i] > MAX_MISSED:
            log.info("Lost transmitter on channel %d, synchronizing again", transmitter + 1)
            self._synchronize(now)
        else:
            self._hop_next(now)


class Problem(Exception):
    """A condition the user has to fix. The message says how."""


def _install_hint(package_apt, package_pip):
    """Return how to install a dependency in the Python environment WeeWX uses."""
    if sys.prefix != getattr(sys, 'base_prefix', sys.prefix):
        return "%s -m pip install %s" % (sys.executable, package_pip)
    return "sudo apt install %s" % package_apt


def load_numpy():
    """Import the demodulator, or raise Problem with installation instructions."""
    try:
        importlib.import_module('user.rtldavis_dsp')
    except ImportError as e:
        if getattr(e, 'name', None) != 'numpy':
            raise
        raise Problem("numpy is missing. Install it with: %s"
                      % _install_hint('python3-numpy', 'numpy'))


class RtlSdr:
    """Minimal ctypes binding of librtlsdr for one open dongle."""

    _CALLBACK = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_ubyte),
                                 ctypes.c_uint32, ctypes.c_void_p)
    _library = None

    @classmethod
    def library(cls):
        """Load librtlsdr once, or raise Problem with installation instructions."""
        if cls._library is None:
            name = ctypes.util.find_library('rtlsdr')
            try:
                library = ctypes.CDLL(name or 'librtlsdr.so.0')
            except OSError as e:
                if name:
                    raise Problem("librtlsdr is installed but cannot be loaded: %s" % e)
                raise Problem("librtlsdr is missing. Install it with: sudo apt install "
                              "rtl-sdr (Fedora and Arch: rtl-sdr as well)")
            pointer = ctypes.c_void_p
            library.rtlsdr_get_device_count.restype = ctypes.c_uint32
            library.rtlsdr_get_device_name.restype = ctypes.c_char_p
            library.rtlsdr_get_device_name.argtypes = [ctypes.c_uint32]
            library.rtlsdr_get_index_by_serial.argtypes = [ctypes.c_char_p]
            library.rtlsdr_open.argtypes = [ctypes.POINTER(pointer), ctypes.c_uint32]
            library.rtlsdr_close.argtypes = [pointer]
            library.rtlsdr_set_center_freq.argtypes = [pointer, ctypes.c_uint32]
            library.rtlsdr_set_sample_rate.argtypes = [pointer, ctypes.c_uint32]
            library.rtlsdr_set_tuner_gain_mode.argtypes = [pointer, ctypes.c_int]
            library.rtlsdr_set_tuner_gain.argtypes = [pointer, ctypes.c_int]
            library.rtlsdr_get_tuner_gains.argtypes = [pointer, ctypes.POINTER(ctypes.c_int)]
            library.rtlsdr_reset_buffer.argtypes = [pointer]
            library.rtlsdr_read_async.argtypes = [pointer, cls._CALLBACK, pointer,
                                                  ctypes.c_uint32, ctypes.c_uint32]
            library.rtlsdr_cancel_async.argtypes = [pointer]
            library.rtlsdr_read_sync.argtypes = [pointer, ctypes.c_void_p, ctypes.c_int,
                                                 ctypes.POINTER(ctypes.c_int)]
            cls._library = library
        return cls._library

    def __init__(self, device):
        """Open a dongle, or raise Problem explaining why it cannot be opened.

        Args:
            device (str): Index such as '0', or the dongle's serial number.
        """
        library = self.library()
        count = library.rtlsdr_get_device_count()
        if count == 0:
            raise Problem("No RTL-SDR dongle found. Plug it in; the receiver "
                          "starts as soon as it appears.")
        index = library.rtlsdr_get_index_by_serial(device.encode())
        if index < 0:
            index = int(device) if device.isdigit() else -1
        if not 0 <= index < count:
            raise Problem("RTL-SDR device %r not found. %d dongle(s) present; set "
                          "device to an index from 0 or a serial number." % (device, count))
        self._device = ctypes.c_void_p()
        result = library.rtlsdr_open(ctypes.byref(self._device), index)
        if result < 0:
            raise Problem(_open_failure(result))
        log.info("Using RTL-SDR dongle %d: %s", index,
                 library.rtlsdr_get_device_name(index).decode(errors='replace'))

    def _check(self, result, action):
        if result < 0:
            raise OSError("librtlsdr failed to %s (error %d)" % (action, result))

    def configure(self, gain):
        """Set the sample rate and gain.

        Args:
            gain (float|None): Tuner gain in dB, or None for automatic gain. The
                nearest gain the tuner supports is used.
        """
        self._check(self._library.rtlsdr_set_sample_rate(self._device, SAMPLE_RATE),
                    'set the sample rate')
        if gain is None:
            self._check(self._library.rtlsdr_set_tuner_gain_mode(self._device, 0),
                        'select automatic gain')
            return
        self._check(self._library.rtlsdr_set_tuner_gain_mode(self._device, 1),
                    'select manual gain')
        count = self._library.rtlsdr_get_tuner_gains(self._device, None)
        gains = (ctypes.c_int * max(count, 1))()
        self._library.rtlsdr_get_tuner_gains(self._device, gains)
        tenths = min(gains[:count] or [int(gain * 10)], key=lambda g: abs(g - gain * 10))
        self._check(self._library.rtlsdr_set_tuner_gain(self._device, tenths), 'set the gain')
        log.info("Tuner gain %.1f dB", tenths / 10)

    def tune(self, frequency):
        self._check(self._library.rtlsdr_set_center_freq(self._device, frequency),
                    'tune to %d Hz' % frequency)

    def stream(self, receive, synchronous=False):
        """Call receive(data, time) for each USB block until cancel() or an error.

        Blocks the calling thread, which also makes the calls to receive.

        Args:
            receive (Callable[[bytes, float], None]): Called as
                ``receive(data, time)`` with a block of IQ bytes and the
                time.monotonic() at which it arrived.
            synchronous (bool): True to read one block at a time instead of
                keeping several USB transfers queued. Default is False.

        Returns:
            int: librtlsdr's result; nonzero after a USB error.
        """
        self._check(self._library.rtlsdr_reset_buffer(self._device), 'reset the buffer')
        self._cancelled = False
        if synchronous:
            block = ctypes.create_string_buffer(16384)
            length = ctypes.c_int()
            while not self._cancelled:
                result = self._library.rtlsdr_read_sync(self._device, block, len(block),
                                                        ctypes.byref(length))
                if result < 0:
                    return result
                receive(block.raw[:length.value], time.monotonic())
            return 0

        def callback(buffer, length, context):
            receive(ctypes.string_at(buffer, length), time.monotonic())

        self._callback = self._CALLBACK(callback)
        # 8 transfers of 30 ms let processing pause for 240 ms without losing
        # samples. librtlsdr needs the length in multiples of 512 bytes.
        return self._library.rtlsdr_read_async(self._device, self._callback, None, 8, 16384)

    def cancel(self):
        """End stream(). Safe to call from receive and from another thread."""
        self._cancelled = True
        self._library.rtlsdr_cancel_async(self._device)

    def close(self):
        self._library.rtlsdr_close(self._device)


def _open_failure(result):
    """Explain a negative librtlsdr open result, which is a libusb error code."""
    user = pwd.getpwuid(os.geteuid()).pw_name
    if result == -3:
        try:
            plugdev = grp.getgrnam('plugdev').gr_gid
        except KeyError:
            plugdev = None
        if plugdev is not None and plugdev not in os.getgroups():
            return ("No permission to open the RTL-SDR dongle. Add user %s to the "
                    "plugdev group, then restart WeeWX: sudo usermod -aG plugdev %s"
                    % (user, user))
        return ("No permission to open the RTL-SDR dongle. Install rtl-sdr, whose library "
                "package provides the udev rule, then unplug and reconnect the dongle.")
    if result == -6:
        return ("The RTL-SDR dongle is in use. Stop other programs using it, such as "
                "rtl_tcp, rtl_433 or a second WeeWX. If no other program runs, the DVB "
                "kernel driver holds it: sudo modprobe -r dvb_usb_rtl28xxu, and to make "
                "this permanent, echo 'blacklist dvb_usb_rtl28xxu' | sudo tee "
                "/etc/modprobe.d/blacklist-rtlsdr.conf")
    return "Cannot open the RTL-SDR dongle (libusb error %d)." % result


def check(device):
    """Print whether each requirement of the receiver is met.

    Args:
        device (str): Dongle index or serial number to open.

    Returns:
        bool: True if the dongle could be opened.
    """
    ready = True
    try:
        load_numpy()
        print("numpy: ok")
    except Problem as problem:
        print("numpy: %s" % problem)
        ready = False
    try:
        RtlSdr.library()
        print("librtlsdr: ok")
    except Problem as problem:
        print("librtlsdr: %s" % problem)
        return False
    try:
        RtlSdr(device).close()
        print("dongle: ok")
    except Problem as problem:
        print("dongle: %s" % problem)
        ready = False
    if ready:
        print("Ready. Start WeeWX; the first observations follow within a few minutes.")
    return ready


class Radio(threading.Thread):
    """Run a Receiver against a dongle until stopped, recovering from USB errors.

    Problems the user has to fix are logged with instructions and retried, so a
    dongle plugged in or a package installed later is picked up without a
    restart. Frames appear in ``frames``.

    Args:
        receiver (Receiver): Hop logic to run.
        device (str): Dongle index or serial number. Default is '0'.
        gain (float|None): Tuner gain in dB, or None for automatic gain.
    """

    RETRY = 15
    REPEAT_PROBLEM = 600
    # librtlsdr 0.6.0-4, which Debian bookworm and Raspberry Pi OS based on it
    # ship, queues its USB transfers in buffers mapped with mmap. On ARM, a
    # kernel fault can leave these buffers zeroed, and librtlsdr's check for the
    # fault misses it most of the time (Debian bug #1041701). Synchronous reads
    # do not use these buffers. After this many all-zero blocks in a row, the
    # radio switches to them; a real dongle never delivers a zero block.
    EMPTY_BLOCKS = 3
    # Seconds to wait for the USB thread after cancelling.
    USB_JOIN = 5

    def __init__(self, receiver, device='0', gain=None):
        threading.Thread.__init__(self, name='rtldavis-radio', daemon=True)
        self.receiver = receiver
        self.device = device
        self.gain = gain
        self.frames = queue.Queue()
        self.failure = None
        self._stopping = threading.Event()
        self._dongle = None
        self._problem = (None, 0)
        self._synchronous = False

    def run(self):
        try:
            while not self._stopping.is_set():
                try:
                    load_numpy()
                    self._dongle = RtlSdr(self.device)
                except Problem as problem:
                    self._report(str(problem))
                    self._stopping.wait(self.RETRY)
                    continue
                self._problem = (None, 0)
                try:
                    self._run_dongle(self._dongle)
                except OSError as e:
                    log.error("RTL-SDR error: %s. Reconnecting.", e)
                finally:
                    self._dongle = None
                if not self._stopping.is_set():
                    self._stopping.wait(self.RETRY)
        except Exception as e:
            # Not a hardware condition but a defect. The driver raises it in the
            # WeeWX loop, which logs it and restarts the driver.
            log.exception("receiver failed")
            self.failure = e

    def _report(self, message):
        """Log a problem when it is new, and again every REPEAT_PROBLEM seconds."""
        last, logged = self._problem
        if message != last or time.monotonic() - logged > self.REPEAT_PROBLEM:
            log.error(message)
            self._problem = (message, time.monotonic())

    def _run_dongle(self, dongle):
        """Stream from an open dongle until stopped or USB fails, then close it."""
        blocks = queue.Queue(maxsize=100)
        dropped = [0]
        empty = [0]
        result = []
        # A synchronous read cannot be cancelled. If one is still running when
        # streaming ends, the USB thread closes the dongle once the read
        # returns, never this thread while the read still uses it.
        lock = threading.Lock()
        usb_state = {'running': False, 'close': False}

        def receive(data, arrival):
            if not data.strip(b'\0'):
                empty[0] += 1
                if empty[0] >= self.EMPTY_BLOCKS and not self._synchronous:
                    dongle.cancel()
                return
            empty[0] = 0
            try:
                blocks.put_nowait((data, arrival))
            except queue.Full:
                dropped[0] += 1

        def stream():
            try:
                result.append(dongle.stream(receive, self._synchronous))
            finally:
                with lock:
                    usb_state['running'] = False
                    if usb_state['close']:
                        dongle.close()

        try:
            dongle.configure(self.gain)
            self.receiver.start(time.monotonic())
            tuned = self.receiver.frequency
            dongle.tune(tuned)
            while True:
                del result[:]
                with lock:
                    usb_state['running'] = True
                usb = threading.Thread(target=stream, name='rtldavis-usb', daemon=True)
                usb.start()
                try:
                    while usb.is_alive() or not blocks.empty():
                        if self._stopping.is_set():
                            break
                        try:
                            data, arrival = blocks.get(timeout=1)
                        except queue.Empty:
                            continue
                        for frame in self.receiver.feed(data, arrival):
                            self.frames.put(frame)
                        if self.receiver.frequency != tuned:
                            tuned = self.receiver.frequency
                            try:
                                dongle.tune(tuned)
                            except OSError as e:
                                # The next hop retunes anyway; one failure is not fatal.
                                log.warning("%s", e)
                        if dropped[0]:
                            log.warning("Processing fell behind; %d sample blocks lost",
                                        dropped[0])
                            dropped[0] = 0
                finally:
                    dongle.cancel()
                    usb.join(self.USB_JOIN)
                if self._stopping.is_set() or usb.is_alive():
                    return
                if empty[0] >= self.EMPTY_BLOCKS and not self._synchronous:
                    log.warning("librtlsdr delivers empty sample blocks, a known fault of "
                                "librtlsdr 0.6.0 on ARM. Reading synchronously instead.")
                    self._synchronous = True
                    empty[0] = 0
                    continue
                log.error("The RTL-SDR dongle stopped delivering samples (result %s). "
                          "Reconnecting.", result[0] if result else 'unknown')
                return
        finally:
            with lock:
                if usb_state['running']:
                    log.error("A USB read did not end. The dongle is closed when it does.")
                    usb_state['close'] = True
                else:
                    dongle.close()

    def stop(self):
        """Stop streaming and wait for the thread. Safe to call more than once."""
        self._stopping.set()
        if self.is_alive():
            self.join(10)


def replay(path):
    """Decode a recording without hopping, as an offline check.

    Args:
        path (str): File of unsigned 8-bit IQ samples at 268800 samples/s,
            tuned as the receiver tunes a channel.

    Yields:
        Candidate: Each CRC-valid frame, from any transmitter.
    """
    dsp = importlib.import_module('user.rtldavis_dsp')
    demodulator = dsp.Demodulator()
    with open(path, 'rb') as recording:
        while True:
            block = recording.read(16384)
            if len(block) < 2:
                break
            for candidate in demodulator.feed(block[:len(block) // 2 * 2], WIDE_SHIFTS):
                yield candidate
    for candidate in demodulator.flush():
        yield candidate
