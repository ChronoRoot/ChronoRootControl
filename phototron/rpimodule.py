#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Created on 26 févr. 2018
Refactored: Feb 2026 

@author: Vladimir Daric
@email: vladimir.daric@cnrs.fr
'''

import logging
import os
import shutil
import time
from datetime import datetime
from config import Config
from phototron.camera_selector import SelectorFactory
from phototron.light import Light
from app.experiment.models import Experiment
from filelock import FileLock, Timeout

class RpiModule(object):
    """
    Toplevel class - SINGLETON
    Implements all ChronoRoot robot functions.
    Ensures hardware is initialized exactly ONCE to prevent GPIO thrashing.
    """
    _instance = None
    
    def __new__(cls):
        """
        Singleton Pattern: Returns the existing instance if it exists.
        Prevents multiple 'RpiModule' objects from fighting over GPIO pins.
        """
        if cls._instance is None:
            cls._instance = super(RpiModule, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        # Only initialize the hardware ONCE per program run
        if self._initialized:
            return
            
        self.logger = self._get_logger()
        self.logger.debug("RpiModule: Initializing Hardware Controller...")

        self.lock = FileLock(Config.LOCK_FILE, Config.LOCK_TIMEOUT)
        self.light = Light()

        # Init Selector once. It stays alive to hold GPIO state.
        selector_type = Config.SELECTOR_TYPE
        self.selector = SelectorFactory.createSelector(selector_type)
        
        self._initialized = True
        self.logger.debug("RpiModule: Hardware Controller Ready.")

    def _get_logger(self):
        return logging.getLogger(__name__)

    @staticmethod
    def take_picture(xpid, status_manager=None):
        """
        Takes the requested pictures. 
        Raises exceptions on failure so the Scheduler can handle errors.
        """
        
        def report(state=None, cam_id=None, cam_status=None, last_pic=False):
            if status_manager:
                status_manager.update_hardware_status(
                    state=state, 
                    cam_id=cam_id, 
                    cam_status=cam_status, 
                    last_pic=last_pic
                )

        # 1. Get Singleton (Safe Hardware Access)
        rpi = RpiModule() 
        rpi.logger.info(f'taking picture for task : {xpid}')
        light = rpi.light
        
        # Load Experiment
        try:
            exp = Experiment.load_from_id(xpid)
        except Exception as e:
            rpi.logger.error(f"Failed to load experiment {xpid}: {e}")
            raise e # RESTORED: Let scheduler know the DB failed

        cameras = exp.cameras
        params = exp.img_params

        # 2. Hardware Self-Check
        if not rpi.selector.self_check():
             msg = 'Multiplexer fatal error during self-check'
             rpi.logger.error(msg)
             report(state="MULTIPLEXER_ERROR")
             exp.status = "ERROR"
             exp.message = msg
             exp.save()
             raise RuntimeError(msg) # RESTORED: Raise error for scheduler
        
        report(state="OK")

        # 3. Acquisition Loop
        retries = 0
        while retries < Config.CAM_RETRIES:
            retries += 1
            try:
                # Attempt to get lock (With Timeout)
                with rpi.lock.acquire(timeout=Config.LOCK_TIMEOUT):
                    if status_manager:
                        status_manager.update_lock_state(status="LOCKED", owner="Scheduler", details=f"Exp {xpid}")

                    rpi.logger.info('Hardware lock acquired')

                    try:
                        # --- Turn Lights ON ---
                        if exp.ir:
                            light.state = Light.ON
                            params["exposure_mode"] = "backlight"
                        else:
                            light.state = Light.OFF

                        # --- Capture Sequence ---
                        step_images = []
                        
                        for camera in cameras:
                            if camera not in Config.CAMS:
                                continue 
                            
                            now_obj = datetime.now()
                            timestamp_log = now_obj.strftime(Config.PRETTY_FORMAT)
                            timestamp_file = now_obj.strftime(Config.DATE_FORMAT)

                            report(cam_id=camera, cam_status={"health": "CAPTURING", "last_check": timestamp_log})
                            
                            camdir = os.path.join(exp.workdir, str(camera))
                            os.makedirs(camdir, exist_ok=True)
                            imagepath = os.path.join(camdir, f'{timestamp_file}_{camera}.png')

                            # Perform Capture
                            success = rpi.selector.capture(camera, imagepath, params)

                            if success:
                                step_images.append((timestamp_file, camera, imagepath))
                                rel_path = f"{xpid}/{camera}/{timestamp_file}_{camera}.png"
                                report(cam_id=camera, last_pic=True, cam_status={
                                    "health": "OK",
                                    "last_check": timestamp_log,
                                    "path": rel_path
                                })
                            else:
                                # Logic failure (Camera didn't crash, but didn't return image)
                                msg = f"Camera {camera} failed to return image."
                                rpi.logger.error(msg)
                                report(cam_id=camera, cam_status={"health": "FAILED", "last_check": timestamp_log, "path": None})
                                raise RuntimeError(msg) # RESTORED: Treat as failure

                        # --- Success ---
                        if len(step_images) > 0:
                            if hasattr(exp, 'new_step'):
                                exp.new_step(tuple(step_images))
                            exp.message = "OK"
                            rpi.logger.info("Sequence completed successfully.")
                            return True
                        else:
                            msg = "No images captured (List empty)."
                            rpi.logger.warning(msg)
                            raise RuntimeWarning(msg) # RESTORED

                    except Exception as e:
                        # CRITICAL: Catch errors during capture, log, and RE-RAISE
                        rpi.logger.error(f"Critical error during capture: {e}")
                        report(state="MULTIPLEXER_ERROR")
                        raise e 
                        
                    finally:
                        # Safety: Ensure lights are off even if we crash/raise
                        light.state = Light.OFF
                        if status_manager:
                            status_manager.update_lock_state(status="FREE", owner=None, details=None)

            except Timeout:
                rpi.logger.warning(f"Lock busy. Retry {retries}/{Config.CAM_RETRIES}")
                if retries >= Config.CAM_RETRIES:
                     raise Timeout(f"Could not acquire hardware lock after {retries} retries") # RESTORED

            # If we are here, we are retrying the loop...
            time.sleep(Config.CAM_WAIT_AFTER_RETRAY)

        # Should be unreachable if Timeout raises above, but just in case:
        raise Timeout("Hardware lock acquisition failed.")

    @staticmethod
    def check_cameras(status_manager=None):
        """
        Scans cameras. PROBES first to prevent freezing on dead ports.
        """
        def report(state=None, cam_id=None, cam_status=None, last_pic=False):
            if status_manager:
                status_manager.update_hardware_status(
                    state=state, 
                    cam_id=cam_id, 
                    cam_status=cam_status, 
                    last_pic=last_pic
                )
                
        rpi = RpiModule()
        light = rpi.light 
        results = {}
        rpi.logger.info("Starting hardware diagnostic scan...")
        
        system_dir = os.path.join(Config.WORKING_DIR, "system")
        os.makedirs(system_dir, exist_ok=True)

        try:
            with rpi.lock.acquire(timeout=5):
                report(state="SCANNING")
                if status_manager:
                    status_manager.update_lock_state(status="LOCKED", owner="System", details="Diagnostics")

                try:
                    rpi.logger.info("Diagnostics: Powering ON backlights")
                    light.state = Light.ON

                    for cam_id in Config.CAMS:
                        timestamp_log = datetime.now().strftime(Config.PRETTY_FORMAT)
                        report(cam_id=cam_id, cam_status={"health": "TESTING", "last_check": timestamp_log})
                        
                        try:
                            # 1. SAFETY PROBE: Prevent Freeze
                            if not rpi.selector.probe(cam_id):
                                error_data = {"health": "NOT DETECTED", "last_check": timestamp_log, "path": None}
                                results[cam_id] = error_data
                                report(cam_id=cam_id, cam_status=error_data)
                                continue

                            # 2. CAPTURE
                            timestamp_file = datetime.now().strftime(Config.DATE_FORMAT)
                            camdir = os.path.join(system_dir, str(cam_id))
                            os.makedirs(camdir, exist_ok=True)
                            imagepath = os.path.join(camdir, f'{timestamp_file}_camera_{cam_id}.png')
                            
                            diag_params = Config.CAM_PARAMS.copy()
                            diag_params["exposure_mode"] = "backlight"

                            success = rpi.selector.capture(cam_id, imagepath, diag_params)
                            
                            status = "OK" if success else "FAILED"
                            path = f"system/{cam_id}/{timestamp_file}_camera_{cam_id}.png" if success else None
                            
                            cam_data = {"health": status, "last_check": timestamp_log, "path": path}
                            results[cam_id] = cam_data
                            report(cam_id=cam_id, cam_status=cam_data, last_pic=success)
                            
                        except Exception as probe_err:
                            rpi.logger.error(f"Cam {cam_id} check failed: {probe_err}")
                            error_data = {"health": "ERROR", "last_check": timestamp_log, "path": None}
                            results[cam_id] = error_data
                            report(cam_id=cam_id, cam_status=error_data)
                    
                    report(state="OK")
                
                finally:
                    light.state = Light.OFF 
                    if status_manager:
                        status_manager.update_lock_state(status="FREE", owner=None, details=None)

        except Timeout:
            # For check_cameras, we usually return a dict error, but if you want strictness:
            # raise Timeout("Diagnostic scan failed: Locked")
            return {"error": "LOCKED"} 
        except Exception as e:
            report(state="MULTIPLEXER_ERROR")
            # raise e  <-- You can uncomment this if you want check_cameras to crash the caller too
            return {"error": str(e)}
        
        return results