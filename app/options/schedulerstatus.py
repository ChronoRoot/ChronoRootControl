#!/usr/bin/env python3
"""
Scheduler Status Manager

This module manages the status of the scheduler, including loading and writing
status files, updating the status from the scheduler, and setting the status of
experiments.

Author: Thomas Blein
Date: 2025-05-31
"""
from datetime import datetime
from flask import json

# Define a class SchedulerStatus to manage the status of a scheduler
class SchedulerStatus(object):
    # Specify the file path for the status file
    status_file = "/run/chronoroot_scheduler_status.json"
    log = None
    
    # Initialize booting time
    boot_at = datetime.now()
    
    # Initialize the scheduler and jobs information
    scheduler = None
    jobs_info = {}
    status = {}

    # Constructor method to initialize the SchedulerStatus object
    def __init__(self, scheduler=None, log=None):
        # If a scheduler is provided, assign it to the object's scheduler attribute
        if scheduler is not None:
            self.scheduler = scheduler
        if log is not None:
            self.log = log
        # Load the status from the file
        self.load()

    # Method to load the status from the file
    def load(self):
        try:
            # Open the status file in read mode
            with open(self.status_file, 'r') as f:
                # Load the JSON data from the file
                status_json = json.load(f)
                # Update the object's status and jobs information
                self.status = status_json["scheduler"]
                self.jobs_info = status_json["jobs"]
        except Exception as e:
            # Log an error if loading the file fails
            if self.log is not None:
                self.log.error('Failed to load scheduler status file: %s' % e)

    # Method to write the status to the file
    def write(self):
        try:
            # Open the status file in write mode
            with open(self.status_file, 'w') as f:
                # Dump the JSON data to the file
                json.dump({"scheduler" : self.status,
                           "jobs" : self.jobs_info}, f, indent=2)
        except Exception as e:
            # Log an error if writing to the file fails
            if self.log is not None:
                self.log.error('Failed to write scheduler status file: %s' % e)

    # Method to update the status from the scheduler
    def update_from_scheduler(self):
        # Check if a scheduler is assigned to the object
        if self.scheduler is not None:
            # Iterate over each job in the scheduler
            for job in self.scheduler.get_jobs():
                # Check if the job is already in the jobs information
                if job.id in self.jobs_info:
                    # Update the job's next run time and trigger
                    self.jobs_info[job.id]['next_run_time'] = str(job.next_run_time) if job.next_run_time else None
                    self.jobs_info[job.id]['trigger'] = str(job.trigger)
                else:
                    # Add the job to the jobs information if it's not already there
                    self.jobs_info[job.id] = {
                        'next_run_time': str(job.next_run_time) if job.next_run_time else None,
                        'trigger': str(job.trigger),
                        'status' : 'RUNNING' if job.next_run_time else None
                    }
            # Update the scheduler's status
            self.status = {
                'running': self.scheduler.running,
                'last_update': datetime.now().isoformat()
            }

    # Method to set the status of an experiment
    def set_exp_status(self, expid, status):
        # Update the status from the scheduler
        self.refresh_scheduler_status()
        # Update the status of the experiment in the jobs information
        self.jobs_info[expid]['status'] = status

    # Method to refresh the scheduler status
    def refresh_scheduler_status(self):
        # Update the status from the scheduler
        self.update_from_scheduler()
        # Write the updated status to the file
        self.write()

    def remove_experiment(self, expid):
        del self.jobs_info[expid]
        self.refresh_scheduler_status()

    def get_info(self):
        uptime_delta = datetime.now() - self.boot_at
        # Convert delta to a simple string format: H:MM:SS
        total_seconds = int(uptime_delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        scheduler_info = {
            "status": "running" if self.status["running"] else "stopped",
            "last_update": self.status["last_update"],
            "uptime": uptime_str,  
            "running_jobs": [exp_id for exp_id, job_info in self.jobs_info.items() if job_info["status"] == "RUNNING"]
        }
        return scheduler_info
