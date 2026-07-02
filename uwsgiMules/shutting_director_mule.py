#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import uwsgi
import logging
import time
from datetime import datetime, timedelta
from flask import json, Flask
import threading

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
from app.sync.manager import run_rclone_sync
import subprocess
from app.options.config_manager import save_user_config

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

    if exp.status not in ["FINISHED", "CANCELLED"]:
        summary = Experiment.get_archived_summary(exp.expid, allow_active=True)
        if summary:
            completion_msg = Experiment.format_completion_message(summary)
            exp.log_event(completion_msg)
            exp.message = completion_msg
        exp.status = "FINISHED"
        exp.save()

    scheduler_status.remove_experiment(exp.expid)

# --- SCHEDULER CALLBACKS ---

def shed_evt_job_executed(event):
    exp = load_exp_safe(event.job_id)
    if not exp: return

    job = scheduler.get_job(event.job_id)
    
    if job is None or job.next_run_time is None:
        end_experiment(exp)
        return

    if exp.status in ["SCHEDULED", "NEW", "ERROR"]:
        exp.status = "RUNNING"
        exp.save()
        log.info(f"Experiment {exp.expid} recovered/started -> RUNNING")
    
    scheduler_status.refresh_scheduler_status()

def shed_evt_job_error(event):
    exp = load_exp_safe(event.job_id)
    if not exp: return

    # Safely get the error message (handles EVENT_JOB_MISSED lacking an exception)
    error_msg = str(getattr(event, 'exception', 'Job missed scheduled run.'))
    exp.log_event(error_msg)
    
    if exp.status != "ERROR":
        timestamp = datetime.now().strftime(Config.PRETTY_FORMAT)
        exp.status = "ERROR"
        exp.message = f"Job Failed at {timestamp}: {error_msg}"
        exp.save()

    scheduler_status.set_exp_status(event.job_id, "ERROR")
    log.error(f"Experiment {exp.expid} failed. Status set to ERROR.")

    # --- THE FIX: Mark as finished if the time has come ---
    job = scheduler.get_job(event.job_id)
    if job is None or job.next_run_time is None:
        end_experiment(exp)

scheduler.add_listener(shed_evt_job_executed, EVENT_JOB_EXECUTED)
scheduler.add_listener(shed_evt_job_error, EVENT_JOB_ERROR | EVENT_JOB_MISSED)

class AutoSyncDaemon(threading.Thread):
    """
    A standalone background worker that loops infinitely.
    Writes its exact future intentions directly to the RAM-disk state.
    """
    def __init__(self):
        super().__init__(daemon=True)
        self.logger = logging.getLogger(__name__)

    def run(self):
        self.logger.info("AutoSyncDaemon initialized.")
        while True:
            try:
                # 1. Read live config on every loop
                is_enabled = getattr(Config, 'SYNC_ENABLED', False)
                interval_mins = int(getattr(Config, 'SYNC_INTERVAL', 60))
                
                # 2. Write enabled status to shared RAM
                scheduler_status.load()
                scheduler_status.state["sync"]["sync_enabled"] = is_enabled
                scheduler_status.write()
                
                if is_enabled:
                    self.logger.info("Auto-Sync triggered.")
                    
                    # This blocks until the sync is totally finished
                    run_rclone_sync() 
                    
                    # 3. Calculate and publish exact next wake-up time
                    now = datetime.now()
                    next_time = now + timedelta(minutes=interval_mins)
                    
                    scheduler_status.load()
                    scheduler_status.state["sync"]["next_sync"] = next_time.strftime(Config.PRETTY_FORMAT)
                    scheduler_status.write()
                    
                    # 4. Sleep for the exact interval
                    time.sleep(interval_mins * 60)
                else:
                    # 5. If disabled, tell the UI and sleep for a minute to check again
                    scheduler_status.load()
                    scheduler_status.state["sync"]["next_sync"] = "Disabled"
                    scheduler_status.write()
                    
                    time.sleep(60)
                    
            except Exception as e:
                self.logger.error(f"AutoSyncDaemon Error: {e}")
                time.sleep(60)

from app.storage.stats import get_storage_stats

