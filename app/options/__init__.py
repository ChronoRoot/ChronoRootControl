##
# Configuration management
#

from datetime import datetime
from flask import Blueprint, flash, jsonify, render_template, request, url_for, redirect

from .form import AppSettingsForm, HardwareSettingsForm, CameraProfileForm, HostnameForm
from config import Config

from phototron.rpimodule import RpiModule
from app.options.schedulerstatus import SchedulerStatus

# Import our new system and config managers
from app.options.config_manager import (apply_system_time_config, save_user_config,
                                        apply_hostname_config, run_git_update)

config_page = Blueprint('config_page', __name__,
                        template_folder='templates',
                        static_folder='static')

@config_page.route('/', methods=['GET', 'POST'])
def conf():
    """
    Device settings page
    """
    now = datetime.now().strftime(Config.PRETTY_FORMAT)
    rpi = RpiModule()
    light = rpi.light
    status_mgr = SchedulerStatus()
    scheduler_info = status_mgr.get_info()

    # 1. Initialize forms
    app_setting_form = AppSettingsForm(prefix="app_setting")
    hw_form = HardwareSettingsForm(prefix="hw")
    camera_form = CameraProfileForm(prefix="cam")
    hostname_form = HostnameForm(prefix="host")

    # 2. Pre-fill forms on GET
    if request.method == 'GET':
        app_setting_form.sync_mode.data = getattr(Config, 'USE_NTP', True)
        app_setting_form.time_zone.data = getattr(Config, 'TIME_ZONE', 'UTC')
        app_setting_form.ntp_server.data = getattr(Config, 'NTP_SERVER', 'pool.ntp.org')
        
        # Pre-fill Hardware
        hw_form.selector_type.data = getattr(Config, 'SELECTOR_TYPE', 'SINGLE')
        hw_form.camera_type.data = getattr(Config, 'CAMERA_TYPE', 'RPICAM_V2')
        
        # Use process_data() to force WTForms to actually tick the boxes
        active_cams = [int(c) for c in getattr(Config, 'CAMS', (1,))]
        hw_form.cams.process_data(active_cams)
        
        # Map the boolean config back to the dropdown selector
        keep_af = getattr(Config, 'KEEP_AUTOFOCUS', False)
        hw_form.focus_mode.data = 'auto' if keep_af else 'manual'

        hw_form.crop_square.data = getattr(Config, 'CROP_TO_SQUARE', False)

        # Pre-fill Camera Capture Profile (tunes backlight_manual)
        camera_form.default_profile.data = getattr(Config, 'DEFAULT_CAPTURE_PROFILE', 'backlight_manual')
        manual_controls = getattr(Config, 'CAM_CAPTURE_PROFILES', {}).get('backlight_manual', {}).get('controls', {})
        camera_form.exposure_time.data = manual_controls.get('ExposureTime', 30000)
        camera_form.analogue_gain.data = manual_controls.get('AnalogueGain', 1.0)
        camera_form.denoise.data = manual_controls.get('NoiseReductionMode', 0) != 0

        # Pre-fill the current hostname so the user edits in place
        hostname_form.hostname.data = scheduler_info.get('identity', {}).get('hostname', '')
        
    # --- 1. System Date & Time Logic ---
    if request.form.get('action') == 'set_time' and app_setting_form.validate_on_submit():
        # Extract data from the updated WTForm
        sync_mode = app_setting_form.sync_mode.data
        timezone = app_setting_form.time_zone.data
        ntp_server = app_setting_form.ntp_server.data
        
        # Format the manual date if provided
        date_str = None
        if app_setting_form.systemDate.data:
            date_str = app_setting_form.systemDate.data.strftime('%Y-%m-%d %H:%M:%S')

        mode_str = 'network' if sync_mode else 'manual'

        # 1. Ask the OS to apply the changes
        success, msg = apply_system_time_config(
            mode=mode_str, 
            date_str=date_str, 
            timezone=timezone, 
            ntp_server=ntp_server
        )

        if success:
            # 2. If OS applied it, save it to user_config.py persistently
            config_saved, save_msg = save_user_config({
                'USE_NTP': sync_mode,
                'TIME_ZONE': timezone,
                'NTP_SERVER': ntp_server
            })
            
            if config_saved:
                setattr(Config, 'USE_NTP', sync_mode)
                setattr(Config, 'TIME_ZONE', timezone)
                setattr(Config, 'NTP_SERVER', ntp_server)
                
                flash(f'System time and preferences updated successfully.', 'success')
            else:
                flash(f'Time applied, but failed to save config to disk: {save_msg}', 'warning')
        else:
            flash(f'Error updating time: {msg}', 'danger')
            
        return redirect(url_for('config_page.conf'))

    # --- 2. Hardware Settings Logic ---
    if request.form.get('action') == 'set_hw':
        # Bypass WTForms strict validation and read the raw HTML POST data directly
        sel_type = request.form.get('hw-selector_type', 'SINGLE')
        cam_type = request.form.get('hw-camera_type', 'RPICAM_V2')
        
        # Get the exact list of checked boxes sent by the browser
        raw_cams = request.form.getlist('hw-cams')
        
        # Read the new dropdown
        focus_mode_val = request.form.get('hw-focus_mode', 'manual')
        keep_af = (focus_mode_val == 'auto')

        crop_square = request.form.get('hw-crop_square') is not None
        
        # Backend Safety Validations
        if sel_type in ['SINGLE', 'NullSelector']:
            final_cams = [1] # Force single camera to port 1
        else:
            if not raw_cams:
                final_cams = [1] # Fallback if user unchecks everything
            else:
                try:
                    # Safely attempt to cast browser data to integers
                    final_cams = [int(c) for c in raw_cams]
                except ValueError:
                    # If tampering occurred, fallback to a safe state
                    final_cams = [1]
        
        # Security: Force AF to false if the user manipulated the DOM to submit AF for a V2 camera
        cam_profile = Config.CAMERA_PROFILES.get(cam_type, {})
        if not cam_profile.get("autofocus", False):
            keep_af = False
            
        success, msg = save_user_config({
            'SELECTOR_TYPE': sel_type,
            'CAMERA_TYPE': cam_type,
            'CAMS': tuple(final_cams),
            'KEEP_AUTOFOCUS': keep_af,
            'CROP_TO_SQUARE': crop_square
        })
        
        if success:
            flash("Hardware config saved! Restarting services to apply changes.", "success")
            return render_template('restarting.html', target_url=url_for('config_page.conf'))
        else:
            flash(f"Critical error saving config to disk: {msg}", "danger")

            return redirect(url_for('config_page.conf'))

    # --- 3. Camera Capture Profile Logic ---
    if request.form.get('action') == 'set_camera':
        valid_profiles = ('backlight_manual', 'backlight_auto', 'color_auto')
        sel_profile = request.form.get('cam-default_profile', 'backlight_manual')
        if sel_profile not in valid_profiles:
            sel_profile = 'backlight_manual'

        # Safely parse the manual backlight tuning fields
        try:
            exposure_time = int(request.form.get('cam-exposure_time', 30000))
        except (ValueError, TypeError):
            exposure_time = 30000
        try:
            analogue_gain = float(request.form.get('cam-analogue_gain', 1.0))
        except (ValueError, TypeError):
            analogue_gain = 1.0
        denoise_on = request.form.get('cam-denoise') is not None

        # Rebuild the full profile set, injecting the tuned manual values.
        profiles = {
            'backlight_manual': {
                'grayscale': True,
                'controls': {
                    'AeEnable': False,
                    'AwbEnable': False,
                    'ColourGains': (1.0, 1.0),
                    'NoiseReductionMode': 1 if denoise_on else 0,
                    'ExposureTime': exposure_time,
                    'AnalogueGain': analogue_gain,
                },
            },
            'backlight_auto': {
                'grayscale': True,
                'controls': {
                    'AeEnable': True,
                    'AwbEnable': True,
                    'NoiseReductionMode': 0,
                },
            },
            'color_auto': {
                'grayscale': False,
                'controls': {
                    'AeEnable': True,
                    'AwbEnable': True,
                    'NoiseReductionMode': 1,
                },
            },
        }

        success, msg = save_user_config({
            'DEFAULT_CAPTURE_PROFILE': sel_profile,
            'CAM_CAPTURE_PROFILES': profiles,
        })

        if success:
            flash("Camera profile saved! Restarting services to apply changes.", "success")
            return render_template('restarting.html', target_url=url_for('config_page.conf'))
        else:
            flash(f"Critical error saving config to disk: {msg}", "danger")
            return redirect(url_for('config_page.conf'))

    # --- 4. Advanced: Hostname Change Logic ---
    if request.form.get('action') == 'set_hostname':
        # Answered as JSON: the frontend coordinates the follow-up reboot via
        # the existing /api/reboot endpoint after this returns success.
        new_hostname = request.form.get('host-hostname', '')
        success, msg = apply_hostname_config(new_hostname)
        return jsonify({'result': success, 'message': msg}), (200 if success else 400)

    # --- 5. Advanced: Software Update (git pull) ---
    if request.form.get('action') == 'update_app':
        success, msg = run_git_update()
        return jsonify({'result': success, 'message': msg}), (200 if success else 400)

    # Calculate initial autofocus visibility state for the template
    current_profile = Config.CAMERA_PROFILES.get(getattr(Config, 'CAMERA_TYPE', 'RPICAM_V2'), {})
    has_autofocus = current_profile.get("autofocus", False)
                  
    return render_template('config.html', date=now,
            app_setting_form=app_setting_form,
            hw_form=hw_form, 
            camera_form=camera_form,
            hostname_form=hostname_form,
            light_state=light.state, config=Config, 
            scheduler_info=scheduler_info,
            has_autofocus=has_autofocus)