##
# Configuration management
#

from .form import AppSettingsForm, BackLightForm
from .settings_manager import Settings
from datetime import datetime
import subprocess
import os
from time import sleep
from io import BytesIO
from config import Config

from phototron.rpimodule import RpiModule
from app.options.schedulerstatus import SchedulerStatus


from flask import (Blueprint, abort, flash, render_template, request,
                   url_for, Response)

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
    scheduler_info = SchedulerStatus().get_info()

    app_setting_form = AppSettingsForm(prefix="app_setting")
    backlight_form = BackLightForm(ir=light.state, prefix="backlight")

    # --- 1. System Date Logic ---
    if app_setting_form.validate_on_submit() and app_setting_form.data:
        new_date = app_setting_form.systemDate.data
        retval = setSystemTime(new_date, network=False)

        if retval == 0:
            readable_date = new_date.strftime(Config.PRETTY_FORMAT)
            flash('System date successfully changed to: %s' % readable_date, 'success')
        else:
            flash('Error: Date change failed. System said: %s' % retval, 'danger')
            
        target_url = url_for('config_page.conf')
        return f"<script>window.location.href = '{target_url}';</script>"

    # --- 2. Backlight Logic ---
    if backlight_form.validate_on_submit() and backlight_form.data:
        if backlight_form.data['ir']:
            light.state = light.ON
        else:
            light.state = light.OFF
            
        target_url = url_for('config_page.conf')
        return f"<script>window.location.href = '{target_url}';</script>"

    return render_template('config.html', date=now,
            app_setting_form=app_setting_form,
            backlight_form=backlight_form,
            light_state=light.state, config=Config, scheduler_info=scheduler_info)

def setSystemTime(mydate, network=True):
    """ Set System system time
          * manually
          * or request time from ntpd server
    """
    if network:
        pass
    else:
        datestring = mydate.strftime(Config.PRETTY_FORMAT)
        p = subprocess.Popen('date -s "%s"'%datestring, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = b""
        for line in p.stdout.readlines():
            output += line
        retval = p.wait()
        if retval == 0:
            return retval
        else:
            return output.decode('UTF8')
