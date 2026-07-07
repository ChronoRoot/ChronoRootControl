import os
import shutil
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
        # Telemetry is loaded lazily (see the schedulerstatus property) so that
        # constructing an Experiment, e.g. when ExperimentList loads every folder,
        # does not trigger a full SchedulerStatus init (network identity probe +
        # possible RAM-file write) per experiment.
        self._schedulerstatus = None

        # Defaults
        self.expid = None
        self.name = ""
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
        self.next_run_time = ""
        self.workdir = ""
        
        # Mutable defaults
        self.cameras = []
        self.ir = False
        self.img_params = Config.CAM_PARAMS.copy() if Config.CAM_PARAMS else {}
        
        # Logs: Transient list, populated from log.txt
        self.logs = []

    @property
    def schedulerstatus(self):
        """Lazily build a read-only telemetry reader, cached per instance."""
        if self._schedulerstatus is None:
            # Lazy import to avoid circular dependency
            from app.options.schedulerstatus import SchedulerStatus
            self._schedulerstatus = SchedulerStatus.for_read()
        return self._schedulerstatus

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
        
    @property
    def expected_pictures(self):
        """Calculates total pictures based on time window and interval."""
        if not self.interval or int(self.interval) <= 0: 
            return 0
        duration_minutes = (self.end - self.start).total_seconds() / 60.0
        if duration_minutes < 0: 
            return 0
        return int(duration_minutes // int(self.interval)) + 1

    @expected_pictures.setter
    def expected_pictures(self, value):
        pass
    
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
                self.next_run_time = self.schedulerstatus.state['jobs'][self.expid].get("next_run_time", "Pending")
            else:
                self.next_run_time = "Not in scheduler"

    def generate_id(self):
        if self.expid == 'system': return
        start_dt = self.start
        date_str = start_dt.strftime(Config.DATE_FORMAT)
        
        # Enforce max 16 chars and strip special characters to prevent folder path breaks
        clean_name = "".join(c for c in str(self.name) if c.isalnum() or c == "_")[:16] if self.name else ""
        
        if clean_name:
            self.expid = f"{clean_name}_{socket.gethostname()}_{date_str}"
        else:
            self.expid = f"{socket.gethostname()}_{date_str}"
            
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
            "name": self.name,
            "desc": self.desc,
            "status": self.status,
            "message": self.message,
            "creation": self.creation,
            "modification": self.modification,
            "start":  self._start,
            "end": self._end,
            'interval': self.interval,
            "expected_pictures": self.expected_pictures,
            "cameras":  self.cameras,
            "ir": self.ir,
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
        if self.status in ("CANCELLED", "FINISHED"):
            return

        self.status = "CANCELLED"
        self.message = "Cancelled by user."
        self.log_event("Experiment cancelled by user.")
        self.save()

        from app.options.schedulerstatus import SchedulerStatus
        mgr = SchedulerStatus()
        mgr.load()
        cancelled = mgr.state.setdefault("cancelled_experiments", [])
        if self.expid not in cancelled:
            cancelled.append(self.expid)
        if self.expid in mgr.state.get("jobs", {}):
            del mgr.state["jobs"][self.expid]
        mgr.write()

        self.status_update()

        message = {'id': self.expid, 'action': 'CANCEL'}
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)

    def delete(self):
        if self.status in ("CANCELLED", "FINISHED"):
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
        
    def validate_rules(self, is_new=False):
        """
        Validates intrinsic business logic (dates, intervals, hardware, and storage).
        Returns (is_valid: bool, error_message: str)
        """
        # 1. Check interval
        if int(self.interval) < 5:
            return False, "Interval too short. Minimum time between pictures is 5 minutes."

        # 2. Check start time 
        if is_new and self.start < (datetime.now() + timedelta(minutes=1)):
             return False, "Start time must be at least 1 minute in the future."

        # 3. Check minimum duration
        min_duration = timedelta(minutes=5)
        if self.end < (self.start + min_duration):
            return False, "Duration too short. Experiment must run for at least 5 minutes."

        # 4. Check cameras 
        if not self.cameras or len(self.cameras) < 1:
            return False, "Select at least one camera."

        # =========================================================
        # 5. PREDICTIVE STORAGE CHECK
        # =========================================================
        # Estimate: 4MB per picture per camera
        total_pics_expected = self.expected_pictures
        num_cams = len(self.cameras)
        required_mb = 4.0 * total_pics_expected * num_cams
        required_gb = required_mb / 1024.0
        
        # Fetch the latest storage stats (live fallback when RAM cache is stale)
        self.schedulerstatus.load()
        storage_info = self.schedulerstatus.state.get("system_health", {}).get("storage", {})
        if storage_info.get("free_gb", 0) == 0 or storage_info.get("last_check") == "Never":
            try:
                from app.storage.stats import get_storage_stats
                free_gb = get_storage_stats()["free_gb"]
            except (FileNotFoundError, OSError):
                free_gb = 0
        else:
            free_gb = storage_info.get("free_gb", 0)
        
        # Check if we have enough space (leaving a 0.5 GB safety buffer for system logs)
        if free_gb > 0 and required_gb > (free_gb - 0.5):
            return False, f"Insufficient storage! This experiment will generate ~{round(required_gb, 2)} GB of data, but only {free_gb} GB is free."

        return True, ""

    def update(self):
        """
        Saves changes to an existing experiment and notifies the scheduler
        without resetting the status to NEW.
        """
        self.log_event("Experiment parameters updated.")
        self.save()
        
        # Send an UPDATE command instead of a CREATE command
        message = {'id': self.expid, 'action': 'UPDATE'}
        uwsgi.mule_msg(json.dumps(message), Config.MULE_NO)
    
    @classmethod
    def _count_images_in_dir(cls, cam_dir):
        if not os.path.isdir(cam_dir):
            return 0
        return len([f for f in os.listdir(cam_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))])

    @classmethod
    def get_archived_summary(cls, expid, allow_active=False):
        """
        Lightweight reader to fetch summary data for archived experiments directly 
        from disk, bypassing expensive log loading and scheduler status checks.
        """
        
        if expid == 'system':
            return None
        
        workdir = os.path.join(Config.WORKING_DIR, expid)
        json_path = os.path.join(workdir, 'info.json')
        
        if not os.path.exists(json_path):
            return None
        
        with open(json_path, 'r') as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                return None
        
        if not allow_active and data.get("status") not in ["FINISHED", "CANCELLED"]:
            return None        
        
        cameras = data.get("cameras", [])
        expected_pictures = data.get("expected_pictures", 0)
        per_camera = {}
        count_message = ""
        
        if not os.path.exists(workdir):
            count_message = "Workdir missing. Unable to count taken pictures."
        elif not cameras:
            count_message = "No cameras found. Unable to count taken pictures."
        else:
            for cam in cameras:
                cam_dir = os.path.join(workdir, str(cam))
                per_camera[str(cam)] = cls._count_images_in_dir(cam_dir)

        taken_pictures = per_camera.get(str(cameras[0]), 0) if cameras else 0
        any_taken = any(per_camera.values()) if per_camera else False
        all_ok = (
            bool(cameras)
            and expected_pictures > 0
            and all(per_camera.get(str(cam), 0) >= expected_pictures for cam in cameras)
        )
                
        return {
            "name": data.get("name"),
            "expid": data.get("expid"),
            "status": data.get("status"),
            "start": data.get("start"),
            "end": data.get("end"),
            "interval": data.get("interval"),
            "cameras": cameras,
            "expected_pictures": expected_pictures,
            "taken_pictures": taken_pictures,
            "per_camera": per_camera,
            "all_ok": all_ok,
            "any_taken": any_taken,
            "message": count_message
        }

    @staticmethod
    def format_completion_message(summary):
        """Build a human-readable finish message from get_archived_summary() output."""
        if not summary:
            return "Experiment finished."

        expected = summary.get("expected_pictures", 0)
        per_camera = summary.get("per_camera") or {}
        cameras = summary.get("cameras") or list(per_camera.keys())
        any_taken = summary.get("any_taken", False)
        all_ok = summary.get("all_ok", False)

        if not any_taken:
            return f"Experiment finished with no pictures captured. Expected {expected} per camera."
        if all_ok:
            ref_taken = per_camera.get(str(cameras[0]), summary.get("taken_pictures", 0))
            return f"Experiment finished successfully. {ref_taken}/{expected} pictures per camera."

        parts = [f"Cam{cam}: {per_camera.get(str(cam), 0)}/{expected}" for cam in cameras]
        return f"Experiment finished with issues. {', '.join(parts)}."