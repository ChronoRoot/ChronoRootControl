"""
Organisation of the ChronoRootControl application
"""
import faulthandler
import logging
import os
import sys
from flask import Flask, render_template

from config import Config

# Logging must be ready before blueprint imports: importing app.focus also imports
# the camera streamer, which creates its hardware logger immediately.
for log_path in (Config.LOGFILE, Config.SHDL_LOG_FILE, Config.CRASH_LOG_FILE):
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except OSError as exc:
        print("Warning: could not create log directory for %s: %s" % (log_path, exc), file=sys.stderr)

_crash_log_handle = None
try:
    _crash_log_handle = open(Config.CRASH_LOG_FILE, 'a')
    faulthandler.enable(file=_crash_log_handle, all_threads=True)
except (OSError, RuntimeError) as exc:
    print("Warning: crash logging unavailable: %s" % exc, file=sys.stderr)
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except RuntimeError:
        pass

# Blueprints
from app.experimentlist import main_page
from app.experiment import experiment_page
from app.doc import help_page
from app.api import api_exp
from app.options import config_page
from app.focus import focus_page
from app.storage import storage_page
from app.wifi import wifi_page
from app.sync import sync_page

"""
Creation and configuration of the flask application
"""

app = Flask(__name__)

app.config.update(
    DEBUG=Config.DEBUG,
    SECRET_KEY=Config.SECRET_KEY,
    WTF_CSRF_ENABLED = Config.WTF_CSRF_ENABLED,
    FLASK_LOGGING_EXTRAS_KEYWORDS = {'category': '<unset>'},
    FLASK_LOGGING_EXTRAS_BLUEPRINT = ('blueprint', __name__, '<NOT REQUEST>')
)

# Register Blueprints
app.register_blueprint(main_page)
app.register_blueprint(experiment_page, url_prefix='/exp')
app.register_blueprint(api_exp, url_prefix='/api')
app.register_blueprint(help_page, url_prefix='/help')
app.register_blueprint(config_page, url_prefix='/config')
app.register_blueprint(focus_page, url_prefix='/preview')
app.register_blueprint(storage_page, url_prefix='/storage')
app.register_blueprint(wifi_page, url_prefix='/wifi')
app.register_blueprint(sync_page, url_prefix='/sync')

# Logging Setup
app.logger.setLevel(logging.INFO)
formatter = logging.Formatter(Config.LOG_FORMAT)
try:
    handler = logging.FileHandler(Config.LOGFILE, mode='a')
except OSError:
    handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(formatter)
handler.setLevel(logging.INFO)

app.logger.addHandler(handler)
app.logger.info('Starting Flask app :  %s' % app.name)

def render_error(e):
    try:
        return render_template('errors/%s.html' % e.code, config=app.config), e.code
    except AttributeError:
        print(e)
        return "%s" % e

for e in [401, 404, 500]:
    app.errorhandler(e)(render_error)