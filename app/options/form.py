"""
The forms used in the application

AppSettingsForm: Global Application Settings
"""

import datetime
from flask_wtf import FlaskForm as Form
from wtforms import (BooleanField, SelectField, StringField, SelectMultipleField,
                     IntegerField, FloatField)
from wtforms.fields import DateTimeField
from wtforms.validators import Optional, DataRequired, Length, Regexp
from wtforms import widgets


def _timezone_choices():
    """IANA zones for the config-page select; skip the sentinel 'localtime'."""
    zones = []
    try:
        from zoneinfo import available_timezones
        zones = sorted(z for z in available_timezones() if z != 'localtime')
    except Exception:
        try:
            import pytz
            zones = list(pytz.common_timezones)
        except Exception:
            zones = ['UTC']
    if 'UTC' not in zones:
        zones.insert(0, 'UTC')
    return [(z, z) for z in zones]


class AppSettingsForm(Form):
    """
    Settings of the application Time and Config
    """
    sync_mode = BooleanField("Use Network Time", default=True)

    time_zone = SelectField("Time Zone", choices=[], default="UTC")

    ntp_server = StringField("NTP Server", default="pool.ntp.org")

    systemDate = DateTimeField("Manual Date", format='%Y-%m-%d %H:%M:%S',
                        default=datetime.datetime.now, validators=[Optional()])

    def __init__(self, *args, **kwargs):
        super(AppSettingsForm, self).__init__(*args, **kwargs)
        choices = _timezone_choices()
        try:
            from config import Config
            current = getattr(Config, 'TIME_ZONE', None)
            if current and (current, current) not in choices:
                choices = [(current, current)] + choices
        except Exception:
            pass
        self.time_zone.choices = choices
    
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

class HostnameForm(Form):
    """
    Advanced: renames the device on the network (staged via raspi-config,
    applied on the next reboot).
    """
    hostname = StringField('Device Hostname', validators=[
        DataRequired(),
        Length(min=1, max=63),
        Regexp(r'^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$',
               message="Only letters, digits and hyphens; cannot start or end with a hyphen.")
    ])


class DebugClearForm(Form):
    """CSRF-only form for destructive log truncation."""
    pass