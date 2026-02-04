"""
The forms used in the application

SettingsForm: Settings of an experiment
"""
import datetime

from flask_wtf import FlaskForm as Form
from wtforms import (BooleanField, IntegerField, SelectMultipleField,
                     TextAreaField, ValidationError)
from wtforms.ext.dateutil.fields import DateTimeField
from wtforms.validators import Optional
from config import Config

from phototron.rpimodule import RpiModule

class SettingsForm(Form):
    """
    Settings of an experiment
    """

    rpi = RpiModule()

    desc = TextAreaField(u"Description:",
                        validators=[Optional()],
                        description='About this experiment')
    
    # 24-hour format enforced in display_format
    start = DateTimeField("Start Time",
                          display_format='%Y-%m-%d %H:%M:%S %z',
                          default=datetime.datetime.now)
                          
    end = DateTimeField("End Time",
                        display_format='%Y-%m-%d %H:%M:%S %z',
                        default=datetime.datetime.now)
                        
    interval = IntegerField("Interval (minutes)",
                            default=15,
                            description='Interval in minutes')
                            
    ir = BooleanField("IR Backlight",
                      default=True,
                      description="Turn the infrared lights on/off")

    cameras = SelectMultipleField('Cameras',
                                  choices=[(cam, "Camera %s" % cam) for cam in Config.CAMS],
                                  default=[1],
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
        Validate interval is at least 10 minutes
        """
        # We handle the logic here, not in the HTML
        if field.data < 10:
            raise ValidationError("Interval too short. Minimum time between pictures is 10 minutes.")

    def validate_start(self, field):
        """
        Validate start time is at least 1 minute in the future
        """
        if field.data:
            now = datetime.datetime.now(field.data.tzinfo)
            if field.data < now + datetime.timedelta(minutes=1):
                raise ValidationError("Start time must be at least 1 minute in the future.")

    def validate_end(self, field):
        """
        Validate end time is at least 10 minutes after start time
        """
        if field.data and self.start.data:
            min_duration = datetime.timedelta(minutes=10)
            if field.data < (self.start.data + min_duration):
                raise ValidationError("Duration too short. Experiment must run for at least 10 minutes.")