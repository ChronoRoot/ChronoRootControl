#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Created on 8 mars 2018
Modified on Feb 2026 by Nicolás Gaggion

@author: Vladimir Daric
@email: "vladimir.daric@cnrs.fr"
'''

import gc
import logging
import time
import threading
import subprocess
from phototron.camera import CameraFactory
from config import Config

# --- Helper to setup SHDL logger ---
def get_hw_logger(name):
    logger = logging.getLogger("HW_" + name)
    logger.setLevel(Config.LOG_LEVEL)
    if not logger.handlers:
        handler = logging.FileHandler(Config.SHDL_LOG_FILE)
        handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
        logger.addHandler(handler)
        logger.propagate = False
    return logger

# --- Factory Class ---
class SelectorFactory(object):
    factories = {}

    @staticmethod
    def createSelector(selector_identifier):
        if not selector_identifier: selector_identifier = 'SINGLE'
        if selector_identifier == 'IVPORT_4': selector_identifier = 'TYPE_QUAD2'
            
        if selector_identifier not in SelectorFactory.factories:
            factory_obj = SelectorFactory.class_from_identifier(selector_identifier)
            if not factory_obj:
                logging.getLogger(__name__).error(f"CRITICAL: Selector '{selector_identifier}' not recognized.")
                factory_obj = SingleCamera.Factory()
            SelectorFactory.factories[selector_identifier] = factory_obj
            
        return SelectorFactory.factories[selector_identifier].create()

    @staticmethod
    def class_from_identifier(identifier):
        for c in Selector.__subclasses__():
            if getattr(c, 'selector_identifier', None) == identifier:
                return c.Factory()
        return None

# --- Abstract Base Class ---
class Selector(object):
    selector_identifier = None
    def __init__(self, cameras): pass
    def is_free(self): raise NotImplementedError
    def self_check(self): raise NotImplementedError
    def enable_cam(self, port): raise NotImplementedError
    def get_active_camera(self): raise NotImplementedError
    def is_camera_v2(self): raise NotImplementedError
    def is_dual(self): raise NotImplementedError
    def jumper(self): raise NotImplementedError
    def ivport_type(self): raise NotImplementedError
    def capture(self, camera_id, image_path, params): raise NotImplementedError
    def get_camera(self): raise NotImplementedError


#################################
## Single Camera Implementation
#################################

class SingleCamera(Selector):
    selector_identifier = 'SINGLE'
    Count = 0

    def __init__(self, cameras=(1,)):
        self.logger = get_hw_logger(__name__)
        self.lock = threading.Lock()
        self.camera_type = Config.CAMERA_TYPE
        SingleCamera.Count += 1
        self.logger.debug("SingleCamera initialized.")

    def __del__(self):
        SingleCamera.Count -= 1

    def is_free(self):
        locked = self.lock.acquire(blocking=False)
        if locked:
            self.lock.release()
            return True
        return False

    def enable_cam(self, port):
        self.logger.debug(f"Virtual switch to port {port} ignored (Single Mode).")
        time.sleep(0.1)

    def capture(self, camera_id, image_path, params):
        self.enable_cam(camera_id)
        camera = None
        try:
            params["current_cam_id"] = str(camera_id)
            camera = CameraFactory.createCamera(self.camera_type)
            return camera.capture(image_path, params)
        except Exception as e:
            self.logger.error(f"Capture error on Cam {camera_id}: {e}")
            raise e
        finally:
            if camera:
                if hasattr(camera, 'close'): 
                    camera.close()
                del camera   
                gc.collect() 
    def get_camera(self):
        return CameraFactory.createCamera(self.camera_type)

    def self_check(self):
        try:
            camera = self.get_camera()
            is_healthy = camera.camera_check()
            if hasattr(camera, 'close'): camera.close()
            return is_healthy
        except Exception as e:
            self.logger.error(f"SingleCamera self-check error: {e}")
            return False
        
    def probe(self, camera_id):
        self.enable_cam(camera_id)
        
        import os
        if os.path.exists('/dev/video0'):
            return True
        else:
            self.logger.error("Probe failed: /dev/video0 not found.")
            return False
        
    class Factory:
        def create(self): return SingleCamera()


#################################
## IVPort_v2 Implementation
#################################

class IVPort_v2(Selector):
    selector_identifier = 'TYPE_QUAD2'
    Count = 0

    def __init__(self, cameras=(1, 2, 3, 4)):
        self.logger = get_hw_logger(__name__)
        self.lock = threading.Lock()
        self.camera_type = Config.CAMERA_TYPE
        self.iv = None
        
        try:
            from ivport_v2 import ivport
            # PASS THE TYPE_QUAD2 constant to the IVPort constructor!
            self.iv = ivport.IVPort(iv_type=ivport.TYPE_QUAD2) 
            self.logger.info("IVPort_v2 initialized successfully.")
        except Exception as e:
            self.logger.error(f"IVPort hardware init failed: {e}")

        IVPort_v2.Count += 1

    def __del__(self):
        IVPort_v2.Count -= 1
        if self.iv and hasattr(self.iv, 'close'): self.iv.close()

    def is_free(self):
        locked = self.lock.acquire(blocking=False)
        if locked:
            self.lock.release()
            return True
        return False

    def enable_cam(self, port):
        if self.iv:
            self.logger.debug(f"Multiplexer: Switching to port {port}")
            self.iv.camera_change(port)
            time.sleep(0.2)
        else:
            self.logger.warning(f"Multiplexer: Switch to {port} failed - no hardware.")

    def capture(self, camera_id, image_path, params):
        self.enable_cam(camera_id)
        camera = None
        try:
            params["current_cam_id"] = str(camera_id)
            camera = CameraFactory.createCamera(self.camera_type)
            return camera.capture(image_path, params)
        except Exception as e:
            self.logger.error(f"Capture error on Cam {camera_id}: {e}")
            raise e
        finally:
            if camera:
                if hasattr(camera, 'close'): 
                    camera.close()
                del camera   # Destroy the Python object reference
                gc.collect() # Force immediate CMA memory release back to the OS

    def get_camera(self):
        return CameraFactory.createCamera(self.camera_type)

    def self_check(self):
        if not self.iv: return False
        try:
            result = subprocess.run(['i2cget', '-y', '1', '0x10'], capture_output=True)
            return result.returncode == 0
        except Exception as e:
            self.logger.error(f"Multiplexer self-check error: {e}")
            return False

    def probe(self, camera_id):
        if not self.iv: return False
        try:
            self.enable_cam(camera_id)
            time.sleep(0.1)
            result = subprocess.run(['i2cget', '-y', '1', '0x10'], capture_output=True)
            if result.returncode != 0:
                self.logger.error(f"Probe: Cam {camera_id} failed I2C check.")
                return False
                
            camera = self.get_camera()
            is_healthy = camera.camera_check()
            if hasattr(camera, 'close'): camera.close()
            return is_healthy
        except Exception as e:
            self.logger.error(f"Probe failed for Cam {camera_id}: {e}")
            return False
        
    class Factory:
        def create(self): return IVPort_v2()