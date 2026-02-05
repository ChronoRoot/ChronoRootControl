import io
import time
import logging
import picamera
from filelock import FileLock, Timeout
from config import Config
from .base_camera import BaseCamera
from ivport_v2 import ivport
from app.options.schedulerstatus import SchedulerStatus  # Import the status manager

# Setup local logger
logger = logging.getLogger(__name__)

class Camera(BaseCamera):
    def __init__(self, cam_id):
        BaseCamera.__init__(self, cam_id)
    
    @staticmethod
    def frames(cam_id):
        """
        Generator that yields frames. 
        It manages both the physical FileLock and the SchedulerStatus reporting.
        """
        logger.info(f"Focus stream requested for Cam {cam_id}")
        
        # 1. Instantiate Status Manager
        # We don't pass scheduler/log args because we are in the Web process, 
        # it will auto-load the state from /run/
        status_manager = SchedulerStatus()
        
        # 2. Notify System: User is requesting access
        status_manager.update_lock_state(
            status="REQUESTING", 
            owner="User (Web Interface)", 
            details=f"Waiting for Cam {cam_id}"
        )

        lock = FileLock(Config.LOCK_FILE, timeout=1)

        try:
            # 3. Attempt to acquire hardware access (Wait up to 5s)
            with lock.acquire(timeout=5):
                logger.info(f"Lock acquired. Starting stream for Cam {cam_id}")

                # 4. Notify System: Lock Acquired
                status_manager.update_lock_state(
                    status="LOCKED", 
                    owner="User (Web Interface)", 
                    details=f"Live Preview: Cam {cam_id}"
                )

                # --- Hardware Initialization ---
                try:
                    iv = ivport.IVPort(ivport.TYPE_QUAD2)
                    iv.camera_change(cam_id)
                    time.sleep(0.5)  # Settle time for the switch
                except Exception as e:
                    logger.error(f"Multiplexer switch failed: {e}")
                    status_manager.update_hardware_status(state="MULTIPLEXER_ERROR")
                    return # Stop generator

                # --- Camera Capture Loop ---
                try:
                    with picamera.PiCamera() as camera:
                        # Match your website/experiment settings
                        camera.resolution = (800, 600) 
                        camera.framerate = 24
                        camera.color_effects = (128, 128) # Grayscale
                        camera.exposure_mode = 'backlight'
                        
                        # Warmup
                        time.sleep(1.0)

                        stream = io.BytesIO()
                        
                        # Continuous capture yields a stream of JPEGs
                        for _ in camera.capture_continuous(stream, 'jpeg', use_video_port=True):
                            stream.seek(0)
                            yield stream.read()

                            # Reset stream for next frame
                            stream.seek(0)
                            stream.truncate()
                            
                except Exception as e:
                    logger.error(f"PiCamera error: {e}")
                    
        except Timeout:
            # If we land here, RpiModule is likely running an experiment.
            logger.warning("Could not acquire hardware lock. Experiment in progress?")
            
            # Update status to reflect we were denied access
            status_manager.update_lock_state(
                status="BUSY", 
                owner="System (Experiment)", 
                details="Access Denied"
            )
            return

        except Exception as e:
            logger.error(f"Critical stream error: {e}")
        
        finally:
            # 5. CRITICAL: Always release the status when done
            # This ensures the dashboard doesn't get stuck saying "LOCKED" 
            # if the user closes the tab or the stream errors out.
            logger.info("Stream finished. Lock released.")
            status_manager.update_lock_state(status="FREE", owner=None, details=None)
            
            # Optional: Reset multiplexer status text to OK if it wasn't an error
            status_manager.update_hardware_status(state="OK")