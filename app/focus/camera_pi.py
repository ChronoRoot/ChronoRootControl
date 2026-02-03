import io
import time
import picamera
from .base_camera import BaseCamera
#from phototron.rpimodule import RpiModule
from ivport_v2 import ivport

class Camera(BaseCamera):
    def __init__(self, cam_id):
        BaseCamera.__init__(self, cam_id)
    
    @staticmethod
    def frames(cam_id):
        print("frames called from %s on cam %s" % (__class__, cam_id))
        
        # 1. Initialize Multiplexer & Switch

        iv = ivport.IVPort(ivport.TYPE_QUAD2)
        print("Switching IVPort to cam %s" % cam_id)
        iv.camera_change(cam_id)
        
        time.sleep(1.0)  # Allow time for the camera switch to take effect

        # 2. Initialize Camera with Focus/Grayscale settings
        with picamera.PiCamera() as camera:
            camera.resolution = (800, 600) 
            camera.framerate = 24

            # Set to Grayscale
            camera.color_effects = (128, 128)
            camera.exposure_mode = 'backlight'
            time.sleep(1.5)

            stream = io.BytesIO()
            
            for foo in camera.capture_continuous(stream, 'jpeg', use_video_port=True):
                stream.seek(0)
                yield stream.read()

                stream.seek(0)
                stream.truncate()