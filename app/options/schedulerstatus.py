#!/usr/bin/env python3
"""
Scheduler Status Manager - Unified State Version
Managed in RAM-disk (/run/) for cross-process synchronization.
"""
from datetime import datetime, timedelta
import os
import json
import socket
import uuid
from config import Config
import fcntl
import time
from app.storage.stats import get_storage_stats

class SchedulerStatus(object):
    # Shared configuration
    status_file = "/run/chronoroot_scheduler_status.json"
    log = None
    scheduler = None
    last_load_retries = 0

    # --- The Single Source of Truth ---
    # We initialize this with the default skeleton.
    # Any field added here is automatically supported by load/write.
    default_state = {
        "identity": {
            "hostname": "UNKNOWN",
            "ip": "UNKNOWN",
            "mac": "UNKNOWN"
        },
        "system_health": {
            "storage": {
                "total_gb": 0,
                "free_gb": 0,
                "percent_used": 0,
                "last_check": "Never"
            }
        },
        "scheduler": {
            "running": False,
            "last_update": None,
            "uptime_start": "",
            "next_picture": None  
        },
        "jobs": {}, 
        "cancelled_experiments": [],
        "hardware": {
            "last_picture": None,
            "all_cameras_failed": None,
            "camera_gaps": [],
            "camera_gap_logged": {},
            "lock_info": {
                "status": "FREE", 
                "owner": None, 
                "details": None,
                "acquired_at": None  
            },
            "cams": {},
            "lights": {
                "state": "OFF",
                "health_check": {
                    "last_test": "Never",
                    "status": "UNTESTED"
                }
            }
        },
        "sync": {
            "is_syncing": False,
            "status_msg": "Idle",
            "last_success": None,
            "last_start": None,
            "last_error": None,     
            "next_sync": None,      
            "sync_enabled": False  
        }
    }

    def __init__(self, scheduler=None, log=None, read_only=False):
        self.scheduler = scheduler
        self.log = log

        # 1. Start with defaults
        self.state = json.loads(json.dumps(self.default_state))

        # 2. Load disk state into RAM
        self.load()

        # Read-only callers (e.g. GET /api/status) only need the loaded snapshot.
        # They must NOT fetch network identity or write back to the RAM file, which
        # adds latency and lock contention to every status poll. The mule keeps the
        # identity and bootstrap fields fresh via update_identity()/normal __init__.
        if read_only:
            return

        needs_write = False

        old_identity = self.state.get("identity", {})
        self._fetch_system_identity()
        if self.state["identity"] != old_identity:
            needs_write = True

        # 3. Dynamically sync cameras based on the config file
        configured_cams = [str(c) for c in getattr(Config, 'CAMS', (1,))]
        
        # A. Add new cameras
        for cam_str in configured_cams:
            if cam_str not in self.state["hardware"]["cams"]:
                self.state["hardware"]["cams"][cam_str] = {
                    "health": "UNTESTED", "activity": "IDLE",
                    "last_check": "N/A", "path": None
                }
                needs_write = True
            elif "activity" not in self.state["hardware"]["cams"][cam_str]:
                self.state["hardware"]["cams"][cam_str]["activity"] = "IDLE"
                needs_write = True
            elif self.state["hardware"].get("lock_info", {}).get("status") == "FREE":
                if self.state["hardware"]["cams"][cam_str].get("activity") != "IDLE":
                    self.state["hardware"]["cams"][cam_str]["activity"] = "IDLE"
                    needs_write = True

        # B. Remove ghost cameras (ones that exist in state but were removed from Config)
        existing_cams = list(self.state["hardware"]["cams"].keys())
        for cam_str in existing_cams:
            if cam_str not in configured_cams:
                del self.state["hardware"]["cams"][cam_str]
                needs_write = True

        # 4. Handle Uptime
        if not self.state["scheduler"].get("uptime_start"):
            try:
                ts = datetime.now().strftime(Config.PRETTY_FORMAT)
            except Exception:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.state["scheduler"]["uptime_start"] = ts
            needs_write = True
        
        # 5. Lights status
        if "lights" not in self.state["hardware"]:
            self.state["hardware"]["lights"] = {
                "state": "OFF",
                "health_check": {
                    "last_test": "Never",
                    "status": "UNTESTED"
                }
            }
            needs_write = True

        # 7. Commit to disk if anything changed (skip if load failed — avoid clobbering good data with defaults)
        if needs_write and getattr(self, '_load_ok', True):
            self.write()
            
    @classmethod
    def for_read(cls):
        """
        Lightweight constructor for read-only consumers (e.g. GET /api/status).

        Loads the RAM-disk snapshot once and skips the network identity probe,
        camera reconciliation, and any write-back. Pair with get_info(reload=False)
        to serve a status request with a single file read.
        """
        return cls(read_only=True)

    def update_identity(self):
        """
        Refreshes hostname/IP/MAC in the RAM-disk state, writing only on change.

        Intended to be called periodically by the mule so HTTP status reads never
        have to perform the (potentially slow) network identity probe themselves.
        """
        self.load()
        old_identity = self.state.get("identity", {})
        self._fetch_system_identity()
        if self.state["identity"] != old_identity:
            self.write()

    def _fetch_system_identity(self):
        """Fetches and updates the hostname, IP address, and MAC address."""
        # 1. Hostname
        hostname = socket.gethostname()
        
        # 2. MAC Address (Formats the raw integer into XX:XX:XX:XX:XX:XX)
        mac_num = uuid.getnode()
        mac = ':'.join(('%012X' % mac_num)[i:i+2] for i in range(0, 12, 2))
        
        # 3. IP Address (Connects a dummy UDP socket to find the active routing IP)
        ip = "127.0.0.1"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass # Fallback remains 127.0.0.1 if completely offline

        # Update state directly
        self.state["identity"] = {
            "hostname": hostname,
            "ip": ip,
            "mac": mac
        }

    # ------------------------------------------------------------------
    # GENERIC FILE HANDLING (Never needs modification)
    # ------------------------------------------------------------------
    
    def load(self):
        """
        Reads the JSON file with a non-blocking shared lock and a retry system.
        """
        # Number of lock-contention retries the last load() had to perform.
        # Exposed so callers (e.g. /api/status) can report contention.
        self.last_load_retries = 0
        self._load_ok = True
        if not os.path.exists(self.status_file):
            return

        max_retries = 10
        for attempt in range(max_retries):
            try:
                with open(self.status_file, 'r') as f:
                    # Request a Shared Lock. Non-Blocking throws an error if a writer has it.
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    try:
                        disk_data = json.load(f)
                        self.state.update(disk_data)
                        self.last_load_retries = attempt
                        return  # Success, exit the retry loop
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
                        
            except (BlockingIOError, IOError):
                # File is currently being written to. Sleep 50ms and try again.
                time.sleep(0.05)
            except json.JSONDecodeError:
                # Edge case: Caught it mid-write. Sleep and retry.
                time.sleep(0.05)
                
        self._load_ok = False
        if self.log:
            self.log.warning('Status file was locked or corrupted; skipping load this cycle to prevent hang.')

    def _ensure_storage_stats(self, data):
        """Fill storage from live disk usage when the RAM cache is still at defaults."""
        storage = data.setdefault("system_health", {}).setdefault("storage", {})
        if storage.get("last_check") != "Never" and storage.get("total_gb", 0) > 0:
            return
        try:
            stats = get_storage_stats()
            storage.update({
                "total_gb": stats["total_gb"],
                "used_gb": stats["used_gb"],
                "free_gb": stats["free_gb"],
                "percent_used": stats["percent_used"],
                "last_check": datetime.now().strftime(Config.PRETTY_FORMAT),
            })
        except (FileNotFoundError, OSError):
            pass

    def write(self):
        """
        Writes to the JSON file with an exclusive lock, safely avoiding the 'w' truncation trap.
        """
        max_retries = 10
        for attempt in range(max_retries):
            try:
                # Open with 'a+' (append + read) so we DO NOT truncate the file before locking it.
                with open(self.status_file, 'a+') as f:
                    # Request an Exclusive Lock. Non-Blocking throws an error if ANYONE else is using it.
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        # Now that we own the lock, we can safely clear the file and write
                        f.seek(0)
                        f.truncate()
                        json.dump(self.state, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                        return  # Success, exit the retry loop
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
                        
            except (BlockingIOError, IOError):
                # File is currently in use. Sleep 50ms and try again.
                time.sleep(0.05)
                
        if self.log:
            self.log.error('CRITICAL: Could not acquire lock to write scheduler status after max retries.')

    # ------------------------------------------------------------------
    # SPECIFIC UPDATERS (Helpers to modify the dictionary cleanly)
    # ------------------------------------------------------------------

    def update_hardware_status(self, cam_id=None, cam_status=None, last_pic=False):
        """Updates per-camera hardware keys."""
        self.load()

        if cam_id is not None and cam_status:
            cid = str(cam_id)
            if cid in self.state["hardware"]["cams"]:
                self.state["hardware"]["cams"][cid].update(cam_status)

        if last_pic:
            self.state["hardware"]["last_picture"] = datetime.now().strftime(Config.PRETTY_FORMAT)
        
        self.write()

    def update_lock_state(self, status="FREE", owner=None, details=None):
        """Updates lock info and records the exact time it was acquired."""
        self.load()
        
        # Determine the acquisition time
        acquired_time = None
        if status == "LOCKED":
            acquired_time = datetime.now().strftime(Config.PRETTY_FORMAT)
        
        self.state["hardware"]["lock_info"] = {
            "status": status,
            "owner": owner,
            "details": details,
            "acquired_at": acquired_time  # <-- Added to state
        }
        
        # If lock is released, ensure all cameras are marked as IDLE
        if status == "FREE":
            for cam_id in self.state["hardware"]["cams"]:
                self.state["hardware"]["cams"][cam_id]["activity"] = "IDLE"
             
        self.write()

    def refresh_scheduler_status(self):
        """
        Syncs the internal scheduler object state to the dictionary
        WITHOUT overwriting the metadata stored in the jobs.
        """
        if self.scheduler:
            # 1. Update Running State
            self.state["scheduler"]["running"] = self.scheduler.running
            self.state["scheduler"]["last_update"] = datetime.now().strftime(Config.PRETTY_FORMAT)

            # 2. Sync Jobs and find the absolute next execution
            next_runtimes = []
            active_job_ids = []
            
            # Ensure "jobs" dict exists
            if "jobs" not in self.state:
                self.state["jobs"] = {}

            cancelled = self.state.get("cancelled_experiments", [])

            for job in self.scheduler.get_jobs():
                active_job_ids.append(job.id)

                if job.id in cancelled:
                    if job.id in self.state["jobs"]:
                        self.state["jobs"][job.id]['next_run_time'] = None
                    continue
                
                # If job doesn't exist in state yet, create an empty dict to avoid KeyError
                if job.id not in self.state["jobs"]:
                    self.state["jobs"][job.id] = {}
                    
                # Update only the scheduling properties (preserves name, progress, etc.)
                if job.next_run_time:
                    next_runtimes.append(job.next_run_time)
                    self.state["jobs"][job.id]['next_run_time'] = job.next_run_time.strftime(Config.PRETTY_FORMAT)
                    self.state["jobs"][job.id]['status'] = 'RUNNING'
                else:
                    self.state["jobs"][job.id]['next_run_time'] = None
                    # Only change to IDLE if it was RUNNING. This protects manual statuses like "ERROR".
                    if self.state["jobs"][job.id].get('status') == 'RUNNING':
                        self.state["jobs"][job.id]['status'] = 'IDLE'
                        
                self.state["jobs"][job.id]['trigger'] = str(job.trigger)

            # Clean up: Mark jobs that disappeared from the APScheduler as IDLE
            for jid in self.state["jobs"]:
                if jid not in active_job_ids:
                    self.state["jobs"][jid]['next_run_time'] = None
                    if self.state["jobs"][jid].get('status') == 'RUNNING':
                        self.state["jobs"][jid]['status'] = 'IDLE'

            # 3. Handle the "Next Picture" Global Timestamp
            if next_runtimes:
                earliest_job = min(next_runtimes)
                self.state["scheduler"]["next_picture"] = earliest_job.strftime(Config.PRETTY_FORMAT)
            else:
                self.state["scheduler"]["next_picture"] = None
            
            self.write()

    def remove_experiment(self, expid):
        """Removes an experiment and forces a full state refresh."""
        self.load()
        if expid in self.state["jobs"]:
            del self.state["jobs"][expid]
            self.refresh_scheduler_status()
            if self.log: 
                self.log.info(f"Experiment {expid} removed. State synchronized.")

    def set_exp_status(self, expid, status):
        self.load()
        if expid in self.state["jobs"]:
            self.state["jobs"][expid]['status'] = status
            self.refresh_scheduler_status()
            
            
    # ------------------------------------------------------------------
    # Experiments updaters
    # ------------------------------------------------------------------
    
    def register_job_metadata(self, expid, name, expected, starting_count=0, start_str=None, interval=None, end_str=None):
        """Called when scheduled. Caches metadata for the health daemon to use."""
        self.load()
        cid = str(expid)
        if cid not in self.state["jobs"]:
            self.state["jobs"][cid] = {}
            
        self.state["jobs"][cid]["name"] = name
        self.state["jobs"][cid]["start"] = start_str
        self.state["jobs"][cid]["interval"] = interval
        self.state["jobs"][cid]["end"] = end_str
        
        # Calculate initial expected_so_far
        expected_so_far = 0
        if start_str and interval:
            try:
                start_dt = datetime.strptime(start_str, Config.PRETTY_FORMAT)
                if datetime.now() >= start_dt:
                    elapsed_mins = (datetime.now() - start_dt).total_seconds() / 60.0
                    expected_so_far = int(elapsed_mins // int(interval)) + 1
            except: pass
            
        # Add the next_run_time key explicitly when setting up the initial metadata
        self.state["jobs"][cid]["progress"] = {
            "taken": starting_count, 
            "expected": expected,
            "expected_so_far": expected_so_far
        }
        self.state["jobs"][cid]["next_run_time"] = None 
        self.state["jobs"][cid]["status"] = "SCHEDULED" 
        self.write()

    def increment_job_progress(self, expid, last_status="SUCCESS"):
        """Called by the camera script to +1 the counter in RAM and dynamically update expectations."""
        self.load()
        cid = str(expid)
        if cid in self.state["jobs"]:
            if "progress" not in self.state["jobs"][cid]:
                self.state["jobs"][cid]["progress"] = {"taken": 0, "expected": 0, "expected_so_far": 0}
                
            # 1. Increment the actual taken counter
            self.state["jobs"][cid]["progress"]["taken"] += 1
            
            # 2. Update the last capture timestamp
            self.state["jobs"][cid]["last_capture"] = {
                "time": datetime.now().strftime(Config.PRETTY_FORMAT),
                "result": last_status
            }
            
            # --- NEW: Dynamically recalculate "Expected So Far" ---
            start_str = self.state["jobs"][cid].get("start")
            interval = self.state["jobs"][cid].get("interval")
            
            if start_str and interval:
                try:
                    start_dt = datetime.strptime(start_str, Config.PRETTY_FORMAT)
                    now = datetime.now()
                    if now >= start_dt:
                        elapsed_mins = (now - start_dt).total_seconds() / 60.0
                        expected_so_far = int(elapsed_mins // int(interval)) + 1
                        
                        # Cap it so it never exceeds the total expected lifespan of the experiment
                        total_expected = self.state["jobs"][cid]["progress"].get("expected", expected_so_far)
                        self.state["jobs"][cid]["progress"]["expected_so_far"] = min(expected_so_far, total_expected)
                except Exception as e:
                    if self.log: self.log.error(f"Error calculating expected progress: {e}")
                    
        self.write()

    def update_diagnostic_result(self, result_status, message, detailed_results=None):
        """Called when a hardware scan finishes, storing a snapshot of all cameras."""
        self.load()
        
        if "last_diagnostic" not in self.state["hardware"]:
            self.state["hardware"]["last_diagnostic"] = {}
            
        self.state["hardware"]["last_diagnostic"] = {
            "time": datetime.now().strftime(Config.PRETTY_FORMAT),
            "global_result": result_status,
            "message": message,
            "cam_snapshot": detailed_results or {} # <-- NEW: StoresY exactly what each cam reported
        }
        self.write()

    
    def update_lights_state(self, state_str):
        """Records whether the system believes the lights are currently ON or OFF based on our signal."""
        self.load()
        if "lights" not in self.state["hardware"]:
            self.state["hardware"]["lights"] = {}
        self.state["hardware"]["lights"]["state"] = state_str
        self.write()

    def update_lights_status(self, health_data):
        """Records the mathematical results of a diagnostic light test."""
        self.load()
        if "lights" not in self.state["hardware"]:
            self.state["hardware"]["lights"] = {"state": "OFF"}
        self.state["hardware"]["lights"]["health_check"] = health_data
        self.write()
        
    # ------------------------------------------------------------------
    # UI FORMATTER
    # ------------------------------------------------------------------

    def get_info(self, reload=True):
        """
        Returns the dictionary for the Flask template and external API, 
        enriched with calculated uptime, anomaly detection, and full state.

        Set reload=False when the caller has just loaded the state (e.g. via
        for_read()) to avoid a redundant file read and lock acquisition.
        """
        if reload:
            self.load()
        data = self.state
        now = datetime.now()

        self._ensure_storage_stats(data)
        
        # --- Uptime & True Boot Time Calculation ---
        uptime_str = "Unknown"
        try:
            # Direct kernel read - completely immune to NTP time jumps
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
            
            # 1. Exact Duration
            m, s = divmod(int(uptime_seconds), 60)
            h, m = divmod(m, 60)
            d, h = divmod(h, 24)
            uptime_str = f"{d}d {h}h {m}m {s}s" if d > 0 else f"{h}h {m}m {s}s"

            # 2. Retroactive Boot Time Auto-Correction
            # Subtracting the monotonic duration from the current (NTP-corrected) time 
            # yields the actual real-world boot time.
            true_boot_time = now - timedelta(seconds=uptime_seconds)
            data["scheduler"]["uptime_start"] = true_boot_time.strftime(Config.PRETTY_FORMAT)
            
        except Exception:
            # Fallback method if /proc/uptime fails (uses stored JSON state)
            try:
                boot_dt = datetime.strptime(data["scheduler"]["uptime_start"], Config.PRETTY_FORMAT)        
                delta = now - boot_dt
                m, s = divmod(int(delta.total_seconds()), 60)
                h, m = divmod(m, 60)
                d, h = divmod(h, 24)
                uptime_str = f"{d}d {h}h {m}m {s}s" if d > 0 else f"{h}h {m}m {s}s"
            except:
                pass

        # --- Anomaly Detection ---
        alerts = {
            "has_warnings": False,
            "lock_stuck": False,
            "picture_overdue": False,
            "issues": []
        }
        
        # 1. Check for Stuck Lock
        lock_info = data["hardware"]["lock_info"]
        is_user_stream = lock_info.get("owner") == "User (Web Interface)"
        
        if lock_info.get("status") == "LOCKED" and lock_info.get("acquired_at"):
            try:
                acquired_time = datetime.strptime(lock_info["acquired_at"], Config.PRETTY_FORMAT)
                lock_hold_duration = (now - acquired_time).total_seconds()
                
                if is_user_stream:
                    # Live previews get their own (longer) allowance: a human may
                    # legitimately spend a while focusing, but a preview lock held
                    # for longer than this is almost certainly stale/hung.
                    max_allowed = getattr(Config, 'USER_LOCK_ALLOWANCE', 30) * 60
                    if lock_hold_duration > max_allowed:
                        alerts["lock_stuck"] = True
                        alerts["has_warnings"] = True
                        alerts["issues"].append(
                            f"Stale live-preview lock: held by the web interface for "
                            f"{int(lock_hold_duration // 60)} mins (max {int(max_allowed // 60)} mins). "
                            f"The preview stream may have hung."
                        )
                else:
                    per_camera_allowance = Config.PER_CAMERA_ALLOWANCE * 60 
                    num_cameras = len(Config.CAMS) 
                    max_allowed = per_camera_allowance * num_cameras
                    
                    if lock_hold_duration > max_allowed: 
                        alerts["lock_stuck"] = True
                        alerts["has_warnings"] = True
                        alerts["issues"].append(
                            f"Hardware lock held too long ({int(lock_hold_duration // 60)} mins). "
                            f"Max allowed for {num_cameras} cams is {int(max_allowed // 60)} mins."
                        )
            except Exception as e:
                if self.log: self.log.error(f"Error calculating lock time: {e}")

        cancelled = data.get("cancelled_experiments", [])
        active_jobs = {
            k: v for k, v in data.get("jobs", {}).items()
            if k not in cancelled
        }

        camera_gaps = data.get("hardware", {}).get("camera_gaps", [])
        for gap in camera_gaps:
            alerts["has_warnings"] = True
            alerts["issues"].append(
                f"Camera {gap.get('cam')} on {gap.get('expid')} is {gap.get('behind_by')} pictures behind schedule."
            )

        all_cam_fail = data.get("hardware", {}).get("all_cameras_failed")
        if all_cam_fail:
            alerts["all_cameras_failed"] = True
            alerts["has_warnings"] = True
            alerts["issues"].append(
                f"All cameras failed on experiment {all_cam_fail.get('expid')} — watchdog may reboot."
            )

        # Recompute next_picture from non-cancelled jobs only
        next_times = []
        for job_data in active_jobs.values():
            nrt = job_data.get("next_run_time")
            if nrt:
                try:
                    next_times.append(datetime.strptime(nrt, Config.PRETTY_FORMAT))
                except Exception:
                    pass
        next_pic_str = min(next_times).strftime(Config.PRETTY_FORMAT) if next_times else None

        has_active_jobs = len(active_jobs) > 0

        if next_pic_str and has_active_jobs:
            try:
                next_pic_time = datetime.strptime(next_pic_str, Config.PRETTY_FORMAT)
                overdue_duration = (now - next_pic_time).total_seconds()
                
                if overdue_duration > (Config.PER_CAMERA_ALLOWANCE * 60):
                    alerts["picture_overdue"] = True
                    alerts["has_warnings"] = True
                    alerts["issues"].append(f"Scheduled picture is {int(overdue_duration // 60)}m late.")
            except:
                pass

        # Watchdog reboot count (append-only log on disk)
        watchdog_reboots = 0
        guard_file = os.path.join(Config.WORKING_DIR, "watchdog_reboot_dates.txt")
        if os.path.exists(guard_file):
            try:
                with open(guard_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                dt = datetime.strptime(line, Config.PRETTY_FORMAT)
                                if (now - dt).total_seconds() < 21600:
                                    watchdog_reboots += 1
                            except Exception:
                                pass
            except Exception:
                pass
        watchdog_limit_reached = watchdog_reboots >= 3
        if watchdog_limit_reached:
            alerts["has_warnings"] = True
            alerts["issues"].append(
                "Auto-reboot limit reached (3 in last 6 hours) — manual intervention required."
            )

        # --- API & UI Payload Construction ---
        information_summary = {
            # Identity & Health
            "identity": data.get("identity", {}),
            "system_health": data.get("system_health", {}),
            "uptime": uptime_str,
            "status": "running" if data["scheduler"]["running"] else "waiting",
            "system_time": now.strftime(Config.PRETTY_FORMAT), 
            
            # Hardware & Diagnostics
            "lock_info": lock_info,
            "cam_reports": data["hardware"]["cams"],
            "lights_info": data["hardware"].get("lights", {}),  
            "last_diagnostic": data["hardware"].get("last_diagnostic", {}),
            "last_picture": data["hardware"].get("last_picture", "Never"),
            "camera_gaps": camera_gaps,
            "all_cameras_failed": all_cam_fail,
            
            # Scheduler & Detailed Jobs
            "next_picture": next_pic_str if next_pic_str else "None",
            "active_jobs_count": len([k for k, v in active_jobs.items() if v.get("status") == "RUNNING"]),
            "jobs": active_jobs,
            
            # Sync & Alerts
            "sync": data.get("sync", {}),
            "alerts": alerts,
            "watchdog": {
                "reboots_last_6h": watchdog_reboots,
                "reboot_limit": 3,
                "limit_reached": watchdog_limit_reached,
            },
        }

        return information_summary