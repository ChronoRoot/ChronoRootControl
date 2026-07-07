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
            
        # Initialize both loggers (Hardware & Experiment)
        self._setup_loggers()
        
        self.hw_logger.debug("RpiModule: Initializing Hardware Controller...")

        self.lock = FileLock(Config.LOCK_FILE, Config.LOCK_TIMEOUT)
        self.light = Light()

        # Init Selector once. It stays alive to hold GPIO state.
        selector_type = Config.SELECTOR_TYPE
        self.selector = SelectorFactory.createSelector(selector_type)
        
        self._initialized = True
        self.hw_logger.debug("RpiModule: Hardware Controller Ready.")

    def _setup_loggers(self):
        """
        Sets up two distinct loggers:
        1. hw_logger -> SHDL file for hardware status and errors.
        2. exp_logger -> Main log file for experiment workflow.
        """
        # --- Hardware Logger (SHDL) ---
        self.hw_logger = logging.getLogger("HW_" + __name__)
        self.hw_logger.setLevel(Config.LOG_LEVEL)
        if not self.hw_logger.handlers:
            hw_handler = logging.FileHandler(Config.SHDL_LOG_FILE)
            hw_handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
            self.hw_logger.addHandler(hw_handler)
            self.hw_logger.propagate = False # Prevent duplicate logging to root

        # --- Experiment Logger ---
        self.exp_logger = logging.getLogger("EXP_" + __name__)
        self.exp_logger.setLevel(Config.LOG_LEVEL)
        if not self.exp_logger.handlers:
            exp_handler = logging.FileHandler(Config.LOGFILE)
            exp_handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
            self.exp_logger.addHandler(exp_handler)
            self.exp_logger.propagate = False

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
        
        report_func(cam_id=camera, cam_status={"activity": "CAPTURING"})

        try:
            # 2. PROBE: Switch Mux and Verify
            if not self.selector.probe(camera):
                self.hw_logger.warning(f"Hardware Probe: Cam {camera} failed. Retrying...")
                time.sleep(0.5)
                if not self.selector.probe(camera):
                     raise RuntimeError(f"Not detected on bus")

            # 3. CAPTURE
            if self.selector.capture(camera, imagepath, params):
                rel_path = f"{xpid}/{camera}/{timestamp_file}_{camera}.png"
                report_func(cam_id=camera, last_pic=True, cam_status={
                    "health": "OK",
                    "activity": "IDLE",
                    "last_check": timestamp_log,
                    "path": rel_path
                })
                return True, (timestamp_file, camera, imagepath), None
            else:
                raise RuntimeError("Capture returned False")

        except Exception as e:
            # Hardware failure is reported to SHDL
            self.hw_logger.error(f"Hardware FAIL on Camera {camera}: {e}")
            health = "NOT DETECTED" if "Not detected on bus" in str(e) else "ERROR"
            report_func(cam_id=camera, cam_status={
                "health": health, "activity": "IDLE",
                "last_check": timestamp_log, "path": None
            })
            return False, None, str(e)
        
    @staticmethod
    def _experiment_stopped(xpid, status_manager):
        """True when the experiment was cancelled or finished (disk or RAM tombstone)."""
        try:
            exp = Experiment.load_from_id(xpid)
            if exp.status in ("CANCELLED", "FINISHED"):
                return True
        except Exception:
            pass
        status_manager.load()
        if xpid in status_manager.state.get("cancelled_experiments", []):
            return True
        return False

    @staticmethod
    def take_picture(xpid, status_manager):
        
        def report(cam_id=None, cam_status=None, last_pic=False):
            status_manager.update_hardware_status(cam_id=cam_id, cam_status=cam_status, last_pic=last_pic)

        rpi = RpiModule() 
        # Experiment workflow logs to the main EXP logger
        rpi.exp_logger.info(f'Starting picture sequence for experiment: {xpid}')
        
        # Load Experiment
        try:
            exp = Experiment.load_from_id(xpid)
        except Exception:
            rpi.exp_logger.error(f"Experiment {xpid} not found or failed to load.")
            raise RuntimeError(f"Experiment {xpid} not found or failed to load.")

        if RpiModule._experiment_stopped(xpid, status_manager):
            rpi.exp_logger.info(f"Skipping capture for {xpid}: experiment is cancelled or finished.")
            return False

        # --- Main Retry Loop (For Lock Only) ---
        retries = 0
        while retries < Config.CAM_RETRIES:
            retries += 1
            try:
                with rpi.lock.acquire(timeout=Config.LOCK_TIMEOUT):
                    status_manager.update_lock_state("LOCKED", "Scheduler", f"Exp {xpid}")

                    try:
                        if RpiModule._experiment_stopped(xpid, status_manager):
                            rpi.exp_logger.info(f"Aborting capture for {xpid}: cancelled while waiting for lock.")
                            return False

                        # 1. Lights ON
                        if exp.ir:
                            rpi.light.state = Light.ON
                            status_manager.update_lights_state("ON")
                            
                            if Config.IR_WARM_UP != 0:
                                time.sleep(Config.IR_WARM_UP)
                        else:
                            rpi.light.state = Light.OFF
                            status_manager.update_lights_state("OFF")

                        step_images = []
                        failed_cameras = [] 
                        
                        # 2. Iterate Cameras
                        for camera in exp.cameras:
                            if camera not in Config.CAMS:
                                continue
                            if RpiModule._experiment_stopped(xpid, status_manager):
                                rpi.exp_logger.info(f"Aborting capture for {xpid}: cancelled mid-round.")
                                return False

                            success, data, error = rpi._capture_single_camera(camera, xpid, exp.img_params, report)
                            
                            if success:
                                # Report success to the main experiment logger
                                rpi.exp_logger.info(f"Picture taken successfully for experiment {xpid} on Cam {camera}")
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
                                rpi.exp_logger.warning(f"Logged failure for Cam {cam_id} during experiment {xpid}")
                            
                        # B. Save Successes
                        if len(step_images) > 0:
                            status_manager.increment_job_progress(xpid, "SUCCESS")

                            hw = status_manager.state.setdefault("hardware", {})
                            acf = hw.get("all_cameras_failed")
                            if acf and acf.get("expid") == xpid:
                                hw["all_cameras_failed"] = None
                                status_manager.write()
                                
                            if exp.status == "ERROR":
                                rpi.exp_logger.info(f"Experiment {xpid} recovered from previous error.")
                                exp.log_event("Recovered from error.")
                                exp.status = "RUNNING"
                                exp.message = "Recovered from error"
                                exp.save()
                            return True
                        
                        else:
                            msg = f"All cameras failed. Errors: {[x[1] for x in failed_cameras]}"
                            rpi.hw_logger.error(f"Catastrophic hardware fail during {xpid}: {msg}")
                            rpi.exp_logger.error(f"Experiment {xpid} failed entirely: {msg}")

                            status_manager.load()
                            status_manager.state.setdefault("hardware", {})["all_cameras_failed"] = {
                                "expid": xpid,
                                "at": datetime.now().strftime(Config.PRETTY_FORMAT),
                            }
                            status_manager.write()
                        
                            if exp.status not in ("ERROR", "CANCELLED", "FINISHED"):
                                exp.status = "ERROR"
                                exp.message = msg
                                exp.save()
                            raise RuntimeError(msg)

                    finally:  
                        rpi.light.state = Light.OFF
                        status_manager.update_lights_state("OFF")
                        status_manager.update_lock_state("FREE", None, None)

            except Timeout:
                # Lock issues are hardware-level contention
                rpi.hw_logger.warning(f"Hardware lock busy during {xpid}. Retry {retries}/{Config.CAM_RETRIES}")
                time.sleep(Config.CAM_WAIT_AFTER_RETRAY)

        rpi.hw_logger.error(f"Hardware lock acquisition completely failed for {xpid}.")
        raise Timeout("Hardware lock acquisition failed.")

    @staticmethod
    def check_cameras(status_manager):
        """
        Scans cameras. PROBES first to prevent freezing on dead ports.
        All logs here are purely hardware diagnostics (SHDL).
        """
        def report(cam_id=None, cam_status=None, last_pic=False):
            status_manager.update_hardware_status(
                cam_id=cam_id,
                cam_status=cam_status,
                last_pic=last_pic
            )
            
        rpi = RpiModule()
        light = rpi.light 
        results = {}
        
        rpi.hw_logger.info("Starting hardware diagnostic scan...")
        
        system_dir = os.path.join(Config.WORKING_DIR, "system")
        os.makedirs(system_dir, exist_ok=True)

        try:
            with rpi.lock.acquire(timeout=5):
                status_manager.update_lock_state(status="LOCKED", owner="System", details="Diagnostics")

                try:
                    # ==========================================
                    # 1. LIGHTS DIAGNOSTIC (Using Cam 1)
                    # ==========================================
                    test_cam = 1 if 1 in Config.CAMS else (Config.CAMS[0] if Config.CAMS else None)
                    
                    if test_cam and rpi.selector.probe(test_cam):
                        rpi.hw_logger.info(f"Diagnostics: Running Light Test on Cam {test_cam}")
                        # Utilize the shared helper (lock is already held)
                        rpi._run_light_test(test_cam, status_manager, report)               
                    
                    # ==========================================
                    # 2. CAMERA SWEEP (Standard Diagnostics)
                    # ==========================================
                    
                    rpi.hw_logger.info("Diagnostics: Powering ON backlights")
                    light.state = Light.ON
                    status_manager.update_lights_state("ON")
                    
                    if Config.IR_WARM_UP != 0:
                        time.sleep(Config.IR_WARM_UP)

                    for cam_id in Config.CAMS:
                        timestamp_log = datetime.now().strftime(Config.PRETTY_FORMAT)
                        report(cam_id=cam_id, cam_status={"activity": "CAPTURING"})
                        
                        try:
                            # 1. SAFETY PROBE: Prevent Freeze
                            if not rpi.selector.probe(cam_id):
                                rpi.hw_logger.warning(f"Diagnostics: Camera {cam_id} NOT DETECTED on bus.")
                                error_data = {
                                    "health": "NOT DETECTED", "activity": "IDLE",
                                    "last_check": timestamp_log, "path": None
                                }
                                results[cam_id] = error_data
                                report(cam_id=cam_id, cam_status=error_data)
                                continue

                            # 2. CAPTURE
                            timestamp_file = datetime.now().strftime(Config.DATE_FORMAT)
                            camdir = os.path.join(system_dir, str(cam_id))
                            os.makedirs(camdir, exist_ok=True)
                            imagepath = os.path.join(camdir, f'{timestamp_file}_camera_{cam_id}.png')
                            
                            diag_params = Config.CAM_PARAMS.copy()

                            success = rpi.selector.capture(cam_id, imagepath, diag_params)
                            
                            status = "OK" if success else "ERROR"
                            if success:
                                rpi.hw_logger.info(f"Diagnostics: Camera {cam_id} passed health check.")
                            else:
                                rpi.hw_logger.error(f"Diagnostics: Camera {cam_id} failed to capture image.")
                                
                            path = f"system/{cam_id}/{timestamp_file}_camera_{cam_id}.png" if success else None
                            
                            cam_data = {
                                "health": status, "activity": "IDLE",
                                "last_check": timestamp_log, "path": path
                            }
                            results[cam_id] = cam_data
                            report(cam_id=cam_id, cam_status=cam_data, last_pic=success)
                            
                        except Exception as probe_err:
                            rpi.hw_logger.error(f"Diagnostics: Cam {cam_id} check raised exception: {probe_err}")
                            error_data = {
                                "health": "ERROR", "activity": "IDLE",
                                "last_check": timestamp_log, "path": None
                            }
                            results[cam_id] = error_data
                            report(cam_id=cam_id, cam_status=error_data)
                
                finally:
                    light.state = Light.OFF 
                    rpi.hw_logger.info("Diagnostics: Powering OFF backlights")
                    status_manager.update_lights_state("OFF")
                    status_manager.update_lock_state(status="FREE", owner=None, details=None)

        except Timeout:
            rpi.hw_logger.warning("Diagnostics scan aborted: Hardware lock is currently busy.")
            return {"error": "LOCKED"} 
        except Exception as e:
            rpi.hw_logger.error(f"Diagnostics scan encountered a critical error: {e}")
            return {"error": str(e)}
        
        return results
    
    @staticmethod
    def test_single_camera(cam_id, status_manager, use_ir=None):
        """
        Takes a single test picture on demand and updates the SchedulerStatus.
        Restores IR state afterward (unlike scheduled capture / diagnostics).
        """
        def report(cam_id=None, cam_status=None, last_pic=False):
            status_manager.update_hardware_status(cam_id=cam_id, cam_status=cam_status, last_pic=last_pic)

        rpi = RpiModule()
        if use_ir is None:
            use_ir = (rpi.light.state == Light.ON)

        rpi.hw_logger.info(f"Starting single camera test for Cam {cam_id}")
        
        try:
            with rpi.lock.acquire(timeout=5):
                lights_before = rpi.light.state
                status_manager.update_lock_state("LOCKED", "System", f"Test Cam {cam_id}")

                try:
                    params = Config.CAM_PARAMS.copy()
                    if use_ir:
                        rpi.light.state = Light.ON
                        status_manager.update_lights_state("ON")
                        
                        if Config.IR_WARM_UP != 0:
                            time.sleep(Config.IR_WARM_UP)
                    else:
                        rpi.light.state = Light.OFF
                        status_manager.update_lights_state("OFF")
                    
                    # Reuse existing method; saves to working_dir/system/cam_id/...
                    success, data, error = rpi._capture_single_camera(cam_id, "system", params, report)
                    
                    if success:
                        rpi.hw_logger.info(f"Test Cam {cam_id} success.")
                        timestamp, _, _ = data
                        rel_path = f"system/{cam_id}/{timestamp}_{cam_id}.png"
                        return {"result": True, "path": rel_path}
                    else:
                        return {"error": error}
                finally:
                    rpi.light.state = lights_before
                    status_manager.update_lights_state("ON" if lights_before == Light.ON else "OFF")
                    status_manager.update_lock_state("FREE", None, None)
        except Timeout:
            rpi.hw_logger.warning("Single test aborted: lock busy.")
            return {"error": "LOCKED"}
        except Exception as e:
            return {"error": str(e)}

    def _run_light_test(self, cam_id, status_manager, report_func=None):
        """
        Internal helper for light diagnostics. 
        Assumes the hardware lock is already acquired by the caller.
        """
        import numpy as np
        from PIL import Image

        # 1. Setup exact, clean paths: system/lights/camera_n_on.png
        system_lights_dir = os.path.join(Config.WORKING_DIR, "system", "lights")
        os.makedirs(system_lights_dir, exist_ok=True)
        
        path_off = os.path.join(system_lights_dir, f"camera_{cam_id}_off.png")
        path_on = os.path.join(system_lights_dir, f"camera_{cam_id}_on.png")
        
        # 2. Capture the ON/OFF pair with identical hardware settings. The
        # configured default profile (backlight_manual) locks exposure/AWB so the
        # effect-size comparison is valid.
        params = Config.CAM_PARAMS.copy()

        # 3. Capture ON first to lock the settings, then OFF
        self.light.state = Light.ON 
        status_manager.update_lights_state("ON")
        
        if Config.IR_WARM_UP != 0:
            time.sleep(Config.IR_WARM_UP)

        timestamp_log = datetime.now().strftime(Config.PRETTY_FORMAT)

        if report_func:
            report_func(cam_id=cam_id, cam_status={"activity": "CAPTURING"})
            
        succ_on = self.selector.capture(cam_id, path_on, params)
        
        # 4. Capture OFF image with the same locked settings
        self.light.state = Light.OFF
        status_manager.update_lights_state("OFF")
        
        if Config.IR_WARM_UP != 0:
            time.sleep(Config.IR_WARM_UP)

        if report_func:
            report_func(cam_id=cam_id, cam_status={"activity": "CAPTURING"})
            
        succ_off = self.selector.capture(cam_id, path_off, params)
        
        if not (succ_off and succ_on):
            status_manager.update_lights_status({
                "last_test": datetime.now().strftime(Config.PRETTY_FORMAT),
                "status": "NOT DETECTED",
            })
            if report_func:
                report_func(cam_id=cam_id, cam_status={
                    "health": "ERROR", "activity": "IDLE",
                    "last_check": timestamp_log, "path": None
                })
            return {"error": "Failed to capture diagnostic images."}
            
        # 5. Math & Analysis
        
        img_off = np.array(Image.open(path_off).convert('L'))
        img_on = np.array(Image.open(path_on).convert('L'))

        mean_off, std_off = float(np.mean(img_off)), float(np.std(img_off))
        mean_on, std_on = float(np.mean(img_on)), float(np.std(img_on))
        
        # Calculate raw difference
        diff = mean_on - mean_off
        
        # Calculate Standardized Effect Size (prevent division by zero if image is pitch black)
        # Measures how many standard deviations the mean shifted compared to the OFF baseline
        
        denominator = std_off if std_off > 1.0 else 1.0
        effect_size = abs(diff) / denominator
        
        # A threshold of 1.0 means the light shifted the entire image's average 
        # brightness by more than a full standard deviation of the baseline noise.
        lights_working = effect_size > 1.0

        rel_path_off = f"system/lights/camera_{cam_id}_off.png"
        rel_path_on = f"system/lights/camera_{cam_id}_on.png"

        # Report Final Status to the camera telemetry
        if report_func:
            report_func(cam_id=cam_id, last_pic=True, cam_status={
                "health": "OK",
                "activity": "IDLE",
                "last_check": timestamp_log,
                "path": rel_path_on 
            })

        # 6. Format Result
        result_dict = {
            "result": True,
            "lights_working": lights_working,
            "statistical_effect_size": round(effect_size, 2),
            "intensity_off_mean": round(mean_off, 2),
            "intensity_off_std": round(std_off, 2),
            "intensity_on_mean": round(mean_on, 2),
            "intensity_on_std": round(std_on, 2),
            "difference": round(diff, 2),
            "path_off": rel_path_off,
            "path_on": rel_path_on
        }

        # 7. Update System Telemetry
        status_manager.update_lights_status({
            "last_test": datetime.now().strftime(Config.PRETTY_FORMAT),
            "status": "OK" if lights_working else "NOT DETECTED",
            "statistical_effect_size": round(effect_size, 2),
            "intensity_off_mean": round(mean_off, 2),
            "intensity_off_std": round(std_off, 2),
            "intensity_on_mean": round(mean_on, 2),
            "intensity_on_std": round(std_on, 2),
            "difference": round(diff, 2),
            "path_off": rel_path_off,
            "path_on": rel_path_on
        })

        return result_dict
    
    @staticmethod
    def test_camera_lights(cam_id, status_manager):
        """
        Diagnoses IR lights individually using the shared internal helper.
        """
        def report(cam_id=None, cam_status=None, last_pic=False):
            status_manager.update_hardware_status(cam_id=cam_id, cam_status=cam_status, last_pic=last_pic)

        rpi = RpiModule()
        rpi.hw_logger.info(f"Starting lights diagnostic for Cam {cam_id}")

        try:
            with rpi.lock.acquire(timeout=5):
                status_manager.update_lock_state("LOCKED", "System", f"Light Test Cam {cam_id}")

                try:
                    # Pass the report function into the helper
                    result = rpi._run_light_test(cam_id, status_manager, report)
                    return result

                finally:
                    status_manager.update_lights_state("OFF")
                    rpi.light.state = Light.OFF
                    status_manager.update_lock_state("FREE", None, None)
                        
        except Timeout:
            rpi.hw_logger.warning("Lights test aborted: lock busy.")
            return {"error": "LOCKED"}
        except Exception as e:
            return {"error": str(e)}