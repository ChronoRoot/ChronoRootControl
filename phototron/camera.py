#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import time
import io
import os
from config import Config
from picamera2 import Picamera2
from PIL import Image

class CameraFactory(object):
    """Factory class to manage different camera modules"""
    factories = {}

    @staticmethod
    def createCamera(camera_identifier):
        if camera_identifier not in CameraFactory.factories.keys():
            factory_obj = CameraFactory.class_from_identifier(camera_identifier)
            if not factory_obj:
                logging.getLogger(__name__).warning(f"Camera type '{camera_identifier}' not found. Defaulting to RPICAM.")
                factory_obj = RaspiCamera.Factory()
            CameraFactory.factories[camera_identifier] = factory_obj
        return CameraFactory.factories[camera_identifier].create()

    @staticmethod
    def class_from_identifier(identifier):
        for c in Camera.__subclasses__():
            if c.camera_identifier and identifier.startswith(c.camera_identifier):
                return c.Factory()
        return None

class Camera(object):
    """Universal Camera abstract class"""
    camera_identifier = None

    def capture(self, image_path, params={}):
        raise NotImplementedError

    def stream_frames(self, cam_id=None):
        raise NotImplementedError
        
    def close(self):
        pass

class RaspiCamera(Camera):
    """Deals with RaspberryPi camera module using Picamera2"""
    camera_identifier = "RPICAM"
    Count = 0   
    
    settings = { 
        'Image effect' : {
            'values' : {'none': 0, 'grayscale': 1},
            'default' : Config.CAM_PARAMS.get('image_effect', 0),
            'type' : 'list',
        },
        'AWB mode' : {
            'values' : {'auto': 0, 'incandescent': 1, 'tungsten': 2, 'fluorescent': 3, 'indoor': 4, 'daylight': 5, 'cloudy': 6},
            'default' : Config.CAM_PARAMS.get('awb_mode', 0),
            'type' : 'list',
        }
    }
    
    def __init__(self):
        # Setup specific hardware logger for SHDL
        self.hw_logger = logging.getLogger("HW_" + __name__)
        self.hw_logger.setLevel(Config.LOG_LEVEL)
        if not self.hw_logger.handlers:
            handler = logging.FileHandler(Config.SHDL_LOG_FILE)
            handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
            self.hw_logger.addHandler(handler)
            self.hw_logger.propagate = False
            
        self.hw_logger.debug("RaspiCamera object initialized")
        self.picam2 = None
        RaspiCamera.Count += 1

    def __del__(self):
        RaspiCamera.Count -= 1
        self.close()

    def close(self):
        if self.picam2:
            try:
                self.picam2.stop()
                self.picam2.close()
            except Exception:
                pass
            del self.picam2 
            self.picam2 = None

    def camera_check(self):
        try:
            if self.picam2 is not None:
                return True
            try:
                test_cam = Picamera2()
            except IndexError:
                self.hw_logger.error("Hardware Check: Libcamera found 0 cameras.")
                return False
            
            test_cam.close()
            del test_cam 
            time.sleep(0.5) 
            
            return True
        except Exception as e:
            self.hw_logger.error(f"Hardware detection failed: {e}")
            return False

    def stream_frames(self, cam_id=None): 
        profile = Config.CAMERA_PROFILES[Config.CAMERA_TYPE]
        try:
            self.hw_logger.info("Stream: Initializing preview pipeline...")
            if not self.picam2:
                try:
                    self.picam2 = Picamera2()
                except IndexError:
                    self.hw_logger.error("Stream FATAL: 0 cameras detected.")
                    raise RuntimeError("0 cameras detected. Is dtoverlay=imx219 set in config.txt?")
                
            out_size = profile["resolution"]
            stream_size = profile["stream_resolution"]
            if getattr(Config, "CROP_TO_SQUARE", False):
                side = min(out_size)
                out_size = (side, side)
                s = min(stream_size)
                stream_size = (s, s)

            config = self.picam2.create_preview_configuration(
                main={"format": "RGB888", "size": stream_size}
            )
            config["sensor"]["output_size"] = out_size
            
            self.picam2.configure(config)
            self.picam2.start()

            # Apply the active capture profile so the preview matches the final
            # picture (exposure / AWB / denoise) and respect its grayscale flag.
            preview_profile = self._apply_capture_profile({})
            preview_gray = preview_profile.get("grayscale", True)
            
            if profile.get("autofocus"):
                # 1. Setup Tracking Variables for Live Tuning & Auto-Focus
                last_live_focus = None
                target_file = f"/dev/shm/focus_cam_{cam_id}.txt"
                
                # Variables for the Auto-Focus trigger
                af_trigger_file = f"/dev/shm/do_af_cam_{cam_id}.txt"
                af_result_file = f"/dev/shm/af_result_{cam_id}.txt"
                doing_af = False
                af_start_time = 0
                
                # Clean up any stale values from previous sessions
                for f in [target_file, af_trigger_file, af_result_file]:
                    if os.path.exists(f):
                        os.remove(f)
                
                keep_af = getattr(Config, 'KEEP_AUTOFOCUS', False)
                saved_distances = getattr(Config, 'FOCUS_DISTANCES', {})
                target_focus = saved_distances.get(str(cam_id)) if cam_id else None
                self.hw_logger.info(f"Stream: Keep Autofocus = {keep_af}. Target focus {target_focus} for Cam {cam_id}. ")
                    
                if target_focus is not None and not keep_af:
                    # STRICT MANUAL: Lock focus for live preview based on saved config
                    self.hw_logger.info(f"Stream: Locking focus to {target_focus} for Cam {cam_id}")
                    self.picam2.set_controls({
                        "AfMode": 0,
                        "LensPosition": float(target_focus)
                    })
                else:
                    # DYNAMIC SWEEP: Continuous AF for live preview
                    self.hw_logger.info("Stream: Using Continuous Autofocus")
                    self.picam2.set_controls({"AfMode": 2})

            self.hw_logger.info("Stream: Warmup complete. Yielding frames.")
            
            while True:
                if profile.get("autofocus") and cam_id:
                    
                    # --- AUTOFOCUS TRIGGER LOGIC ---
                    if os.path.exists(af_trigger_file) and not doing_af:
                        os.remove(af_trigger_file)
                        self.hw_logger.info("Stream: Triggering Dynamic AF Sweep via UI")
                        self.picam2.set_controls({"AfMode": 2}) # 2 is Continuous Sweep
                        doing_af = True
                        af_start_time = time.time()

                    if doing_af:
                        # Match the 3.0s delay used in the capture() logic
                        if time.time() - af_start_time > 10.0:
                            self.picam2.set_controls({"AfMode": 0}) # Lock focus
                            time.sleep(0.1) # Tiny pause to let the lock register
                            
                            # Grab metadata to find out where the lens stopped
                            meta = self.picam2.capture_metadata()
                            final_focus = meta.get("LensPosition", 0.0) if meta else 0.0
                            
                            self.hw_logger.info(f"Stream: AF sweep finished. Final LensPosition: {final_focus}")
                            doing_af = False
                            
                            # Write result for Flask route to pick up
                            with open(af_result_file, 'w') as f:
                                f.write(str(final_focus))
                                
                            # Update the slider's target file so they stay in sync
                            with open(target_file, 'w') as f:
                                f.write(str(final_focus))
                            last_live_focus = final_focus

                    # --- EXISTING MANUAL SLIDER LOGIC ---
                    # 'and not doing_af' prevents the manual slider from fighting the auto-sweep
                    elif os.path.exists(target_file) and not doing_af:
                        try:
                            with open(target_file, 'r') as f:
                                current_live_focus = float(f.read().strip())
                                
                            # Only hit the I2C bus if the slider actually moved
                            if current_live_focus != last_live_focus:                        
                                self.hw_logger.info(f"Stream: changing focus manually to {current_live_focus}")
                                self.picam2.set_controls({
                                    "AfMode": 0, # Ensure manual mode is locked
                                    "LensPosition": current_live_focus
                                })
                                last_live_focus = current_live_focus
                        except Exception:
                            self.hw_logger.debug("Stream: error reading manual focus file")
                            pass
                    
                img_array = self.picam2.capture_array()
                pil_img = Image.fromarray(img_array)
                if preview_gray:
                    pil_img = pil_img.convert('L')
                
                stream = io.BytesIO()
                pil_img.save(stream, format='JPEG')
                yield stream.getvalue()
                stream.close()

        except Exception as e:
            self.hw_logger.error(f"Stream crash: {e}")
        finally:
            self.close()

    def _resolve_capture_profile(self, params):
        """Return a valid capture profile name, falling back to the default."""
        name = params.get("capture_profile", Config.DEFAULT_CAPTURE_PROFILE)
        if name not in Config.CAM_CAPTURE_PROFILES:
            self.hw_logger.warning(
                f"Capture: unknown profile '{name}'. Falling back to '{Config.DEFAULT_CAPTURE_PROFILE}'."
            )
            name = Config.DEFAULT_CAPTURE_PROFILE
        return name

    def _apply_capture_profile(self, params):
        """Apply the selected profile's libcamera controls and return the profile dict."""
        name = self._resolve_capture_profile(params)
        profile_cfg = Config.CAM_CAPTURE_PROFILES[name]
        controls = dict(profile_cfg.get("controls", {}))
        if controls:
            self.picam2.set_controls(controls)
        self.hw_logger.debug(f"Capture: applied profile '{name}' with controls {controls}")
        return profile_cfg

    def capture(self, image_path, params={}):
        retries = 0
        profile = Config.CAMERA_PROFILES[Config.CAMERA_TYPE]
        
        while retries <= Config.CAM_RETRIES:
            retries += 1
            try:
                if not self.picam2:
                    self.picam2 = Picamera2()

                res = profile["resolution"]
                if getattr(Config, "CROP_TO_SQUARE", False):
                    side = min(res)
                    res = (side, side)
                config = self.picam2.create_still_configuration(main={"size": res})
                self.picam2.configure(config)
                self.picam2.start()
                
                if profile.get("autofocus"):
                    # 1. Pull the user's preference and distances
                    keep_af = getattr(Config, 'KEEP_AUTOFOCUS', False)
                    saved_distances = getattr(Config, 'FOCUS_DISTANCES', {})
                    cam_id = params.get("current_cam_id")
                    target_focus = saved_distances.get(cam_id)

                    # 2. Decide: Fixed Manual OR Continuous Sweep
                    if target_focus is not None and not keep_af:
                        # STRICT MANUAL: Override the autofocus completely
                        self.picam2.set_controls({
                            "AfMode": 0,                     
                            "LensPosition": float(target_focus)
                        })
                        time.sleep(0.5) 
                        
                    else:
                        # DYNAMIC SWEEP: User requested AF, or no manual config exists
                        self.picam2.set_controls({"AfMode": 2}) 
                        time.sleep(3.0)
                        self.picam2.set_controls({"AfMode": 0}) 
                        time.sleep(0.5) 
                else:
                    time.sleep(Config.CAM_WARMUP)

                # Apply the selected capture profile's libcamera controls.
                profile_cfg = self._apply_capture_profile(params)
                # Manual exposure (AeEnable False) needs longer to stabilize.
                settle = 0.5 if profile_cfg.get("controls", {}).get("AeEnable") is False else 0.2
                time.sleep(settle)

                # Grab the uncompressed pixels directly into standard RAM (NumPy array)
                img_array = self.picam2.capture_array()

                # 5. Shut down the hardware immediately to free up the Pi's CMA memory.
                self.picam2.stop()
                self.picam2.close() 
                self.picam2 = None  

                # 6. Process the in-memory array using PIL
                with Image.fromarray(img_array) as pil_img:
                    if profile_cfg.get("grayscale"):
                        bw_img = pil_img.convert('L')
                        bw_img.save(image_path, format="PNG")
                    else:
                        pil_img.save(image_path, format="PNG")

                return True

            except Exception as e:
                self.hw_logger.error(f"Capture error (Attempt {retries}): {e}")
                if self.picam2:
                    self.picam2.stop()
                    self.picam2.close() 
                    self.picam2 = None
                if retries <= Config.CAM_RETRIES:
                    time.sleep(Config.CAM_WAIT_AFTER_RETRAY)

        return False
        
    class Factory:
        def create(self):
            return RaspiCamera()