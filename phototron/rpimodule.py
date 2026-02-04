#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Created on 26 févr. 2018

@author: Vladimir Daric
@email: vladimir.daric@cnrs.fr

ChronoRoot robot module implementation

RpiModule class impements all ChronoRoot robot functions

'''

import logging
import os
import shutil
import time, arrow
from config import Config
from phototron.camera_selector import SelectorFactory
from phototron.light import Light
from app.experiment.models import Experiment
from filelock import FileLock, Timeout

class RpiModule(object):
    """Toplevel class
       Implements all ChronoRoot robot functions and manages
       configuration and initialisation of all components

       This class has only one static method. The take_picture method
       all the complexity is delegated to subclasses and sub-modules.
    """
    Count = 0   # This represents the count of objects of this class
    def __init__(self):
        """Init - get the selector type from the configuration file and
        init the selector, init the logger, init the file lock

        file lock is used to prevent simultaneous acces to camera
        """

        selector_type = Config.SELECTOR_TYPE
        self.selector  = SelectorFactory.createSelector(selector_type)
        self.light = Light()
        self.logger = self.logger()
        self.logger.debug("RpiModule object initialized")
        self.lock = FileLock(Config.LOCK_FILE, Config.LOCK_TIMEOUT)
        RpiModule.Count += 1

    def __del__(self):
        """properly remove RpiModule object instances
        """

        self.logger.debug('deleting : %s'%(self))
        RpiModule.Count -= 1
        if RpiModule.Count == 0:
            self.logger.debug('Last RpiModule object deleted')
        else:
            self.logger.debug('%s RpiModule objects remaining ' % RpiModule.Count)
        del self.selector
        del self


    def logger(self):
        """Logger initialisation
        """

        logger = logging.getLogger(__name__)
        return logger

    @staticmethod
    def take_picture(xpid, status_manager=None):
        """
        Takes the requested pictures. Updates global hardware state with dictionaries.
        """
        
        # --- Helper for UI reporting ---
        def report(state=None, cam_id=None, cam_status=None, last_pic=False):
            if status_manager:
                status_manager.update_hardware_status(
                    state=state, 
                    cam_id=cam_id, 
                    cam_status=cam_status, 
                    last_pic=last_pic
                )

        rpi = RpiModule()
        rpi.logger.info(f'taking picture for task : {xpid}')
        light = rpi.light
        exp = Experiment(directory=os.path.join(Config.WORKING_DIR, xpid))

        cameras = exp.cameras
        params = exp.img_params

        # 1. Pre-Check
        if not rpi.selector.self_check():
            rpi.logger.error('Multiplexer fatal error during self-check')
            report(state="MULTIPLEXER_ERROR")
            exp.status = "FAILED"
            exp.message = "Multiplexer fatal error"
            exp.dump()
            return False
        
        report(state="OK")

        # 2. Acquisition Loop
        retries = 0
        while retries < Config.CAM_RETRIES:
            retries += 1
            try:
                # Attempt to get lock
                with rpi.lock.acquire():
                    try: 
                        # --- START CRITICAL SECTION ---
                        # Notify Status Manager: WE HAVE THE LOCK
                        if status_manager:
                            status_manager.update_lock_state(
                                status="LOCKED", 
                                owner="Scheduler", 
                                details=f"Exp {xpid}"
                            )

                        rpi.logger.info('Hardware lock acquired')

                        # --- A. Turn Lights ON ---
                        if exp.ir:
                            light.state = Light.ON
                            params["exposure_mode"] = "backlight"
                        else:
                            light.state = Light.OFF

                        # --- B. Execute Capture Sequence ---
                        step_images = []
                        
                        try:
                            for camera in cameras:
                                if camera not in Config.CAMS:
                                    continue 
                                
                                report(cam_id=camera, cam_status={
                                    "health": "CAPTURING", 
                                    "last_check": arrow.now().format('HH:mm:ss')
                                })
                                
                                instant_date = arrow.now().format('YYYY-MM-DD_HH-mm-ss')
                                camdir = os.path.join(exp.workdir, str(camera))
                                os.makedirs(camdir, exist_ok=True)
                                imagepath = os.path.join(camdir, f'{instant_date}_{camera}.png')

                                success = rpi.selector.capture(camera, imagepath, params)

                                if success:
                                    step_images.append((instant_date, camera, imagepath))
                                    rel_path = f"{xpid}/{camera}/{instant_date}_{camera}.png"
                                    report(cam_id=camera, last_pic=True, cam_status={
                                        "health": "OK",
                                        "last_check": arrow.now().format('HH:mm:ss'),
                                        "path": rel_path
                                    })
                                else:
                                    rpi.logger.error(f"Camera {camera} failed.")
                                    report(cam_id=camera, cam_status={
                                        "health": "FAILED",
                                        "last_check": arrow.now().format('HH:mm:ss'),
                                        "path": None
                                    })
                                    light.state = Light.OFF 
                                    return False

                        except Exception as e:
                            rpi.logger.error(f"Critical error on Cam {camera}: {e}")
                            report(cam_id=camera, cam_status={
                                "health": "HW_ERROR",
                                "last_check": arrow.now().format('HH:mm:ss'),
                                "path": None
                            })
                            report(state="MULTIPLEXER_ERROR")
                            light.state = Light.OFF 
                            return False

                        # --- C. Success Path ---
                        light.state = Light.OFF
                        
                        if len(step_images) > 0:
                            exp.new_step(tuple(step_images))
                            exp.message = "OK"
                            rpi.logger.info("Sequence completed successfully.")
                            return True
                        else:
                            rpi.logger.warning("No images captured.")
                            return False
                    
                    finally:
                        if status_manager:
                            status_manager.update_lock_state(
                                status="FREE", 
                                owner=None, 
                                details=None
                            )

            except Timeout:
                rpi.logger.warning(f"Lock busy. Retry {retries}/{Config.CAM_RETRIES}")
                time.sleep(Config.CAM_WAIT_AFTER_RETRAY)

        rpi.logger.error('Could not acquire hardware lock.')
        return False
    
    @staticmethod
    def check_cameras(status_manager=None):
        """
        Scans all configured camera ports.
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
        results = {}
        rpi.logger.info("Starting hardware diagnostic scan...")
        
        system_dir = os.path.join(Config.WORKING_DIR, "system")
        if os.path.exists(system_dir):
            try:
                shutil.rmtree(system_dir)
            except Exception:
                pass
        os.makedirs(system_dir, exist_ok=True)

        try:
            with rpi.lock.acquire(timeout=5):
                report(state="SCANNING")
        
                try:
                    # Notify Status Manager: WE HAVE THE LOCK
                    if status_manager:
                        status_manager.update_lock_state(
                            status="LOCKED", 
                            owner="System", 
                            details="Diagnostics"
                        )

                    for cam_id in Config.CAMS:
                        report(cam_id=cam_id, cam_status={
                            "health": "TESTING", 
                            "last_check": arrow.now().format('HH:mm:ss')
                        })
                        
                        try:
                            is_online = rpi.selector.probe(cam_id)
                            cam_data = {"health": "NOT DETECTED", "last_check": arrow.now().format('HH:mm:ss'), "path": None}
                            
                            if is_online:
                                time.sleep(2.0)
                                instant_date = arrow.now().format('YYYY-MM-DD_HH-mm-ss')
                                camdir = os.path.join(system_dir, str(cam_id))
                                os.makedirs(camdir, exist_ok=True)
                                imagepath = os.path.join(camdir, f'{instant_date}_camera_{cam_id}.png')
                                
                                rpi.selector.capture(cam_id, imagepath, Config.CAM_PARAMS)
                                rel_path = f"system/{cam_id}/{instant_date}_camera_{cam_id}.png"
                                
                                cam_data["health"] = "OK"
                                cam_data["path"] = rel_path
                                report(last_pic=True)
                                
                            results[cam_id] = cam_data
                            report(cam_id=cam_id, cam_status=cam_data)
                            
                        except Exception as probe_err:
                            rpi.logger.error(f"Probe crashed on Cam {cam_id}: {probe_err}")
                            error_data = {"health": "ERROR", "last_check": arrow.now().format('HH:mm:ss'), "path": None}
                            report(cam_id=cam_id, cam_status=error_data)
                            results[cam_id] = error_data
                            report(state="CAMERA_ERROR")
                            return {"error": str(probe_err)}
                    
                    report(state="OK")
                
                finally:
                    # Release status lock
                    if status_manager:
                        status_manager.update_lock_state(status="FREE", owner=None, details=None)

        except Timeout:
            rpi.logger.error("Scan failed: Hardware locked.")
            return {"error": "LOCKED"}
        except Exception as e:
            rpi.logger.error(f"Scan failed: {e}")
            report(state="MULTIPLEXER_ERROR")
            return {"error": str(e)}
        
        return results