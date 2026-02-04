#!/usr/bin/env python3
"""
Scheduler Status Manager - Unified State Version
Managed in RAM-disk (/run/) for cross-process synchronization.
"""
from datetime import datetime
import os
import logging
import json 

class SchedulerStatus(object):
    # Shared configuration
    status_file = "/run/chronoroot_scheduler_status.json"
    log = None
    scheduler = None

    # --- The Single Source of Truth ---
    # We initialize this with the default skeleton.
    # Any field added here is automatically supported by load/write.
    default_state = {
        "scheduler": {
            "running": False,
            "last_update": None,
            "uptime_start": datetime.now().isoformat() # Moved boot time here
        },
        "jobs": {},  # Stores all experiment job info
        "hardware": {
            "multiplexer": "UNKNOWN",
            "last_picture": None,
            "lock_info": {
                "status": "FREE", 
                "owner": None, 
                "details": None
            },
            "cams": {
                "1": {"health": "IDLE", "last_check": "N/A", "path": None},
                "2": {"health": "IDLE", "last_check": "N/A", "path": None},
                "3": {"health": "IDLE", "last_check": "N/A", "path": None},
                "4": {"health": "IDLE", "last_check": "N/A", "path": None}
            }
        }
    }

    def __init__(self, scheduler=None, log=None):
        self.scheduler = scheduler
        self.log = log
        # Deep copy default state to instance to avoid shared references
        self.state = json.loads(json.dumps(self.default_state))
        
        # Try to load existing state from disk
        self.load()

    # ------------------------------------------------------------------
    # GENERIC FILE HANDLING (Never needs modification)
    # ------------------------------------------------------------------
    
    def load(self):
        """
        Reads the JSON file and merges it into self.state.
        This function is 'schema-agnostic' - it accepts whatever is in the file.
        """
        if not os.path.exists(self.status_file):
            return

        try:
            with open(self.status_file, 'r') as f:
                disk_data = json.load(f)
                self.state.update(disk_data)
                
        except Exception as e:
            if self.log:
                self.log.error(f'Failed to load status: {e}')

    def write(self):
        """
        Dumps self.state to JSON.
        No matter what new keys you added to self.state, they get saved.
        """
        try:
            with open(self.status_file, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            if self.log:
                self.log.error(f'Failed to write status: {e}')

    # ------------------------------------------------------------------
    # SPECIFIC UPDATERS (Helpers to modify the dictionary cleanly)
    # ------------------------------------------------------------------

    def update_hardware_status(self, state=None, cam_id=None, cam_status=None, last_pic=False):
        """Updates hardware specific keys."""
        self.load() 

        if state:
            self.state["hardware"]["multiplexer"] = state
            
        if cam_id is not None and cam_status:
            cid = str(cam_id)
            if cid in self.state["hardware"]["cams"]:
                self.state["hardware"]["cams"][cid].update(cam_status)

        if last_pic:
            self.state["hardware"]["last_picture"] = datetime.now().isoformat()
        
        self.write()

    def update_lock_state(self, status="FREE", owner=None, details=None):
        """Updates lock info."""
        self.load()
        
        self.state["hardware"]["lock_info"] = {
            "status": status,
            "owner": owner,
            "details": details
        }
             
        self.write()

    def refresh_scheduler_status(self):
        """Syncs the internal scheduler object state to the dictionary."""
        if self.scheduler:
            # 1. Update Running State
            self.state["scheduler"]["running"] = self.scheduler.running
            self.state["scheduler"]["last_update"] = datetime.now().isoformat()

            # 2. Update Jobs
            current_jobs = {}
            for job in self.scheduler.get_jobs():
                current_jobs[job.id] = {
                    'next_run_time': str(job.next_run_time) if job.next_run_time else None,
                    'trigger': str(job.trigger),
                    'status': 'RUNNING' if job.next_run_time else 'IDLE'
                }
            self.state["jobs"] = current_jobs
            
            # 3. Write to disk
            self.write()

    def remove_experiment(self, expid):
        self.load()
        if expid in self.state["jobs"]:
            del self.state["jobs"][expid]
            self.write()
            if self.log: self.log.info(f"Experiment {expid} removed from status.")

    def set_exp_status(self, expid, status):
        self.load()
        if expid in self.state["jobs"]:
            self.state["jobs"][expid]['status'] = status
            self.write()

    # ------------------------------------------------------------------
    # UI FORMATTER
    # ------------------------------------------------------------------

    def get_info(self):
        """
        Returns the dictionary for the Flask template.
        Now it just returns self.state mostly as-is, plus calculated uptime.
        """
        self.load()
        
        data = self.state
        
        # Calculate Uptime
        uptime_str = "Unknown"
        try:
            boot_dt = datetime.fromisoformat(data["scheduler"]["uptime_start"])
            delta = datetime.now() - boot_dt
            # formatting helper
            m, s = divmod(int(delta.total_seconds()), 60)
            h, m = divmod(m, 60)
            uptime_str = f"{h}h {m}m {s}s"
        except:
            pass

        # Pretty print Last Picture
        last_pic_str = "Never"
        raw_lp = data["hardware"]["last_picture"]
        if raw_lp:
            try:
                last_pic_str = datetime.fromisoformat(raw_lp).strftime('%Y-%m-%d %H:%M:%S')
            except:
                last_pic_str = str(raw_lp)

        return {
            "status": "running" if data["scheduler"]["running"] else "stopped",
            "uptime": uptime_str,
            "last_picture": last_pic_str,
            "multiplexer": data["hardware"]["multiplexer"],
            "lock_info": data["hardware"]["lock_info"],
            "cam_reports": data["hardware"]["cams"],
            "running_jobs": [k for k, v in data["jobs"].items() if v.get("status") == "RUNNING"]
        }