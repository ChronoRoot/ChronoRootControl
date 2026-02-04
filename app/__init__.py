"""
Organisation of the ChronoRootControl application
"""
import os
import fnmatch
import logging
from flask import Flask, render_template
from app.experimentlist import main_page
from app.experiment import experiment_page
from app.help import help_page
from app.api import api_exp
from app.options import config_page
from app.focus import focus_page

from config import Config

from app.experiment.models import Experiment
from app.storage import storage_page

"""
Creation and configuration of the flask application
"""

app = Flask(__name__)

app.config.update(
    DEBUG=Config.DEBUG,
    SECRET_KEY=Config.SECRET_KEY,
    WTF_CSRF_ENABLED = Config.WTF_CSRF_ENABLED,
    FLASK_LOGGING_EXTRAS_KEYWORDS = {'category': '<unset>'},
    FLASK_LOGGING_EXTRAS_BLUEPRINT = ('blueprint', __name__,
                                      '<NOT REQUEST>')
)

# # Registering the different part on the corresponding path
app.register_blueprint(main_page)
app.register_blueprint(experiment_page, url_prefix='/exp')
app.register_blueprint(api_exp, url_prefix='/api')
app.register_blueprint(help_page, url_prefix='/help')
app.register_blueprint(config_page, url_prefix='/config')
app.register_blueprint(focus_page, url_prefix='/preview')
app.register_blueprint(storage_page, url_prefix='/storage')

app.logger.setLevel(logging.INFO)
formatter = logging.Formatter(Config.LOG_FORMAT)
handler = logging.FileHandler(Config.LOGFILE, mode='a')
handler.setFormatter(formatter)
handler.setLevel(logging.INFO)

app.logger.addHandler(handler)
app.logger.info('Starting Flask app :  %s'%app.name)

#load XPs from json files
for root, dirs, files in os.walk(Config.WORKING_DIR):
    for afile in files:
        if fnmatch.fnmatch(afile, '*.json'):
            #use foder name as experiment ID,
            expid = os.path.basename(root)
            try:
                exp = Experiment(directory=root, app=app)
                app.logger.info('Loading exp %s'%expid)
                if exp.expid != expid:
                    exp.expid = expid
                if exp.status not in ('ENDED', 'Canceled',):
                    exp.create()
            except Exception as e:
                app.logger.error('Error while loading experiments from file-system: %s'%e)
                continue

def render_error(e):
    try:
        return render_template('errors/%s.html' % e.code), e.code
    except AttributeError:
        print(e)
        print(dir(e))
        return "%s"%e



for e in [401, 404, 500]:
    app.errorhandler(e)(render_error)
