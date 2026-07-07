import importlib.util
import os
import re
import time
import os
import subprocess
import socket

USER_CONFIG_PATH = '/srv/ChronoRootControl/user_config.py'
REPO_DIR = '/srv/ChronoRootControl'

# RFC 952/1123 single-label hostname: 1-63 chars, alphanumeric + hyphens,
# no leading/trailing hyphen.
HOSTNAME_PATTERN = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')

def save_user_config(new_settings):
    """
    Reads existing user config (if any), merges new settings, 
    and writes back to user_config.py safely.
    """
    current_settings = {}
    
    # 1. Read existing config safely
    if os.path.exists(USER_CONFIG_PATH):
        try:
            spec = importlib.util.spec_from_file_location("user_config", USER_CONFIG_PATH)
            user_config = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(user_config)
            if hasattr(user_config, 'Config'):
                for attr in dir(user_config.Config):
                    if not attr.startswith('_'):
                        current_settings[attr] = getattr(user_config.Config, attr)
        except Exception as e:
            print(f"Warning: Could not read existing user_config.py: {e}")
            
    # 2. Merge in the new settings
    current_settings.update(new_settings)
    
    # 3. Write back to disk
    lines = [
        "#!/usr/bin/env python3",
        "# Auto-generated configuration file",
        "",
        "class Config(object):"
    ]
    
    for key, value in current_settings.items():
        if isinstance(value, str):
            lines.append(f"    {key} = '{value}'")
        elif isinstance(value, bool):
            lines.append(f"    {key} = {value}")
        elif isinstance(value, (int, float)):
            lines.append(f"    {key} = {value}")
        elif isinstance(value, (tuple, list, dict)):
            # Use repr() to safely format dicts and lists as valid Python code
            lines.append(f"    {key} = {repr(value)}")
        else:
            # Catch-all to prevent silent failures in the future
            print(f"Warning: Unsupported config type for key {key}: {type(value)}")
            
    try:
        os.makedirs(os.path.dirname(USER_CONFIG_PATH), exist_ok=True)
        with open(USER_CONFIG_PATH, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        return True, "Config saved successfully."
    except Exception as e:
        return False, str(e)

def apply_system_time_config(mode, date_str=None, timezone=None, ntp_server=None):
    """
    Interfaces with Raspberry Pi OS to set time, timezone, and NTP.
    """
    try:
        # 1. Set Timezone
        if timezone:
            subprocess.run(['sudo', 'timedatectl', 'set-timezone', timezone], check=True)
            
            # CRITICAL FIX: Force the running Python Flask app to reload the timezone!
            if 'TZ' in os.environ:
                del os.environ['TZ']
            time.tzset() 

        # 2. Apply Time Mode
        if mode == 'network':
            target_server = ntp_server if ntp_server else "pool.ntp.org"
            
            # CRITICAL FIX: Replaced 'ping' with a native UDP socket test on port 123
            # This bypasses ICMP blocks and actually tests the NTP protocol directly.
            try:
                client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                client.settimeout(2.0) # 2-second timeout
                # 0x1B is a standard 48-byte NTP client request packet
                client.sendto(b'\x1b' + 47 * b'\0', (target_server, 123))
                client.recvfrom(1024)
                client.close()
            except OSError:
                if 'client' in locals():
                    client.close()
                return False, f"Robot is offline or cannot reach NTP server on UDP port 123: {target_server}"

            # Apply NTP config if UDP check passed
            if ntp_server:
                config_line = f"NTP={ntp_server}"
                subprocess.run(['sudo', 'sed', '-i', f's/^#*NTP=.*/{config_line}/', '/etc/systemd/timesyncd.conf'], check=True)
                
            subprocess.run(['sudo', 'timedatectl', 'set-ntp', 'true'], check=True)
            subprocess.run(['sudo', 'systemctl', 'restart', 'systemd-timesyncd'], check=True)

        elif mode == 'manual' and date_str:
            subprocess.run(['sudo', 'timedatectl', 'set-ntp', 'false'], check=True)
            subprocess.run(['sudo', 'date', '-s', date_str], check=True)
            
        return True, "Time configuration applied successfully."
        
    except subprocess.CalledProcessError as e:
        return False, f"OS Command Failed: {str(e)}"

def apply_hostname_config(new_hostname):
    """
    Stages a hostname change using raspi-config's non-interactive mode:
        raspi-config nonint do_hostname <name>

    Why this is the safe path (and cannot hang sudo):
    - raspi-config rewrites BOTH /etc/hostname and the 127.0.1.1 line in
      /etc/hosts, so the machine can always resolve its own name. A mismatch
      between those two files is what makes sudo stall for 10+ seconds.
    - The change only takes effect on the NEXT reboot; the live hostname is
      left untouched, so the current session stays fully consistent.
    - 'sudo -n' never prompts for a password (it fails fast instead of
      blocking on a tty), and the hard timeout guards against any other
      unexpected stall.
    """
    new_hostname = (new_hostname or '').strip().lower()

    if not HOSTNAME_PATTERN.match(new_hostname):
        return False, ("Invalid hostname. Use 1-63 characters: lowercase letters, "
                       "digits and hyphens (cannot start or end with a hyphen).")

    try:
        result = subprocess.run(
            ['sudo', '-n', 'raspi-config', 'nonint', 'do_hostname', new_hostname],
            capture_output=True, text=True, timeout=20
        )
    except subprocess.TimeoutExpired:
        return False, "Hostname change timed out. The running system was not renamed."
    except FileNotFoundError:
        return False, "raspi-config is not available on this system."

    if result.returncode != 0:
        err = (result.stderr or result.stdout or '').strip()
        return False, f"raspi-config failed (code {result.returncode}): {err or 'unknown error'}"

    return True, f"Hostname staged as '{new_hostname}'. It takes effect after the next reboot."

def run_git_update():
    """
    Runs a plain 'git pull' inside REPO_DIR and returns
    (success, message, changed) with a human-readable summary of what happened.

    - success: the pull ran without error.
    - changed: new code was actually pulled (False when already up to date, or
      on any failure). Callers use this to decide whether a service restart is
      needed.

    Classifies the common outcomes explicitly:
    - already up to date
    - updated successfully (with a short summary of the pull)
    - no internet / remote unreachable
    - blocked by local changes or a diverged branch (manual intervention)
    - any other git failure
    """
    # Never let git block waiting for interactive input: disable the credential
    # prompt (HTTPS) and force SSH into batch mode. Combined with the timeout,
    # this guarantees the call returns instead of hanging on a private repo.
    env = dict(os.environ)
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_SSH_COMMAND'] = 'ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new'

    # Failsafe for "detected dubious ownership": the service runs as root while
    # the repo is owned by the user (or vice versa). Inject safe.directory for
    # THIS command only via -c, so we never mutate any global git config.
    try:
        result = subprocess.run(
            ['git', '-c', f'safe.directory={REPO_DIR}', '-C', REPO_DIR, 'pull', '--ff-only'],
            capture_output=True, text=True, timeout=120, env=env
        )
    except subprocess.TimeoutExpired:
        return False, ("The update timed out after 2 minutes. This usually means a slow "
                       "or dropped internet connection. Please try again."), False
    except FileNotFoundError:
        return False, "git is not installed on this system, so the app cannot self-update.", False

    out = (result.stdout or '').strip()
    err = (result.stderr or '').strip()
    combined = '\n'.join(part for part in (out, err) if part).strip()
    low = combined.lower()

    if result.returncode == 0:
        if 'already up to date' in low or 'already up-to-date' in low:
            return True, "You are already running the latest version. No update was needed.", False
        summary = out or "Changes were pulled from the remote repository."
        return True, ("Update successful! The latest code has been pulled.\n\n"
                       f"{summary}\n\nRestart the services or reboot to run the new version."), True

    network_markers = [
        'could not resolve host', 'unable to access', 'connection timed out',
        'could not read from remote repository', 'network is unreachable',
        'temporary failure in name resolution', 'failed to connect', 'connection refused',
    ]
    if any(marker in low for marker in network_markers):
        return False, ("No internet connection detected. The device could not reach the "
                       "remote repository. Check the network and try again."), False

    conflict_markers = [
        'would be overwritten', 'local changes', 'not possible to fast-forward',
        'diverging', 'non-fast-forward', 'unmerged', 'needs merge',
    ]
    if any(marker in low for marker in conflict_markers):
        return False, ("Update blocked: this device has local changes or its branch has "
                        "diverged from the remote. Manual intervention is required.\n\n"
                        f"{combined}"), False

    return False, f"Update failed (git exit code {result.returncode}):\n\n{combined or 'Unknown error.'}", False