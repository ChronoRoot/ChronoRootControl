#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import uwsgi
import logging
from datetime import datetime
from flask import json, Flask

# APScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.events import *

# App Modules
from config import Config

# --- FLASK CONTEXT FIX ---
app_flask = Flask(__name__)
app_flask.config.from_object(Config)
app_ctx = app_flask.app_context()
app_ctx.push()

from phototron.rpimodule import RpiModule
from app.experiment.models import Experiment
from app.experimentlist.models import ExperimentList 
from app.options.schedulerstatus import SchedulerStatus

logging.basicConfig(filename=Config.SHDL_LOG_FILE,
                    level=Config.LOG_LEVEL,
                    format=Config.LOG_FORMAT)
log = logging.getLogger(__name__)

# --- SCHEDULER SETUP ---
try:
    scheduler
except NameError:
    scheduler = BackgroundScheduler(executors={'default': ThreadPoolExecutor(2)})

try:
    scheduler_status
except NameError:
    scheduler_status = SchedulerStatus(scheduler, log)

# --- HELPER FUNCTIONS ---

def load_exp_safe(expid):
    try:
        return Experiment.load_from_id(expid)
    except FileNotFoundError:
        log.error(f"Scheduler Error: Experiment {expid} file not found.")
        return None

def end_experiment(exp):
    if not exp: return
    scheduler_status.remove_experiment(exp.expid)
    # check if status wasn't finished
    if exp.status != "FINISHED":
        exp.message = "Experiment completed."
        exp.status = "FINISHED"
        exp.save()

# --- SCHEDULER CALLBACKS ---

def shed_evt_job_executed(event):
    exp = load_exp_safe(event.job_id)
    if not exp: return

    job = scheduler.get_job(event.job_id)
    
    # If the job is finished (no next run time), mark as FINISHED
    if job is None or job.next_run_time is None:
        end_experiment(exp)
        return

    # If it was SCHEDULED, NEW, or ERROR, but it just ran successfully, set it to RUNNING.
    if exp.status in ["SCHEDULED", "NEW", "ERROR"]:
        exp.status = "RUNNING"
        exp.save()
        log.info(f"Experiment {exp.expid} recovered/started -> RUNNING")
    
    # Always refresh status so the UI shows the correct 'next_run_time'
    scheduler_status.refresh_scheduler_status()

def shed_evt_job_error(event):
    exp = load_exp_safe(event.job_id)
    if not exp: return

    error_msg = str(event.exception)
    exp.log_event(error_msg)
    
    if exp.status != "ERROR":
        timestamp = datetime.now().strftime(Config.PRETTY_FORMAT)
        exp.status = "ERROR"
        exp.message = f"Job Failed at {timestamp}: {error_msg}"
        exp.save()

    # 3. Update memory status
    scheduler_status.set_exp_status(event.job_id, "ERROR")
    log.error(f"Experiment {exp.expid} failed. Status set to ERROR.")

# Register Listeners
scheduler.add_listener(shed_evt_job_executed, EVENT_JOB_EXECUTED)
scheduler.add_listener(shed_evt_job_error, EVENT_JOB_ERROR | EVENT_JOB_MISSED)

# --- CHIEF OPERATOR ---

