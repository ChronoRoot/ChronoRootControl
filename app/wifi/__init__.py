import os
import subprocess
import socket
import fcntl
import struct
from flask import Blueprint, render_template, jsonify
from config import Config

# --- BLUEPRINT DEFINITION ---
wifi_page = Blueprint('wifi_page', __name__,
                      template_folder='templates',
                      static_folder='static')

@wifi_page.context_processor
def inject_config():
    """
    Makes the 'config' variable available to all templates 
    rendered by this blueprint.
    """
    return dict(config=Config)

# --- HELPER FUNCTIONS ---

def get_wlan0_ip():
    """
    Directly asks the Linux kernel for the IP of the wlan0 interface.
    This works even if the Pi has no active internet connection.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ip = socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', bytes("wlan0"[:15], 'utf-8'))
        )[20:24])
        return ip
    except Exception:
        # Fallback if wlan0 is down or unassigned
        return "127.0.0.1"


# --- ROUTES ---

@wifi_page.route('/')
def index():
    """
    Renders the main Wi-Fi settings page. 
    Determines if the UI should show the Setup Portal or the Connected Status.
    """
    ip = get_wlan0_ip()
    
    # Comitup dynamically assigns IPs starting with 10.41. when hosting its Setup AP
    is_ap_mode = ip.startswith("10.41.")
    
    return render_template('wifi.html', 
                           is_ap_mode=is_ap_mode, 
                           ip=ip)

@wifi_page.route('/reset', methods=['POST'])
def reset_wifi():
    """
    Called via AJAX/Fetch from the frontend.
    Forces Comitup to forget the current network and immediately restart Hotspot mode.
    """
    try:
        # The 'd' flag tells comitup to delete the current connection and revert to AP
        subprocess.run(["sudo", "comitup-cli", "d"], check=True)
        return jsonify(
            success=True, 
            message="Resetting... The Pi will reboot into Hotspot mode."
        )
    except subprocess.CalledProcessError as e:
        return jsonify(
            success=False, 
            message=f"Command failed. Is the comitup service running? Error: {e}"
        ), 500
    except Exception as e:
        return jsonify(
            success=False, 
            message=f"System error: {e}"
        ), 500