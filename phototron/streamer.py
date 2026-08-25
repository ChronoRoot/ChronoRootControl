import datetime
import os
import resource
import subprocess
import sys
import time
import threading
import logging
from filelock import FileLock, Timeout
from config import Config
from app.options.schedulerstatus import SchedulerStatus
from phototron.rpimodule import RpiModule
from phototron.camera import CameraFactory

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
    try:
        file_handler = logging.FileHandler(Config.SHDL_LOG_FILE)
    except OSError:
        file_handler = logging.StreamHandler(sys.stderr)
    file_handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
    logger.addHandler(file_handler)
    logger.propagate = False # Prevent duplicate logs if root logger is active

def _read_text(path):
    try:
        with open(path, 'r') as handle:
            return handle.read().replace('\x00', '').strip()
    except OSError:
        return None


def get_resource_snapshot():
    """Return bounded diagnostics that work on Raspberry Pi and development hosts."""
    snapshot = {
        "board_model": _read_text("/proc/device-tree/model") or "unknown",
        "process_max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    meminfo = _read_text("/proc/meminfo")
    if meminfo:
        wanted = {"MemTotal", "MemAvailable", "SwapFree"}
        for line in meminfo.splitlines():
            key, _, value = line.partition(":")
            if key in wanted:
                snapshot[key.lower() + "_kb"] = value.strip().split()[0]
    temperature = _read_text("/sys/class/thermal/thermal_zone0/temp")
    if temperature:
        try:
            snapshot["temperature_c"] = round(float(temperature) / 1000.0, 1)
        except ValueError:
            pass
    try:
        throttled = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if throttled.returncode == 0:
            snapshot["throttled"] = throttled.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return snapshot


class CameraEvent(object):
    def __init__(self):
        self.events = {}
        self._lock = threading.Lock()

    def wait(self, timeout=None):
        ident = get_ident()
        with self._lock:
            event = self.events.setdefault(ident, [threading.Event(), time.time()])[0]
        return event.wait(timeout)

    def set(self):
        now = time.time()
        with self._lock:
            remove = []
            for ident, event in self.events.items():
                if not event[0].is_set():
                    event[0].set()
                    event[1] = now
                elif now - event[1] > 5:
                    remove.append(ident)
            for ident in remove:
                self.events.pop(ident, None)

    def clear(self):
        with self._lock:
            event = self.events.get(get_ident())
            if event is not None:
                event[0].clear()

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
    # Serializes multi-request camera hand-offs without blocking the stream
    # thread's own lifecycle-finally lock.
    _handoff_lock = threading.Lock()
    _generation = 0
    _preview_operation_lock = threading.Lock()
    _state_lock = threading.Lock()
    _state = {
        "status": "stopped",
        "camera_id": None,
        "last_error": None,
        "updated_at": None,
        "last_frame_at": None,
        "resources": {},
    }

    def __init__(self, cam_id):
        self.cam_id = cam_id
        self.generation = None

        with CameraStream._handoff_lock:
            with CameraStream._lifecycle_lock:
                if CameraStream.thread is not None and not CameraStream.thread.is_alive():
                    logger.debug("Cleaning up dead thread reference.")
                    CameraStream.thread = None
                    CameraStream.reset = False

                old_thread = CameraStream.thread

                if old_thread is None:
                    CameraStream.cam_id = cam_id
                    self.generation = self._start_stream()

            if old_thread is not None:
                # A stream thread is already running: ask it to stop, then wait for it
                # to actually die before spawning a replacement. We must NEVER abandon
                # a live thread, because it may still hold the hardware FileLock.
                CameraStream.last_access = time.time()
                CameraStream.reset = True
                logger.info(
                    "Thread %s already running. Requesting reset for Cam %s.",
                    old_thread.name,
                    cam_id,
                )

                if not self._wait_for_thread_death(old_thread, timeout=5.0):
                    # The old thread is likely blocked inside capture_array(). Force-close
                    # the camera to make the pending capture raise, which unwinds the
                    # generator's finally blocks and releases the FileLock.
                    logger.error(
                        "Timeout: Old stream thread appears deadlocked. "
                        "Force-closing camera to unblock it."
                    )
                    CameraStream._force_close_active_camera()

                    if not self._wait_for_thread_death(old_thread, timeout=10.0):
                        message = "Old stream thread refused to die; not starting camera %s." % cam_id
                        logger.error(message)
                        CameraStream._set_state("error", message, camera_id=cam_id, persist=True)
                        return

                with CameraStream._lifecycle_lock:
                    if CameraStream.thread is None or not CameraStream.thread.is_alive():
                        CameraStream.cam_id = cam_id
                        CameraStream.reset = False
                        self.generation = self._start_stream()

        if old_thread is None:
            start_wait = time.time()
            while self.get_frame() is None:
                if self.generation != CameraStream._generation:
                    logger.info("Camera %s preview request was superseded by a newer stream.", cam_id)
                    return
                if time.time() - start_wait > 15.0:
                    message = "Camera %s stream failed to produce a frame within 15 seconds." % cam_id
                    logger.error(message)
                    CameraStream._set_state("error", message, camera_id=cam_id, persist=True)
                    break
                time.sleep(0.1)
            return

    @classmethod
    def _start_stream(cls):
        """Spawn the stream thread and its stall watchdog. Caller must ensure no live stream thread exists."""
        stream_cam_id = cls.cam_id
        cls._generation += 1
        stream_generation = cls._generation
        cls.last_access = time.time()
        cls.last_frame_time = time.time()
        cls.frame = None
        cls.event = CameraEvent()
        with cls._state_lock:
            cls._state["last_frame_at"] = None
        resources = get_resource_snapshot()
        logger.info("Starting camera stream thread with cam %s. Resources: %s", stream_cam_id, resources)
        cls._set_state("starting", resources=resources, camera_id=stream_cam_id, persist=True)
        cls.thread = threading.Thread(
            target=cls._thread,
            args=(stream_cam_id,),
            name="CameraStream-%s" % stream_cam_id,
        )
        cls.thread.start()

        watchdog = threading.Thread(
            target=cls._watchdog,
            args=(cls.thread, stream_cam_id),
            daemon=True,
        )
        watchdog.start()
        return stream_generation

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
        with cls._preview_operation_lock:
            try:
                camera.close()
            except Exception:
                logger.exception("[WATCHDOG] Error force-closing camera")

    @classmethod
    def _set_state(cls, status, error=None, resources=None, camera_id=None, persist=False):
        error_text = str(error)[:500] if error else None
        now = datetime.datetime.now().strftime(Config.PRETTY_FORMAT)
        state_camera_id = cls.cam_id if camera_id is None else camera_id
        with cls._state_lock:
            cls._state["status"] = status
            cls._state["camera_id"] = state_camera_id
            cls._state["last_error"] = error_text
            cls._state["updated_at"] = now
            if resources is not None:
                cls._state["resources"] = resources
        if persist:
            try:
                SchedulerStatus().update_stream_status(status, state_camera_id, error_text)
            except Exception:
                logger.exception("Failed to persist stream status")

    @classmethod
    def get_status(cls):
        with cls._state_lock:
            status = dict(cls._state)
        status["thread_alive"] = bool(cls.thread and cls.thread.is_alive())
        status["last_frame_age_seconds"] = (
            round(time.time() - cls.last_frame_time, 1) if cls.last_frame_time else None
        )
        return status

    @classmethod
    def run_with_preview_hardware(cls, operation):
        """
        Run an operation only while this worker safely owns preview hardware.

        The release path holds this same guard from active_camera cleanup until
        after the real FileLock exits, closing the shared-status TOCTOU window.
        """
        with cls._preview_operation_lock:
            active = bool(cls.thread and cls.thread.is_alive() and cls.active_camera is not None)
            if not active:
                return False, None
            return True, operation()

    @classmethod
    def _watchdog(cls, stream_thread, stream_cam_id=None):
        """
        Monitors the stream thread. If it is alive but has not produced a frame
        for STREAM_STALL_TIMEOUT seconds (e.g. capture_array() hung after a light
        toggle), force-close the camera so the generator unwinds and the hardware
        FileLock is released instead of being held forever.
        """
        if stream_cam_id is None:
            stream_cam_id = cls.cam_id
        stall_timeout = getattr(Config, 'STREAM_STALL_TIMEOUT', 15)
        while stream_thread.is_alive():
            time.sleep(2)
            stalled_for = time.time() - cls.last_frame_time
            if stream_thread.is_alive() and stalled_for > stall_timeout:
                message = "No frame for %.1fs; force-closing camera to release the lock." % stalled_for
                logger.error("[WATCHDOG] %s", message)
                cls._set_state(
                    "stalled",
                    message,
                    resources=get_resource_snapshot(),
                    camera_id=stream_cam_id,
                    persist=True,
                )
                cls._force_close_active_camera()
                # Give the unwind time to complete before considering another close.
                cls.last_frame_time = time.time()
        logger.debug("[WATCHDOG] Stream thread exited; watchdog stopping.")

    def get_frame(self):
        if self.generation is None or self.generation != CameraStream._generation:
            return None
        CameraStream.last_access = time.time()
        got_signal = CameraStream.event.wait(timeout=10.0)
        
        if self.generation != CameraStream._generation:
            return None
        if not got_signal:
            logger.warning("get_frame timeout: Hardware stopped sending frames.")
            CameraStream._set_state(
                "stalled",
                "No frame was received within 10 seconds.",
                camera_id=self.cam_id,
                persist=True,
            )
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
        release_guard_acquired = False

        try:
            with lock.acquire(timeout=5):
                lock_acquired = True
                logger.info(f"[STREAM] Lock acquired. Starting hardware boot for Cam {cam_id}")
                status_manager.update_lock_state(status="LOCKED", owner="User (Web Interface)", details=f"Live Preview: Cam {cam_id}")
                cls._set_state("starting", camera_id=cam_id, persist=True)

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
                    # Block preview-side GPIO operations while the camera closes,
                    # and retain the guard until after the outer FileLock exits.
                    cls._preview_operation_lock.acquire()
                    release_guard_acquired = True
                    try:
                        camera.close()
                    finally:
                        cls.active_camera = None

        except Timeout:
            # We never owned the lock: leave the shared lock_info untouched so the
            # real holder's "LOCKED" entry survives. Only log who has it.
            try:
                status_manager.load()
                current_owner = status_manager.state.get("hardware", {}).get("lock_info", {}).get("owner") or "unknown process"
            except Exception:
                current_owner = "unknown process"
            message = "Hardware is in use by %s; cannot start camera %s." % (current_owner, cam_id)
            logger.error("[STREAM ERROR] Lock timeout: %s", message)
            cls._set_state("busy", message, camera_id=cam_id, persist=True)
            return

        except Exception as exc:
            logger.exception("[STREAM CRITICAL] Generator crashed on Cam %s", cam_id)
            cls._set_state(
                "error",
                exc,
                resources=get_resource_snapshot(),
                camera_id=cam_id,
                persist=True,
            )
            timestamp_log = datetime.datetime.now().strftime(Config.PRETTY_FORMAT)
            status_manager.update_hardware_status(cam_id=cam_id, cam_status={"health": "ERROR", "last_check": timestamp_log})
            raise
        
        finally:
            try:
                # Only the invocation that actually held the FileLock may declare it FREE.
                if lock_acquired:
                    logger.info(f"[STREAM] Finished. Releasing lock.")
                    try:
                        status_manager.update_lock_state(status="FREE", owner=None, details=None)
                    except Exception:
                        logger.exception("[STREAM ERROR] Failed to release lock status")
            finally:
                if release_guard_acquired:
                    cls._preview_operation_lock.release()

    @classmethod
    def _thread(cls, stream_cam_id=None):
        if stream_cam_id is None:
            stream_cam_id = cls.cam_id
        terminal_error = False
        try:
            logger.info("[STREAM THREAD] Starting loop...")
            frames_iterator = cls.frames(stream_cam_id)
            
            for frame in frames_iterator:
                CameraStream.frame = frame
                CameraStream.last_frame_time = time.time()
                with CameraStream._state_lock:
                    CameraStream._state["last_frame_at"] = datetime.datetime.now().strftime(Config.PRETTY_FORMAT)
                    was_running = CameraStream._state["status"] == "running"
                if not was_running:
                    CameraStream._set_state("running", camera_id=stream_cam_id, persist=True)
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
                    
        except Exception as exc:
            terminal_error = True
            logger.exception("[STREAM THREAD CRASH] Fatal exception")
            CameraStream._set_state(
                "error",
                exc,
                resources=get_resource_snapshot(),
                camera_id=stream_cam_id,
                persist=True,
            )
            
        finally:
            logger.info("[STREAM THREAD] Exiting%s.", " after error" if terminal_error else " cleanly")
            with CameraStream._lifecycle_lock:
                # Only clear the reference if it still points to us; a successor
                # thread may already have been registered by __init__.
                if CameraStream.thread is threading.current_thread():
                    CameraStream.thread = None
                    CameraStream.reset = False
            with CameraStream._state_lock:
                current_status = CameraStream._state["status"]
            if not terminal_error and current_status in ("starting", "running"):
                CameraStream._set_state("stopped", camera_id=stream_cam_id, persist=True)
            CameraStream.event.set()