#!/usr/bin/env python3
#
# This file is part of Ivport.
# Copyright (C) 2015 Ivmech Mechatronics Ltd. <bilgi@ivmech.com>
# Copyright (C) 2017 IPS2 Daric Vladimir <vladimir.daric@cnrs.fr>
# Ivport is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ivport is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

#title           :IIC.py
#description     :IIC module for ivport v2 camera multiplexer
#author          :Caner Durmusoglu
#modification    :Vladimir Daric
#date            :20160514
#version         :0.1
#usage           :
#notes           :
#python_version  :3.4
#==============================================================================

from datetime import datetime

import smbus2

iic_address = (0x70)
iic_register = (0x00)

iic_bus0 = (0x01)
iic_bus1 = (0x02)
iic_bus2 = (0x04)
iic_bus3 = (0x08)

class IIC():
    def __init__(self, twi=1, addr=iic_address, bus_enable=iic_bus0):
        self._bus = smbus2.SMBus(twi)
        self._addr = addr
        self._write(iic_register, bus_enable)

    def __del__(self):
        self._bus.close()
        del self

    def _write(self, register, data):
        self._bus.write_byte_data(self._addr, register, data)

    def _read(self):
        return self._bus.read_byte(self._addr)

    def read_control_register(self):
        return self._read()

    def write_control_register(self, config):
        self._write(iic_register, config)
