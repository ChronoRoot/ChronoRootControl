"""
Live preview from camera
"""
from ..options.form import BackLightForm
import os
from config import Config
from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for, Response)
from .camera_pi import Camera

from phototron.rpimodule import RpiModule
from app.options.schedulerstatus import SchedulerStatus  
import time

focus_page = Blueprint('focus_page', __name__,
                       template_folder='templates',
                       static_folder='static')

@focus_page.route('/<int:cam_id>', methods=['GET', 'POST'])
def index(cam_id):
    """Video streaming home page."""
    rpi = RpiModule()
    light = rpi.light
    
    # --- Check Lock Status ---
    status_mgr = SchedulerStatus()
    lock_info = status_mgr.state['hardware']['lock_info']
    
    # If locked by anyone (even "REQUESTING"), we consider it busy
    is_locked = lock_info['status'] in ['LOCKED', 'REQUESTING']
    lock_owner = lock_info.get('owner', 'Unknown Process')
    
    # --- Handle Backlight Form ---
    backlight_form = BackLightForm(ir=light.state, prefix="backlight")
    if backlight_form.validate_on_submit() and backlight_form.data:
        if backlight_form.data['ir']:
            light.state = light.ON
        else:
            light.state = light.OFF

    return render_template('focus.html', 
            cam_id=cam_id,
            light_state=light.state,
            backlight_form=backlight_form,
            is_locked=is_locked,   # Pass to template
            lock_owner=lock_owner) # Pass to template

def gen(camera):
    """Video streaming generator function."""
    print("gen : camera %s (%s)" % (camera.cam_id, camera))
    while True:
        frame = camera.get_frame()
        # If the camera generator returns None (due to lock fail), we stop yielding
        if frame is None:
            break
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.05) # Small sleep to prevent CPU hogging


@focus_page.route('/video_feed/<int:cam_id>')
def video_feed(cam_id):
    """Video streaming route. Put this in the src attribute of an img tag."""
    print("video_feed feed called for camera %s"%cam_id)
    return Response(gen(Camera(cam_id=cam_id)),
                    mimetype='multipart/x-mixed-replace; boundary=frame')