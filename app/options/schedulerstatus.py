#!/usr/bin/env python3
"""
Scheduler Status Manager - Hardware Aware Version
Managed in RAM-disk (/run/) for cross-process synchronization.
"""
from datetime import datetime
import os
from flask import json

class SchedulerStatus(object):
    # Shared configuration
    status_file = "/run/chronoroot_scheduler_status.json"
    log = None
    scheduler = None

    # --- Class-Level Shared State ---
    # These reset only when the entire service restarts.
    # We use this dictionary as the SINGLE source of truth for hardware.
    hardware_health = {
        "boot_at": datetime.now().isoformat(),
        "last_picture_at": None,
        "multiplexer": "UNKNOWN",
        "cams": {1: "IDLE", 2: "IDLE", 3: "IDLE", 4: "IDLE"}
    }
    
    # Standard scheduler info
    jobs_info = {}
    status_running = False

    def __init__(self, scheduler=None, log=None):
        if scheduler is not None:
            self.scheduler = scheduler
        if log is not None:
            self.log = log
        # Sync current state from RAM-disk upon instantiation
        self.load()

    def load(self):
        """Loads data from RAM-disk into the class state"""
        if not os.path.exists(self.status_file):
            return
        try:
            with open(self.status_file, 'r') as f:
                data = json.load(f)
                
                # 1. Load Scheduler Data
                self.status_running = data.get("scheduler", {}).get("running", False)
                self.jobs_info = data.get("jobs", {})
                
                # 2. Load Hardware Data directly into the dictionary
                health = data.get("health", {})
                
                if "multiplexer" in health:
                    self.hardware_health["multiplexer"] = health["multiplexer"]
                
                if "last_picture" in health:
                    self.hardware_health["last_picture_at"] = health["last_picture"]

                # JSON converts keys to strings; convert them back to ints for consistency
                cams = health.get("cams", {})
                for k, v in cams.items():
                    try:
                        self.hardware_health["cams"][int(k)] = v
                    except ValueError:
                        pass

        except Exception as e:
            if self.log:
                self.log.error(f'Failed to load health status: {e}')

    def write(self):
        """Writes the current class state to the /run/ RAM-disk"""
        try:
            # Update local status from scheduler before writing
            if self.scheduler:
                self.status_running = self.scheduler.running

            output = {
                "scheduler": {
                    "running": self.status_running,
                    "last_update": datetime.now().isoformat()
                },
                "jobs": self.jobs_info,
                "health": {
                    "multiplexer": self.hardware_health["multiplexer"],
                    "cams": self.hardware_health["cams"],
                    "last_picture": self.hardware_health["last_picture_at"]
                }
            }
            with open(self.status_file, 'w') as f:
                json.dump(output, f, indent=2)
        except Exception as e:
            if self.log:
                self.log.error(f'Failed to write health status: {e}')
        
    def update_hardware_status(self, state=None, cam_id=None, cam_status=None, last_pic=False):
        """
        Updates the shared dictionary. 
        """
        if state:
            self.hardware_health["multiplexer"] = state
        if cam_id is not None and cam_status:
            self.hardware_health["cams"][int(cam_id)] = cam_status   
        if last_pic:
            self.hardware_health["last_picture_at"] = datetime.now().isoformat()
        
        self.write()

    def get_info(self):
        """Formatted dictionary for the Flask UI"""
        # Always reload before providing info to ensure UI sees recent hardware updates
        self.load()
        
        # Calculate Uptime (Convert string to datetime for math)
        try:
            boot_dt = datetime.fromisoformat(self.hardware_health['boot_at'])
            uptime_delta = datetime.now() - boot_dt
            total_seconds = int(uptime_delta.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"
        except (ValueError, TypeError):
            uptime_str = "Unknown"

        # Format Last Picture
        last_pic_str = "Never"
        if self.hardware_health["last_picture_at"]:
            try:
                lp_dt = datetime.fromisoformat(self.hardware_health["last_picture_at"])
                last_pic_str = lp_dt.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                last_pic_str = str(self.hardware_health["last_picture_at"])

        return {
            "status": "running" if self.status_running else "stopped",
            "uptime": uptime_str,
            "last_picture": last_pic_str,
            "multiplexer": self.hardware_health["multiplexer"],
            "cam_reports": self.hardware_health["cams"],
            "running_jobs": [eid for eid, info in self.jobs_info.items() if info.get("status") == "RUNNING"]
        }
        
    def update_from_scheduler(self):
        """Pulls real job data from the scheduler instance into the class storage"""
        if self.scheduler is not None:
            # Clear and rebuild to ensure we don't keep ghost jobs
            current_jobs = {}
            for job in self.scheduler.get_jobs():
                current_jobs[job.id] = {
                    'next_run_time': str(job.next_run_time) if job.next_run_time else None,
                    'trigger': str(job.trigger),
                    'status': 'RUNNING' if job.next_run_time else 'IDLE'
                }
            self.jobs_info = current_jobs
            
            # Update the running status
            self.status_running = self.scheduler.running

    def refresh_scheduler_status(self):
        """The entry point used by the Mule to sync everything"""
        self.update_from_scheduler()
        self.write()
        
    def remove_experiment(self, expid):
        """Removes an experiment from the jobs info and syncs to RAM-disk"""
        if expid in self.jobs_info:
            del self.jobs_info[expid]
            self.write()
        if self.log:
            self.log.info(f"Experiment {expid} removed from status.")
            
    def set_exp_status(self, expid, status):
        """Updates the status of a specific experiment and syncs to RAM-disk"""
        if expid in self.jobs_info:
            self.jobs_info[expid]['status'] = status
            self.write()