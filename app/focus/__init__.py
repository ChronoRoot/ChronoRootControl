"""
Live preview from camera
"""
from ..options.form import BackLightForm
import os
from flask import Blueprint, render_template, url_for, Response, jsonify, request, redirect

from phototron.streamer import CameraStream
from phototron.rpimodule import RpiModule
from app.options.schedulerstatus import SchedulerStatus  
import logging
from config import Config
import time 
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
    
    lock_info = info['lock_info']
    is_locked = lock_info['status'] in ['LOCKED', 'REQUESTING']
    lock_owner = lock_info.get('owner', 'Unknown Process')
    
    cam_reports = info.get('cam_reports', {})
    this_cam_data = cam_reports.get(str(cam_id), {})
    cam_health = this_cam_data.get('health', 'UNTESTED')
    
    has_hw_error = cam_health in ['ERROR', 'NOT DETECTED']

    backlight_form = BackLightForm(ir=(light.state == light.ON), prefix="backlight")
    
    cam_profile = Config.CAMERA_PROFILES.get(Config.CAMERA_TYPE, {})
    has_autofocus = cam_profile.get("autofocus", False)
    saved_distances = getattr(Config, 'FOCUS_DISTANCES', {})
    saved_focus = saved_distances.get(str(cam_id), 7.5)
    
    if backlight_form.validate_on_submit():
        if backlight_form.ir.data:
            light.state = light.ON
            status_mgr.update_lights_state("ON")
        else:
            light.state = light.OFF
            status_mgr.update_lights_state("OFF")

        return redirect(url_for('focus_page.index', cam_id=cam_id))

    return render_template('focus.html', 
            cam_id=cam_id,
            light_state=(light.state == light.ON),
            backlight_form=backlight_form,
            is_locked=is_locked,
            lock_owner=lock_owner,
            has_hw_error=has_hw_error,
            cam_health=cam_health,
            has_autofocus=has_autofocus,
            saved_focus=saved_focus)
    
def get_fallback_frame():
    error_image_path = os.path.join(os.path.dirname(__file__), 'error_frame.jpeg')
    try:
        with open(error_image_path, 'rb') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Could not load error frame: {e}")
        return b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x00\xff\xd9'

def gen(camera):
    """Video streaming generator function."""
    error_frame = get_fallback_frame()
    
    try:
        while True:
            frame = camera.get_frame()
            if frame is None:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + error_frame + b'\r\n')
                break
                
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            
    except Exception as e:
        logger.error(f"Streaming error on camera {camera.cam_id}: {e}")
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + error_frame + b'\r\n')
    finally:
        logger.info(f"Stream closed for camera {camera.cam_id}")

@focus_page.route('/video_feed/<int:cam_id>')
def video_feed(cam_id):
    logger.info(f"User requested live preview for camera {cam_id}")
    cam_obj = CameraStream(cam_id=cam_id)
    return Response(gen(cam_obj),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@focus_page.route('/api/toggle_light', methods=['POST'])
def toggle_light():
    """Background endpoint to toggle the IR light without reloading the page."""
    rpi = RpiModule()
    light = rpi.light
    status_mgr = SchedulerStatus()
    
    # Get the requested state from the JSON payload
    data = request.get_json()
    turn_on = data.get('ir_state', False)
    
    try:
        if turn_on:
            light.state = light.ON
            status_mgr.update_lights_state("ON")
            new_state = True
        else:
            light.state = light.OFF
            status_mgr.update_lights_state("OFF")
            new_state = False
            
        return jsonify({"success": True, "light_state": new_state})
    except Exception as e:
        logger.error(f"Error toggling light: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    
@focus_page.route('/api/set_live_focus/<int:cam_id>', methods=['POST'])
def set_live_focus(cam_id):
    """Writes the live lens position to shared memory for the stream thread to pick up."""
    data = request.get_json()
    lens_position = float(data.get('focus_value', 0.0))
    
    target_file = f"/dev/shm/focus_cam_{cam_id}.txt"
    
    try:
        with open(target_file, 'w') as f:
            f.write(str(lens_position))
        return jsonify({"success": True, "lens_position": lens_position})
    except Exception as e:
        import logging
        logging.error(f"Failed to write live focus to RAM disk: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    

@focus_page.route('/api/run_autofocus/<int:cam_id>', methods=['POST'])
def run_autofocus(cam_id):
    """Signals the camera thread to run an AF sweep and waits for the result."""
    af_trigger_file = f"/dev/shm/do_af_cam_{cam_id}.txt"
    af_result_file = f"/dev/shm/af_result_{cam_id}.txt"
    
    # Clean up any stale result files
    if os.path.exists(af_result_file):
        os.remove(af_result_file)
        
    try:
        with open(af_trigger_file, 'w') as f:
            f.write("trigger")
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to signal hardware: {e}"}), 500
        
    # Poll for the result. The sweep takes ~10 seconds, so we timeout at 15 seconds.
    start_wait = time.time()
    while time.time() - start_wait < 15.0:
        if os.path.exists(af_result_file):
            try:
                with open(af_result_file, 'r') as f:
                    val = f.read().strip()
                os.remove(af_result_file)
                return jsonify({"success": True, "lens_position": float(val)})
            except Exception as e:
                return jsonify({"success": False, "error": f"Failed to read AF result: {e}"}), 500
        time.sleep(0.2) 
        
    return jsonify({"success": False, "error": "Hardware timed out during Autofocus sweep."}), 504