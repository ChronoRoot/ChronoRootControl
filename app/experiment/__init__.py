"""
Management of an experiment
"""
import os

from app.experiment.form import SettingsForm
from app.experiment.models import Experiment
from .models import Experiment
from config import Config
from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from wtforms import SelectMultipleField, TextAreaField
from datetime import datetime

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


    # Create a BLANK experiment. It has no ID yet.
    exp = Experiment() 
    actions = "new"

    if form.validate_on_submit():
        if form.validate_overlap():
            # Populate the blank object
            form.populate_obj(exp)
            exp.status = "NEW"
            # This triggers generate_id() -> creates folder -> saves json -> notifies scheduler
            exp.create() 
            
            flash('Launched! ID: %s' % exp.expid, 'success')
            return redirect(url_for('experiment_page.setuped_experiment', expid=exp.expid))
        else:
             flash('Scheduling conflict.', 'danger')

    return render_template('experiment.html', form=form, exp=exp, config=Config, 
                           actions=actions, now=datetime.now().strftime(Config.PRETTY_FORMAT))

@experiment_page.route('/<expid>', methods=['GET', 'POST'])
def setuped_experiment(expid):   
    try:
        exp = Experiment.load_from_id(expid)
    except FileNotFoundError:
        abort(404)
        
    form = SettingsForm(obj=exp)

    # 1. Determine permissions based on Status
    if exp.status in ["NEW", "SCHEDULED"]:
        actions = "editable"
    elif exp.status == "SETUP":
        actions = "new"
    elif exp.status == "RUNNING":
        actions = "cancelable"
    else:
        actions = "readonly"

    if request.method == 'POST':
        action = request.form['action']

        # 2. Handle SAVE (Only if allowed)
        if action == "save" and actions == "editable":
            if form.validate_on_submit():
                # Optional: You might want to run form.validate_overlap() here too
                form.populate_obj(exp)
                exp.create() # Saves changes to JSON
                flash('Changes saved successfully.', 'success')
                # Reload to show new values
                return redirect(url_for('experiment_page.setuped_experiment', expid=exp.expid))
            else:
                flash('Error saving changes. Check the form.', 'danger')

        # 3. Handle CANCEL
        elif action == "cancel" and actions in ["editable", "cancelable"]:
            exp.cancel()
            flash('The experiment %s has been canceled' % exp.expid, 'warning')
            return redirect(url_for('experiment_page.setuped_experiment', expid=exp.expid))

        # 4. Handle DELETE
        elif action == "delete":
            exp.delete()
            flash('The experiment %s has been deleted' % exp.expid, 'danger')
            return redirect(url_for('experiment_page.new_experiment')) # Redirect to home/new after delete

    return render_template('experiment.html', form=form, exp=exp, config=Config,
                           actions=actions, now=datetime.now().strftime(Config.PRETTY_FORMAT))