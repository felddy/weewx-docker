#!/usr/bin/env python
# coding: latin-1
#
# This driver is a merge of modified versions of both
# the weewx-sdr driver and the weewx_meteostick driver.
# The receiver is a Python port of the rtldavis package.
# See: https://github.com/matthewwall/weewx-sdr
#      https://github.com/matthewwall/weewx-meteostick
#      https://github.com/bemasher/rtldavis
#
# Copyright 2019 Matthew Wall, Luc Heijst
# Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
# Thanks to kobuki for the calc_wind_speed_ec logic.
#
# Distributed under the terms of the GNU Public License (GPLv3)
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.
#
# See http://www.gnu.org/licenses/

"""Receive Davis wireless observations with an RTL-SDR dongle.

The receiver runs inside WeeWX: user.rtldavis_receiver follows the transmitters
through their frequency hops and user.rtldavis_dsp demodulates the samples.
See README.md for installation and docs/configuration.md for sensor mapping.
"""
import math
import queue
import time

import weewx.drivers
import weewx.engine
import weewx.units
import weewx.wxformulas
from weewx.units import obs_group_dict
from weeutil.weeutil import tobool

import user.rtldavis_receiver

# Use new-style weewx logging
import weeutil.logger
import logging
log = logging.getLogger(__name__)

def logdbg(msg):
    log.debug(msg)

def loginf(msg):
    log.info(msg)

def logerr(msg):
    log.error(msg)

DRIVER_NAME = 'Rtldavis'
DRIVER_VERSION = '0.22.2'

weewx.units.obs_group_dict['frequency'] = 'group_frequency'
weewx.units.USUnits['group_frequency'] = 'hertz'
weewx.units.MetricUnits['group_frequency'] = 'hertz'
weewx.units.MetricWXUnits['group_frequency'] = 'hertz'
weewx.units.default_unit_format_dict['hertz'] = '%.0f'
weewx.units.default_unit_label_dict['hertz'] = ' Hz'

DEBUG_RAIN = 0
DEBUG_PARSE = 0
DEBUG_RTLD = 0
MPH_TO_MPS = 1609.34 / 3600.0 # meter/mile * hour/second

def loader(config_dict, engine):
    return RtldavisDriver(engine, config_dict)

def confeditor_loader():
    return RtldavisConfigurationEditor()

def dbg_parse(verbosity, msg):
    if DEBUG_PARSE >= verbosity:
        logdbg(msg)

def _fmt(data):
    if not data:
        return ''
    return ' '.join(['%02x' % ord(x) for x in data])

# default temperature for soil moisture and leaf wetness sensors that
# do not have a temperature sensor.
# Also used to normalize raw values for a standard temperature.
DEFAULT_SOIL_TEMP = 24 # C

RAW = 0  # indices of table with raw values
POT = 1  # indices of table with potentials

# Lookup table for soil_moisture_raw values to get a soil_moisture value based
# upon a linear formula.  Correction factor = 0.009
SM_MAP = {RAW: ( 99.2, 140.1, 218.7, 226.9, 266.8, 391.7, 475.6, 538.2, 596.1, 673.7, 720.1),
          POT: (  0.0,   1.0,   9.0,  10.0,  15.0,  35.0,  55.0,  75.0, 100.0, 150.0, 200.0)}

# Lookup table for leaf_wetness_raw values to get a leaf_wetness value based
# upon a linear formula.  Correction factor = 0.0
LW_MAP = {RAW: (857.0, 864.0, 895.0, 911.0, 940.0, 952.0, 991.0, 1013.0),
          POT: ( 15.0,  14.0,   5.0,   4.0,   3.0,   2.0,   1.0,    0.0)}


def calculate_thermistor_temp(temp_raw):
    """Convert a valid Davis thermistor count to degrees Celsius.

    Args:
        temp_raw (int|float): Ten-bit thermistor count.

    Returns:
        float|None: Temperature, or None for an invalid resistance.
    """

    # Convert temp_raw to a resistance (R) in kiloOhms
    a = 18.81099
    b = 0.0009988027
    if temp_raw <= 0:
        return None
    r = a / (1.0 / temp_raw - b) / 1000 # k ohms

    # Steinhart-Hart parameters
    s1 = 0.002783573
    s2 = 0.0002509406
    try:
        thermistor_temp = 1 / (s1 + s2 * math.log(r)) - 273
        dbg_parse(3, 'r (k ohm) %s temp_raw %s thermistor_temp %s' %
                  (r, temp_raw, thermistor_temp))
        return thermistor_temp
    except ValueError as e:
        logerr('thermistor_temp failed for temp_raw %s r (k ohm) %s'
               'error: %s' % (temp_raw, r, e))
    return None


def lookup_potential(sensor_name, norm_fact, sensor_raw, sensor_temp, lookup):
    """Look up potential based upon a normalized raw value (i.e. temp corrected
    for DEFAULT_SOIL_TEMP) and a linear function between two points in the
    lookup table.
    :param lookup: a table with both sensor_raw_norm values and corresponding
                   potential values. the table is composed for a specific
                   norm-factor.
    :param sensor_temp: sensor temp in C
    :param sensor_raw: sensor raw potential value
    :param norm_fact: temp correction factor for normalizing sensor-raw values
    :param sensor_name: string used in debug messages
    """

    # normalize raw value for standard temperature (DEFAULT_SOIL_TEMP)
    if sensor_temp is None:
        sensor_temp = DEFAULT_SOIL_TEMP
    sensor_raw_norm = sensor_raw * (1 + norm_fact * (sensor_temp - DEFAULT_SOIL_TEMP))

    numcols = len(lookup[RAW])
    if sensor_raw_norm >= lookup[RAW][numcols - 1]:
        potential = lookup[POT][numcols - 1] # preset potential to last value
        dbg_parse(2, "%s: temp=%s fact=%s raw=%s norm=%s potential=%s >= RAW=%s" %
                  (sensor_name, sensor_temp, norm_fact, sensor_raw,
                   sensor_raw_norm, potential, lookup[RAW][numcols - 1]))
    else:
        potential = lookup[POT][0] # preset potential to first value
        # lookup sensor_raw_norm value in table
        for x in range(0, numcols):
            if sensor_raw_norm < lookup[RAW][x]:
                if x == 0:
                    # 'pre zero' phase; potential = first value
                    dbg_parse(2, "%s: temp=%s fact=%s raw=%s norm=%s potential=%s < RAW=%s" %
                              (sensor_name, sensor_temp, norm_fact, sensor_raw,
                               sensor_raw_norm, potential, lookup[RAW][0]))
                    break
                else:
                    # determine the potential value
                    potential_per_raw = (lookup[POT][x] - lookup[POT][x - 1]) / (lookup[RAW][x] - lookup[RAW][x - 1])
                    potential_offset = (sensor_raw_norm - lookup[RAW][x - 1]) * potential_per_raw
                    potential = lookup[POT][x - 1] + potential_offset
                    dbg_parse(2, "%s: temp=%s fact=%s raw=%s norm=%s potential=%s RAW=%s to %s POT=%s to %s " %
                              (sensor_name, sensor_temp, norm_fact, sensor_raw,
                               sensor_raw_norm, potential,
                               lookup[RAW][x - 1], lookup[RAW][x],
                               lookup[POT][x - 1], lookup[POT][x]))
                    break
    return potential