class ChiefOperator(object):
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def mainLoop():
        cop = ChiefOperator()
        try:
            cop.resync_with_disk()
            
            if not scheduler.running:
                cop.logger.info("Starting Scheduler...")
                scheduler.start()
                cop.logger.info("Scheduler Started.")
                
        except Exception as e:
            cop.logger.critical(f"CRASH during startup: {e}")
            # Prevent tight loop restart if it's a code error
            import time
            time.sleep(10)
            return 

        while True:
            try:
                xpid, action = cop.getMessage()
                cop.handle_experiment(xpid, action)
            except Exception as e:
                cop.logger.error(f"MainLoop Error: {e}")

    def getMessage(self):
        while True:
            try:
                msg_raw = uwsgi.mule_get_msg()
                if not msg_raw: continue 
                message = json.loads(msg_raw)
                return message.get('id'), message.get('action')
            except Exception as e:
                self.logger.error(f"Message Decode Error: {e}")

    def handle_experiment(self, xpid, action):
        self.logger.info(f"Handling: {action} on {xpid}")
        try:
            if action == 'CREATE':
                self.schedule_job(xpid)
            elif action == 'CANCEL':
                self.cancel_job(xpid)
            elif action == 'CHECK_HARDWARE':
                self.check_hardware(xpid)
            else:
                self.logger.warning(f"Unknown action: {action}")
        except Exception as e:
            self.logger.error(f"Handle Error: {e}")

    def resync_with_disk(self):
        self.logger.info("--- Resyncing Scheduler with Disk ---")
        exp_list = ExperimentList()
        running_jobs = {job.id for job in scheduler.get_jobs()}
        
        for exp in exp_list.exps:
            # IGNORE SYSTEM / DIAGNOSTICS
            if exp.expid == 'system' or exp.status == 'DIAGNOSTICS':
                continue

            if exp.status in ['CANCELLED', 'FINISHED']:
                continue

            now = datetime.now()

            if exp.end < now:
                if exp.status != 'FINISHED':
                    self.logger.info(f"Exp {exp.expid} expired while offline. Marking FINISHED.")
                    exp.log_event("Experiment expired while offline. Marking as FINISHED.")
                    exp.status = "FINISHED"
                    exp.save()
                continue

            if exp.expid not in running_jobs:
                self.logger.info(f"Restoring job: {exp.expid}")
                exp.log_event("Resync after reboot: Job restored to scheduler.")
                self.schedule_job(exp.expid)
            else:
                self.logger.info(f"Job {exp.expid} is already active.")
        
        self.logger.info("--- Resync Complete ---")

    def schedule_job(self, exp_id):
        exp = load_exp_safe(exp_id)
        if not exp: return

        start_dt = exp.start
        end_dt = exp.end
        now = datetime.now()
        
        # 1. Past check
        if end_dt < now:
            self.logger.warning(f"Cannot schedule {exp_id}: End time is in the past.")
            end_experiment(exp)
            return

        # 2. Add to Scheduler (This is the "Retry" part)
        # We add it regardless of whether it was ERROR or RUNNING before.
        scheduler.add_job(
            RpiModule.take_picture,
            args=(exp_id, scheduler_status),
            trigger='interval',
            start_date=start_dt,
            end_date=end_dt,
            minutes=int(exp.interval),
            id=exp.expid,
            replace_existing=True
        )
        
        if start_dt > now:
            if exp.status != "SCHEDULED":
                exp.status = "SCHEDULED"
                exp.save()
                self.logger.info(f"Scheduled {exp_id} (Future)")
        else:
            if exp.status == "NEW": 
                exp.status = "RUNNING"
                exp.save()
            
            self.logger.info(f"Rescheduled {exp_id} (Active Window). Waiting for execution to confirm status.")

        if scheduler.running:
            scheduler_status.refresh_scheduler_status()

    def cancel_job(self, expid):
        if scheduler.get_job(expid):
            scheduler.remove_job(expid)
            
        exp = load_exp_safe(expid)
        if exp:
            exp.status = "CANCELLED"
            exp.message = "Cancelled by user."
            exp.save()
            
        scheduler_status.remove_experiment(expid)
        self.logger.info(f"Cancelled {expid}")

    def check_hardware(self, xpid):
        """
        Run the hardware check IMMEDIATELY (Bypassing APScheduler).
        """
        self.logger.info("Running Hardware Diagnostic (Immediate)...")
        RpiModule.check_cameras(status_manager=scheduler_status)
        exp = load_exp_safe(xpid)
        if exp:
            exp.status = "FINISHED"
            exp.save()

if __name__ == '__main__':
    ChiefOperator.mainLoop()