# Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
#
# Python port of the rtldavis demodulator, Copyright (C) 2015 Douglas Hall,
# modified by Luc Heijst 2019-2020 and Steve Wormley 2020.
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

"""Demodulate Davis ISS frames from unsigned 8-bit RTL-SDR samples.

The signal chain follows the original rtldavis receiver: quarter-rate rotation,
a 9-tap low-pass filter, a phase discriminator and a search for the 16-bit sync
word. Each symbol spans 14 samples at 268800 samples per second.

The sync word is searched with two decision thresholds. The fixed threshold of
zero is the original one and is the most sensitive when the tuning is correct.
The second threshold is the mean of the 16 sync-word samples. The sync word has
as many ones as zeros, so this mean is the frequency error of the burst. Slicing
against it decodes frames whose carrier is up to about 20 kHz off, where the
fixed threshold fails beyond about 3 kHz. The receiver uses this to find the
frequency error of an uncalibrated dongle.
"""

import math

import numpy

SYMBOL_LENGTH = 14
SAMPLE_RATE = 19200 * SYMBOL_LENGTH
SYNC_WORD = (1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 0, 1)
# A frame is the sync word, six data bytes, a CRC-16 and nothing else the
# decoder relies on. The two bytes some transmitters append are not decoded.
FRAME_SYMBOLS = 80
FRAME_LENGTH = FRAME_SYMBOLS * SYMBOL_LENGTH

_LOOKUP = (numpy.arange(256) - 127.4) / 127.6
_ROTATION = numpy.array([1, 1j, -1, -1j])
_TAPS = numpy.array([0.017682261285, 0.048171339939, 0.122424706672,
                     0.197408519126, 0.228626345955, 0.197408519126,
                     0.122424706672, 0.048171339939, 0.017682261285])
_SYNC = numpy.array(SYNC_WORD, dtype=bool)
# Davis sends each byte least significant bit first.
_REVERSED = bytes(int('{:08b}'.format(value)[::-1], 2) for value in range(256))


def _crc_table():
    table = []
    for index in range(256):
        crc = index << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
        table.append(crc & 0xFFFF)
    return table


_CRC_TABLE = _crc_table()


def crc16(data):
    """Return the CCITT CRC-16 of data, starting from zero.

    Args:
        data (bytes): Bytes to check. A frame including its CRC returns 0.

    Returns:
        int: The checksum.
    """
    crc = 0
    for value in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[(crc >> 8) ^ value]
    return crc


class Candidate:
    """A CRC-valid frame found in the sample stream.

    Attributes:
        index (int): Sample index of the first sync-word symbol, counted from the
            first sample passed to the demodulator.
        payload (bytes): The eight bytes after the sync word, including the CRC.
        frequency_error (float): Carrier offset in Hz from the tuned frequency.
            Tuning higher by this amount centers the carrier.
    """

    def __init__(self, index, payload, frequency_error):
        self.index = index
        self.payload = payload
        self.frequency_error = frequency_error


class _Branch:
    """Filter and search state for one digital frequency shift."""

    def __init__(self, shift, start):
        self.shift = shift
        self.phase = 0.0
        self.history = numpy.zeros(len(_TAPS), dtype=complex)
        self.last = numpy.zeros(1, dtype=complex)
        self.tail = numpy.zeros(0)
        self.tail_start = start


