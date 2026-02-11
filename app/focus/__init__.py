"""
Live preview from camera
"""
from ..options.form import BackLightForm
import os
from config import Config
from flask import (Blueprint, abort, flash, render_template, request,
                   url_for, Response)
from .camera_pi import Camera

from phototron.rpimodule import RpiModule
from app.options.schedulerstatus import SchedulerStatus  
import time
import logging
logger = logging.getLogger(__name__)

focus_page = Blueprint('focus_page', __name__,
                       template_folder='templates',
                       static_folder='static')

@focus_page.route('/<int:cam_id>', methods=['GET', 'POST'])
def index(cam_id):
    """Video streaming home page."""
    rpi = RpiModule()
    light = rpi.light
    
    status_mgr = SchedulerStatus()
    info = status_mgr.get_info()
    
    # 1. Extract Lock Info
    lock_info = info['lock_info']
    is_locked = lock_info['status'] in ['LOCKED', 'REQUESTING']
    lock_owner = lock_info.get('owner', 'Unknown Process')
    
    # 2. Extract Specific Camera Health
    # SchedulerStatus.get_info() returns cams under 'cam_reports'
    cam_reports = info.get('cam_reports', {})
    this_cam_data = cam_reports.get(str(cam_id), {})
    cam_health = this_cam_data.get('health', 'UNKNOWN')
    
    # 3. Determine if we should block for Hardware Error
    # If the camera is physically failing, we show the error instead of the stream
    has_hw_error = cam_health in ['CAMERA_ERROR', 'HW_FAILURE', 'FAILED', 'NOT DETECTED']

    # --- Handle Backlight Form ---
    backlight_form = BackLightForm(ir=(light.state == light.ON), prefix="backlight")
    
    if backlight_form.validate_on_submit():
        if backlight_form.ir.data:
            light.state = light.ON
        else:
            light.state = light.OFF
        
        target_url = url_for('focus_page.index', cam_id=cam_id)
        return f"<script>window.location.href = '{target_url}';</script>"

    return render_template('focus.html', 
            cam_id=cam_id,
            light_state=(light.state == light.ON),
            backlight_form=backlight_form,
            is_locked=is_locked,
            lock_owner=lock_owner,
            has_hw_error=has_hw_error,
            cam_health=cam_health)
    
import os

def get_fallback_frame():
    """Helper to load the static 'error_frame.png' if the stream fails."""
    # Adjust this path if your folder structure is slightly different
    # This assumes error_frame.png is in the same folder as focus.html's static folder
    error_image_path = os.path.join(os.path.dirname(__file__), 'error_frame.jpeg')
    
    try:
        with open(error_image_path, 'rb') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Could not load error frame: {e}")
        # Ultimate fallback: a tiny 1x1 black JPEG byte string so the browser doesn't break
        return b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x00\xff\xd9'

def gen(camera):
    """Video streaming generator function."""
    error_frame = get_fallback_frame()
    
    try:
        while True:
            frame = camera.get_frame()
            
            # This is where our new BaseCamera timeout saves the day!
            if frame is None:
                logger.warning(f"gen: Stream timed out for cam {camera.cam_id}. Yielding error frame.")
                # 1. Send the error frame to the browser
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + error_frame + b'\r\n')
                # 2. Break the loop so the dead thread can be cleaned up
                break
                
            # Normal streaming
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            
    except Exception as e:
        logger.error(f"gen: streaming exception on cam {camera.cam_id}: {e}")
        # Catch unexpected crashes and still show the error frame
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + error_frame + b'\r\n')
               
    finally:
        logger.info(f"gen: cleaning up stream for camera {camera.cam_id}")

@focus_page.route('/video_feed/<int:cam_id>')
def video_feed(cam_id):
    logger.info("STEP 1: video_feed route hit")
    cam_obj = Camera(cam_id=cam_id)
    logger.info("STEP 2: Camera object created")
    return Response(gen(cam_obj),
                    mimetype='multipart/x-mixed-replace; boundary=frame')