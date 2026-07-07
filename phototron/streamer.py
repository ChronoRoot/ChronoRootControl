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
    last_frame_time = 0
    cam_id = 1 
    event = CameraEvent()
    active_camera = None
    # Serializes all reads/writes of the class-level thread lifecycle state
    # (thread / reset) across web worker threads and the stream thread itself.
    _lifecycle_lock = threading.Lock()

    def __init__(self, cam_id):
        self.cam_id = cam_id
        CameraStream.cam_id = cam_id

        with CameraStream._lifecycle_lock:
            if CameraStream.thread is not None and not CameraStream.thread.is_alive():
                logger.debug("Cleaning up dead thread reference.")
                CameraStream.thread = None
                CameraStream.reset = False

            old_thread = CameraStream.thread

            if old_thread is None:
                self._start_stream()

        if old_thread is None:
            start_wait = time.time()
            while self.get_frame() is None:
                if time.time() - start_wait > 15.0:
                    logger.error(f"Timeout: Camera {cam_id} stream failed to produce a frame.")
                    break
                time.sleep(0.1)
            return

        # A stream thread is already running: ask it to stop, then wait for it
        # to actually die before spawning a replacement. We must NEVER abandon a
        # live thread, because it may still hold the hardware FileLock.
        CameraStream.last_access = time.time()
        CameraStream.reset = True
        logger.info("Thread %s already running. Requesting reset for Cam %s." % (old_thread.name, cam_id))

        if not self._wait_for_thread_death(old_thread, timeout=5.0):
            # The old thread is likely blocked inside capture_array(). Force-close
            # the camera to make the pending capture raise, which unwinds the
            # generator's finally blocks and releases the FileLock.
            logger.error("Timeout: Old stream thread appears deadlocked. Force-closing camera to unblock it.")
            CameraStream._force_close_active_camera()

            if not self._wait_for_thread_death(old_thread, timeout=10.0):
                # Spawning a second thread now would just create a second orphaned
                # lock holder. Bail out; video_feed will serve the fallback frame.
                logger.error(f"Old stream thread refused to die. NOT starting a new stream for Cam {cam_id}.")
                return

        with CameraStream._lifecycle_lock:
            if CameraStream.thread is None or not CameraStream.thread.is_alive():
                CameraStream.reset = False
                self._start_stream()

    @classmethod
    def _start_stream(cls):
        """Spawn the stream thread and its stall watchdog. Caller must ensure no live stream thread exists."""
        cls.last_access = time.time()
        cls.last_frame_time = time.time()
        logger.info('Starting camera stream thread with cam %s.' % cls.cam_id)
        cls.thread = threading.Thread(target=cls._thread)
        cls.thread.start()

        watchdog = threading.Thread(target=cls._watchdog, args=(cls.thread,), daemon=True)
        watchdog.start()

    @staticmethod
    def _wait_for_thread_death(thread, timeout):
        start_wait = time.time()
        while thread.is_alive():
            if time.time() - start_wait > timeout:
                return False
            time.sleep(0.1)
        return True

    @classmethod
    def _force_close_active_camera(cls):
        """Close the camera from outside the stream thread to unblock a hung capture_array()."""
        camera = cls.active_camera
        if camera is None:
            return
        try:
            camera.close()
        except Exception as e:
            logger.error(f"[WATCHDOG] Error force-closing camera: {e}")

    @classmethod
    def _watchdog(cls, stream_thread):
        """
        Monitors the stream thread. If it is alive but has not produced a frame
        for STREAM_STALL_TIMEOUT seconds (e.g. capture_array() hung after a light
        toggle), force-close the camera so the generator unwinds and the hardware
        FileLock is released instead of being held forever.
        """
        stall_timeout = getattr(Config, 'STREAM_STALL_TIMEOUT', 15)
        while stream_thread.is_alive():
            time.sleep(2)
            stalled_for = time.time() - cls.last_frame_time
            if stream_thread.is_alive() and stalled_for > stall_timeout:
                logger.error(f"[WATCHDOG] Stream stalled: no frame for {stalled_for:.1f}s. Force-closing camera to release the lock.")
                cls._force_close_active_camera()
                # Give the unwind time to complete before considering another close.
                cls.last_frame_time = time.time()
        logger.debug("[WATCHDOG] Stream thread exited; watchdog stopping.")

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
        
        # NOTE: we deliberately do NOT write any lock status before acquisition.
        # Writing "REQUESTING" here would clobber the real owner's "LOCKED" entry
        # in the shared status file if acquisition then fails.
        lock = FileLock(Config.LOCK_FILE, timeout=1)
        lock_acquired = False

        try:
            with lock.acquire(timeout=5):
                lock_acquired = True
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
            # We never owned the lock: leave the shared lock_info untouched so the
            # real holder's "LOCKED" entry survives. Only log who has it.
            try:
                status_manager.load()
                current_owner = status_manager.state.get("hardware", {}).get("lock_info", {}).get("owner") or "unknown process"
            except Exception:
                current_owner = "unknown process"
            logger.error(f"[STREAM ERROR] Lock Timeout: Hardware in use by {current_owner}. Cannot start stream for Cam {cam_id}")
            return

        except Exception as e:
            logger.error(f"[STREAM CRITICAL] Generator crashed on Cam {cam_id}: {e}")
            timestamp_log = datetime.datetime.now().strftime(Config.PRETTY_FORMAT)
            status_manager.update_hardware_status(cam_id=cam_id, cam_status={"health": "ERROR", "last_check": timestamp_log})
        
        finally:
            # Only the invocation that actually held the FileLock may declare it FREE.
            if lock_acquired:
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
                CameraStream.last_frame_time = time.time()
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
            with CameraStream._lifecycle_lock:
                # Only clear the reference if it still points to us; a successor
                # thread may already have been registered by __init__.
                if CameraStream.thread is threading.current_thread():
                    CameraStream.thread = None
                    CameraStream.reset = False
            CameraStream.event.set()