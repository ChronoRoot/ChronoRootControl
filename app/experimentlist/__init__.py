"""
The main views of the application
"""
from app.experimentlist.models import ExperimentList
from config import Config
from flask import Blueprint, abort, render_template
from jinja2 import TemplateNotFound
from datetime import datetime

main_page = Blueprint('main_page', __name__,
                      template_folder='templates',
                      static_folder='static')
experiment_page = Blueprint('experiment_page', __name__,
                            template_folder='templates',
                            static_folder='static')


@main_page.route('/index')
@main_page.route('/')
def experiment_status():
    """
    Return a summary of all the experiment of the module
    """
    exps = ExperimentList()
    try:
        return render_template('index.html', exps=exps, now=datetime.now(), config=Config)
    except TemplateNotFound:
        abort(404)
