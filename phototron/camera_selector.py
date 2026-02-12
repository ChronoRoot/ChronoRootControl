#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Created on 8 mars 2018
Updated for Stability: Feb 2026

@author: Vladimir Daric
@email: "vladimir.daric@cnrs.fr"
'''

import logging
import time
import subprocess
import threading
from phototron.camera import CameraFactory
from config import Config

# --- Factory Class ---
class SelectorFactory(object):
    """Factory class to manage different multiplexer modules
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

# --- Abstract Base Class ---
class Selector(object):
    """Universal abstract class
    All selector classes should inherit from this.
    """
    selector_identifier = None
    
    def __init__(self, cameras):
        pass

    def is_free(self):
        raise NotImplementedError

    def self_check(self):
        raise NotImplementedError

    def enable_cam(self, port):
        raise NotImplementedError

    def get_active_camera(self):
        raise NotImplementedError

    def is_camera_v2(self):
        raise NotImplementedError

    def is_dual(self):
        raise NotImplementedError

    def jumper(self):
        raise NotImplementedError

    def ivport_type(self):
        raise NotImplementedError

    def capture(self, camera_id, image_path, params):
        raise NotImplementedError

    def get_camera(self):
        raise NotImplementedError


#################################
## IVPort_v2 Implementation
#################################

# Ensure this import works in your file structure
try:
    from ivport_v2 import ivport
except ImportError:
    print("CRITICAL ERROR: Could not import 'ivport_v2.ivport'. Check your directory structure.")
    # We don't exit here to allow the script to be imported, but it will fail at runtime if not fixed.

class IVPort_v2(Selector):
    """IVPort_v2 module implementation with stability fixes
    """
    
    Count = 0   # This represents the count of objects of this class
    selector_identifier = 'TYPE_QUAD2'

    def __init__(self, cameras=(1, 2, 3, 4)):
        self.logger = self._get_logger()
        self.logger.debug("IVPort_v2 object initializing...")
        
        # FIX: Initialize the lock to prevent AttributeError in is_free()
        self.lock = threading.Lock()
        
        self.camera_type = Config.CAMERA_TYPE
        
        # Camera List Validation
        try:
            cameras = [int(elem) for elem in cameras] # Ensure list of ints
        except ValueError:
            raise ValueError('Please provide integer list')
        
        cameras = set(cameras)
        if len(cameras) > 4:
            raise ValueError('IVPort Quad module can handle maximum of 4 cameras')
        elif max(cameras) > 4 or min(cameras) < 1:
            raise ValueError('Invalid IVPort Quad module port value (1 to 4)')
            
        # Initialize Hardware
        self.iv = ivport.IVPort(getattr(ivport, self.selector_identifier))
        IVPort_v2.Count += 1
        self.logger.debug("IVPort_v2 object initialized successfully")

    def __del__(self):
        # FIX: Safer deletion to avoid crashes if init failed
        if hasattr(self, 'logger'):
            self.logger.debug('deleting : %s'%(self))
        
        IVPort_v2.Count -= 1
        
        if hasattr(self, 'iv'):
            self.iv.close()
            del self.iv

    def _get_logger(self):
        return logging.getLogger(__name__)

    def is_free(self):
        """returns true only if the lock is NOT acquired"""
        # Note: Threading locks don't have is_locked() in older python versions.
        # This is a non-blocking check.
        locked = self.lock.acquire(blocking=False)
        if locked:
            self.lock.release()
            return True
        return False

    def enable_cam(self, port):
        # Optimization: Don't switch if we are already there!
        if self.iv.camera == port:
            return

        self.logger.debug(f"Switching to camera port {port}")
        self.iv.camera_change(port)
        time.sleep(0.2)

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
        """
        Switches camera, waits for stabilization, captures image, and closes camera.
        """
        self.enable_cam(camera_id)
        
        camera = None
        try:
            # Create Camera Instance
            camera = CameraFactory.createCamera(self.camera_type)
            
            # Capture
            return camera.capture(image_path, params)
            
        except Exception as e:
            self.logger.error(f"Error capturing from Camera {camera_id}: {e}")
            raise e
            
        finally:
            # FIX: Resource Management
            # We must close the camera software connection before we allow 
            # the system to switch ports again later.
            if camera and hasattr(camera, 'close'):
                camera.close()

    def get_camera(self):
        # Warning: Using this without closing it manually might cause freezes
        return CameraFactory.createCamera(self.camera_type)


    def self_check(self):
        """Test if multiplexer is working using external tool"""
        try:
            # FIX: Use list format for subprocess
            result = subprocess.run(["tools/multiplexer_detected"], capture_output=True)
            return result.returncode == 0
        except FileNotFoundError:
            self.logger.error("tools/multiplexer_detected binary not found.")
            return False
        except Exception as e:
            self.logger.error(f"Self check failed: {e}")
            return False

    def probe(self, camera_id):
        """
        Checks if a camera is responsive on the I2C bus at a specific port.
        """
        try:
            # Switch the mux
            self.iv.camera_change(camera_id)
            time.sleep(Config.CAM_WARMUP)
            
            # Check if an I2C device exists at 0x10 (Standard for Pi Cam)
            # -y 1 means bus 1. 0x10 is the address.
            result = subprocess.run(['i2cget', '-y', '1', '0x10'], 
                                   capture_output=True)
            
            # If returncode is 0, the chip responded
            return result.returncode == 0
        except Exception as e:
            self.logger.error(f"Probe failed: {e}")
            return False
        
    class Factory:
        def create(self):
            return IVPort_v2()


#################################
## Null selector - No multiplexer installed
#################################

class NullSelector(Selector):
    """No camera multiplexer installed
    Camera connected directly to Raspberry module.
    """
    selector_identifier = 'NullSelector'
    
    def __init__(self, cameras=1):
        self.cameras = 1
        # Placeholder for direct camera access
        self.logger = logging.getLogger(__name__)

    class Factory:
        def create(self):
            return NullSelector()

    # Stub implementations to prevent crashes if methods are called
    def is_free(self): return True
    def self_check(self): return True
    def enable_cam(self, port): pass
    def capture(self, camera_id, image_path, params):
        # Direct capture without switching
        camera = CameraFactory.createCamera(Config.CAMERA_TYPE)
        try:
            return camera.capture(image_path, params)
        finally:
            if hasattr(camera, 'close'):
                camera.close()