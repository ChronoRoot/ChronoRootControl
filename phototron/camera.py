#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Created on 8 mars 2018
Modified on Feb 2026 by Nicolás Gaggion

@author: Vladimir Daric
email: "vladimir.daric@cnrs.fr"
'''

import logging
from config import Config
import subprocess
import time
import io
from picamera import PiCamera, PiCameraMMALError
from PIL import Image

class CameraFactory(object):
    """Factroy class to manage different camera modules
    Returns the appropriate class object.
    """
    factories = {}

    @staticmethod
    def createCamera(camera_identifier):
        if camera_identifier not in CameraFactory.factories.keys():
            CameraFactory.factories[camera_identifier] = CameraFactory.class_from_identifier(camera_identifier)
        return CameraFactory.factories[camera_identifier].create()

    @staticmethod
    def class_from_identifier(identifier):
        for c in Camera.__subclasses__():
            if c.camera_identifier == identifier:
                return c.Factory()


class Camera(object):
    """Universal Camera abstract class

    All camera classes should heritate and surcharge, at least
    the capture method
    """
    camera_identifier = None

    def capture(self, image_path, params={}):
        raise NotImplemented


class VirtualCamera(Camera):
    """When no camera is available.
    """
    camera_identifier = "VIRT"

    class Factory:
        def create(self):
            return VirtualCamera()

class LinuxCamera(Camera):
    """
    class LinuxCamera(Camera)
    """
    camera_identifier = "LINUX"

    class Factory:
        def create(self):
            return LinuxCamera()


class RaspiCamera(Camera):
    """Deals with RapberryPi camera module"""
    camera_identifier = "RPICAM"
    Count = 0   # This represents the count of objects of this class
    settings = { # name, possible values, default
        'Image effect' : {
            'values' : PiCamera.IMAGE_EFFECTS,
            'default' : Config.CAM_PARAMS['image_effect'],
            'type' : 'list',
            },
        'AWB mode' : {
            'values' : PiCamera.AWB_MODES,
            'default' : Config.CAM_PARAMS['awb_mode'],
            'type' : 'list',
            }

    }

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.logger.debug("RaspiCamera object initialized")
        RaspiCamera.Count += 1

    def __del__(self):
        RaspiCamera.Count -= 1
        if hasattr(self, 'logger'):
             self.logger.debug(f'RaspiCamera objects remaining: {RaspiCamera.Count}')

    def camera_check(self):
        """Check if camera is detected"""
        try:
            result = subprocess.run(['vcgencmd', 'get_camera'], stdout=subprocess.PIPE)
            return b'detected=1' in result.stdout
        except Exception:
            return False

    def capture(self, image_path, params={}):
        """
        Takes a picture using Occam's Razor approach:
        Hardware-forced grayscale -> extract single channel -> save.
        """
        retries = 0
        while retries <= Config.CAM_RETRIES:
            retries += 1
            try:
                with PiCamera() as camera:
                    # 1. Setup
                    camera.resolution = params.get('resolution', (3280, 2464))
                    camera.exposure_mode = params.get("exposure_mode", 'backlight')
                    
                    # 2. Warmup (Required for Multiplexer stability)
                    time.sleep(Config.CAM_WARMUP)

                    # 3. The Grayscale Logic
                    if params.get('exposure_mode') == "backlight":
                        self.logger.debug("Capturing picture grayscale")
                        
                        # FORCE hardware to output Y-channel into RGB slots
                        # (128, 128) means "Zero Color"
                        camera.color_effects = (128, 128)
                        
                        # Capture to memory stream (Fast)
                        stream = io.BytesIO()
                        # Use 'png' or 'bmp' for lossless data. 'jpeg' adds noise.
                        camera.capture(stream, format='png') 
                        stream.seek(0)
                        
                        # Open and Extract
                        img = Image.open(stream)
                        # Since R=Y, G=Y, B=Y, we just grab the first channel (Band 0)
                        # This avoids the "weighted average" math of .convert('L')
                        y_channel = img.split()[0] 
                        
                        y_channel.save(image_path)
                        
                    else:
                        self.logger.debug("Capturing picture color")
                        camera.capture(image_path, 'png')

                return True

            except PiCameraMMALError:
                if retries < Config.CAM_RETRIES:
                    time.sleep(Config.CAM_WAIT_AFTER_RETRAY)
                else:
                    self.logger.error("Failed to acquire camera")
            except Exception as e:
                self.logger.error(f"Error: {e}")
                return False

        return False
        
    class Factory:
        def create(self):
            return RaspiCamera()