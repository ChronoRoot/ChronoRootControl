import time
import threading
try:
    from greenlet import getcurrent as get_ident
except ImportError:
    try:
        from thread import get_ident
        #thread don't exist
    except ImportError:
        from _thread import get_ident
        #this import works

import logging
logger = logging.getLogger(__name__)

class CameraEvent(object):
    """An Event-like class that signals all active clients when a new frame is
    available.
    """
    def __init__(self):
        self.events = {}

    def wait(self, timeout=None):
        """Invoked from each client's thread to wait for the next frame."""
        ident = get_ident()
        if ident not in self.events:
            self.events[ident] = [threading.Event(), time.time()]
        
        # Pass the timeout down to the threading.Event
        return self.events[ident][0].wait(timeout)

    def set(self):
        """Invoked by the camera thread when a new frame is available."""
        now = time.time()
        remove = None
        for ident, event in self.events.items():
            if not event[0].isSet():
                # if this client's event is not set, then set it
                # also update the last set timestamp to now
                event[0].set()
                event[1] = now
            else:
                # if the client's event is already set, it means the client
                # did not finished processing a previous frame
                # if the event is still present after 5 seconds, assume
                # the client is gone and remove it
                if now - event[1] > 5:
                    remove = ident
        if remove:
            del self.events[remove]

    def clear(self):
        """Invoked from each client's thread after a frame was processed."""
        self.events[get_ident()][0].clear()


class BaseCamera(object):
    thread = None  
    frame = None  
    reset = False
    last_access = 0  
    cam_id = 1 
    event = CameraEvent()

    def __init__(self, cam_id):
        self.cam_id = cam_id
        BaseCamera.cam_id = cam_id

        # 1. Clear out "ghost" threads immediately
        if BaseCamera.thread is not None and not BaseCamera.thread.is_alive():
            logger.debug("Cleaning up dead thread reference.")
            BaseCamera.thread = None
            BaseCamera.reset = False

        if BaseCamera.thread is None:
            BaseCamera.last_access = time.time()
            logger.info('Starting camera thread with cam %s.' % self.cam_id)
            BaseCamera.thread = threading.Thread(target=self._thread)
            BaseCamera.thread.start()

            start_wait = time.time()
            while self.get_frame() is None:
                if time.time() - start_wait > 5.0:
                    logger.error(f"Timeout: Camera {cam_id} thread failed to produce a frame on startup.")
                    break
                time.sleep(0.1)
                
        else:
            BaseCamera.last_access = time.time()
            BaseCamera.reset = True
            logger.info("Thread %s already running. Requesting reset." % BaseCamera.thread.name)
            
            start_wait = time.time()
            # 2. Check is_alive() so we don't wait on a dead thread
            while BaseCamera.thread is not None and BaseCamera.thread.is_alive():
                if time.time() - start_wait > 5.0:
                    logger.error("Timeout: Old camera thread is deadlocked. Forcing overwrite.")
                    BaseCamera.thread = None 
                    break
                time.sleep(0.1)
            
            # 3. CRITICAL: Clear the reset flag before the new thread spawns!
            BaseCamera.reset = False 
            BaseCamera.thread = threading.Thread(target=self._thread)
            BaseCamera.thread.start()

    def get_frame(self):
        """Return the current camera frame."""
        BaseCamera.last_access = time.time()

        # FIX: Wait for a signal, but give up after 2 seconds
        got_signal = BaseCamera.event.wait(timeout=2.0)
        
        if not got_signal:
            logger.warning("get_frame timeout: Hardware stopped sending frames.")
            return None # This triggers the None check in your Flask gen() function!

        BaseCamera.event.clear()
        return BaseCamera.frame

    @staticmethod
    def frames(cam_id):
        """"Generator that returns frames from the camera."""
        raise RuntimeError('Must be implemented by subclasses.')

    @classmethod
    def _thread(cls):
        """Camera background thread."""
        try:
            frames_iterator = cls.frames(cls.cam_id)
            for frame in frames_iterator:
                BaseCamera.frame = frame
                BaseCamera.event.set()  # send signal to clients
                time.sleep(0)

                # if there hasn't been any clients asking for frames in
                # the last 10 seconds then stop the thread
                if time.time() - BaseCamera.last_access > 5:
                    frames_iterator.close()
                    logger.info('Stopping camera thread due to inactivity.')
                    break
                elif BaseCamera.reset:
                    frames_iterator.close()
                    logger.info('Stopping camera thread as demanded.')
                    BaseCamera.reset = False
                    break
                    
        except Exception as e:
            logger.error(f"Camera background thread crashed: {e}")
            
        finally:
            # This guarantees the thread variable is cleared even on a crash
            logger.info("Background thread exiting. Cleaning up state.")
            BaseCamera.thread = None
            BaseCamera.reset = False
            # Clear the event so any waiting get_frame() calls unblock immediately
            BaseCamera.event.set()
