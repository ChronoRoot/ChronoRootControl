import subprocess
import os
import time
import logging
import configparser
from datetime import datetime
from config import Config
from app.options.schedulerstatus import SchedulerStatus

log = logging.getLogger(__name__)
RCLONE_CONF = os.path.join(Config.APP_ROOT, 'rclone.conf')

def _get_obfuscated_password(remote_name="chronosync"):
    parser = configparser.ConfigParser()
    parser.read(RCLONE_CONF)
    if remote_name in parser.sections():
        return parser[remote_name].get('pass', '')
    return ''

def _inject_obfuscated_password(remote_name, obf_pass):
    if not obf_pass: return
    parser = configparser.ConfigParser()
    parser.read(RCLONE_CONF)
    if remote_name not in parser.sections():
        parser.add_section(remote_name)
    parser.set(remote_name, 'pass', obf_pass)
    with open(RCLONE_CONF, 'w') as f:
        parser.write(f)

def setup_rclone_remote(remote_type, host, user, password, port=None):
    try:
        old_obfuscated_pass = ""
        if password == "********":
            old_obfuscated_pass = _get_obfuscated_password("chronosync")

        subprocess.run(["rclone", "--config", RCLONE_CONF, "config", "delete", "chronosync"], capture_output=True)
        
        cmd = ["rclone", "--config", RCLONE_CONF, "config", "create", "chronosync", remote_type, 
               "host", host, "user", user]
        
        if port: cmd.extend(["port", str(port)])
        if remote_type == 'sftp': cmd.extend(["md5sum_command", "none", "sha1sum_command", "none"])
        
        if password and password != "********":
            cmd.extend(["pass", password])
            
        subprocess.run(cmd, check=True, capture_output=True)

        if password == "********" and old_obfuscated_pass:
            _inject_obfuscated_password("chronosync", old_obfuscated_pass)

        return True, "Remote configured successfully."
    except subprocess.CalledProcessError as e:
        return False, f"Rclone config failed: {e.stderr.decode()}"
    except Exception as e:
        return False, f"Configuration error: {str(e)}"

