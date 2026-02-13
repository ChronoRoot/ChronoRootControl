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
        """        
        # Lazy import to avoid circular dependency
        from app.options.schedulerstatus import SchedulerStatus
        self.schedulerstatus = SchedulerStatus()

        # Defaults
        self.expid = None
        self.desc = ""
        self.status = "SETUP"
        self.message = ""
        
        # Times
        now_str = datetime.now().strftime(Config.PRETTY_FORMAT)
        self.creation = now_str
        self.modification = ""
        self._start = now_str
        self._end = now_str
        
        self.interval = 15
        self.steps_nb = 0
        self.next_run_time = ""
        self.workdir = ""
        
        # Mutable defaults
        self.cameras = []
        self.ir = False
        self.steps = []
        self.img_params = Config.CAM_PARAMS.copy() if Config.CAM_PARAMS else {}
        
        # Logs: Transient list, populated from log.txt
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
        
        if not os.path.exists(json_path):
            raise FileNotFoundError(f"Experiment {expid} not found at {json_path}")
            
        with open(json_path, 'r') as f:
            data = json.load(f)
            instance.from_dict(data)
        
        # Load logs from the text file into memory
        instance.load_logs()
            
        instance.status_update()
        return instance

    def load_logs(self):
        """
        Reads log.txt line by line and populates self.logs
        Format: YYYY-MM-DD HH:MM:SS | Description
        """
        self.logs = []
        if not self.workdir: return

        log_path = os.path.join(self.workdir, 'log.txt')
        if not os.path.exists(log_path):
            return

        try:
            with open(log_path, 'r') as f:
                lines = f.readlines()
                
            for index, line in enumerate(lines):
                # Split only on the first separator
                parts = line.strip().split(" | ", 1)
                if len(parts) == 2:
                    timestamp, description = parts
                    self.logs.append({
                        "id": index + 1,
                        "timestamp": timestamp,
                        "description": description
                    })
        except Exception as e:
            print(f"Error reading log file: {e}")

    def log_event(self, description):
        """
        1. Updates memory (self.logs) for immediate UI display.
        2. Appends to log.txt on disk.
        Does NOT touch info.json.
        """
        if not self.workdir:
            self.generate_id()

        timestamp = datetime.now().strftime(Config.PRETTY_FORMAT)
        
        # Update Memory
        self.logs.append({
            "id": len(self.logs) + 1,
            "timestamp": timestamp,
            "description": description
        })

        # Update Disk (Text File)
        log_path = os.path.join(self.workdir, 'log.txt')
        try:
            with open(log_path, 'a') as f:
                f.write(f"{timestamp} | {description}\n")
        except Exception as e:
            print(f"Failed to write to log.txt: {e}")

    # --- Properties & Helper Methods ---
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

    def _parse_date_string(self, date_str):
        if not date_str or not isinstance(date_str, str):
            return datetime.now()
        formats = [Config.PRETTY_FORMAT, "%Y-%m-%d %H:%M:%S%z"]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.replace(tzinfo=None) 
            except ValueError:
                continue
        return datetime.now()

    def _format_date_input(self, value):
        if isinstance(value, datetime):
            return value.strftime(Config.PRETTY_FORMAT)
        elif isinstance(value, str):
            dt = self._parse_date_string(value)
            return dt.strftime(Config.PRETTY_FORMAT)
        return value

    def status_update(self):
        self.schedulerstatus.load()
        if self.schedulerstatus.state and 'jobs' in self.schedulerstatus.state:
            if self.expid in self.schedulerstatus.state['jobs']:
                self.next_run_time = self.schedulerstatus.state['jobs'][self.expid]["next_run_time"]
            else:
                self.next_run_time = "Not in scheduler"

    def generate_id(self):
        if self.expid == 'system': return
        start_dt = self.start
        date_str = start_dt.strftime(Config.DATE_FORMAT)
        self.expid = "%s_%s" % (socket.gethostname(), date_str)
        self.workdir = os.path.join(Config.WORKING_DIR, self.expid)
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)

    def from_dict(self, data):
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def to_dict(self):
        """
        Serializes experiment data for info.json.
        """
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
            "img_params": self.img_params
        }

    def save(self):
        """
        Saves experiment METADATA to info.json.
        """
        if not self.expid:
            self.generate_id()
            
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)
            
        self.modification = datetime.now().strftime(Config.PRETTY_FORMAT)
        target_path = os.path.join(self.workdir, 'info.json')
        
        with open(target_path, 'w+') as f:
            json.dump(self.to_dict(), f, sort_keys=True, indent=4)
            f.flush() 
            os.fsync(f.fileno()) 

    def create(self):
        self.status = "NEW"
        self.log_event("Experiment created.")
        self.save() 
        
        message = {'id': self.expid, 'action': 'CREATE'}
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)

    def cancel(self):        
        message = {'id': self.expid, 'action': 'CANCEL'}
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)
        
        self.status = "CANCELLED"
        self.message = "Stop requested by user."
        self.log_event("Experiment cancelled by user.")
        self.save()
        self.status_update() 

    def delete(self):
        if self.status != "CANCELLED" or self.status != "FINISHED":
            if os.path.exists(self.workdir):
                shutil.rmtree(self.workdir)
                
    def diagnostic(self):
        self.status = "DIAGNOSTICS"
        self.message = "Hardware scan requested..."
        
        now = datetime.now()
        self.start = now
        self.end = now + timedelta(minutes=8)

        self.expid = 'system'
        self.workdir = os.path.join(Config.WORKING_DIR, 'system')

        self.save() 
        
        message = { 
            'id' : self.expid,
            'action' : 'CHECK_HARDWARE'
        }
        
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)