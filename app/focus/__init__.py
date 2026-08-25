"""
Live preview from camera
"""
import io
import logging
import os
import sys
import time
from flask import Blueprint, render_template, Response, jsonify, request
from filelock import FileLock, Timeout
from PIL import Image, ImageDraw

from phototron.streamer import CameraStream
from phototron.rpimodule import RpiModule
from app.options.schedulerstatus import SchedulerStatus  
from config import Config

logger = logging.getLogger(__name__)
logger.setLevel(Config.LOG_LEVEL)
if not logger.handlers:
    try:
        handler = logging.FileHandler(Config.SHDL_LOG_FILE)
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(Config.LOG_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False

focus_page = Blueprint('focus_page', __name__,
                       template_folder='templates',
                       static_folder='static')

@focus_page.route('/<int:cam_id>', methods=['GET'])
def index(cam_id):
    """Video streaming home page."""
    rpi = RpiModule()
    light = rpi.light
    
    status_mgr = SchedulerStatus(log=logger)
    status_mgr.reconcile_hardware_lock()
    info = status_mgr.get_info()
    
    lock_info = info['lock_info']
    lock_owner = lock_info.get('owner') or 'Unknown Process'
    # A refresh/new tab may reconnect to the web preview. CameraStream safely
    # serializes the hand-off and releases the previous stream.
    is_locked = (
        lock_info['status'] in ['LOCKED', 'REQUESTING']
        and lock_owner != "User (Web Interface)"
    )
    
    cam_reports = info.get('cam_reports', {})
    this_cam_data = cam_reports.get(str(cam_id), {})
    cam_health = this_cam_data.get('health', 'UNTESTED')
    
    has_hw_error = cam_health in ['ERROR', 'NOT DETECTED']

    cam_profile = Config.CAMERA_PROFILES.get(Config.CAMERA_TYPE, {})
    has_autofocus = cam_profile.get("autofocus", False)
    saved_distances = getattr(Config, 'FOCUS_DISTANCES', {})
    saved_focus = saved_distances.get(str(cam_id), 7.5)

    # NOTE: the light is toggled exclusively via the AJAX endpoint
    # /api/toggle_light. The old POST-and-redirect form path was removed because
    # the redirect re-created the CameraStream mid-preview and raced the lock.

    return render_template('focus.html', 
            cam_id=cam_id,
            light_state=(light.state == light.ON),
            is_locked=is_locked,
            lock_owner=lock_owner,
            has_hw_error=has_hw_error,
            cam_health=cam_health,
            has_autofocus=has_autofocus,
            saved_focus=saved_focus)
    
def get_fallback_frame(message="Camera preview unavailable"):
    """Create a visible MJPEG-compatible error frame without a binary asset."""
    try:
        image = Image.new("RGB", (800, 450), color=(45, 45, 45))
        draw = ImageDraw.Draw(image)
        draw.text((40, 190), "CAMERA PREVIEW UNAVAILABLE", fill=(255, 110, 110))
        draw.text((40, 225), (message or "Camera preview unavailable")[:100], fill=(235, 235, 235))
        stream = io.BytesIO()
        image.save(stream, format="JPEG", quality=80)
        return stream.getvalue()
    except Exception:
        logger.exception("Could not generate camera error frame")

    error_image_path = os.path.join(os.path.dirname(__file__), 'error_frame.jpeg')
    try:
        with open(error_image_path, 'rb') as f:
            return f.read()
    except Exception:
        logger.exception("Could not load fallback camera error frame")
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
            
    except Exception:
        logger.exception("Streaming response failed on camera %s", camera.cam_id)
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + error_frame + b'\r\n')
    finally:
        logger.info(f"Stream closed for camera {camera.cam_id}")

@focus_page.route('/video_feed/<int:cam_id>')
def video_feed(cam_id):
    logger.info(f"User requested live preview for camera {cam_id}")
    cam_obj = CameraStream(cam_id=cam_id)
    response = Response(
        gen(cam_obj),
        mimetype='multipart/x-mixed-replace; boundary=frame',
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response

@focus_page.route('/api/stream_status/<int:cam_id>', methods=['GET'])
def stream_status(cam_id):
    """Return bounded live-preview health for the focus page."""
    local_status = CameraStream.get_status()
    shared_status = SchedulerStatus.for_read().get_info(reload=False).get("stream_status", {})

    # After a worker restart, local state is empty but shared state can explain
    # why the previous stream disappeared.
    if (
        local_status.get("status") == "stopped"
        and shared_status.get("status") in ("stalled", "error", "worker_terminating")
    ):
        local_status["last_error"] = shared_status.get("last_error")
        local_status["updated_at"] = shared_status.get("updated_at")

    local_status["requested_camera_id"] = cam_id
    return jsonify(local_status)

@focus_page.route('/api/toggle_light', methods=['POST'])
def toggle_light():
    """Background endpoint to toggle the IR light without reloading the page."""
    rpi = RpiModule()
    light = rpi.light
    status_mgr = SchedulerStatus()

    # Lock awareness: refuse to drive the GPIO while the scheduler or a system
    # task (diagnostics, capture) holds the hardware. Toggling during the user's
    # own live preview is allowed -- that is the intended focusing workflow.
    lock_info = status_mgr.state.get("hardware", {}).get("lock_info", {})
    lock_status = lock_info.get("status")
    lock_owner = lock_info.get("owner") or "Unknown Process"
    if lock_status in ("LOCKED", "REQUESTING") and lock_owner != "User (Web Interface)":
        logger.warning(f"Light toggle refused: hardware locked by {lock_owner}.")
        return jsonify({
            "success": False,
            "error": f"Hardware busy: {lock_owner} is using the cameras. Try again shortly."
        }), 409

    # Get the requested state from the JSON payload
    data = request.get_json(silent=True) or {}
    turn_on = data.get('ir_state', False)

    def apply_light_state():
        if turn_on:
            light.state = light.ON
            status_mgr.update_lights_state("ON")
            return True
        light.state = light.OFF
        status_mgr.update_lights_state("OFF")
        return False
    
    try:
        preview_applied = False
        if (
            lock_status == "LOCKED"
            and lock_owner == "User (Web Interface)"
        ):
            preview_applied, new_state = CameraStream.run_with_preview_hardware(
                apply_light_state
            )
        if not preview_applied:
            with FileLock(Config.LOCK_FILE).acquire(timeout=0):
                new_state = apply_light_state()
            
        return jsonify({"success": True, "light_state": new_state})
    except Timeout:
        return jsonify({
            "success": False,
            "error": "Hardware became busy before the light could be changed.",
        }), 409
    except Exception as e:
        logger.exception("Error toggling light")
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
        logger.exception("Failed to write live focus to RAM disk")
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