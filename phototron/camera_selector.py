#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Created on 8 mars 2018

@author: Vladimir Daric
@email: "vladimir.daric@cnrs.fr"
'''

import logging
from phototron.camera import CameraFactory
from config import Config
import time

class SelectorFactory(object):
    """Factroy class to manage different multiplexer modules

    Returns the appropriate class object.
    """
    factories = {}

    @staticmethod
    def createSelector(selector_identifier):
        if selector_identifier not in SelectorFactory.factories.keys():
            SelectorFactory.factories[selector_identifier] = SelectorFactory.class_from_identifier(selector_identifier)
        return SelectorFactory.factories[selector_identifier].create()

    @staticmethod
    def class_from_identifier(identifier):
        for c in Selector.__subclasses__():
            if c.selector_identifier == identifier:
                return c.Factory()


class Selector(object):
    """Universal abstract class

    All selector classes should heritate and surcharge this classes methods

    If you want to implement a new camera selector, subclass this class. please
    look at IVPort_v2 class below.
    """
    selector_identifier = None
    def __init__(self, cameras):
        pass

    def is_free(self):
        raise NotImplemented

    def self_check(self):
        raise NotImplemented

    def enable_cam(self, port):
        raise NotImplemented

    def get_active_camera(self):
        raise NotImplemented

    def is_camera_v2(self):
        raise NotImplemented

    def is_dual(self):
        raise NotImplemented

    def jumper(self):
        raise NotImplemented

    def ivport_type(self):
        raise NotImplemented

    def capture(self, camera_id, image_path, params):
        raise NotImplemented

    def get_camera(self):
        raise NotImplemented


    def self_check(self):
        """Test if mutliplexer is working
        """
        raise NotImplemented


#################################
## IVPort_v2
###############################

from ivport_v2 import ivport

class IVPort_v2(Selector):
    """IVPort_v2 module implementation
    """

    Count = 0   # This represents the count of objects of this class
    selector_identifier = 'TYPE_QUAD2'

    def __init__(self, cameras=(1, 2, 3, 4)):
        self.logger = self.logger()
        self.logger.debug("IVPort_v2 object initialized")
        self.camera_type = Config.CAMERA_TYPE
        try:
            cameras = [int(elem) for elem in cameras] # be sure that cameras is a list of ints
        except ValueError:
            raise ValueError('Please provide integer list')
        cameras = set(cameras)
        if len(cameras) > 4:
            raise ValueError('IVPort Quand module can handle maximum of 4 cameras')
        elif max(cameras) > 4 or min(cameras) < 1:
            raise ValueError('Invalid IVPort Quand module port value (1 to 4)')
        self.iv = ivport.IVPort(getattr(ivport, self.selector_identifier))
        IVPort_v2.Count += 1

    def __del__(self):
        self.logger.debug('deleting : %s'%(self))
        IVPort_v2.Count -= 1
        if IVPort_v2.Count == 0:
            self.logger.debug('Last IVPort_v2 object deleted')
        else:
            self.logger.debug('Still %s IVPort_v2 objects' % IVPort_v2.Count)
        self.iv.close()
        del self.iv
        del self.logger
        del self

    def logger(self):
        logger = logging.getLogger(__name__)
        return logger

    def is_free(self):
        """returns true only if the lock is set by this object instance
        """
        return self.lock.is_locked

    def enable_cam(self, port):
        self.iv.camera_change(port)

    def get_active_camera(self):
        return self.iv.camera

    def is_camera_v2(self):
        return self.iv.is_camera_v2

    def is_dual(self):
        return self.iv.is_dual

    def jumper(self):
        return self.iv.ivport_jumper

    def ivport_type(self):
        return self.iv.ivport_type

    def capture(self, camera_id, image_path, params):
        self.enable_cam(camera_id)
        camera = CameraFactory.createCamera(self.camera_type)
        return camera.capture(image_path, params)

    def get_camera(self):
        return CameraFactory.createCamera(self.camera_type)


    def self_check(self):
        """Test if mutliplexer is working
        """
        try:
            subprocess
        except NameError:
            import subprocess
        result = subprocess.run("tools/multiplexer_detected")
        return result.returncode == 0

    def probe(self, camera_id):
        import subprocess
        try:
            # Switch the mux
            self.iv.camera_change(camera_id)
            time.sleep(0.2)
            
            # Check if an I2C device exists at 0x10 (Standard for Pi Cam)
            # Use i2cget -y 1 0x10
            result = subprocess.run(['i2cget', '-y', '1', '0x10'], 
                                   capture_output=True)
            
            # If returncode is 0, the chip responded
            return result.returncode == 0
        except:
            return False
        
    class Factory:
        def create(self):
            return IVPort_v2()

#################################
## Null selector - Nom multiplexer installed
###############################

class NullSelector(Selector):
    """No camera multiplexer installed

    Camera connected directly to Raspberry module
        Only one process (image or video) should be able to use camera
        This class locks the camera
    """
    selector_identifier = 'NullSelector'
    def __init__(self, cameras=1):
        self.cameras = 1
        self.cam1 = Camera(1)

        class Factory:
            def create(self):
                return NullSelector()
