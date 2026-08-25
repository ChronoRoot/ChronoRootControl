import importlib.util
import logging
import os
import re
import time
import subprocess
import socket

USER_CONFIG_PATH = '/srv/ChronoRootControl/user_config.py'
REPO_DIR = '/srv/ChronoRootControl'
GIT_TIMEOUT_SECONDS = 120

logger = logging.getLogger(__name__)

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

def _git_result(result, code, message, changed=False, can_force=False):
    """Build the stable result shape shared by the API and config website."""
    return {
        'result': result,
        'code': code,
        'message': message,
        'changed': changed,
        'can_force': can_force,
    }


def _git_output(result):
    """Return subprocess output for classification/logging, never for clients."""
    return '\n'.join(
        part.strip() for part in (result.stdout or '', result.stderr or '')
        if part.strip()
    )


def _run_git(arguments):
    """
    Run one non-interactive git command in the deployment repository.

    The repository's canonical path is trusted only for this subprocess. If Git
    still reports dubious ownership (common after cloning a complete module),
    transparently retry with a process-local wildcard. No persistent git config
    is changed to solve safe.directory ownership mismatches.
    """
    env = dict(os.environ)
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_SSH_COMMAND'] = 'ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new'

    trusted_path = os.path.realpath(REPO_DIR)
    command = [
        'git', '-c', f'safe.directory={trusted_path}',
        '-C', REPO_DIR, *arguments,
    ]
    result = subprocess.run(
        command, capture_output=True, text=True,
        timeout=GIT_TIMEOUT_SECONDS, env=env
    )

    low = _git_output(result).lower()
    ownership_error = (
        'dubious ownership' in low
        or 'detected dubious ownership' in low
        or 'safe.directory' in low
    )
    if result.returncode != 0 and ownership_error:
        logger.info(
            "Retrying git command with process-local safe.directory wildcard"
        )
        result = subprocess.run(
            ['git', '-c', 'safe.directory=*', '-C', REPO_DIR, *arguments],
            capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, env=env
        )

    return result


def _classify_git_failure(result, allow_force=False):
    """Convert verbose git stderr into a concise, actionable update result."""
    output = _git_output(result)
    low = output.lower()
    logger.warning(
        "Software update git command failed with exit code %s: %s",
        result.returncode, output or 'no output'
    )

    destructive_markers = [
        'would be overwritten', 'local changes', 'not possible to fast-forward',
        'diverging', 'diverged', 'non-fast-forward', 'unmerged', 'needs merge',
        'please commit your changes', 'please stash them',
        'untracked working tree files',
    ]
    if allow_force and any(marker in low for marker in destructive_markers):
        return _git_result(
            False,
            'force_required',
            "The safe update was blocked by local files or commits. "
            "A force update can replace them with the remote version.",
            can_force=True,
        )

    authentication_markers = [
        'authentication failed', 'permission denied (publickey)',
        'could not read username', 'terminal prompts disabled',
        'repository not found',
    ]
    if any(marker in low for marker in authentication_markers):
        return _git_result(
            False,
            'authentication_error',
            "The remote repository rejected this device's credentials. "
            "Check the configured Git credentials.",
        )

    network_markers = [
        'could not resolve host', 'unable to access', 'connection timed out',
        'could not read from remote repository', 'network is unreachable',
        'temporary failure in name resolution', 'failed to connect', 'connection refused',
    ]
    if any(marker in low for marker in network_markers):
        return _git_result(
            False,
            'network_error',
            "The device could not reach the remote repository. "
            "Check its network connection and try again.",
        )

    repository_markers = [
        'not a git repository', 'no such file or directory',
        'no tracking information', 'has no upstream branch',
        'unknown revision or path not in the working tree',
        'does not appear to be a git repository',
    ]
    if any(marker in low for marker in repository_markers):
        return _git_result(
            False,
            'repository_error',
            "The deployment repository or its upstream is not configured correctly.",
        )

    permission_markers = [
        'permission denied', 'cannot lock ref', 'unable to create',
        'index.lock', 'could not open',
    ]
    if any(marker in low for marker in permission_markers):
        return _git_result(
            False,
            'permission_error',
            "Git could not write to the deployment repository. "
            "Check its permissions and ensure no other Git process is running.",
        )

    return _git_result(
        False,
        'git_error',
        f"Git could not complete the update (exit code {result.returncode}). "
        "Check the service log for details.",
    )


