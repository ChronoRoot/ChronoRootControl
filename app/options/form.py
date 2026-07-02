"""
The forms used in the application

AppSettingsForm: Global Application Settings
"""

import datetime
from flask_wtf import FlaskForm as Form
from wtforms import (BooleanField, SelectField, StringField, SelectMultipleField,
                     IntegerField, FloatField)
from wtforms.fields import DateTimeField
from wtforms.validators import Optional
from wtforms import widgets 

class AppSettingsForm(Form):
    """
    Settings of the application Time and Config
    """
    sync_mode = BooleanField("Use Network Time", default=True)
    
    # Add your preferred time zones here
    time_zone = SelectField("Time Zone", choices=[
        ('UTC', 'UTC'),
        ('Europe/London', 'Europe/London'),
        ('Europe/Paris', 'Europe/Paris'),
        ('America/New_York', 'America/New_York'),
        ('America/Chicago', 'America/Chicago'),
        ('Asia/Tokyo', 'Asia/Tokyo'),
        ('Australia/Sydney', 'Australia/Sydney')
    ], default="UTC")
    
    ntp_server = StringField("NTP Server", default="pool.ntp.org")
    
    systemDate = DateTimeField("Manual Date", format='%Y-%m-%d %H:%M:%S',
                        default=datetime.datetime.now, validators=[Optional()])
    
class BackLightForm(Form):
    """
    Switch for the infrared backlight
    """
    ir = BooleanField("ir",
                      default=True,
                      description="Turn the infrared lights on/off")
    
# Helper class for rendering checkboxes properly
class MultiCheckboxField(SelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()

class HardwareSettingsForm(Form):
    selector_type = SelectField('Multiplexer Type', choices=[
        ('SINGLE', 'Direct Connection (Single Camera)'),
        ('TYPE_QUAD2', 'IVPort v2 (4 Cameras)')
    ])
    
    camera_type = SelectField('Camera Sensor', choices=[
        ('RPICAM_V2', 'Camera V2'),
        ('RPICAM_V3_V2COMP', 'Camera V3 with V2 image size crop'),
        ('RPICAM_V3', 'Camera V3'),
        ('RPICAM_V3_WIDE', 'Camera V3 Wide')
    ])
    
    cams = MultiCheckboxField('Connected Cameras', choices=[
        (1, 'Cam 1'), (2, 'Cam 2'), (3, 'Cam 3'), (4, 'Cam 4')
    ], coerce=int)
    
    # --- CHANGED: Now a standard Select dropdown ---
    focus_mode = SelectField('Focus Mode', choices=[
        ('manual', 'Manual (Calibrated)'),
        ('auto', 'Automatic (Continuous AutoFocus)')
    ])

    crop_square = BooleanField('Crop to Square')

class CameraProfileForm(Form):
    """
    Selects the active Picamera2 capture profile and tunes the manual backlight.
    """
    default_profile = SelectField('Active Capture Profile', choices=[
        ('backlight_manual', 'Backlight Manual (locked exposure, grayscale)'),
        ('backlight_auto', 'Backlight Auto (auto exposure/WB, no denoise, grayscale)'),
        ('color_auto', 'Color Auto (auto exposure/WB, denoised, color)'),
    ])
    exposure_time = IntegerField('Manual Exposure Time (us)', validators=[Optional()])
    analogue_gain = FloatField('Manual Analogue Gain', validators=[Optional()])
    denoise = BooleanField('Manual Backlight Denoise')