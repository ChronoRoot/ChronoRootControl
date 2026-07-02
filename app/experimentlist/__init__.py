from app.experimentlist.models import ExperimentList
from app.options.schedulerstatus import SchedulerStatus 
from config import Config
from flask import Blueprint, abort, render_template
from jinja2 import TemplateNotFound
from datetime import datetime
import socket

main_page = Blueprint('main_page', __name__,
                      template_folder='templates',
                      static_folder='static')

@main_page.route('/index')
@main_page.route('/')
def experiment_status():
    """
    Return a summary of all the experiments and global system telemetry.
    """
    exps = ExperimentList()
    status_manager = SchedulerStatus()
    sys_info = status_manager.get_info() # <-- Fetch RAM state
    
    try:
        return render_template('index.html', 
                               exps=exps, 
                               sys_info=sys_info, # <-- Pass to template
                               now=datetime.now(), 
                               config=Config, 
                               hostname=socket.gethostname())
    except TemplateNotFound:
        abort(404)