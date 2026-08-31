import subprocess
import os
import time
import logging
import configparser
import tempfile
import shlex
from datetime import datetime
from config import Config
from app.options.schedulerstatus import SchedulerStatus

log = logging.getLogger(__name__)
RCLONE_CONF = os.path.join(Config.APP_ROOT, 'rclone.conf')

# Remote shell: chmod only paths piped on stdin (relative to dest). Not chmod -R.
_SFTP_CHMOD_SCRIPT = r'''
cd "$1" || exit 1
while IFS= read -r p; do
  [ -n "$p" ] || continue
  chmod g+rwX -- "$p" || true
  d=$(dirname -- "$p")
  while [ "$d" != "." ] && [ "$d" != "/" ]; do
    chmod g+rwXs -- "$d" 2>/dev/null || chmod g+rwX -- "$d" || true
    d=$(dirname -- "$d")
  done
done
'''


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


def _reveal_password(obfuscated):
    if not obfuscated:
        return ""
    try:
        result = subprocess.run(
            ["rclone", "reveal", obfuscated],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        log.warning("Failed to reveal rclone password: %s", e)
    return ""


def _new_or_updated_paths(combined_path):
    """Relative paths rclone created (+) or updated (*) this run."""
    paths = []
    seen = set()
    try:
        with open(combined_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("+ ") or line.startswith("* "):
                    rel = line[2:].strip().lstrip("/")
                    if rel and rel not in seen:
                        seen.add(rel)
                        paths.append(rel)
    except OSError as e:
        log.warning("Could not read rclone combined log: %s", e)
    return paths


def _ssh_with_password(host, user, port, password, remote_cmd, stdin_text=None):
    """Run one SSH command using the rclone SFTP password (SSH_ASKPASS)."""
    askpass_path = None
    try:
        fd, askpass_path = tempfile.mkstemp(prefix="cr_askpass_", suffix=".sh")
        os.write(fd, f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(password or '')}\n".encode())
        os.close(fd)
        os.chmod(askpass_path, 0o700)

        env = os.environ.copy()
        env["SSH_ASKPASS"] = askpass_path
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", ":0")

        ssh_cmd = [
            "ssh",
            "-oBatchMode=no",
            "-oStrictHostKeyChecking=accept-new",
            "-oConnectTimeout=15",
            "-oPreferredAuthentications=password",
            "-oPubkeyAuthentication=no",
        ]
        if port:
            ssh_cmd.extend(["-p", str(port)])
        ssh_cmd.append(f"{user}@{host}")
        ssh_cmd.append(remote_cmd)

        result = subprocess.run(
            ssh_cmd,
            input=stdin_text,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            start_new_session=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "ssh failed").strip()
            return err.split("\n")[-1][:300]
        return None
    except Exception as e:
        return str(e)
    finally:
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass

def _chmod_sftp_new_files(dest_path, combined_path):
    """Group-writable on files this SFTP copy wrote. Does not walk the whole tree."""
    paths = _new_or_updated_paths(combined_path)
    if not paths:
        return None

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(RCLONE_CONF)
    cfg = dict(parser["chronosync"]) if "chronosync" in parser.sections() else {}
    
    host, user = cfg.get("host"), cfg.get("user")
    password = _reveal_password(cfg.get("pass", ""))
    if not host or not user or not password:
        return "SFTP credentials missing for group chmod"

    # Script is -c (uses $1 = dest). Path list is stdin so we do not chmod -R.
    remote_cmd = (
        f"sh -c {shlex.quote(_SFTP_CHMOD_SCRIPT)} "
        f"sh {shlex.quote(dest_path)}"
    )
    return _ssh_with_password(
        host, user, cfg.get("port"), password, remote_cmd, "\n".join(paths) + "\n"
    )


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
    status.update_sync_fields(
        is_syncing=True,
        last_start=datetime.now().strftime(Config.PRETTY_FORMAT),
        status_msg="Calculating transfer size...",
    )

    combined_path = None
    try:
        cmd = [
            "rclone", "--config", RCLONE_CONF, "copy", source, destination,
            "--stats=1s", "--stats-one-line", "--stats-log-level", "NOTICE",
            "--no-update-dir-modtime",
        ]
        if remote_type == "sftp":
            combined_fd, combined_path = tempfile.mkstemp(prefix="rclone_combined_", suffix=".txt")
            os.close(combined_fd)
            cmd.extend(["--combined", combined_path])
        # Local: umask 002 so new files are 664 / dirs 775. sh wrapper avoids
        # Popen preexec_fn, which is unsafe in the mule's threaded context.
        if remote_type == "local":
            cmd = ["/bin/sh", "-c", 'umask 002; exec "$@"', "--"] + cmd

        # rclone emits a stats line every second (--stats=1s). Writing the RAM-disk
        # status file that often takes an exclusive fcntl lock + fsync each time, which
        # starves concurrent GET /api/status reads. Throttle the progress flush so we
        # update at most once every PROGRESS_WRITE_INTERVAL seconds.
        PROGRESS_WRITE_INTERVAL = 3.0
        last_progress_write = 0.0

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
                    status.update_sync_fields(status_msg=latest_progress)
                    last_progress_write = now_ts
            else:
                if "INFO" not in clean_line and "DEBUG" not in clean_line:
                    last_log_lines.append(clean_line)
                    if len(last_log_lines) > 5:
                        last_log_lines.pop(0)

        process.wait()
        
        if process.returncode == 0:
            if remote_type == "sftp" and combined_path:
                status.update_sync_fields(status_msg="Applying group permissions...")
                perm_warning = _chmod_sftp_new_files(custom_path, combined_path)
                if perm_warning:
                    log.warning("Copy succeeded but group permissions were not fully applied: %s", perm_warning)

            status.update_sync_fields(
                is_syncing=False,
                status_msg="Standby",
                last_success=datetime.now().strftime(Config.PRETTY_FORMAT),
                last_error=None,
            )
            return True, "Success"
        else:
            error_details = " | ".join(last_log_lines)
            if not error_details: error_details = "Unknown Error"
            log.error(f"Rclone failed with code {process.returncode}. Details: {error_details}")
            
            status.update_sync_fields(
                is_syncing=False,
                status_msg="Standby",
                last_error=error_details,
            )
            return False, f"Transfer failed: {error_details}"
            
    except Exception as e:
        log.error(f"Rclone failed catastrophically: {e}")
        status.update_sync_fields(
            is_syncing=False,
            status_msg="Standby",
            last_error=f"Catastrophic failure: {e}",
        )
        return False, "Transfer failed."
    finally:
        if combined_path:
            try:
                os.unlink(combined_path)
            except OSError:
                pass

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