class SystemHealthDaemon(threading.Thread):
    """
    Background worker that runs every 15 minutes.
    Checks disk space and updates expected picture counts in RAM.
    """
    def __init__(self):
        super().__init__(daemon=True)
        self.logger = logging.getLogger(__name__)

    def run(self):
        self.logger.info("SystemHealthDaemon initialized.")
        while True:
            now = datetime.now()

            # 1. Update storage in its own try block so job-loop errors cannot block it.
            try:
                scheduler_status.load()
                stats = get_storage_stats()
                scheduler_status.state.setdefault("system_health", {})["storage"] = {
                    "total_gb": stats["total_gb"],
                    "free_gb": stats["free_gb"],
                    "percent_used": stats["percent_used"],
                    "used_gb": stats["used_gb"],
                    "last_check": now.strftime(Config.PRETTY_FORMAT),
                }
                scheduler_status.write()
            except Exception as e:
                self.logger.error(f"Storage update failed: {e}")

            # 2. Update expected_so_far and camera gaps (separate try block).
            try:
                scheduler_status.load()
                cancelled = scheduler_status.state.get("cancelled_experiments", [])
                camera_gaps = []
                gap_logged = scheduler_status.state.setdefault("hardware", {}).setdefault("camera_gap_logged", {})
                allowance_secs = Config.PER_CAMERA_ALLOWANCE * 60

                for expid, job_data in scheduler_status.state.get("jobs", {}).items():
                    if expid in cancelled:
                        continue
                    start_str = job_data.get("start")
                    interval = job_data.get("interval")
                    
                    if start_str and interval and "progress" in job_data:
                        try:
                            start_dt = datetime.strptime(start_str, Config.PRETTY_FORMAT)
                            if now >= start_dt:
                                elapsed_mins = (now - start_dt).total_seconds() / 60.0
                                expected_so_far = int(elapsed_mins // int(interval)) + 1
                                
                                total_expected = job_data["progress"].get("expected", expected_so_far)
                                job_data["progress"]["expected_so_far"] = min(expected_so_far, total_expected)
                        except Exception:
                            pass

                    prog = job_data.get("progress", {})
                    expected_so_far = prog.get("expected_so_far", 0)
                    if expected_so_far < 2:
                        continue
                    workdir = os.path.join(Config.WORKING_DIR, str(expid))
                    if not os.path.isdir(workdir):
                        continue
                    for cam_name in os.listdir(workdir):
                        if not cam_name.isdigit():
                            continue
                        cam_dir = os.path.join(workdir, cam_name)
                        if not os.path.isdir(cam_dir):
                            continue
                        files = [f for f in os.listdir(cam_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
                        actual = len(files)
                        behind = expected_so_far - actual
                        if behind < 2:
                            continue
                        stale = True
                        if files:
                            latest = max(files, key=lambda x: os.path.getmtime(os.path.join(cam_dir, x)))
                            mtime = os.path.getmtime(os.path.join(cam_dir, latest))
                            stale = (now.timestamp() - mtime) > allowance_secs
                        if not stale:
                            continue
                        camera_gaps.append({"expid": expid, "cam": int(cam_name), "behind_by": behind})
                        log_key = f"{expid}_{cam_name}"
                        if log_key not in gap_logged:
                            exp = load_exp_safe(expid)
                            if exp:
                                exp.log_event(f"Camera {cam_name} stalled: {behind} pictures behind schedule")
                            gap_logged[log_key] = True

                scheduler_status.state["hardware"]["camera_gaps"] = camera_gaps
                scheduler_status.state["hardware"]["camera_gap_logged"] = gap_logged
                scheduler_status.write()
            except Exception as e:
                self.logger.error(f"SystemHealthDaemon job update failed: {e}")

            # Sleep for 15 minutes
            time.sleep(900)

class HardwareWatchdog(threading.Thread):
    """
    Sole authority for system reboots. Polls every 60s via get_info().
    Circuit breaker: max 3 reboots per 6 hours.
    """
    def __init__(self):
        super().__init__(daemon=True)
        self.logger = logging.getLogger(__name__)
        self.guard_file = os.path.join(Config.WORKING_DIR, "watchdog_reboot_dates.txt")
        self.reboot_window_secs = 21600

    def run(self):
        self.logger.info("Hardware Watchdog initialized.")
        while True:
            try:                
                scheduler_status.update_identity()
                sys_info = scheduler_status.get_info()
                alerts = sys_info.get("alerts", {})
                all_cam_fail = sys_info.get("all_cameras_failed")

                expid = None
                reason = None

                if all_cam_fail:
                    # Total camera failure (e.g. mux/I2C outage) — take_picture sets this flag
                    expid = all_cam_fail.get("expid")
                    reason = "All cameras failed"
                elif alerts.get("lock_stuck") and alerts.get("picture_overdue"):
                    details = sys_info.get("lock_info", {}).get("details") or ""
                    if details.startswith("Exp "):
                        expid = details[4:].strip()
                    reason = "Hardware lock stuck with overdue capture"
                else:
                    time.sleep(60)
                    continue

                self.logger.critical(f"WATCHDOG: Reboot trigger — {reason}. Initiating recovery protocol.")
                self.handle_stuck_hardware(expid, reason)
                time.sleep(60)
                        
            except Exception as e:
                self.logger.error(f"Watchdog Error: {e}")
                time.sleep(60)

    def handle_stuck_hardware(self, expid=None, reason="Hardware recovery"):
        recent_reboots = 0
        now = datetime.now()
        
        if os.path.exists(self.guard_file):
            try:
                with open(self.guard_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                dt = datetime.strptime(line, Config.PRETTY_FORMAT)
                                if (now - dt).total_seconds() < self.reboot_window_secs:
                                    recent_reboots += 1
                            except Exception: 
                                pass
            except Exception as e:
                self.logger.error(f"Watchdog Failed to read log: {e}")

        if recent_reboots >= 3:
            self.logger.critical("WATCHDOG FATAL: Max reboot limit (3 in 6h) reached! Hardware requires manual human intervention.")
            time.sleep(3600)
            return

        if expid:
            exp = load_exp_safe(expid)
            if exp:
                exp.log_event(
                    f"Watchdog initiated system reboot (strike {recent_reboots + 1}/3 in last 6h): {reason}"
                )

        scheduler_status.load()
        if "hardware" in scheduler_status.state:
            scheduler_status.state["hardware"]["all_cameras_failed"] = None
            scheduler_status.write()

        if not os.path.exists(Config.WORKING_DIR):
            os.makedirs(Config.WORKING_DIR)
            
        try:
            with open(self.guard_file, 'a') as f:
                f.write(f"{now.strftime(Config.PRETTY_FORMAT)}\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            self.logger.error(f"Watchdog Failed to write log: {e}")

        self.logger.critical(f"WATCHDOG: Executing sudo reboot now. (Strike {recent_reboots + 1} of 3)")
        subprocess.run(["sudo", "reboot"])
               
# --- CHIEF OPERATOR ---

class ChiefOperator(object):
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def mainLoop():
        cop = ChiefOperator()

        while True:
            try:
                # During startup, we wait for the OS time to be accurate before resyncing the scheduler with disk.
                
                if getattr(Config, 'USE_NTP', False):
                    # Loop up to 10 times (10 seconds max on a cold boot)
                    for i in range(10):
                        try:
                            # Ask systemd if the clock is officially synced
                            result = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized"], capture_output=True, text=True)
                            
                            if "yes" in result.stdout:
                                # OS time is accurate! Break immediately.
                                # On a normal UWSGI restart, this skips the wait instantly (0 delay).
                                break
                        except Exception as e:
                            pass
                            
                        cop.logger.warning(f"Waiting for OS time sync to finish... ({i+1}/10)")
                        time.sleep(1)                
                
                cop.resync_with_disk()
                
                if not scheduler.running:
                    cop.logger.info("Starting Scheduler...")
                    scheduler.start()
                    cop.logger.info("Scheduler Started.")
                    scheduler_status.refresh_scheduler_status()
                
                # Start the isolated Sync Daemon
                AutoSyncDaemon().start()
                SystemHealthDaemon().start()
                HardwareWatchdog().start()
                
                break 
                
            except Exception as e:
                cop.logger.critical(f"CRASH during startup: {e}. Retrying in 10s...")
                time.sleep(10)

        while True:
            try:
                message = cop.getMessage()
                if message and message.get('action'):
                    cop.handle_experiment(message)
            except Exception as e:
                cop.logger.error(f"MainLoop Error: {e}")

    def getMessage(self):
        while True:
            try:
                msg_raw = uwsgi.mule_get_msg()
                if not msg_raw: continue 
                return json.loads(msg_raw)
            except Exception as e:
                self.logger.error(f"Message Decode Error: {e}")

    def handle_experiment(self, message):
        xpid = message.get('id')
        action = message.get('action')
        self.logger.info(f"Handling: {action} on {xpid}")
        try:
            if action == 'CREATE':
                self.schedule_job(xpid)
            elif action == 'CANCEL':
                self.cancel_job(xpid)
            elif action == 'CHECK_HARDWARE':
                self.check_hardware(xpid)
            elif action == 'TEST_CAMERA':
                RpiModule.test_single_camera(
                    cam_id=message.get('cam_id'),
                    status_manager=scheduler_status,
                    use_ir=message.get('use_ir')
                )
            elif action == 'TEST_LIGHTS':
                RpiModule.test_camera_lights(
                    cam_id=message.get('cam_id'),
                    status_manager=scheduler_status
                )
            elif action == 'SYNC':
                # Manual sync trigger
                sync_thread = threading.Thread(target=run_rclone_sync)
                sync_thread.daemon = True
                sync_thread.start()
            elif action == 'CANCEL_SYNC':
                self.logger.warning("Emergency Kill Signal received! Terminating rclone...")
                # 1. Brute-force kill the network subprocess
                subprocess.Popen(['pkill', '-9', '-f', 'rclone'])
                # 2. Disable Auto-Sync in live memory so the Daemon stops immediately
                Config.SYNC_ENABLED = False
                # 3. Save to disk so it survives a reboot
                save_user_config({'SYNC_ENABLED': False})
                self.logger.info("Auto-sync automatically disabled due to kill switch.")
            else:
                self.logger.warning(f"Unknown action: {action}")
        except Exception as e:
            self.logger.error(f"Handle Error: {e}")

    def resync_with_disk(self):
        self.logger.info("--- Resyncing Scheduler with Disk ---")
        exp_list = ExperimentList()
        running_jobs = {job.id for job in scheduler.get_jobs()}
        
        for exp in exp_list.exps:
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
                
        # Notice we removed the SYNC job injection here. The daemon handles it now.
        self.logger.info("--- Resync Complete ---")

    def schedule_job(self, exp_id):
        exp = load_exp_safe(exp_id)
        if not exp: return

        start_dt = exp.start
        end_dt = exp.end
        now = datetime.now()
        
        # =========================================================
        # 1. PHYSICAL FILE COUNT & MISSED FRAME DETECTION
        # =========================================================
        actual_pics = 0
        
        # A. Count physical files on disk (Use first camera as reference)
        if os.path.exists(exp.workdir) and exp.cameras:
            cam_dir = os.path.join(exp.workdir, str(exp.cameras[0]))
            if os.path.exists(cam_dir):
                actual_pics = len([f for f in os.listdir(cam_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))])

        # B. Calculate expected pictures up to THIS EXACT MOMENT
        if now >= start_dt:
            # If the experiment ended while offline, cap the calculation at end_dt
            calc_end = min(now, end_dt)
            elapsed_minutes = (calc_end - start_dt).total_seconds() / 60.0
            
            # Plus 1 because the first shot triggers at minute 0
            expected_so_far = int(elapsed_minutes // int(exp.interval)) + 1 
            
            missed_pics = expected_so_far - actual_pics
            
            # Log if there is a discrepancy
            if missed_pics > 0:
                msg = f"System recovered. {actual_pics} frames found, {expected_so_far} expected up to now. (~{missed_pics} frames missed due to downtime)."
                self.logger.warning(f"[{exp.expid}] {msg}")
                exp.log_event(msg) # Writes to log.txt so it shows in the UI

        # =========================================================
        # 2. RAM STATE REGISTRATION
        # =========================================================
        scheduler_status.register_job_metadata(exp.expid, exp.name, exp.expected_pictures, starting_count=actual_pics, start_str=exp._start, interval=exp.interval, end_str=exp._end)
        
        # =========================================================
        # 3. APSCHEDULER INJECTION
        # =========================================================
        if end_dt < now:
            self.logger.warning(f"Cannot schedule {exp_id}: End time is in the past.")
            end_experiment(exp)
            return

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
        exp = load_exp_safe(expid)
        if exp:
            exp.status = "CANCELLED"
            exp.message = "Cancelled by user."
            exp.save()

        scheduler_status.load()
        cancelled = scheduler_status.state.setdefault("cancelled_experiments", [])
        if expid not in cancelled:
            cancelled.append(expid)

        if scheduler.get_job(expid):
            scheduler.remove_job(expid)
            
        scheduler_status.remove_experiment(expid)
        self.logger.info(f"Cancelled {expid}")

    def check_hardware(self, xpid):
        self.logger.info("Running Hardware Diagnostic (Immediate)...")
        exp = load_exp_safe(xpid)
        exp.status = "DIAGNOSTICS"
        exp.save()
            
        scheduler_status.register_job_metadata(
            expid=exp.expid, 
            name="System Diagnostic", 
            expected=1,        # Give it a dummy expected count
            starting_count=0, 
            start_str=exp._start, 
            interval=1,        # Dummy interval
            end_str=exp._end
        )
        
        # Force the RAM status to RUNNING so the UI counts it as active
        scheduler_status.set_exp_status(exp.expid, "RUNNING")
        
        try:
                # 1. Run the actual physical check
                results = RpiModule.check_cameras(status_manager=scheduler_status)
                # 2. Write the global success AND the detailed snapshot to RAM
                has_errors = any(data.get("health") != "OK" for data in results.values())
                global_state = "PARTIAL/FAIL" if has_errors else "PASS"
                msg = "Scan complete. Some cameras failed." if has_errors else "All cameras responding."
                
                scheduler_status.update_diagnostic_result(global_state, msg, detailed_results=results)
        except Exception as e:
            scheduler_status.update_diagnostic_result("FAIL", f"Error during scan: {str(e)}")
            self.logger.error(f"Hardware Check Failed: {e}")
        
        # remove it from ram
        scheduler_status.remove_experiment("system")
        
        exp = load_exp_safe(xpid)
        if exp:
            exp.status = "FINISHED"
            exp._end = datetime.now().strftime(Config.PRETTY_FORMAT)
            exp.save()
            
if __name__ == '__main__':
    ChiefOperator.mainLoop()