def run_rclone_sync():
    """
    Executes the sync operation. This blocks the calling thread, 
    so it MUST be executed inside a thread wrapper within the Mule.
    """
    status = SchedulerStatus()
    info = status.get_info()
    
    if info.get("sync", {}).get("is_syncing", False):
        return False, "Sync is already in progress."

    remote_type = getattr(Config, 'SYNC_REMOTE_TYPE', 'local')
    custom_path = getattr(Config, 'SYNC_DESTINATION', '').strip()
    source = Config.WORKING_DIR

    if not custom_path:
        log.warning("Sync aborted: Destination path is empty.")
        return False, "Destination is empty."

    destination = custom_path if remote_type == 'local' else f"chronosync:{custom_path}"

    # Initialize RAM-disk file state
    status.load()
    status.state.setdefault("sync", {})
    status.state["sync"]["is_syncing"] = True
    status.state["sync"]["last_start"] = datetime.now().strftime(Config.PRETTY_FORMAT)
    status.state["sync"]["status_msg"] = "Calculating transfer size..."
    status.write()

    # Optimized logging setup to get streamlined output updates
    cmd = [
        "rclone", "--config", RCLONE_CONF, "copy", source, destination, 
        "--stats=1s", "--stats-one-line", "--stats-log-level", "NOTICE"
    ]

    # rclone emits a stats line every second (--stats=1s). Writing the RAM-disk
    # status file that often takes an exclusive fcntl lock + fsync each time, which
    # starves concurrent GET /api/status reads. Throttle the progress flush so we
    # update at most once every PROGRESS_WRITE_INTERVAL seconds.
    PROGRESS_WRITE_INTERVAL = 3.0
    last_progress_write = 0.0

    try:
        log.info(f"Starting rclone copy to {destination}...")
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        last_log_lines = []
        latest_progress = None
        
        for line in process.stdout:
            clean_line = line.strip()
            if not clean_line:
                continue
                
            # Update matching stats line directly to RAM status file
            if "%" in clean_line and " / " in clean_line:
                # 1. Strip the ugly syslog prefix (e.g. "<5>NOTICE:   ")
                if "NOTICE:" in clean_line:
                    clean_line = clean_line.split("NOTICE:")[-1].strip()
                elif "INFO:" in clean_line:
                    clean_line = clean_line.split("INFO:")[-1].strip()
                elif ">" in clean_line:
                    clean_line = clean_line.split(">")[-1].strip()
                
                # 2. Make it prettier (Replace commas with clean pipes)
                # Transforms: "16.674 MiB / 19.862 MiB, 84%, 2.905 MiB/s, ETA 1s" 
                # Into:       "16.674 MiB / 19.862 MiB | 84% | 2.905 MiB/s | ETA 1s"
                clean_line = clean_line.replace(", ", " | ")

                # Always remember the latest line, but only persist it periodically.
                latest_progress = clean_line
                now_ts = time.monotonic()
                if now_ts - last_progress_write >= PROGRESS_WRITE_INTERVAL:
                    status.load()
                    status.state["sync"]["status_msg"] = latest_progress
                    status.write()
                    last_progress_write = now_ts
            else:
                if "INFO" not in clean_line and "DEBUG" not in clean_line:
                    last_log_lines.append(clean_line)
                    if len(last_log_lines) > 5:
                        last_log_lines.pop(0)

        process.wait()
        
        status.load()
        if process.returncode == 0:
            status.state["sync"]["is_syncing"] = False
            status.state["sync"]["status_msg"] = "Standby" 
            status.state["sync"]["last_success"] = datetime.now().strftime(Config.PRETTY_FORMAT)
            status.state["sync"]["last_error"] = None 
            status.write()
            return True, "Success"
        else:
            error_details = " | ".join(last_log_lines)
            if not error_details: error_details = "Unknown Error"
            log.error(f"Rclone failed with code {process.returncode}. Details: {error_details}")
            
            status.state["sync"]["is_syncing"] = False
            status.state["sync"]["status_msg"] = "Standby" 
            status.state["sync"]["last_error"] = error_details 
            status.write()
            return False, f"Transfer failed: {error_details}"
            
    except Exception as e:
        log.error(f"Rclone failed catastrophically: {e}")
        status.load()
        status.state["sync"]["is_syncing"] = False
        status.state["sync"]["status_msg"] = "Standby" 
        status.state["sync"]["last_error"] = f"Catastrophic failure: {e}" 
        status.write()
        return False, "Transfer failed."

def test_rclone_connection(remote_type, host, user, password, port=None):
    try:
        old_obfuscated_pass = ""
        if password == "********":
            old_obfuscated_pass = _get_obfuscated_password("chronosync")

        subprocess.run(["rclone", "--config", RCLONE_CONF, "config", "delete", "chronotest"], capture_output=True)
        cmd = ["rclone", "--config", RCLONE_CONF, "config", "create", "chronotest", remote_type, 
               "host", host, "user", user]
        
        if port: cmd.extend(["port", str(port)])
        if remote_type == 'sftp': cmd.extend(["md5sum_command", "none", "sha1sum_command", "none"])
        
        if password and password != "********":
            cmd.extend(["pass", password])
            
        subprocess.run(cmd, check=True, capture_output=True)

        if password == "********" and old_obfuscated_pass:
            _inject_obfuscated_password("chronotest", old_obfuscated_pass)

        test_cmd = ["rclone", "--config", RCLONE_CONF, "lsd", "chronotest:/", "--contimeout", "5s", "--timeout", "10s"]
        result = subprocess.run(test_cmd, capture_output=True, text=True)
        
        # Ensure cleanup happens even if validation check registers an error code
        subprocess.run(["rclone", "--config", RCLONE_CONF, "config", "delete", "chronotest"], capture_output=True)

        if result.returncode == 0:
            return True, "Connection successful! Credentials are valid."
        else:
            error_msg = result.stderr.strip().split('\n')[-1] if result.stderr.strip() else "Handshake failed."
            return False, error_msg

    except Exception as e:
        return False, str(e)