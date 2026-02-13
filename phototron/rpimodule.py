#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Created on 26 févr. 2018
Refactored: Feb 2026 by Nicolás Gaggion

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

    def _capture_single_camera(self, camera, xpid, params, report_func):
        """
        Helper: Handles the probe and capture for a SINGLE camera.
        Returns: (success: bool, data: tuple/None, error: str/None)
        """
        now_obj = datetime.now()
        timestamp_log = now_obj.strftime(Config.PRETTY_FORMAT)
        timestamp_file = now_obj.strftime(Config.DATE_FORMAT)
        
        # 1. Prepare Paths
        camdir = os.path.join(Config.WORKING_DIR, xpid, str(camera))
        os.makedirs(camdir, exist_ok=True)
        imagepath = os.path.join(camdir, f'{timestamp_file}_{camera}.png')
        
        report_func(cam_id=camera, cam_status={"health": "CAPTURING", "last_check": timestamp_log})

        try:
            # 2. PROBE: Switch Mux and Verify
            if not self.selector.probe(camera):
                self.logger.warning(f"Cam {camera} probe failed. Retrying...")
                time.sleep(0.5)
                if not self.selector.probe(camera):
                     raise RuntimeError(f"Not detected on bus")

            # 3. CAPTURE
            if self.selector.capture(camera, imagepath, params):
                rel_path = f"{xpid}/{camera}/{timestamp_file}_{camera}.png"
                report_func(cam_id=camera, last_pic=True, cam_status={
                    "health": "OK",
                    "last_check": timestamp_log,
                    "path": rel_path
                })
                # Return success data
                return True, (timestamp_file, camera, imagepath), None
            else:
                raise RuntimeError("Capture returned False")

        except Exception as e:
            self.logger.error(f"FAIL Camera {camera}: {e}")
            report_func(cam_id=camera, cam_status={"health": "FAILED", "last_check": timestamp_log, "path": None})
            return False, None, str(e)
        
    @staticmethod
    def take_picture(xpid, status_manager=None):
        
        def report(state=None, cam_id=None, cam_status=None, last_pic=False):
            if status_manager:
                status_manager.update_hardware_status(state, cam_id, cam_status, last_pic)

        rpi = RpiModule() 
        rpi.logger.info(f'Starting sequence for: {xpid}')
        
        # Load Experiment
        try:
            exp = Experiment.load_from_id(xpid)
        except Exception:
            raise RuntimeError(f"Experiment {xpid} not found or failed to load.")

        # Hardware Check
        if not rpi.selector.self_check():
             msg = "Multiplexer fatal error"
             rpi.logger.error(msg)
             report(state="MULTIPLEXER_ERROR")
             exp.status = "ERROR"
             exp.message = msg
             exp.save()
             raise RuntimeError(msg) 
        
        report(state="OK")
        
        # --- Main Retry Loop (For Lock Only) ---
        retries = 0
        while retries < Config.CAM_RETRIES:
            retries += 1
            try:
                with rpi.lock.acquire(timeout=Config.LOCK_TIMEOUT):
                    if status_manager:
                        status_manager.update_lock_state("LOCKED", "Scheduler", f"Exp {xpid}")

                    try:
                        # 1. Lights ON
                        if exp.ir:
                            rpi.light.state = Light.ON
                            exp.img_params["exposure_mode"] = "backlight"
                        else:
                            rpi.light.state = Light.OFF

                        step_images = []
                        failed_cameras = [] 
                        
                        # 2. Iterate Cameras
                        for camera in exp.cameras:
                            if camera not in Config.CAMS: continue 
                            
                            success, data, error = rpi._capture_single_camera(camera, xpid, exp.img_params, report)
                            
                            if success:
                                step_images.append(data)
                            else:
                                failed_cameras.append((camera, error))

                        # 3. Finalize & Log
                        
                        # A. Log Failures 
                        if failed_cameras:
                            if not hasattr(exp, 'logs'): exp.logs = []
                            
                            for cam_id, err_msg in failed_cameras:
                                log_description = f"Camera {cam_id} failed: {err_msg}"
                                exp.log_event(log_description)
                                rpi.logger.warning(f"Logged failure for Cam {cam_id}")
                            exp.save()
                            
                        # B. Save Successes
                        if len(step_images) > 0:
                            return True
                        
                        else:
                            msg = f"All cameras failed. Errors: {[x[1] for x in failed_cameras]}"
                            rpi.logger.error(msg)
                            raise RuntimeError(msg)

                    finally:  
                        rpi.light.state = Light.OFF
                        if status_manager:
                            status_manager.update_lock_state("FREE", None, None)

            except Timeout:
                rpi.logger.warning(f"Lock busy. Retry {retries}")
                time.sleep(Config.CAM_WAIT_AFTER_RETRAY)

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
            report(state="ERROR")
            # raise e  <-- You can uncomment this if you want check_cameras to crash the caller too
            return {"error": str(e)}
        
        return results