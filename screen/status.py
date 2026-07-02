#!/usr/bin/env python3
import time
import subprocess
import json
import os
import getpass
import socket
from datetime import datetime
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import sh1106

# --- Configuration ---
STATUS_FILE = "/run/chronoroot_scheduler_status.json"
SCREEN_DELAY = 9.5  # 6 screens * 9.5s = 57s total loop

def get_network_info():
    """Gets real network data and accurately identifies the Comitup Hotspot."""
    ip_proc = subprocess.run(["/usr/bin/hostname", "-I"], capture_output=True, text=True)
    ip_list = ip_proc.stdout.strip().split()
    ip = ip_list[0] if ip_list else "NO IP"

    # Comitup assigns IPs in the 10.41.x.x range for its hotspot
    is_hotspot = ip.startswith("10.41.")
    ssid = ""

    if is_hotspot:
        # iwgetid fails in AP mode. We must use NetworkManager to get the Comitup SSID.
        try:
            nm_proc = subprocess.run(["nmcli", "-t", "-f", "NAME", "c", "show", "--active"], capture_output=True, text=True)
            for line in nm_proc.stdout.splitlines():
                if "comitup" in line.lower():
                    ssid = line.strip() 
                    # we need to remove everything after the second - to get the base SSID
                    parts = ssid.split("-")
                    if len(parts) >= 2:
                        ssid = "-".join(parts[:2])
                    break
        except:
            pass
        
        # Fallback just in case nmcli isn't responding fast enough
        if not ssid:
            ssid = "comitup-[ID]"
            
    else:
        # Standard client mode check (connected to a lab router)
        ssid_proc = subprocess.run(["/usr/sbin/iwgetid", "-r"], capture_output=True, text=True)
        ssid = ssid_proc.stdout.strip()
        
        if not ssid:
            ssid = "DISCONNECTED"

    return ssid, ip, is_hotspot

def get_system_uptime():
    """Reads actual hardware uptime directly from the OS kernel."""
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.readline().split()[0])
            hours, remainder = divmod(uptime_seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            return f"{int(hours)}h {int(minutes)}m"
    except:
        return "Unknown"

def load_system_state():
    """Reads the JSON strictly."""
    if not os.path.exists(STATUS_FILE):
        return {}
    with open(STATUS_FILE, 'r') as f:
        return json.load(f)

def main():
    serial = i2c(port=1, address=0x3C)
    device = sh1106(serial, width=128, height=64, rotate=0)

    user = getpass.getuser()
    host = socket.gethostname()

    for step in range(6):
        ssid, ip, is_hotspot = get_network_info()
        state = load_system_state()
        
        # State Parsing
        lock = state.get("hardware", {}).get("lock_info", {})
        is_locked = lock.get("status") == "LOCKED"
        owner = "Web User" if "User" in str(lock.get("owner", "")) else "Auto-Capture"

        last_p = str(state.get("hardware", {}).get("last_picture", "None")).split()[-1]
        next_p = str(state.get("scheduler", {}).get("next_picture", "None")).split()[-1]
        job_count = len(state.get("jobs", {}))

        with canvas(device) as draw:
            # We use Y coordinates: 0, 16, 30, 44 to ensure 4 distinct rows.
            
            if step == 0:
                draw.text((0, 0), "--- SYSTEM ID ---", fill="white")
                draw.text((0, 16), f"User: {user[:15]}", fill="white")
                draw.text((0, 30), f"Host: {host[:15]}", fill="white")
                draw.text((0, 44), f"IP:   {ip}", fill="white")

            elif step == 1:
                # INTELLIGENT NETWORK SCREEN
                if is_hotspot:
                    draw.text((0, 0), "--- HOTSPOT MODE ---", fill="white")
                    draw.text((0, 16), f"Join: {ssid[:15]}", fill="white")
                    draw.text((0, 30), "No Password", fill="white")
                    draw.text((0, 44), "Then open browser", fill="white")
                elif ssid == "ChronoRootWifi":
                    draw.text((0, 0), "--- LAB NETWORK ---", fill="white")
                    draw.text((0, 16), f"Net: {ssid[:16]}", fill="white")
                    draw.text((0, 30), "Pass: chronoroot", fill="white")
                    draw.text((0, 44), f"IP: {ip}", fill="white")
                else:
                    draw.text((0, 0), "--- NETWORK ---", fill="white")
                    draw.text((0, 16), f"Net: {ssid[:16]}", fill="white")
                    draw.text((0, 30), "Status: CONNECTED", fill="white")
                    draw.text((0, 44), f"IP: {ip}", fill="white")

            elif step == 2:
                draw.text((0, 0), "--- CAMERA ---", fill="white")
                if is_locked:
                    draw.text((0, 16), "Status: BUSY", fill="white")
                    draw.text((0, 30), f"By: {owner[:17]}", fill="white")
                else:
                    draw.text((0, 16), "Status: READY", fill="white")
                    draw.text((0, 30), "Waiting for jobs", fill="white")

            elif step == 3:
                draw.text((0, 0), "--- WEB CONTROL ---", fill="white")
                if is_hotspot:
                    draw.text((0, 16), "Configure Wi-Fi at:", fill="white")
                    draw.text((0, 30), f"http://{ip}/wifi", fill="white")
                else:
                    draw.text((0, 16), "Open in browser:", fill="white")
                    draw.text((0, 30), f"http://{ip}", fill="white")
                
            elif step == 4:
                draw.text((0, 0), "--- SCHEDULER ---", fill="white")
                draw.text((0, 16), f"Last: {last_p[:15]}", fill="white")
                draw.text((0, 30), f"Next: {next_p[:15]}", fill="white")
                draw.text((0, 44), f"Jobs: {job_count}", fill="white")

            elif step == 5:
                # NEW TIME & UPTIME SCREEN
                now = datetime.now()
                draw.text((0, 0), "--- SYSTEM TIME ---", fill="white")
                draw.text((0, 16), f"Date: {now.strftime('%Y-%m-%d')}", fill="white")
                draw.text((0, 30), f"Time: {now.strftime('%H:%M:%S')}", fill="white")
                draw.text((0, 44), f"Up:   {get_system_uptime()}", fill="white")

        time.sleep(SCREEN_DELAY)

if __name__ == "__main__":
    main()