# Normalize and interpolate raw wind values at raw angles
def calc_wind_speed_ec(raw_mph, raw_angle):

    # some sanitization: no corrections needed under 3 and no values exist
    # above 150 mph
    if raw_mph < 3 or raw_mph > 150:
        return raw_mph

    # Error correction values for
    #  [ 1..29 by 1, 30..150 by 5 raw mph ]
    #   x
    #  [ 1, 4, 8..124 by 4, 127, 128 raw degrees ]
    #
    # Extracted from a Davis Weather Envoy using a DIY transmitter to
    # transmit raw values and logging LOOP packets.
    # first row: raw angles;
    # first column: raw speed;
    # cells: values provided in response to raw data by the Envoy;
    # [0][0] is filler
    windtab = [
        [0, 1, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60, 64, 68, 72, 76, 80, 84, 88, 92, 96, 100, 104, 108, 112, 116, 120, 124, 127, 128],
        [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0],
        [4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0],
        [5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0],
        [6, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1, 1, 1, 0, 0],
        [7, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 1, 0, 0],
        [8, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 1, 0, 0],
        [9, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 1, 0, 0],
        [10, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 0, 0],
        [11, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 0, 0],
        [12, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 0, 0],
        [13, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 3, 3, 1, 0, 0],
        [14, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 3, 3, 1, 0, 0],
        [15, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 3, 3, 1, 0, 0],
        [16, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 3, 3, 1, 0, 0],
        [17, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 3, 3, 1, 0, 0],
        [18, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 3, 3, 1, 0, 0],
        [19, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 3, 4, 4, 1, 0, 0],
        [20, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 3, 4, 4, 2, 0, 0],
        [21, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 3, 4, 4, 2, 0, 0],
        [22, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 3, 4, 4, 2, 0, 0],
        [23, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 3, 4, 4, 2, 0, 0],
        [24, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 2, 3, 4, 4, 2, 0, 0],
        [25, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 2, 3, 4, 4, 2, 0, 0],
        [26, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 2, 3, 5, 4, 2, 0, 0],
        [27, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 2, 3, 5, 5, 2, 0, 0],
        [28, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 2, 3, 5, 5, 2, 0, 0],
        [29, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 2, 3, 5, 5, 2, 0, 0],
        [30, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 2, 3, 5, 5, 2, 0, 0],
        [35, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 2, 4, 6, 5, 2, 0, -1],
        [40, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 2, 4, 6, 6, 2, 0, -1],
        [45, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 2, 4, 7, 6, 2, -1, -1],
        [50, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 2, 5, 7, 7, 2, -1, -2],
        [55, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 2, 5, 8, 7, 2, -1, -2],
        [60, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 2, 5, 8, 8, 2, -1, -2],
        [65, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 2, 5, 9, 8, 2, -2, -3],
        [70, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 0, 2, 5, 9, 9, 2, -2, -3],
        [75, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 0, 2, 6, 10, 9, 2, -2, -3],
        [80, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 0, 2, 6, 10, 10, 2, -2, -3],
        [85, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 0, 2, 7, 11, 11, 2, -3, -4],
        [90, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 2, 7, 12, 11, 2, -3, -4],
        [95, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 3, 2, 2, 2, 1, 1, 1, 1, 2, 7, 12, 12, 3, -3, -4],
        [100, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 3, 3, 2, 2, 2, 1, 1, 1, 1, 2, 8, 13, 12, 3, -3, -4],
        [105, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 3, 3, 3, 3, 3, 2, 2, 2, 1, 1, 1, 2, 8, 13, 13, 3, -3, -4],
        [110, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 3, 3, 3, 3, 3, 2, 2, 2, 1, 1, 1, 2, 8, 14, 14, 3, -3, -5],
        [115, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 2, 3, 3, 3, 3, 3, 2, 2, 2, 1, 1, 1, 2, 9, 15, 14, 3, -3, -5],
        [120, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 2, 3, 3, 3, 3, 3, 2, 2, 2, 1, 1, 1, 3, 9, 15, 15, 3, -4, -5],
        [125, 1, 1, 2, 1, 1, 0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 3, 3, 3, 3, 3, 2, 2, 1, 1, 1, 3, 10, 16, 16, 3, -4, -5],
        [130, 1, 1, 2, 1, 1, 0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 3, 3, 3, 3, 3, 2, 2, 2, 1, 1, 3, 10, 17, 16, 3, -4, -6],
        [135, 1, 2, 2, 1, 1, 0, 0, 0, -1, 0, 0, 1, 1, 2, 2, 3, 3, 3, 3, 4, 3, 3, 2, 2, 2, 1, 1, 3, 10, 17, 17, 4, -4, -6],
        [140, 1, 2, 2, 1, 1, 0, 0, 0, -1, 0, 0, 1, 1, 2, 2, 3, 3, 3, 4, 4, 3, 3, 2, 2, 2, 1, 1, 3, 11, 18, 17, 4, -4, -6],
        [145, 2, 2, 2, 1, 1, 0, 0, 0, -1, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 4, 3, 3, 3, 2, 2, 1, 1, 3, 11, 19, 18, 4, -4, -6],
        [150, 2, 2, 2, 1, 1, 0, 0, -1, -1, 0, 0, 1, 1, 2, 3, 3, 4, 4, 4, 4, 4, 3, 3, 2, 2, 1, 1, 3, 12, 19, 19, 4, -4, -6]
    ]

    # EC is symmetric between W/E (90/270°) - probably a wrong assumption,
    # table needs to be redone for 0-360°
    if raw_angle > 128:
        raw_angle = 256 - raw_angle

    s0 = a0 = 1

    while windtab[s0][0] < raw_mph:
        s0 += 1
    while windtab[0][a0] < raw_angle:
        a0 += 1

    if windtab[s0][0] == raw_mph:
        s1 = s0
    else:
        if s0 > 1:
            s0 -= 1
        s1 = len(windtab) - 1 if s0 == len(windtab) - 1 else s0 + 1

    if windtab[0][a0] == raw_angle:
        a1 = a0
    else:
        if a0 > 1:
            a0 -= 1
        a1 = len(windtab[0]) - 2 if a0 == len(windtab) - 1 else a0 + 1

    if s0 == s1 and a0 == a1:
        return raw_mph + windtab[s0][a0]
    else:
        return interpolate(windtab[0][a0], windtab[0][a1],
                                      windtab[s0][0], windtab[s1][0],
                                      windtab[s0][a0], windtab[s0][a1],
                                      windtab[s1][a0], windtab[s1][a1],
                                      raw_angle, raw_mph)

