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
from filelock import FileLock, Timeout
from app.storage.stats import get_storage_stats


def _as_local_naive(dt):
    """APScheduler next_run_time is tz-aware; UI compares naive local strings."""
    if dt is None:
        return None
    if getattr(dt, 'tzinfo', None) is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt

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
            "stream": {
                "status": "stopped",
                "camera_id": None,
                "last_error": None,
                "updated_at": None
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

    def _mutate_state(self, mutator):
        """Read-modify-write the status file under one exclusive lock.

        load()+write() on a shared in-memory snapshot races with rclone's 3s
        progress flush and the watchdog: the slower writer puts an older copy
        back and the UI keeps a past next_run_time (false picture-overdue).
        """
        for _attempt in range(10):
            try:
                with open(self.status_file, "a+") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        handle.seek(0)
                        raw = handle.read()
                        if not raw.strip():
                            latest = json.loads(json.dumps(self.default_state))
                        else:
                            try:
                                latest = json.loads(raw)
                            except json.JSONDecodeError:
                                if self.log:
                                    self.log.error("Refusing to overwrite corrupted status during mutate.")
                                return False
                        mutator(latest)
                        handle.seek(0)
                        handle.truncate()
                        json.dump(latest, handle, indent=2)
                        handle.flush()
                        os.fsync(handle.fileno())
                        self.state = latest
                        return True
                    finally:
                        fcntl.flock(handle, fcntl.LOCK_UN)
            except (BlockingIOError, IOError):
                time.sleep(0.05)
        if self.log:
            self.log.error("CRITICAL: Could not acquire lock to mutate scheduler status after max retries.")
        return False

    # ------------------------------------------------------------------
    # SPECIFIC UPDATERS (Helpers to modify the dictionary cleanly)
    # ------------------------------------------------------------------

    def update_hardware_status(self, cam_id=None, cam_status=None, last_pic=False):
        """Updates per-camera hardware keys."""
        def _apply(state):
            if cam_id is not None and cam_status:
                cid = str(cam_id)
                cams = state.setdefault("hardware", {}).setdefault("cams", {})
                if cid in cams:
                    cams[cid].update(cam_status)
            if last_pic:
                state.setdefault("hardware", {})["last_picture"] = datetime.now().strftime(Config.PRETTY_FORMAT)
        self._mutate_state(_apply)

    def update_lock_state(self, status="FREE", owner=None, details=None):
        """Updates lock info and records the exact time it was acquired."""
        def _apply(state):
            acquired_time = None
            if status == "LOCKED":
                acquired_time = datetime.now().strftime(Config.PRETTY_FORMAT)
            hardware = state.setdefault("hardware", {})
            hardware["lock_info"] = {
                "status": status,
                "owner": owner,
                "details": details,
                "acquired_at": acquired_time,
            }
            if status == "FREE":
                for cam_id in hardware.setdefault("cams", {}):
                    hardware["cams"][cam_id]["activity"] = "IDLE"
        self._mutate_state(_apply)

    def update_stream_status(self, status, camera_id=None, last_error=None):
        """Persist low-frequency live-preview lifecycle transitions."""
        stream_state = {
            "status": status,
            "camera_id": camera_id,
            "last_error": last_error,
            "updated_at": datetime.now().strftime(Config.PRETTY_FORMAT),
        }
        for _attempt in range(10):
            try:
                with open(self.status_file, "a+") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        handle.seek(0)
                        raw = handle.read()
                        if not raw.strip():
                            latest = json.loads(json.dumps(self.default_state))
                        else:
                            try:
                                latest = json.loads(raw)
                            except json.JSONDecodeError:
                                if self.log:
                                    self.log.error("Refusing to overwrite corrupted status while updating stream.")
                                return
                        latest.setdefault("hardware", {})["stream"] = stream_state
                        handle.seek(0)
                        handle.truncate()
                        json.dump(latest, handle, indent=2)
                        handle.flush()
                        os.fsync(handle.fileno())
                        self.state = latest
                        return
                    finally:
                        fcntl.flock(handle, fcntl.LOCK_UN)
            except (BlockingIOError, IOError):
                time.sleep(0.05)
        if self.log:
            self.log.error("Could not persist stream status after maximum retries.")

    def _repair_stale_lock_atomically(self):
        """Mutate lock/stream fields while one exclusive status-file lock is held."""
        for _attempt in range(10):
            try:
                with open(self.status_file, "a+") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        handle.seek(0)
                        try:
                            latest = json.load(handle)
                        except json.JSONDecodeError:
                            # Never replace a corrupted/incomplete status snapshot
                            # with defaults during recovery.
                            return None

                        hardware = latest.get("hardware", {})
                        current = hardware.get("lock_info", {})
                        if current.get("status") != "LOCKED":
                            self.state = latest
                            return None

                        stale_owner = current.get("owner") or "unknown process"
                        hardware["lock_info"] = {
                            "status": "FREE",
                            "owner": None,
                            "details": None,
                            "acquired_at": None,
                        }
                        for cam in hardware.get("cams", {}).values():
                            cam["activity"] = "IDLE"
                        hardware["stream"] = {
                            "status": "error",
                            "camera_id": hardware.get("stream", {}).get("camera_id"),
                            "last_error": "Recovered stale lock after its owning process exited.",
                            "updated_at": datetime.now().strftime(Config.PRETTY_FORMAT),
                        }

                        handle.seek(0)
                        handle.truncate()
                        json.dump(latest, handle, indent=2)
                        handle.flush()
                        os.fsync(handle.fileno())
                        self.state = latest
                        return stale_owner
                    finally:
                        fcntl.flock(handle, fcntl.LOCK_UN)
            except (BlockingIOError, IOError):
                time.sleep(0.05)
        return None

    def reconcile_hardware_lock(self):
        """
        Repair stale RAM lock telemetry after a worker/native crash.

        A FileLock is released by the OS when its process dies. Therefore, if
        status says LOCKED while this probe can acquire the real lock, no
        hardware owner remains and it is safe to mark the shared state FREE.
        """
        self.load()
        lock_info = self.state.get("hardware", {}).get("lock_info", {})
        if lock_info.get("status") != "LOCKED":
            return False

        probe = FileLock(Config.LOCK_FILE)
        try:
            with probe.acquire(timeout=0):
                stale_owner = self._repair_stale_lock_atomically()
                if stale_owner is None:
                    return False
                if self.log:
                    self.log.warning(
                        "Recovered stale hardware status owned by %s; OS lock was free.",
                        stale_owner,
                    )
                return True
        except Timeout:
            return False

    def refresh_scheduler_status(self):
        """
        Syncs the internal scheduler object state to the dictionary
        WITHOUT overwriting the metadata stored in the jobs.
        """
        if not self.scheduler:
            return

        def _apply(state):
            scheduler_block = state.setdefault("scheduler", {})
            scheduler_block["running"] = self.scheduler.running
            scheduler_block["last_update"] = datetime.now().strftime(Config.PRETTY_FORMAT)

            next_runtimes = []
            active_job_ids = []
            jobs = state.setdefault("jobs", {})
            cancelled = state.get("cancelled_experiments", [])

            for job in self.scheduler.get_jobs():
                active_job_ids.append(job.id)

                if job.id in cancelled:
                    if job.id in jobs:
                        jobs[job.id]['next_run_time'] = None
                    continue

                if job.id not in jobs:
                    jobs[job.id] = {}

                if job.next_run_time:
                    local_nrt = _as_local_naive(job.next_run_time)
                    next_runtimes.append(local_nrt)
                    jobs[job.id]['next_run_time'] = local_nrt.strftime(Config.PRETTY_FORMAT)
                    jobs[job.id]['status'] = 'RUNNING'
                else:
                    jobs[job.id]['next_run_time'] = None
                    if jobs[job.id].get('status') == 'RUNNING':
                        jobs[job.id]['status'] = 'IDLE'

                jobs[job.id]['trigger'] = str(job.trigger)

            for jid in jobs:
                if jid not in active_job_ids:
                    jobs[jid]['next_run_time'] = None
                    if jobs[jid].get('status') == 'RUNNING':
                        jobs[jid]['status'] = 'IDLE'

            if next_runtimes:
                scheduler_block["next_picture"] = min(next_runtimes).strftime(Config.PRETTY_FORMAT)
            else:
                scheduler_block["next_picture"] = None

        self._mutate_state(_apply)

    def remove_experiment(self, expid):
        """Removes an experiment and forces a full state refresh."""
        cid = str(expid)
        removed = []

        def _apply(state):
            jobs = state.get("jobs", {})
            if cid in jobs:
                del jobs[cid]
                removed.append(True)

        if not self._mutate_state(_apply):
            return
        if removed:
            self.refresh_scheduler_status()
            if self.log:
                self.log.info(f"Experiment {expid} removed. State synchronized.")

    def set_exp_status(self, expid, status):
        cid = str(expid)

        def _apply(state):
            jobs = state.get("jobs", {})
            if cid in jobs:
                jobs[cid]['status'] = status

        if not self._mutate_state(_apply):
            return
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
        cid = str(expid)

        def _apply(state):
            jobs = state.setdefault("jobs", {})
            if cid not in jobs:
                return
            if "progress" not in jobs[cid]:
                jobs[cid]["progress"] = {"taken": 0, "expected": 0, "expected_so_far": 0}

            jobs[cid]["progress"]["taken"] += 1
            jobs[cid]["last_capture"] = {
                "time": datetime.now().strftime(Config.PRETTY_FORMAT),
                "result": last_status
            }

            start_str = jobs[cid].get("start")
            interval = jobs[cid].get("interval")
            if start_str and interval:
                try:
                    start_dt = datetime.strptime(start_str, Config.PRETTY_FORMAT)
                    now = datetime.now()
                    if now >= start_dt:
                        elapsed_mins = (now - start_dt).total_seconds() / 60.0
                        expected_so_far = int(elapsed_mins // int(interval)) + 1
                        total_expected = jobs[cid]["progress"].get("expected", expected_so_far)
                        jobs[cid]["progress"]["expected_so_far"] = min(expected_so_far, total_expected)
                except Exception as e:
                    if self.log:
                        self.log.error(f"Error calculating expected progress: {e}")

        self._mutate_state(_apply)

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
        def _apply(state):
            lights = state.setdefault("hardware", {}).setdefault("lights", {})
            lights["state"] = state_str
        self._mutate_state(_apply)

    def update_lights_status(self, health_data):
        """Records the mathematical results of a diagnostic light test."""
        def _apply(state):
            lights = state.setdefault("hardware", {}).setdefault("lights", {"state": "OFF"})
            lights["health_check"] = health_data
        self._mutate_state(_apply)

    def update_sync_fields(self, **fields):
        """Patch rclone/sync keys without dumping a stale full snapshot."""
        def _apply(state):
            state.setdefault("sync", {}).update(fields)
        self._mutate_state(_apply)
        
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
            "stream_status": data["hardware"].get("stream", {}),
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