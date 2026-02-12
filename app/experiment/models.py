import os
import shutil
import time
from datetime import datetime, timedelta
import json
from config import Config
import socket
import uwsgi

class Experiment(object):
    def __init__(self):
        """
        Initialize a blank/draft experiment with default values.
        Does NOT access disk. Does NOT generate an ID yet.
        """        
        # Lazy import to avoid circular dependency
        from app.options.schedulerstatus import SchedulerStatus
        self.schedulerstatus = SchedulerStatus()

        # Defaults
        self.expid = None
        self.desc = ""
        self.status = "SETUP"
        self.message = ""
        
        # Times (Default to Now)
        now_str = datetime.now().strftime(Config.PRETTY_FORMAT)
        self.creation = now_str
        self.modification = ""
        self._start = now_str
        self._end = now_str
        
        self.interval = 15
        self.steps_nb = 0
        self.next_run_time = ""
        self.workdir = ""
        
        # Mutable defaults (Fresh lists per instance)
        self.cameras = []
        self.ir = False
        self.steps = []
        self.img_params = Config.CAM_PARAMS.copy() if Config.CAM_PARAMS else {}
        
        # Logs
        self.logs = []

    @classmethod
    def load_from_id(cls, expid):
        """
        Factory method to load an EXISTING experiment.
        """
        instance = cls()
        instance.expid = expid
        instance.workdir = os.path.join(Config.WORKING_DIR, expid)
        
        json_path = os.path.join(instance.workdir, 'info.json')
        
        # STRICT CHECK: If file doesn't exist, fail immediately.
        if not os.path.exists(json_path):
            raise FileNotFoundError(f"Experiment {expid} not found at {json_path}")
            
        with open(json_path, 'r') as f:
            data = json.load(f)
            instance.from_dict(data)
            
        instance.status_update()
        return instance

# --- Properties ---
    @property
    def start(self):
        return self._parse_date_string(self._start)

    @start.setter
    def start(self, value):
        self._start = self._format_date_input(value)

    @property
    def end(self):
        return self._parse_date_string(self._end)

    @end.setter
    def end(self, value):
        self._end = self._format_date_input(value)

    # --- Helper Methods for Robust Parsing ---
    def _parse_date_string(self, date_str):
        """Attempts to parse string into datetime, handling old and new formats."""
        if not date_str or not isinstance(date_str, str):
            return datetime.now()

        # List of formats to try: [New Format, Old Format with TZ]
        formats = [Config.PRETTY_FORMAT, "%Y-%m-%d %H:%M:%S%z"]
        
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                # If it has timezone info, strip it to maintain consistency with new data
                return dt.replace(tzinfo=None) 
            except ValueError:
                continue
        
        # Fallback if everything fails
        return datetime.now()

    def _format_date_input(self, value):
        """Ensures setters always save in the NEW format."""
        if isinstance(value, datetime):
            return value.strftime(Config.PRETTY_FORMAT)
        elif isinstance(value, str):
            # Parse it first to validate/clean it, then re-format to new standard
            dt = self._parse_date_string(value)
            return dt.strftime(Config.PRETTY_FORMAT)
        return value

    # --- Methods ---
    def status_update(self):
        self.schedulerstatus.load()
        if self.schedulerstatus.state and 'jobs' in self.schedulerstatus.state:
            if self.expid in self.schedulerstatus.state['jobs']:
                self.next_run_time = self.schedulerstatus.state['jobs'][self.expid]["next_run_time"]
            else:
                self.next_run_time = "Not in scheduler"

    def generate_id(self):
        """
        Generates a unique ID using:
        Hostname + Scheduled Start Time.
        
        Example: mypi_2026-02-05_14-00-00
        """
        if self.expid == 'system': return
        
        # Access the datetime object via the property
        start_dt = self.start
        date_str = start_dt.strftime(Config.DATE_FORMAT)
        self.expid = "%s_%s" % (socket.gethostname(), date_str)
        self.workdir = os.path.join(Config.WORKING_DIR, self.expid)

    def from_dict(self, data):
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def to_dict(self):
        return {
            "expid": self.expid,
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
            "img_params": self.img_params,
            "logs": self.logs
        }

    def save(self):
        """
        Saves the experiment to disk ATOMICALLY.
        This prevents the 'empty file' race condition.
        """
        if not self.expid:
            self.generate_id()
            
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)
            
        self.modification = datetime.now().strftime(Config.PRETTY_FORMAT)
        
        # Define the target path and a temporary path
        target_path = os.path.join(self.workdir, 'info.json')
        
        with open(target_path, 'w+') as f:
            json.dump(self.to_dict(), f, sort_keys=True, indent=4)
            f.flush() 
            os.fsync(f.fileno()) 

    def create(self):
        """Launch the experiment"""
        # RULE: SETUP -> NEW
        self.status = "NEW"
        self.save() 
        
        # Notify Scheduler
        message = {'id': self.expid, 'action': 'CREATE'}
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)

    def cancel(self):        
        message = {'id': self.expid, 'action': 'CANCEL'}
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)
        
        self.status = "CANCELLED"
        self.message = "Stop requested by user."
        self.save()
        self.status_update() 

    def delete(self):
        # To delete an experiment, it should be CANCELED or FINISHED
        if self.status != "CANCELLED" or self.status != "FINISHED":
            if os.path.exists(self.workdir):
                shutil.rmtree(self.workdir)
                
    def diagnostic(self):
        """
        Trigger the hardware diagnostic via the uWSGI mule.
        RULE: Always set to last 10 minutes from NOW.
        """
        self.status = "DIAGNOSTICS"
        self.message = "Hardware scan requested..."
        
        # 1. Set Time Window (Now -> Now + 10min)
        now = datetime.now()
        self.start = now
        self.end = now + timedelta(minutes=8)

        # 2. Ensure ID (System or temp)
        if not self.expid: 
            self.expid = 'system'
            self.workdir = os.path.join(Config.WORKING_DIR, 'system')

        self.save() 
        
        message = { 
            'id' : self.expid,
            'action' : 'CHECK_HARDWARE'
        }
        
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)