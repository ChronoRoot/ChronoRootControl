import configparser
import os
from flask import Blueprint, render_template, request, flash, url_for
from .form import SyncSettingsForm
from config import Config
from app.options.config_manager import save_user_config
from app.options.schedulerstatus import SchedulerStatus
from .manager import setup_rclone_remote, RCLONE_CONF
from app.storage import get_mounted_drives 

sync_page = Blueprint('sync_page', __name__, template_folder='templates', static_folder='static')

@sync_page.route('/', methods=['GET', 'POST'])
def index():
    form = SyncSettingsForm()
    status_info = SchedulerStatus().get_info().get("sync", {})
    mounts = get_mounted_drives() 
    has_valid_dest = bool(getattr(Config, 'SYNC_DESTINATION', '').strip())

    if request.method == 'GET':
        form.sync_enabled.data = getattr(Config, 'SYNC_ENABLED', False)
        form.sync_interval.data = getattr(Config, 'SYNC_INTERVAL', 60)
        form.remote_type.data = getattr(Config, 'SYNC_REMOTE_TYPE', 'local')
        form.destination_path.data = getattr(Config, 'SYNC_DESTINATION', '')

        # Populate form with current saved credentials
        if os.path.exists(RCLONE_CONF):
            try:
                parser = configparser.ConfigParser()
                parser.read(RCLONE_CONF)
                if 'chronosync' in parser.sections():
                    form.host.data = parser['chronosync'].get('host', '')
                    form.user.data = parser['chronosync'].get('user', '')
                    form.port.data = parser['chronosync'].get('port', '')
                    
                    if parser['chronosync'].get('pass', ''):
                        form.password.data = "********"
            except Exception as e:
                print(f"Error reading rclone.conf for UI pre-fill: {e}")

    if form.validate_on_submit():
        rtype = form.remote_type.data
        
        if rtype in ['sftp', 'ftp']:
            if not form.host.data or not form.user.data:
                flash("Host and Username are required for SSH/FTP.", "danger")
                return render_template('sync.html', form=form, sync_status=status_info, mounts=mounts, has_dest=has_valid_dest)
            
            target_pass = form.password.data
            if not target_pass:
                flash("Password is required to configure a connection.", "danger")
                return render_template('sync.html', form=form, sync_status=status_info, mounts=mounts, has_dest=has_valid_dest)

            success, msg = setup_rclone_remote(rtype, form.host.data, form.user.data, target_pass, form.port.data)
            if not success:
                flash(msg, "danger")
                return render_template('sync.html', form=form, sync_status=status_info, mounts=mounts, has_dest=has_valid_dest)

        success, msg = save_user_config({
            'SYNC_ENABLED': form.sync_enabled.data,
            'SYNC_INTERVAL': form.sync_interval.data,
            'SYNC_REMOTE_TYPE': rtype,
            'SYNC_DESTINATION': form.destination_path.data
        })
        
        if success:
            flash("Sync configuration saved! Restarting services...", "success")
            return render_template('restarting.html', target_url=url_for('sync_page.index'))
        else:
            flash(f"Error saving config: {msg}", "danger")

    return render_template('sync.html', form=form, sync_status=status_info, mounts=mounts, has_dest=has_valid_dest)