# Simple bilinear interpolation
#
#  a0         a1 <-- fixed raw angles
#  x0---------x1 s0
#  |          |
#  |          |
#  |      * <-|-- raw input angle, raw speed value (x, y)
#  |          |
#  y0---------y1 s1
#                ^
#                \__ speed: measured raw / correction values
#
def interpolate(rx0, rx1,
                ry0, ry1,
                x0, x1,
                y0, y1,
                x, y):

    dbg_parse(2, "rx0=%s, rx1=%s, ry0=%s, ry1=%s, x0=%s, x1=%s, y0=%s, y1=%s, x=%s, y=%s" %
              (rx0, rx1, ry0, ry1, x0, x1, y0, y1, x, y))

    if rx0 == rx1:
        return y + x0 + (y - ry0) / float(ry1 - ry0) * (y1 - y0)

    if ry0 == ry1:
        return y + y0 + (x - rx0) / float(rx1 - rx0) * (x1 - x0)

    dy0 = x0 + (y - ry0) / float(ry1 - ry0) * (y0 - x0)
    dy1 = x1 + (y - ry0) / float(ry1 - ry0) * (y1 - x1)

    return y + dy0 + (x - rx0) / float(rx1 - rx0) * (dy1 - dy0)

class RtldavisConfigurationEditor(weewx.drivers.AbstractConfEditor):
    @property
    def default_stanza(self):
        return """
[Rtldavis]
    # This section is for Davis wireless stations received with an RTL-SDR dongle.

    # Frequency band of the station: EU (868 MHz), US (915 MHz) or NZ (921 MHz).
    transceiver_frequency = EU

    # Transmitter channels as set on the transmitter and shown by the console,
    # 1-8. 0 means not present. Only the ISS is required.
    iss_channel = 1
    anemometer_channel = 0
    leaf_soil_channel = 0
    temp_hum_1_channel = 0
    temp_hum_2_channel = 0

    # The driver to use:
    driver = user.rtldavis
"""

    def prompt_for_settings(self):
        """Ask for the settings that differ between stations.

        Returns:
            dict: Values for transceiver_frequency, iss_channel and rain_bucket_type.
        """
        settings = dict()
        print("Specify the frequency band of the station: EU for 868 MHz,")
        print("US for 915 MHz in North America, NZ for 921 MHz in New Zealand.")
        settings['transceiver_frequency'] = self._prompt(
            'transceiver_frequency', 'EU', ['EU', 'US', 'NZ'])
        print("Specify the channel of the ISS, 1 to 8. The console shows it, and")
        print("it is set with the DIP switches in the transmitter.")
        settings['iss_channel'] = self._prompt(
            'iss_channel', '1', [str(channel) for channel in range(1, 9)])
        print("Specify the rain bucket: 0 for 0.01 inch, 1 for 0.2 mm per tip.")
        bucket = '0' if settings['transceiver_frequency'] == 'US' else '1'
        settings['rain_bucket_type'] = self._prompt('rain_bucket_type', bucket, ['0', '1'])
        return settings

