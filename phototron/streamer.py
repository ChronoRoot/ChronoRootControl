import time
import threading
import logging
from filelock import FileLock, Timeout
from config import Config
from app.options.schedulerstatus import SchedulerStatus
from phototron.rpimodule import RpiModule
from phototron.camera import CameraFactory
import datetime

try:
    from greenlet import getcurrent as get_ident
except ImportError:
    try:
        from thread import get_ident
    except ImportError:
        from _thread import get_ident

# --- EXPLICIT FILE LOGGER SETUP ---
logger = logging.getLogger(__name__)
logger.setLevel(Config.LOG_LEVEL)
if not logger.handlers:
    file_handler = logging.FileHandler(Config.SHDL_LOG_FILE)
    file_handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
    logger.addHandler(file_handler)
    logger.propagate = False # Prevent duplicate logs if root logger is active

class CameraEvent(object):
    def __init__(self):
        self.events = {}

    def wait(self, timeout=None):
        ident = get_ident()
        if ident not in self.events:
            self.events[ident] = [threading.Event(), time.time()]
        return self.events[ident][0].wait(timeout)

    def set(self):
        now = time.time()
        remove = None
        for ident, event in self.events.items():
            if not event[0].isSet():
                event[0].set()
                event[1] = now
            else:
                if now - event[1] > 5:
                    remove = ident
        if remove:
            del self.events[remove]

    def clear(self):
        self.events[get_ident()][0].clear()

class CameraStream(object):
    thread = None  
    frame = None  
    reset = False
    last_access = 0  
    cam_id = 1 
    event = CameraEvent()
    active_camera = None

    def __init__(self, cam_id):
        self.cam_id = cam_id
        CameraStream.cam_id = cam_id

        if CameraStream.thread is not None and not CameraStream.thread.is_alive():
            logger.debug("Cleaning up dead thread reference.")
            CameraStream.thread = None
            CameraStream.reset = False

        if CameraStream.thread is None:
            CameraStream.last_access = time.time()
            logger.info('Starting camera stream thread with cam %s.' % self.cam_id)
            CameraStream.thread = threading.Thread(target=self._thread)
            CameraStream.thread.start()

            start_wait = time.time()
            while self.get_frame() is None:
                if time.time() - start_wait > 15.0:
                    logger.error(f"Timeout: Camera {cam_id} stream failed to produce a frame.")
                    break
                time.sleep(0.1)
                
        else:
            CameraStream.last_access = time.time()
            CameraStream.reset = True
            logger.info("Thread %s already running. Requesting reset for Cam %s." % (CameraStream.thread.name, cam_id))
            
            start_wait = time.time()
            while CameraStream.thread is not None and CameraStream.thread.is_alive():
                if time.time() - start_wait > 5.0:
                    logger.error("Timeout: Old stream thread is deadlocked. Forcing overwrite.")
                    CameraStream.thread = None 
                    break
                time.sleep(0.1)
            
            CameraStream.reset = False 
            CameraStream.thread = threading.Thread(target=self._thread)
            CameraStream.thread.start()

    def get_frame(self):
        CameraStream.last_access = time.time()
        got_signal = CameraStream.event.wait(timeout=10.0)
        
        if not got_signal:
            logger.warning("get_frame timeout: Hardware stopped sending frames.")
            return None 

        CameraStream.event.clear()
        return CameraStream.frame

    @classmethod
    def frames(cls, cam_id):
        logger.info(f"[STREAM] Generation requested for Cam {cam_id}")
        
        status_manager = SchedulerStatus()
        rpi = RpiModule()
        
        status_manager.update_lock_state(status="REQUESTING", owner="User (Web Interface)", details=f"Waiting for Cam {cam_id}")
        lock = FileLock(Config.LOCK_FILE, timeout=1)

        try:
            with lock.acquire(timeout=5):
                logger.info(f"[STREAM] Lock acquired. Starting hardware boot for Cam {cam_id}")
                status_manager.update_lock_state(status="LOCKED", owner="User (Web Interface)", details=f"Live Preview: Cam {cam_id}")

                rpi.selector.enable_cam(cam_id)
                time.sleep(0.1)

                camera = CameraFactory.createCamera(Config.CAMERA_TYPE)
                cls.active_camera = camera 
                
                try:
                    timestamp_log = datetime.datetime.now().strftime(Config.PRETTY_FORMAT)
                    status_manager.update_hardware_status(cam_id=cam_id, cam_status={"health": "OK", "last_check": timestamp_log})

                    for frame in camera.stream_frames(cam_id=cam_id):
                        yield frame
                        
                finally:
                    camera.close()
                    cls.active_camera = None

        except Timeout:
            logger.error(f"[STREAM ERROR] Lock Timeout: Hardware in use. Cannot start stream for Cam {cam_id}")
            status_manager.update_lock_state(status="BUSY", owner="System (Experiment)", details="Access Denied")
            return

        except Exception as e:
            logger.error(f"[STREAM CRITICAL] Generator crashed on Cam {cam_id}: {e}")
            timestamp_log = datetime.datetime.now().strftime(Config.PRETTY_FORMAT)
            status_manager.update_hardware_status(cam_id=cam_id, cam_status={"health": "ERROR", "last_check": timestamp_log})
        
        finally:
            logger.info(f"[STREAM] Finished. Releasing lock.")
            try:
                status_manager.update_lock_state(status="FREE", owner=None, details=None)
            except Exception as e:
                logger.error(f"[STREAM ERROR] Failed to release lock: {e}")

    @classmethod
    def _thread(cls):
        try:
            logger.info(f"[STREAM THREAD] Starting loop...")
            frames_iterator = cls.frames(cls.cam_id)
            
            for frame in frames_iterator:
                CameraStream.frame = frame
                CameraStream.event.set() 
                time.sleep(0)

                if time.time() - CameraStream.last_access > 5:
                    logger.info(f"[STREAM THREAD] Stopping due to inactivity (Client disconnected).")
                    frames_iterator.close()
                    break
                elif CameraStream.reset:
                    logger.info(f"[STREAM THREAD] Stopping as demanded by reset.")
                    frames_iterator.close()
                    CameraStream.reset = False
                    break
                    
        except Exception as e:
            logger.error(f"[STREAM THREAD CRASH] Fatal exception: {e}")
            
        finally:
            logger.info(f"[STREAM THREAD] Exiting cleanly.")
            CameraStream.thread = None
            CameraStream.reset = False
            CameraStream.event.set()