def _read_head():
    """Read HEAD, returning either its hash or a classified failure."""
    result = _run_git(['rev-parse', 'HEAD'])
    if result.returncode != 0:
        return None, _classify_git_failure(result)
    return (result.stdout or '').strip(), None


def _run_safe_git_update():
    old_head, failure = _read_head()
    if failure:
        return failure

    pull = _run_git(['pull', '--ff-only'])
    if pull.returncode != 0:
        return _classify_git_failure(pull, allow_force=True)

    new_head, failure = _read_head()
    if failure:
        return failure

    # A local branch that is ahead of its upstream makes `git pull` report
    # success even though the checkout does not match the fleet's remote state.
    upstream = _run_git([
        'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}',
    ])
    if upstream.returncode != 0:
        return _classify_git_failure(upstream)

    remote_ref = (upstream.stdout or '').strip()
    ahead = _run_git(['rev-list', '--count', f'{remote_ref}..HEAD'])
    if ahead.returncode != 0:
        return _classify_git_failure(ahead)

    try:
        ahead_count = int((ahead.stdout or '0').strip())
    except ValueError:
        return _git_result(
            False,
            'git_error',
            "Git returned an unexpected branch comparison result. "
            "Check the service log for details.",
        )

    if ahead_count:
        return _git_result(
            False,
            'force_required',
            f"The device has {ahead_count} local commit"
            f"{'s' if ahead_count != 1 else ''} not present on its remote branch. "
            "A force update can replace them with the remote version.",
            can_force=True,
        )

    if old_head == new_head:
        return _git_result(
            True,
            'up_to_date',
            "This device is already running the latest remote version.",
        )

    return _git_result(
        True,
        'updated',
        f"Software updated from {old_head[:8]} to {new_head[:8]}. "
        "Restart the services or reboot to run the new version.",
        changed=True,
    )


def _run_forced_git_update():
    old_head, failure = _read_head()
    if failure:
        return failure

    status = _run_git(['status', '--porcelain'])
    if status.returncode != 0:
        return _classify_git_failure(status)
    had_local_files = bool((status.stdout or '').strip())

    fetch = _run_git(['fetch', '--prune'])
    if fetch.returncode != 0:
        return _classify_git_failure(fetch)

    upstream = _run_git([
        'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}',
    ])
    if upstream.returncode != 0:
        return _classify_git_failure(upstream)
    remote_ref = (upstream.stdout or '').strip()

    reset = _run_git(['reset', '--hard', remote_ref])
    if reset.returncode != 0:
        return _classify_git_failure(reset)

    clean = _run_git(['clean', '-fd'])
    if clean.returncode != 0:
        return _classify_git_failure(clean)

    new_head, failure = _read_head()
    if failure:
        return failure

    changed = had_local_files or old_head != new_head
    if changed:
        return _git_result(
            True,
            'force_updated',
            f"Force update completed at {new_head[:8]}. The device now matches "
            "its remote branch. Restart the services or reboot to use it.",
            changed=True,
        )

    return _git_result(
        True,
        'up_to_date',
        "This device already matches its remote branch; no files were changed.",
    )


def run_git_update(force=False):
    """
    Update the deployment checkout and return a structured, readable result.

    A normal update is non-destructive. If local files or commits prevent it,
    the result has code ``force_required`` and callers may explicitly retry
    with ``force=True``. A force update resets to the configured upstream and
    deletes untracked files, while preserving ignored files.
    """
    try:
        if force:
            return _run_forced_git_update()
        return _run_safe_git_update()
    except subprocess.TimeoutExpired:
        return _git_result(
            False,
            'timeout',
            "The update timed out after 2 minutes. Check the device's network "
            "connection and try again.",
        )
    except FileNotFoundError:
        return _git_result(
            False,
            'git_unavailable',
            "Git is not installed on this device, so it cannot self-update.",
        )