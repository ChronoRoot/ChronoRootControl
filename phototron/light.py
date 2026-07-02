#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Created on 8 mars 2018

@author: Vladimir Daric
email: "vladimir.daric@cnrs.fr"
'''

import RPi.GPIO as GPIO
import time
from config import Config

class Light:
    """Use GPIO to switch the light on and off

    Usage :
    >>> l = Light();
    >>> l.ON
    >>> l.OFF
    """
    ON = GPIO.HIGH
    OFF = GPIO.LOW

    def __init__(self, IR_GPIO=32):
        self.IR_GPIO = IR_GPIO
        GPIO.setwarnings(False)
        if GPIO.getmode() != GPIO.BOARD:
            GPIO.setmode(GPIO.BOARD)
        GPIO.setup(IR_GPIO , GPIO.OUT)

    @property
    def state(self):
        return GPIO.input(self.IR_GPIO)

    @state.setter
    def state(self, new_state):
        if new_state not in (GPIO.HIGH, GPIO.LOW):
            raise TypeError("Bas argument type")
        # Always drive the output. Reading back an output pin via GPIO.input()
        # is unreliable across processes (mule vs web worker), so we never skip
        # the write based on the read-back state.
        if GPIO.getmode() != GPIO.BOARD:
            GPIO.setmode(GPIO.BOARD)
        if GPIO.gpio_function(self.IR_GPIO) != GPIO.OUT:
            GPIO.setup(self.IR_GPIO , GPIO.OUT)
        GPIO.output(self.IR_GPIO, new_state)

        return True
