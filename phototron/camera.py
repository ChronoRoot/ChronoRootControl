#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Created on 8 mars 2018

@author: Vladimir Daric
email: "vladimir.daric@cnrs.fr"
'''

import logging
from config import Config
import subprocess
import time

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

import io
from picamera import PiCamera, PiCameraMMALError
from PIL import Image

class RaspiCamera(Camera):
    """Deals with RapberryPi camera module
    """
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
        self.logger = self.logger()
        self.logger.debug("RaspiCamera object initialized")
        RaspiCamera.Count += 1

    def __del__(self):
        self.logger.debug('deleting : %s'%(self))
        RaspiCamera.Count -= 1
        if RaspiCamera.Count == 0:
            self.logger.debug('Last RaspiCamera object deleted')
        else:
            self.logger.debug('Still %s RaspiCamera objects' % RaspiCamera.Count)
        del self.logger
        del self

    def logger(self):
        logger = logging.getLogger('__name__')
        return logger

    def camera_check(self):
        """Check if the camera module is correctly initialised
        """
        right_answer = b'supported=1 detected=1\n'
        result = subprocess.run(['vcgencmd', 'get_camera'], stdout=subprocess.PIPE)
        if result.stdout != right_answer:
            return False
        return True

    def capture(self, image_path, params={}):
        """take a picture

            image parameters management still not implemented
        """
        retries = 0
        while retries <= Config.CAM_RETRIES:
            retries += 1
            try:
                with PiCamera() as camera:
                    time.sleep(Config.CAM_WARMUP)
                    
                    # TODO: generaliser
                    camera.resolution = params['resolution']
                    camera.exposure_mode = params["exposure_mode"]

                    # Wait for autobalance
                    camera.start_preview()
                    time.sleep(Config.CAM_ADJUST_TIME)

                    if params['exposure_mode'] == "backlight":
                        self.logger.debug("backlight settings")
                        #
                        # Create a byte stream to hold image data
                        stream = io.BytesIO()
                        # Capture the image in RGB format
                        camera.capture(stream, format='png')

                        # Convert PNG image to a Pillow image
                        stream.seek(0)
                        image = Image.open(stream)

                        # convert to grayscale
                        gray_image = image.convert('L')

                        # save the grayscale image as png
                        gray_image.save(image_path)

                    else:
                        self.logger.debug("default color settings")
                        # save the color image as png
                        camera.capture(image_path, 'png')
                return True
            except PiCameraMMALError:
                #retry
                if retries < Config.CAM_RETRIES:
                    time.sleep(Config.CAM_WAIT_AFTER_RETRAY)
                else:
                    print("Error: Unable to acquire camera. Abandoned after %d seconds" % retries * Config.CAM_WAIT_AFTER_RETRAY)
                    #TODO: log instead pf print
        return False

    class Factory:
        def create(self):
            return RaspiCamera()
