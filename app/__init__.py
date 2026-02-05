"""
Organisation of the ChronoRootControl application
"""
import logging
from flask import Flask, render_template

# Blueprints
from app.experimentlist import main_page
from app.experiment import experiment_page
from app.help import help_page
from app.api import api_exp
from app.options import config_page
from app.focus import focus_page
from app.storage import storage_page

from config import Config

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

# Logging Setup
app.logger.setLevel(logging.INFO)
formatter = logging.Formatter(Config.LOG_FORMAT)
handler = logging.FileHandler(Config.LOGFILE, mode='a')
handler.setFormatter(formatter)
handler.setLevel(logging.INFO)

app.logger.addHandler(handler)
app.logger.info('Starting Flask app :  %s' % app.name)

def render_error(e):
    try:
        return render_template('errors/%s.html' % e.code), e.code
    except AttributeError:
        print(e)
        return "%s" % e

for e in [401, 404, 500]:
    app.errorhandler(e)(render_error)