class RtldavisDriver(weewx.drivers.AbstractDevice, weewx.engine.StdService):
    """Decode selected transmitters and add reception summaries to archive events."""

    NUM_CHAN = 10 # 8 channels, one fake channel (9), one unused channel (0)
    DEFAULT_FREQUENCY = 'EU'
    DEFAULT_RAIN_BUCKET_TYPE = 1
    DEFAULT_SENSOR_MAP = {
        'windSpeed': 'wind_speed',
        'windGust': 'wind_gust',
        'windDir': 'wind_dir',
        'outTemp': 'temperature',
        'outHumidity': 'humidity',
        # WeeWX uses this sensor's interval between bucket tips when
        # rainRate = hardware is selected in [StdWXCalculate].
        'rainRate': 'rain_rate',
        'radiation': 'solar_radiation',
        'UV': 'uv',
        #'extraTemp1': 'temp_1',
        #'extraTemp2': 'temp_2',
        #'extraTemp3': 'temp_3',
        'soilTemp1': 'soil_temp_1',
        'soilTemp2': 'soil_temp_2',
        'soilTemp3': 'soil_temp_3',
        'soilTemp4': 'soil_temp_4',
        'leafTemp1': 'leaf_temp_1',
        #'leafTemp2': 'leaf_temp_2',
        'extraHumid1': 'humid_1',
        'extraHumid2': 'humid_2',
        'soilMoist1': 'soil_moisture_1',
        'soilMoist2': 'soil_moisture_2',
        'soilMoist3': 'soil_moisture_3',
        'soilMoist4': 'soil_moisture_4',
        'leafWet1': 'leaf_wetness_1',
        'leafWet2': 'leaf_wetness_2',
        'rxCheckPercent': 'pct_good_all', # updated in 
        'txBatteryStatus': 'bat_iss',
        'supplyVoltage': 'supercap_volt',
        'referenceVoltage': 'solar_power',
        'windBatteryStatus': 'bat_anemometer',
        'rainBatteryStatus': 'bat_leaf_soil',
        'outTempBatteryStatus': 'bat_th_1',
        'inTempBatteryStatus': 'bat_th_2',
        'extraTemp1': 'pct_good_0',  # renamed
        'extraTemp2': 'pct_good_1',  # renamed
        'extraTemp3': 'pct_good_2',  # renamed
        'leafTemp2': 'pct_good_3',   # renamed
        'consBatteryVoltage': 'freqError0',
        'hail': 'freqError1',
        'hailRate': 'freqError2',
        'heatingTemp': 'freqError3',
        'heatingVoltage': 'freqError4'}


    def __init__(self, engine, config_dict):
        """Load the station mapping and start the receiver thread.

        The receiver waits for a missing dongle or library and logs how to fix
        it, so WeeWX keeps running and picks up the dongle when it appears.

        Args:
            engine (weewx.engine.StdEngine|None): WeeWX engine, or None for diagnostics.
            config_dict (dict): WeeWX configuration containing a Rtldavis section.
        """
        loginf('driver version is %s' % DRIVER_VERSION)
        self.setup_units_rtld_schema()

        if engine:
            weewx.engine.StdService.__init__(self, engine, config_dict)
        stn_dict = config_dict.get(DRIVER_NAME, {})
        self._hardware_name = stn_dict.get('hardware_name', 'Rtldavis')

        global DEBUG_PARSE
        DEBUG_PARSE = int(stn_dict.get('debug_parse', DEBUG_PARSE))
        global DEBUG_RAIN
        DEBUG_RAIN = int(stn_dict.get('debug_rain', DEBUG_RAIN))
        global DEBUG_RTLD
        DEBUG_RTLD = int(stn_dict.get('debug_rtld', DEBUG_RTLD))
        logging.getLogger('user.rtldavis_receiver').setLevel(
            logging.DEBUG if DEBUG_RTLD else logging.INFO)

        # modification by Luc Heijst
        self.last_hum = None
        # end modification by Luc

        bucket_type = int(stn_dict.get('rain_bucket_type',
                                       self.DEFAULT_RAIN_BUCKET_TYPE))
        if bucket_type not in [0, 1]:
            raise ValueError("unsupported rain bucket type %s" % bucket_type)
        self.rain_per_tip = 0.254 if bucket_type == 0 else 0.2 # mm
        loginf('using rain_bucket_type %s' % bucket_type)
        self.sensor_map = dict(self.DEFAULT_SENSOR_MAP)
        if 'sensor_map' in stn_dict:
            self.sensor_map.update(stn_dict['sensor_map'])
        loginf('sensor map is: %s' % self.sensor_map)
        self.stats = self._init_stats()
        self.last_rain_count = None
        self._rain_candidate = None
        self._log_humidity_raw = tobool(stn_dict.get('log_humidity_raw', False))
        self._save_pct_good_per_transmitter = tobool(stn_dict.get('save_pct_good_per_transmitter', False))
        self._sensor_map = stn_dict.get('sensor_map', {})
        loginf('sensor map is %s' % self._sensor_map)
        self.device = str(stn_dict.get('device', '0'))
        gain = str(stn_dict.get('gain', 'auto'))
        self.gain = None if gain.lower() == 'auto' else float(gain)
        self.ppm = float(stn_dict.get('ppm', 0))

        freq = stn_dict.get('transceiver_frequency', self.DEFAULT_FREQUENCY)
        if freq not in ['US', 'NZ', 'EU']:
            raise ValueError("invalid frequency %s" % freq)
        self.frequency = freq
        loginf('using frequency %s' % self.frequency)
        channels = dict()
        channels['iss'] = int(stn_dict.get('iss_channel', 1))
        channels['anemometer'] = int(stn_dict.get('anemometer_channel', 0))
        channels['leaf_soil'] = int(stn_dict.get('leaf_soil_channel', 0))
        channels['temp_hum_1'] = int(stn_dict.get('temp_hum_1_channel', 0))
        channels['temp_hum_2'] = int(stn_dict.get('temp_hum_2_channel', 0))
        if channels['anemometer'] == 0:
            channels['wind_channel'] = channels['iss']
        else:
            channels['wind_channel'] = channels['anemometer']
        self.channels = channels
        loginf('using iss_channel %s' % channels['iss'])
        loginf('using anemometer_channel %s' % channels['anemometer'])
        loginf('using leaf_soil_channel %s' % channels['leaf_soil'])
        loginf('using temp_hum_1_channel %s' % channels['temp_hum_1'])
        loginf('using temp_hum_2_channel %s' % channels['temp_hum_2'])

        self.transmitters, self.tr_count = RtldavisDriver.ch_to_xmit(self, 
            channels['iss'], channels['anemometer'], channels['leaf_soil'],
            channels['temp_hum_1'], channels['temp_hum_2'])
        loginf('using transmitters %d' % self.transmitters)
        loginf('log_humidity_raw %s' % self._log_humidity_raw)

        receiver = user.rtldavis_receiver.Receiver(
            self.frequency, [i for i in range(8) if self.transmitters & (1 << i)], self.ppm)
        self._radio = user.rtldavis_receiver.Radio(receiver, self.device, self.gain)
        # Set per loop by genLoopPackets; see there.
        self.transm_to_store = None
        self._radio.start()

        # bind to new archive record events so that we can update the rf
        # stats on each archive record.
        if engine:
            self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

    @staticmethod
    def ch_to_xmit(self, iss_channel, anemometer_channel, leaf_soil_channel,
                   temp_hum_1_channel, temp_hum_2_channel):
        selected = (iss_channel, anemometer_channel, leaf_soil_channel,
                    temp_hum_1_channel, temp_hum_2_channel)
        if not 1 <= iss_channel <= 8 or any(not 0 <= c <= 8 for c in selected):
            raise ValueError("ISS channel must be 1-8; optional channels must be 0-8")
        transmitters = 0
        for channel in selected:
            if channel:
                transmitters |= 1 << (channel - 1)
        # program main.go reports the current msg count for the first 4 active transmitters
        # table self.stats['activeTrIds'] contain the transmitter ID's (range 0-7) of
        # the active transmitters.
        # This is used as a pointer to self.stats['loop_times'] in _update_summaries.
        mask = 1
        trIdCount = 0
        for i in range(0, 8):
            if transmitters & mask != 0:
                self.stats['activeTrIds'][trIdCount] = i
                self.stats['activeTrIdPtrs'][i] = trIdCount
                trIdCount = trIdCount + 1
            mask = mask << 1
        return transmitters, trIdCount

    def _data_to_packet(self, data):
        """Map decoded observations and derive rain without accepting counter jumps.

        Args:
            data (dict): Raw observations from one accepted transmission.

        Returns:
            dict: WeeWX LOOP packet. Rain is None for an ambiguous counter change.
        """
        packet = dict()
        # map sensor observations to database field names
        for k in self.sensor_map:
            if self.sensor_map[k] in data:
                packet[k] = data[self.sensor_map[k]]
        if 'rain_count' in data:
            current = data['rain_count']
            tips = 0 if self.last_rain_count is None else current - self.last_rain_count
            if tips < -100:
                tips += 128
            # A corrupt count must not become the next baseline: that would
            # turn its recovery into a second, equally false rain event.
            # A repeated rejected count establishes a new baseline after a
            # transmitter reset, without counting the ambiguous transition.
            if 0 <= tips <= 60 or current == self._rain_candidate:
                packet['rain'] = (tips if 0 <= tips <= 60 else 0) * self.rain_per_tip
                self.last_rain_count = current
                self._rain_candidate = None
            else:
                packet['rain'] = None
                self._rain_candidate = current
                logdbg("rejecting implausible rain counter transition to %d" % current)
        packet['dateTime'] = int(time.time() + 0.5)
        packet['usUnits'] = weewx.METRICWX
        return packet

    def _init_stats(self):
        """Create the receiver counters and archive baselines.

        Returns:
            dict: Counters, active-transmitter indices and reception summaries.
        """
        return {
            # theoretical loop times for 8 transmitter channels (plus 100.0 as dummy)
            'loop_times': [2.5625, 2.625, 2.6875, 2.75, 2.8125, 2.875, 2.9375, 3, 100.0],
            'activeTrIds': [9] * 8,    # 9 means: sensor not active
            'activeTrIdPtrs': [0] * 8, # pointer to active transmitter
            'curr_ts': 0,              # time stamp of current archive  
            'last_ts': 0,              # time stamp of previous archive
            'curr_cnt': [0] * 4,       # received messages since startup at current archive
            'last_cnt': [0] * 4,       # received messages since startup at previous archive
            'max_count': [0] * 4,      # max to receive messages per transmitter current archive period
            'count': [0] * 4,          # received messages per transmitter current archive period 
            'missed': [0] * 4,         # missed messages per transmitter current archive period
            'pct_good': [None] * 4,    # percentage of good messages per transmitter
            'pct_good_all': None}      # percentage of good messages for all transmitters

    def _reset_stats(self):
        self.stats['last_ts'] = self.stats['curr_ts']
        for i in range(0, 4):
            self.stats['last_cnt'][i] = self.stats['curr_cnt'][i]
            self.stats['pct_good'][i] = None
        self.stats['pct_good_all'] = None

    def _update_stats(self, curr_cnt0, curr_cnt1, curr_cnt2, curr_cnt3):
        # update the statistics
        # save the message counts since startup
        self.stats['curr_cnt'][0] = int(curr_cnt0)
        self.stats['curr_cnt'][1] = int(curr_cnt1)
        self.stats['curr_cnt'][2] = int(curr_cnt2)
        self.stats['curr_cnt'][3] = int(curr_cnt3)

    def _update_summaries(self):
        """Calculate reception percentages for the four counters in receiver output."""
        self.stats['curr_ts'] = int(time.time())
        period = self.stats['curr_ts'] - self.stats['last_ts']
        if self.stats['last_ts'] <= 0 or period <= 0:
            return
        total_count = total_expected = 0
        for i in range(min(self.tr_count, 4)):
            expected = int(period // self.stats['loop_times'][self.stats['activeTrIds'][i]])
            count = self.stats['curr_cnt'][i] - self.stats['last_cnt'][i]
            # A receiver restart resets cumulative counters. Its partial
            # interval has no meaningful percentage; start again next archive.
            if expected <= 0 or count < 0:
                continue
            count = min(count, expected)
            self.stats['count'][i] = count
            self.stats['max_count'][i] = expected
            self.stats['missed'][i] = expected - count
            self.stats['pct_good'][i] = 100.0 * count / expected
            total_count += count
            total_expected += expected
        if total_expected:
            self.stats['pct_good_all'] = 100.0 * total_count / total_expected

    def new_archive_record(self, event):
        """Add reception percentages to the event before WeeWX saves its record.

        Args:
            event (weewx.Event): NEW_ARCHIVE_RECORD event with a mutable record.
        """
        logdbg("new_archive_record")
        # calculations per archive period
        self._update_summaries()  # calculate summaries
        # Store the summaries in the database
        # for the pct_good values in the DEFAULT_SENSOR_MAP
        if self.stats['pct_good_all'] is not None:
            # Store overal pct_good if value not None
            event.record['rxCheckPercent'] = self.stats['pct_good_all']
            # test if individual pct_good values have to be saved
            if self._save_pct_good_per_transmitter:
                for tr in range(0, 4):
                    data = 'pct_good_%s' % tr
                    for k in self.sensor_map:
                        # Test if sensor is in the sensor map.
                        if self.sensor_map[k] == data:
                            if tr == 0 and self.tr_count > 1:
                                # When tr_count = 1 we don't store the pct_good of transmitter 1
                                # because the value is the same as in rxCheckPercent
                                event.record[k] = self.stats['pct_good'][tr]
                            if tr > 0 and tr < self.tr_count:
                                # save pct_good for active transmitters (max=4)
                                event.record[k] = self.stats['pct_good'][tr]
        self._reset_stats()  # save current stats in last stats

    def closePort(self):
        self._radio.stop()

    @property
    def hardware_name(self):
        return self._hardware_name

    def genLoopPackets(self):
        """Yield observations as frames arrive. Without reception, wait.

        The receiver searches for the transmitters by itself and logs what it
        does, so a silent period is not an error. Only a receiver defect ends
        the loop.

        Yields:
            dict: WeeWX LOOP packets with METRICWX units.
        """
        # Frequency offsets fill five database fields, one per EU channel. With
        # several transmitters, each transmitter's offsets are stored for two
        # days in turn.
        period = 2 * 24 * 3600
        relative = int(((time.time() - 3 * 3600) % (period * self.tr_count)) / period)
        self.transm_to_store = self.stats['activeTrIds'][relative]
        dbg_parse(1, "Number of transmitters: %s, store freqError data for transmitter with ID=%s"
                  % (self.tr_count, self.transm_to_store))
        while True:
            if self._radio.failure is not None:
                raise weewx.WeeWxIOError("receiver failed: %s" % self._radio.failure)
            try:
                frame = self._radio.frames.get(timeout=1)
            except queue.Empty:
                continue
            packet = self._frame_to_packet(frame)
            if packet:
                dbg_parse(3, "pkt= %s" % packet)
                yield packet

    def _frame_to_packet(self, frame):
        """Convert one received frame to a LOOP packet.

        Args:
            frame (user.rtldavis_receiver.Frame): Frame from a configured transmitter.

        Returns:
            dict|None: The observations and, for EU stations, the transmitter's
            frequency offset on the frame's channel. None for a frame without
            observations.
        """
        counts = list(frame.counts[:4]) + [0] * (4 - len(frame.counts[:4]))
        self._update_stats(*counts)
        data = self.parse_readings(frame.payload)
        if not data:
            return None
        if self.frequency == 'EU' and frame.transmitter == self.transm_to_store:
            # Mapped like the observations, to the legacy fields by default.
            data['freqError%d' % frame.channel] = int(round(frame.frequency_offset))
        return self._data_to_packet(data)

    def parse_readings(self, pkt):
        """Decode one eight-byte payload, without its receiver timestamp or CRC check.

        Args:
            pkt (bytes|bytearray): Davis payload including its two checksum bytes.

        Returns:
            dict: Observations in METRICWX units. Empty for an unknown station.
        """
        data = dict()
        if not pkt or len(pkt) != 8:
            return data
        try:
            data = self.parse_raw(self, pkt)
        except ValueError as e:
            logerr("parse failed for %r: %s" % (pkt, e))
        return data

    @staticmethod
    def parse_raw(self, pkt):
        data = dict()
        data['channel'] = (pkt[0] & 0x7) + 1
        battery_low = (pkt[0] >> 3) & 0x1
        if data['channel'] == self.channels['iss']or data['channel'] == self.channels['anemometer'] \
                or data['channel'] == self.channels['temp_hum_1']or data['channel'] == self.channels['temp_hum_2']:
            if data['channel'] == self.channels['iss']:
                data['bat_iss'] = battery_low
            elif data['channel'] == self.channels['anemometer']:
                data['bat_anemometer'] = battery_low
            elif data['channel'] == self.channels['temp_hum_1']:
                data['bat_th_1'] = battery_low
            else:
                data['bat_th_2'] = battery_low
            message_type = pkt[0] >> 4
            # Every type these transmitters send has a branch below, including
            # the undecoded 3 and 0xC. A CRC-valid frame with any other type is
            # corrupt in several bits, so none of its values, not even the
            # battery flag, can be trusted.
            if message_type in (0, 1, 0xB, 0xD, 0xF):
                logdbg("discarding frame with message type 0x%x, which no transmitter "
                       "sends: %s" % (message_type, pkt.hex()))
                return {}
            if (data['channel'] == self.channels['wind_channel']
                    and message_type in (2, 4, 5, 6, 7, 8, 9, 0xA, 0xE)):
                wind_speed_raw, wind_dir_raw = pkt[1], pkt[2]
                # The original Envoy calibration table is retained. Pro/Pro2
                # direction has a dead band around north, encoded as 0 or 255.
                if wind_dir_raw == 0:
                    direction = 5.0
                elif wind_dir_raw == 255:
                    direction = 355.0
                else:
                    direction = 9.0 + (wind_dir_raw - 1) * 342.0 / 253.0
                corrected = round(calc_wind_speed_ec(wind_speed_raw, wind_dir_raw))
                data['wind_speed_ec'] = corrected
                data['wind_speed_raw'] = wind_speed_raw
                data['wind_speed'] = corrected * MPH_TO_MPS
                data['wind_dir'] = direction if corrected else None

            if message_type == 2:
                # supercap voltage (Vue only) max: 0x3FF (1023)
                # message example:
                # 20 04 C3 D4 C1 81 89 EE
                """When the raw values are divided by 300 the maximum
                voltage of the super capacitor will be about 2.8 V. This
                is close to its maximum operating voltage of 2.7 V
                """
                supercap_volt_raw = ((pkt[3] << 2) + (pkt[4] >> 6)) & 0x3FF
                if supercap_volt_raw != 0x3FF:
                    data['supercap_volt'] = supercap_volt_raw / 300.0
                    dbg_parse(2, "supercap_volt_raw=0x%03x value=%s" %
                              (supercap_volt_raw, data['supercap_volt']))
            elif message_type == 3:
                # unknown message type
                # message examples:
                # TODO
                # TODO (no sensor)
                dbg_parse(1, "unknown message with type=0x03; "
                          "pkt[3]=0x%02x pkt[4]=0x%02x pkt[5]=0x%02x"
                          % (pkt[3], pkt[4], pkt[5]))
            elif message_type == 4:
                # uv
                # message examples:
                # 40 00 00 12 45 00 B5 2A
                # 41 00 DE FF C3 00 A9 8D (no sensor)
                uv_raw = ((pkt[3] << 2) + (pkt[4] >> 6)) & 0x3FF
                if uv_raw != 0x3FF:
                    data['uv'] = uv_raw / 50.0
                    dbg_parse(2, "uv_raw=%04x value=%s" %
                              (uv_raw, data['uv']))
            elif message_type == 5:
                # rain rate
                # message examples:
                # 50 00 00 FF 75 00 48 5B (no rain)
                # 50 00 00 FE 75 00 7F 6B (light_rain)
                # 50 00 00 1B 15 00 3F 80 (heavy_rain)
                # 51 00 DB FF 73 00 11 41 (no sensor)
                """ The published rain_rate formulas differ from each
                other. For both light and heavy rain we like to know a
                'time between tips' in s. The rain_rate then would be:
                3600 [s/h] / time_between_tips [s] * 0.2 [mm] = xxx [mm/h]
                """
                time_between_tips_raw = ((pkt[4] & 0x30) << 4) + pkt[3]  # typical: 64-1022
                dbg_parse(2, "time_between_tips_raw=%03x (%s)" %
                          (time_between_tips_raw, time_between_tips_raw))
                if data['channel'] == self.channels['iss']: # rain sensor is present
                    rain_rate = None
                    if time_between_tips_raw == 0x3FF:
                        # no rain
                        rain_rate = 0
                        dbg_parse(3, "no_rain=%s mm/h" % rain_rate)
                    elif time_between_tips_raw == 0:
                        pass  # Zero cannot describe a finite interval between tips.
                    elif pkt[4] & 0x40 == 0:
                        # heavy rain. typical value:
                        # 64/16 - 1020/16 = 4 - 63.8 (180.0 - 11.1 mm/h)
                        time_between_tips = time_between_tips_raw / 16.0
                        rain_rate = 3600.0 / time_between_tips * self.rain_per_tip
                        dbg_parse(2, "heavy_rain=%s mm/h, time_between_tips=%s s" %
                                  (rain_rate, time_between_tips))
                    else:
                        # light rain. typical value:
                        # 64 - 1022 (11.1 - 0.8 mm/h)
                        time_between_tips = time_between_tips_raw
                        rain_rate = 3600.0 / time_between_tips * self.rain_per_tip
                        dbg_parse(2, "light_rain=%s mm/h, time_between_tips=%s s" %
                                  (rain_rate, time_between_tips))
                    data['rain_rate'] = rain_rate
            elif message_type == 6:
                # solar radiation
                # message examples
                # 61 00 DB 00 43 00 F4 3B
                # 60 00 00 FF C5 00 79 DA (no sensor)
                sr_raw = ((pkt[3] << 2) + (pkt[4] >> 6)) & 0x3FF
                if sr_raw < 0x3FE:
                    data['solar_radiation'] = sr_raw * 1.757936
                    dbg_parse(2, "solar_radiation_raw=0x%04x value=%s"
                              % (sr_raw, data['solar_radiation']))
            elif message_type == 7:
                # solar cell output / solar power (Vue only)
                # message example:
                # 70 01 F5 CE 43 86 58 E2
                """When the raw values are divided by 300 the voltage comes
                in the range of 2.8-3.3 V measured by the machine readable
                format
                """
                solar_power_raw = ((pkt[3] << 2) + (pkt[4] >> 6)) & 0x3FF
                if solar_power_raw != 0x3FF:
                    data['solar_power'] = solar_power_raw / 300.0
                    dbg_parse(2, "solar_power_raw=0x%03x solar_power=%s"
                              % (solar_power_raw, data['solar_power']))
            elif message_type == 8:
                # outside temperature
                # message examples:
                # 80 00 00 33 8D 00 25 11 (digital temp)

                # 81 00 00 59 45 00 A3 E6 (analog temp)
                # 81 00 DB FF C3 00 AB F8 (no sensor)
                temp_raw = (pkt[3] << 4) + (pkt[4] >> 4)  # 12-bits temp value
                if temp_raw not in (0xFFC, 0xFF8):
                    if pkt[4] & 0x8:
                        # digital temp sensor
                        # The 12-bit value is signed; the low nibble contains
                        # sensor flags and is not part of the temperature.
                        if temp_raw & 0x800:
                            temp_raw -= 0x1000
                        temp_f = temp_raw / 10.0
                        temp_c = weewx.wxformulas.FtoC(temp_f) # C
                        dbg_parse(2, "digital temp_raw=0x%03x temp_f=%s temp_c=%s"
                                  % (temp_raw, temp_f, temp_c))
                    else:
                        # analog sensor (thermistor)
                        temp_raw /= 4  # 10-bits temp value
                        temp_c = calculate_thermistor_temp(temp_raw)
                        dbg_parse(2, "thermistor temp_raw=%s temp_c=%s"
                                  % (temp_raw, temp_c))
                    if data['channel'] == self.channels['temp_hum_1']:
                        data['temp_1'] = temp_c
                    elif data['channel'] == self.channels['temp_hum_2']:
                        data['temp_2'] = temp_c
                    else:
                        data['temperature'] = temp_c
            elif message_type == 9:
                # Stored wind peak reported by the transmitter
                # message examples:
                # 91 00 DB 00 03 0E 89 85
                # 90 00 00 00 05 00 31 51 (no sensor)
                gust_raw = pkt[3]  # mph
                gust_index_raw = pkt[5] >> 4
                if not(gust_raw == 0 and gust_index_raw == 0):
                    dbg_parse(2, "W10=%s gust_index_raw=%s" %
                              (gust_raw, gust_index_raw))
                    if data['channel'] == self.channels['wind_channel']:
                        data['wind_gust'] = gust_raw * MPH_TO_MPS
            elif message_type == 0xA:
                # outside humidity
                # message examples:
                # A0 00 00 C9 3D 00 2A 87 (digital sensor, variant a)
                # A0 01 3A 80 3B 00 ED 0E (digital sensor, variant b)
                # A0 01 41 7F 39 00 18 65 (digital sensor, variant c)
                # A0 00 00 22 85 00 ED E3 (analog sensor)
                # A1 00 DB 00 03 00 47 C7 (no sensor)
                humidity_raw = ((pkt[4] >> 4) << 8) + pkt[3]
                if humidity_raw != 0:
                    if pkt[4] & 0x08 == 0x8:
                        # digital sensor
                        humidity = humidity_raw / 10.0
                    else:
                        # analog sensor (pkt[4] & 0x0f == 0x5)
                        humidity = humidity_raw * -0.301 + 710.23
                    if data['channel'] == self.channels['temp_hum_1']:
                        data['humid_1'] = humidity
                    elif data['channel'] == self.channels['temp_hum_2']:
                        data['humid_2'] = humidity
                    elif data['channel'] == self.channels['anemometer']:
                        loginf("Warning: humidity sensor of Anemometer Transmitter Kit not in sensor map: %s" % humidity)
                    else:
                        data['humidity'] = humidity
                    dbg_parse(2, "humidity_raw=0x%03x value=%s" %
                              (humidity_raw, humidity))
                    # modification by Luc Heijst
                    if self._log_humidity_raw:
                        # we don't know which bits are used by the old humidity sensor
                        # so we log the full 16 bit code.
                        humidity_raw_full = (pkt[4] << 8) + pkt[3]
                        if self.last_hum is not None and humidity_raw_full != self.last_hum:
                            loginf("rtldavis-luc: humidity_raw= %04x" % humidity_raw_full)
                        self.last_hum = humidity_raw_full
                    # end modification by Luc
            elif message_type == 0xC:
                # unknown message
                # message example:
                # C1 04 D0 00 01 00 E9 A4
                # As we have seen after one day of received data
                # pkt[3] and pkt[5] are always zero;
                # pckt[4] has values 0-3 (ATK) or 5 (temp/hum)
                dbg_parse(3, "unknown pkt[3]=0x%02x pkt[4]=0x%02x pkt[5]=0x%02x" %
                          (pkt[3], pkt[4], pkt[5]))
            elif message_type == 0xE:
                # rain
                # message examples:
                # E0 00 00 05 05 00 9F 3D
                # E1 00 DB 80 03 00 16 8D (no sensor)
                rain_count_raw = pkt[3]
                """We have seen rain counters wrap around at 127 and
                others wrap around at 255.  When we filter the highest
                bit, both counter types will wrap at 127.
                """
                if rain_count_raw != 0x80 and data['channel'] == self.channels['iss']:
                    rain_count = rain_count_raw & 0x7F  # skip high bit
                    data['rain_count'] = rain_count
                    dbg_parse(2, "rain_count_raw=0x%02x value=%s" %
                              (rain_count_raw, rain_count))

        elif data['channel'] == self.channels['leaf_soil']:
            # leaf and soil station
            data['bat_leaf_soil'] = battery_low
            data_type = pkt[0] >> 4
            if data_type == 0xF:
                data_subtype = pkt[1] & 0x3
                sensor_num = ((pkt[1] & 0xe0) >> 5) + 1
                temp_c = DEFAULT_SOIL_TEMP
                temp_raw = ((pkt[3] << 2) + (pkt[5] >> 6)) & 0x3FF
                potential_raw = ((pkt[2] << 2) + (pkt[4] >> 6)) & 0x3FF

                if data_subtype == 1:
                    # soil moisture
                    # message examples:
                    # F2 09 1A 55 C0 00 62 E6
                    # F2 29 FF FF C0 C0 F1 EC (no sensor)
                    if pkt[3] != 0xFF:
                        # soil temperature
                        temp_c = calculate_thermistor_temp(temp_raw)
                        data['soil_temp_%s' % sensor_num] = temp_c
                        dbg_parse(2, "soil_temp_%s=%s 0x%03x" %
                                  (sensor_num, temp_c, temp_raw))
                    if pkt[2] != 0xFF:
                        # soil moisture potential
                        # Lookup soil moisture potential in SM_MAP
                        norm_fact = 0.009  # Normalize potential_raw
                        soil_moisture = lookup_potential(
                            "soil_moisture", norm_fact,
                            potential_raw, temp_c, SM_MAP)
                        data['soil_moisture_%s' % sensor_num] = soil_moisture
                        dbg_parse(2, "soil_moisture_%s=%s 0x%03x" %
                                  (sensor_num, soil_moisture, potential_raw))
                elif data_subtype == 2:
                    # leaf wetness
                    # message examples:
                    # F2 0A D4 55 80 00 90 06
                    # F2 2A 00 FF 40 C0 4F 05 (no sensor)
                    if pkt[3] != 0xFF:
                        # leaf temperature
                        temp_c = calculate_thermistor_temp(temp_raw)
                        data['leaf_temp_%s' % sensor_num] = temp_c
                        dbg_parse(2, "leaf_temp_%s=%s 0x%03x" %
                                  (sensor_num, temp_c, temp_raw))
                    if pkt[2] != 0:
                        # leaf wetness potential
                        # Lookup leaf wetness potential in LW_MAP
                        norm_fact = 0.0  # Do not normalize potential_raw
                        leaf_wetness = lookup_potential(
                            "leaf_wetness", norm_fact,
                            potential_raw, temp_c, LW_MAP)
                        data['leaf_wetness_%s' % sensor_num] = leaf_wetness
                        dbg_parse(2, "leaf_wetness_%s=%s 0x%03x" %
                                  (sensor_num, leaf_wetness, potential_raw))
                else:
                    logerr("unknown subtype '%s' in '%s'" % (data_subtype, temp_raw))

        else:
            logerr("unknown station with channel: %s, raw message: %s" %
                   (data['channel'], pkt.hex()))
            return {}
        return data

    @staticmethod
    def setup_units_rtld_schema():
        obs_group_dict['extraTemp1']        = 'group_percent'
        obs_group_dict['extraTemp2']        = 'group_percent'
        obs_group_dict['extraTemp3']        = 'group_percent'
        obs_group_dict['leafTemp2']         = 'group_percent'
        obs_group_dict['consBatteryVoltage'] = 'group_frequency'
        obs_group_dict['hail']              = 'group_frequency'
        obs_group_dict['hailRate']          = 'group_frequency'
        obs_group_dict['heatingTemp']       = 'group_frequency'
        obs_group_dict['heatingVoltage']    = 'group_frequency'


if __name__ == '__main__':
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog='python3 -m user.rtldavis',
        description="Check the receiver setup, or receive Davis transmitters "
                    "without WeeWX. Stop WeeWX first; only one program can use the dongle.")
    parser.add_argument('--version', action='store_true', help="show the driver version")
    parser.add_argument('--check', action='store_true',
                        help="check numpy, librtlsdr and the dongle, then exit")
    parser.add_argument('--band', default='EU', choices=['EU', 'US', 'NZ'],
                        help="frequency band; default EU")
    parser.add_argument('--channels', default='1',
                        help="transmitter channels, comma-separated; default 1")
    parser.add_argument('--device', default='0', help="dongle index or serial number")
    parser.add_argument('--gain', default='auto', help="tuner gain in dB; default auto")
    parser.add_argument('--ppm', type=float, default=0,
                        help="expected frequency error of the dongle; default 0")
    parser.add_argument('--iq', metavar='FILE',
                        help="decode an unsigned 8-bit IQ recording instead of a dongle")
    parser.add_argument('--debug', action='store_true', help="show every hop and frame")
    options = parser.parse_args()

    if options.version:
        print("rtldavis driver version %s" % DRIVER_VERSION)
        sys.exit(0)
    logging.basicConfig(level=logging.DEBUG if options.debug else logging.INFO,
                        format='%(asctime)s %(message)s')
    if options.check:
        sys.exit(0 if user.rtldavis_receiver.check(options.device) else 1)
    if options.iq:
        for candidate in user.rtldavis_receiver.replay(options.iq):
            print("%s channel %d" % (candidate.payload.hex().upper(),
                                     (candidate.payload[0] & 7) + 1))
        sys.exit(0)

    transmitters = [int(channel) - 1 for channel in options.channels.split(',')]
    receiver = user.rtldavis_receiver.Receiver(options.band, transmitters, options.ppm)
    radio = user.rtldavis_receiver.Radio(
        receiver, options.device, None if options.gain == 'auto' else float(options.gain))
    radio.start()
    try:
        while radio.failure is None:
            try:
                frame = radio.frames.get(timeout=1)
            except queue.Empty:
                continue
            print("%s channel %d, %+.1f kHz, received %s"
                  % (frame.payload.hex().upper(), frame.transmitter + 1,
                     frame.frequency_offset / 1000, list(frame.counts)), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        radio.stop()
    sys.exit(1 if radio.failure else 0)