class Demodulator:
    """Find Davis frames in consecutive blocks of interleaved IQ bytes.

    Every block continues the previous one. ``shifts`` lets one block be searched
    at several carrier offsets at once, each costing one more filter pass.

    One burst decodes at several neighbouring sample offsets, shifts and
    thresholds. Rarely, one of these decodes is a different payload that passes
    the CRC by chance. The demodulator therefore collects all decodes of a burst
    and returns the payload most of them agree on. A burst is returned in the
    block after it ends, once no further decode of it can follow.
    """

    def __init__(self):
        self.sample_count = 0
        # Every burst starting before this sample index has been returned.
        self.settled = 0
        self._branches = {}
        self._pending = []

    def feed(self, raw, shifts=(0,)):
        """Demodulate one block and return the bursts completed before it.

        Args:
            raw (bytes): Interleaved unsigned 8-bit I and Q samples, an even
                number of bytes.
            shifts (tuple[int, ...]): Carrier offsets in Hz to search. A shift
                not listed in the previous call starts without history and can
                miss a frame that began before this block.

        Returns:
            list[Candidate]: One frame per burst, in sample order.
        """
        samples = numpy.frombuffer(raw, dtype=numpy.uint8)
        iq = _LOOKUP[samples[0::2]] + 1j * _LOOKUP[samples[1::2]]
        iq *= _ROTATION[(self.sample_count + numpy.arange(len(iq))) % 4]
        for shift in list(self._branches):
            if shift not in shifts:
                del self._branches[shift]
        for shift in shifts:
            branch = self._branches.get(shift)
            if branch is None:
                branch = self._branches[shift] = _Branch(shift, self.sample_count)
            self._pending.extend(self._search(branch, iq))
        self.sample_count += len(iq)
        next_start = min(branch.tail_start for branch in self._branches.values())
        return self._complete(next_start)

    def flush(self):
        """Return the bursts still waiting for a following block.

        Returns:
            list[Candidate]: One frame per burst, in sample order.
        """
        return self._complete(None)

    def _complete(self, next_start):
        """Vote on each burst no future decode can belong to; keep the rest.

        Args:
            next_start (int|None): Lowest sample index a later decode can start
                at, or None to complete every burst.
        """
        self._pending.sort(key=lambda item: item[0].index)
        bursts = []
        for item in self._pending:
            if bursts and item[0].index - bursts[-1][-1][0].index < FRAME_LENGTH:
                bursts[-1].append(item)
            else:
                bursts.append([item])
        self._pending = []
        self.settled = self.sample_count if next_start is None else next_start - FRAME_LENGTH
        complete = []
        for burst in bursts:
            if next_start is not None and next_start - burst[-1][0].index < FRAME_LENGTH:
                self._pending.extend(burst)
            else:
                complete.append(_vote(burst))
        return complete

    def _search(self, branch, iq):
        """Return (Candidate, residual) pairs for one shift and keep its state.

        The residual is the part of the frequency error left after the shift. The
        smallest residual marks the shift whose filter passed the burst best.
        """
        if branch.shift:
            step = 2 * math.pi * branch.shift / SAMPLE_RATE
            iq = iq * numpy.exp(-1j * (branch.phase + step * numpy.arange(len(iq))))
            branch.phase = (branch.phase + step * len(iq)) % (2 * math.pi)
        extended = numpy.concatenate((branch.history, iq))
        filtered = numpy.convolve(extended, _TAPS, 'valid')[:len(iq)]
        branch.history = extended[-len(_TAPS):]
        pairs = numpy.concatenate((branch.last, filtered))
        branch.last = filtered[-1:]
        current, following = pairs[:-1], pairs[1:]
        power = numpy.maximum(current.real ** 2 + current.imag ** 2, 1e-20)
        discriminated = (current.imag * following.real
                         - current.real * following.imag) / power
        tail = numpy.concatenate((branch.tail, discriminated))
        starts = len(tail) - FRAME_LENGTH + SYMBOL_LENGTH
        found = []
        if starts > 0:
            symbols = [tail[i * SYMBOL_LENGTH:i * SYMBOL_LENGTH + starts]
                       for i in range(len(_SYNC))]
            mean = sum(symbols) / len(symbols)
            fixed = numpy.ones(starts, dtype=bool)
            adaptive = numpy.ones(starts, dtype=bool)
            for symbol, bit in zip(symbols, _SYNC):
                fixed &= (symbol < 0) == bit
                adaptive &= (symbol < mean) == bit
            for start in numpy.flatnonzero(fixed | adaptive):
                thresholds = [0.0] if fixed[start] else []
                if adaptive[start]:
                    thresholds.append(mean[start])
                payload = _decode(tail[start:start + FRAME_LENGTH:SYMBOL_LENGTH], thresholds)
                if payload is None:
                    continue
                # The discriminator returns radians per sample; a negative mean
                # is a carrier above the expected frequency.
                residual = -mean[start] * SAMPLE_RATE / (2 * math.pi)
                found.append((Candidate(branch.tail_start + int(start), payload,
                                        branch.shift + residual), residual))
            tail = tail[starts:]
            branch.tail_start += starts
        branch.tail = tail
        return found


def _decode(symbols, thresholds):
    """Return the CRC-valid payload of 80 symbol samples, or None.

    Args:
        symbols (numpy.ndarray): One discriminator sample per symbol.
        thresholds (list[float]): Decision thresholds to try in order.
    """
    for threshold in thresholds:
        packed = numpy.packbits(symbols < threshold).tobytes()
        payload = bytes(_REVERSED[value] for value in packed[2:])
        # Eight zero bytes pass the CRC. They are what a threshold taken from
        # noise next to a burst decodes, never a Davis frame.
        if crc16(payload) == 0 and any(payload):
            return payload
    return None


def _vote(burst):
    """Return the payload most decodes of one burst agree on.

    Args:
        burst (list[tuple[Candidate, float]]): Decodes with their residuals.

    Returns:
        Candidate: Among decodes of the winning payload, the one with the
        smallest residual, which carries the best frequency estimate.
    """
    votes = {}
    for candidate, _ in burst:
        votes[candidate.payload] = votes.get(candidate.payload, 0) + 1
    winner = max(votes, key=lambda payload: votes[payload])
    return min((item for item in burst if item[0].payload == winner),
               key=lambda item: abs(item[1]))[0]
