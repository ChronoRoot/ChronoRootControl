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
        logger.info(f"Focus stream requested for Cam {cam_id}")
        status_manager = SchedulerStatus()
        
        status_manager.update_lock_state(
            status="REQUESTING", 
            owner="User (Web Interface)", 
            details=f"Waiting for Cam {cam_id}"
        )

        lock = FileLock(Config.LOCK_FILE, timeout=1)

        try:
            with lock.acquire(timeout=5):
                logger.info(f"Lock acquired. Starting stream for Cam {cam_id}")

                status_manager.update_lock_state(
                    status="LOCKED", 
                    owner="User (Web Interface)", 
                    details=f"Live Preview: Cam {cam_id}"
                )

                # --- 1. Multiplexer Switch ---
                try:
                    iv = ivport.IVPort(ivport.TYPE_QUAD2)
                    iv.camera_change(cam_id)
                    time.sleep(0.5) 
                except Exception as e:
                    logger.error(f"Multiplexer switch failed: {e}")
                    status_manager.update_hardware_status(state="MULTIPLEXER_ERROR")
                    return 

                # --- 2. Camera Initialization ---
                try:
                    camera = picamera.PiCamera()
                    # Setup parameters
                    camera.resolution = (800, 600) 
                    camera.framerate = 12
                    camera.color_effects = (128, 128) 
                    camera.exposure_mode = 'backlight'
                    
                    time.sleep(1.0) # Critical warmup
                except picamera.PiCameraError as e:
                    logger.error(f"Failed to initialize PiCamera: {e}")
                    # NOTIFY SYSTEM: Hardware is physically failing
                    status_manager.update_hardware_status(
                        state="CAMERA_ERROR",
                        cam_id=cam_id,
                        cam_status={"health": "HW_FAILURE", "last_check": "NOW"}
                    )
                    return # Exit cleanly so 'finally' releases the lock

                # --- 3. Capture Loop ---
                try:
                    stream = io.BytesIO()
                    for _ in camera.capture_continuous(stream, 'jpeg', use_video_port=True):
                        stream.seek(0)
                        yield stream.read()
                        stream.seek(0)
                        stream.truncate()
                except Exception as e:
                    logger.error(f"Streaming loop interrupted: {e}")
                finally:
                    # Clean up the camera object specifically
                    camera.close()
                    logger.info("PiCamera closed.")
                            
        except Timeout:
            logger.warning("Lock Timeout: Hardware currently in use by another process.")
            status_manager.update_lock_state(
                status="BUSY", 
                owner="System (Experiment)", 
                details="Access Denied"
            )
            return

        except Exception as e:
            logger.error(f"Critical stream error: {e}")
            status_manager.update_hardware_status(state="SYSTEM_ERROR")
        
        finally:
            logger.info("Stream finished. Releasing lock and resetting status.")
            try:
                status_manager.update_lock_state(status="FREE", owner=None, details=None)
                hw_state = status_manager.state["hardware"]["multiplexer"]
                if hw_state not in ["ERROR", "CAMERA_ERROR", "MULTIPLEXER_ERROR"]:
                    status_manager.update_hardware_status(state="OK")
                    
            except Exception as e:
                logger.error(f"Failed to reset hardware status in finally block: {e}")