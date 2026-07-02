"""
Management of an experiment
"""
import os
from datetime import datetime

from flask import Blueprint, abort, flash, render_template, request, url_for, redirect
from wtforms import SelectMultipleField

from app.experiment.form import SettingsForm
from app.experiment.models import Experiment
from app.options.schedulerstatus import SchedulerStatus
from config import Config

experiment_page = Blueprint('experiment_page', __name__,
                            template_folder='templates',
                            static_folder='static')

@experiment_page.route('/', methods=['GET', 'POST'])
def new_experiment():
    form = SettingsForm()
    for name, item in form.camera.settings.items():
        if item['type'] == 'list':
            tmpfield = SelectMultipleField( name,
                                            choices=[(value, effect) for effect, value in item['values'].items()],
                                            default=item['default'],
                                            coerce=int,
                                            description=name)
            setattr(form, name, tmpfield)

    exp = Experiment() 
    actions = "new"
    
    # --- NEW: Fetch Storage info for the top dashboard ---
    status_manager = SchedulerStatus()
    storage_info = status_manager.get_info().get("system_health", {}).get("storage", {})

    if form.validate_on_submit():
        form.populate_obj(exp)
        
        is_valid, error_msg = exp.validate_rules(is_new=True)
        
        if not is_valid:
            flash(f'Cannot launch: {error_msg}', 'danger')
        elif not form.validate_overlap():
            flash('Scheduling conflict with an existing experiment.', 'danger')
        else:
            exp.status = "NEW"
            exp.create() 
            flash('Launched! ID: %s' % exp.expid, 'success')
            target_url = url_for('experiment_page.setuped_experiment', expid=exp.expid)
            return redirect(target_url)

    return render_template('experiment.html', form=form, exp=exp, config=Config, 
                           actions=actions, storage=storage_info, now=datetime.now().strftime(Config.PRETTY_FORMAT))

@experiment_page.route('/<expid>', methods=['GET', 'POST'])
def setuped_experiment(expid):   
    try:
        exp = Experiment.load_from_id(expid)
    except FileNotFoundError:
        abort(404)
        
    form = SettingsForm(obj=exp)
    
    # --- NEW: Fetch RAM State & Storage ---
    status_manager = SchedulerStatus()
    sys_info = status_manager.get_info()
    job_ram = status_manager.state.get("jobs", {}).get(exp.expid, None)
    if exp.status in ("FINISHED", "CANCELLED"):
        job_ram = None
    archive_summary = None
    if exp.status in ("FINISHED", "CANCELLED") and exp.expid:
        archive_summary = Experiment.get_archived_summary(exp.expid)
    storage_info = sys_info.get("system_health", {}).get("storage", {})
    sys_alerts = sys_info.get("alerts", {})

    # --- NEW: Find the exact latest image filename for each camera ---
    last_images = {}
    if os.path.exists(exp.workdir) and exp.cameras:
        for cam in exp.cameras:
            cam_dir = os.path.join(exp.workdir, str(cam))
            if os.path.exists(cam_dir):
                # Fast read-only check
                files = [f for f in os.listdir(cam_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
                if files:
                    latest_file = max(files, key=lambda x: os.path.getmtime(os.path.join(cam_dir, x)))
                    last_images[cam] = f"{exp.expid}/{cam}/{latest_file}"

    # Determine permissions based on Status
    if exp.status in ["NEW", "SCHEDULED", "RUNNING", "ERROR"]:
        actions = "editable"
    elif exp.status == "SETUP":
        actions = "new"
    else:
        actions = "readonly"

    if request.method == 'POST':
        action = request.form['action']

        if action == "save" and actions == "editable":
            if form.validate_on_submit():
                orig_name = exp.name 
                orig_interval = exp.interval
                orig_cameras = exp.cameras
                orig_start = exp._start
                
                form.populate_obj(exp)
                
                exp.name = orig_name 
                exp.interval = orig_interval
                exp.cameras = orig_cameras
                if exp.status in ("RUNNING", "ERROR"):
                    exp._start = orig_start
                
                is_valid, error_msg = exp.validate_rules(is_new=False)
                
                if not is_valid:
                    flash(f'Cannot save: {error_msg}', 'danger')
                elif not form.validate_overlap(current_exp_id=exp.expid):
                    flash('Scheduling conflict with an existing experiment.', 'danger')
                else:
                    exp.update() 
                    flash('Changes saved successfully.', 'success')
                    target_url = url_for('experiment_page.setuped_experiment', expid=exp.expid)
                    return redirect(target_url)
            else:
                flash('Error saving changes. Check the form.', 'danger')

        elif action == "cancel":
            exp.cancel()
            flash('The experiment %s has been canceled' % exp.expid, 'warning')
            target_url = url_for('experiment_page.setuped_experiment', expid=exp.expid)
            return redirect(target_url)

        elif action == "delete":
            exp.cancel() 
            exp.delete()
            flash('The experiment %s has been deleted' % exp.expid, 'danger')
            return "<script>window.location.href = '/';</script>"

    return render_template('experiment.html', form=form, exp=exp, config=Config,
                           actions=actions, job_ram=job_ram, archive_summary=archive_summary,
                           storage=storage_info, last_images=last_images,
                           sys_alerts=sys_alerts, sys_info=sys_info,
                           now=datetime.now().strftime(Config.PRETTY_FORMAT))