"""
Handle the information of one experiment
"""

import os
import shutil
import uuid

import arrow
from flask import json
from flask import current_app
#from app.tasks import take_picture
from config import Config
import socket
import uwsgi


_NOTFOUND = object()

class Experiment(object):
    """
    Handle the information of one experiment
    """
    app = current_app
    schedulerstatus = None
    expid = ""
    desc = ""
    status = "SETUP"
    message = ""
    creation = arrow.now().format('YYYY-MM-DD HH:mm:ssZ')
    modification = ""
    _start = arrow.now().format('YYYY-MM-DD HH:mm:ssZ')
    _end = arrow.now().format('YYYY-MM-DD HH:mm:ssZ')
    interval = 15 # in minutes
    steps_nb = 0
    cameras = []
    ir = False
    steps = []
    next_run_time = ""
    workdir = ""
    params = Config.CAM_PARAMS


    @property
    def start(self):
        return arrow.get(self._start)

    @start.setter
    def start(self, value):
        self._start = arrow.get(value).format('YYYY-MM-DD HH:mm:ssZ')



    @property
    def end(self):
        return arrow.get(self._end)

    @end.setter
    def end(self, value):
        self._end = arrow.get(value).format('YYYY-MM-DD HH:mm:ssZ')

    def __init__(self, directory=None, **kwargs):
        """
        Initialisation of the experiment

        Each parameter could be specified at initialisation:

        Args:
          expid: string
            the id of the xperiment
          desc: string
            field for users describtion of the experiment
          status: string
            the status of the experiment
          message: string
            field for system message
          creation: date
            experiment creation date
          modification: datetime
            experiment modification
          start: string
            the starting datetime of the experiment
          end: string
            the ending datetime of the experiment
          interval: int
            interval between two time points (pictures)
          steps_nb: int
            the number of time point already done
          cameras: list
            camera list
          ir: boolean
            Infrared is ON/OFF
          steps: list of tuples
            list of steps. Each step is a tuple containing
            date of image capture, the image file path and the camera id
        """
        from app.options.schedulerstatus import SchedulerStatus
        self.schedulerstatus = SchedulerStatus()
        if directory is not None:
            self.workdir = directory
            self.load()
        else:
            self.from_dict(kwargs)

        if self.expid == "" : self.expid = self.new_xp_id()

        if self.workdir == '':
            self.workdir = os.path.join(Config.WORKING_DIR, self.expid)

        self.status_update()

    def status_update(self):
        self.schedulerstatus.load()
        if self.expid in self.schedulerstatus.state['jobs']:
            self.next_run_time = self.schedulerstatus.state['jobs'][self.expid]["next_run_time"]
        else:
            self.next_run_time = "Not in the scheduler"

    def __eq__(self, other):
        """method to comppare two Experiments
        """
        for attr in ['expid', 'desc', 'ir', 'cameras', 'start', 'end']:
            v1, v2 = [getattr(obj, attr, _NOTFOUND) for obj in [self, other]]
            if v1 is _NOTFOUND or v2 is _NOTFOUND:
                return False
            elif v1 != v2:
                return False
        return True

    def new_xp_id(self):
        """Generate an Id for the experiment
        """
        self.creation = arrow.now().format('YYYY-MM-DD HH:mm:ssZ')
        self._start = arrow.now().format('YYYY-MM-DD HH:mm:ssZ')
        self._end = arrow.now().format('YYYY-MM-DD HH:mm:ssZ')
        startdate = arrow.get(self.creation).format('YYYY-MM-DD_HH-mm')
        return "%s_%s"%(socket.gethostname(), startdate)


    def from_dict(self, data):
        """
        Convert from dict from serialisation

        Args:
          data: dict
            The dictionnary to use to modify the data
        """
        for key, value in data.items():
            self.__setattr__(key, value)

    def to_dict(self):
        """
        Convert to dict for serialisation

        Returns
          dict: the result dictionnary
        """
        return({"expid": self.expid,
                "desc": self.desc,
                "status": self.status,
                "message": self.message,
                "creation": self.creation,
                "modification": self.modification,
                "start":  self._start,
                "end": self._end,
                'interval': self.interval,
                "steps_nb": self.steps_nb,
                "cameras":  self.cameras,
                "ir": self.ir,
                "steps": self.steps,
                "workdir": self.workdir,
                "img_params": self.params,
                })

    def load(self):
        """
        Load the information of the experiment from the experiment directory

        Args:
          directory: string
            The experiment directory path
        """
        with open('%s/info.json' % self.workdir, 'r') as f:
            self.from_dict(json.load(f))

    def dump(self):
        """
        Save the object to a file (json)

        Args:
          directory: string
              The experiment directory path. By default it use the expid as
              directory name in the working directory
        """
        workdir = self.workdir
        if not os.path.isdir(workdir):
            os.mkdir(workdir)
        with open('%s/info.json' % workdir, 'w+') as f:
                json.dump(self.to_dict(), f, sort_keys=True, indent=4 * ' ')

    def new_step(self, step):
        self.steps_nb += 1
        self.message = "In progress. %s steps accomplished" % self.steps_nb
        self.status =  'RUNNING'
        self.steps.append(step)

    def create(self):
        """
        Create an experiment
        """
        if self.status in ("ENDED", "CANCEL"):
            return
        self.status = "SETUP"
        self.modification = arrow.now().format('YYYY-MM-DD HH:mm:ssZ')
        self.dump() # passer tous les parametres via le message ?
        message = { 'id' : self.expid,
                    'action' : 'CREATE'
        }
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO) # inform scheduler to add XP
        return

    def cancel(self, message=None):
        """
        Cancel an experiment

        Args:
          message: str
            The message to add in the log
        """
        message = { 'id' : self.expid,
                    'action' : 'CANCEL'
        }

        # inform scheduler to cancel XP
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)
        self.app.logger.info('XP %s canceled by user' %self.expid)
        return

    def delete(self):
        """
        Delete an experiment from system

        It remove all links of the experiment from the module: all files are
        lost.

        Args:
          directory: string
              The experiment directory path. By default it use the expid as
              directory name in the working directory
        """
        # First cancel the experiment (for scheduler)
        self.cancel()
        workdir = self.workdir

        shutil.rmtree(workdir)
        self.app.logger.info('XP %s deleted by user' % self.expid)
        return

    def diagnostic(self):
        """
        Trigger the hardware diagnostic via the uWSGI mule
        """
        # Set a status to indicate a check is pending
        self.status = "DIAGNOSTIC"
        self.message = "Hardware scan requested..."
        self.dump()
        
        message = { 
            'id' : self.expid,
            'action' : 'CHECK_HARDWARE'
        }
        
        # Send to the mule using your existing config/imports
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)
        return