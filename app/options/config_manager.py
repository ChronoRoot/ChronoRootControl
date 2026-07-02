import importlib.util
import os
import time
import os
import subprocess
import socket

USER_CONFIG_PATH = '/srv/ChronoRootControl/user_config.py'

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