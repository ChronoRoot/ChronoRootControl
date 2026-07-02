"""
The forms used in the application

SettingsForm: Settings of an experiment
"""
import datetime

from flask_wtf import FlaskForm as Form
from wtforms import (BooleanField, IntegerField, SelectMultipleField,
                     TextAreaField, ValidationError, StringField)
from wtforms.fields import DateTimeField
from wtforms.validators import Optional, Length
from config import Config

from phototron.rpimodule import RpiModule

class SettingsForm(Form):
    """
    Settings of an experiment
    """

    rpi = RpiModule()

    name = StringField("Experiment Name",
                       validators=[Optional(), Length(max=16)],
                       description='Short name for the experiment folder')

    desc = TextAreaField(u"Description:",
                        validators=[Optional()],
                        description='About this experiment')
    
    # Use Config.PRETTY_FORMAT (No %z)
    start = DateTimeField("Start Time",
                          format=Config.PRETTY_FORMAT,
                          default=datetime.datetime.now)
                          
    end = DateTimeField("End Time",
                        format=Config.PRETTY_FORMAT,
                        default=datetime.datetime.now)
                        
    interval = IntegerField("Interval (minutes)",
                            default=15,
                            description='Interval in minutes')
                            
    ir = BooleanField("IR Backlight",
                      default=True,
                      description="Turn the infrared lights on/off")

    cameras = SelectMultipleField('Cameras',
                                  choices=[(cam, "Camera %s" % cam) for cam in Config.CAMS],
                                  default=list(Config.CAMS),  
                                  coerce=int,
                                  description='Cameras to use')
    
    camera = rpi.selector.get_camera()

    # --- BACKEND VALIDATIONS ---

    def validate_cameras(self, field):
        """
        At least one camera should be selected
        """
        if len(field.data) < 1:
            raise ValidationError("Select at least one camera")

    def validate_interval(self, field):
        """
        Validate interval is at least 5 minutes
        """
        if field.data < 5:
            raise ValidationError("Interval too short. Minimum time between pictures is 5 minutes.")

    def validate_start(self, field):
        """
        Validate start time is at least 1 minute in the future
        """
        if field.data:
            # --- FIX: FORCE NAIVE DATETIME ---
            # Even if the form parser detects a timezone, we strip it 
            # to ensure we compare against naive Server Time.
            start_naive = field.data.replace(tzinfo=None)
            
            now = datetime.datetime.now()
            if start_naive < now + datetime.timedelta(minutes=1):
                raise ValidationError("Start time must be at least 1 minute in the future.")

    def validate_end(self, field):
        """
        Validate end time is at least 5 minutes after start time
        """
        if field.data and self.start.data:
            # Force naive for comparison
            end_naive = field.data.replace(tzinfo=None)
            start_naive = self.start.data.replace(tzinfo=None)
            
            min_duration = datetime.timedelta(minutes=5)
            if end_naive < (start_naive + min_duration):
                raise ValidationError("Duration too short. Experiment must run for at least 5 minutes.")
            
    def validate_overlap(self, current_exp_id=None):
        """
        Custom validator to call manually in the view.
        Returns True if valid, False if conflict found (and adds error to form).
        """
        from app.experimentlist.models import ExperimentList
        
        exp_list = ExperimentList()
        
        # --- FIX: FORCE NAIVE BEFORE PASSING TO LOGIC ---
        # The experiment list contains naive datetimes (from JSON).
        # We must ensure our inputs are also naive.
        s_naive = self.start.data.replace(tzinfo=None) if self.start.data else None
        e_naive = self.end.data.replace(tzinfo=None) if self.end.data else None
        
        if not s_naive or not e_naive:
            return False

        conflict_exp = exp_list.find_conflict(
            s_naive, 
            e_naive, 
            ignore_id=current_exp_id
        )

        if conflict_exp:
            # Using strftime for standard formatting
            msg = f"Time Conflict! Overlaps with running experiment: {conflict_exp.expid} ({conflict_exp.start.strftime('%H:%M')} - {conflict_exp.end.strftime('%H:%M')})"
            self.start.errors.append(msg)
            return False
            
        return True