# ChronoRoot Setup Guide

This guide provides a streamlined procedure to set up a ChronoRoot module on a **Raspberry Pi Zero 2 W** or **Raspberry Pi 3B/3B+ with multiplexer**. 

## 1. Prepare the OS

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to flash your MicroSD card (High Endurance recommended).

1. **Device:** Raspberry Pi Zero 2 W or Raspberry Pi 3B/3B+.
2. **OS:** Raspberry Pi OS Lite (64-bit) — *Trixie*.
3. **OS Customization:**
* **Hostname:** e.g., `chronoroot-mini`.
* **User:** Define your username and password.
* **WiFi:** Enter your current local WiFi credentials (needed for initial package installation).
* **Services:** Enable **SSH**.

## 2. Install System Dependencies

This command installs the web server stack, imaging libraries, and the **Picamera2** environment. This set of dependencies is required for both single-camera and multiplexer setups, so you can run it regardless of your hardware configuration choice. 

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y \
    git i2c-tools libffi-dev \
    python3 python3-pip python3-rpi.gpio python3-venv \
    python3-picamera2 libcamera-apps \
    nginx-full uwsgi uwsgi-plugin-python3 \
    libtiff-dev libjpeg-dev libopenjp2-7-dev zlib1g-dev \
    libfreetype6-dev liblcms2-dev libwebp-dev \
    libharfbuzz-dev libfribidi-dev libxcb1-dev \
    network-manager comitup rclone

```

## 3. Hardware Configuration

Your hardware configuration depends on whether you are connecting a single camera directly to the Raspberry Pi, or using a multiplexer (like the IVPort) to connect multiple cameras.

Note that memory split and boot-wait settings are handled dynamically by modern Raspberry Pi OS, so you do not need to configure them manually.

### Option A: Single Camera Setup (No Multiplexer)

Note: This was tested only for Raspberry Pi Zero 2 W, but should work on any Raspberry Pi with a camera port.

If you are connecting a single camera directly to the Raspberry Pi's camera port, the system will automatically detect the camera model (V2, V3, HQ, etc.) and load the correct drivers.

Simply enable the I2C interface (used for system diagnostics) and reboot:

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot

```

Here is the complete, updated text for **Option B** to drop directly into your installation guide. It replaces the old Option B entirely, explaining the cold-boot issue and providing the unified, copy-pasteable script that handles everything safely.

### Option B: Multiplexer Setup (IVPort with Multiple Cameras)

**Note:** This was tested on the IVPort 4-channel multiplexer on Raspberry Pi 3B, 3B+, and Zero 2 W models.

If you are using a camera multiplexer, you must apply a special configuration. Standard Raspberry Pi OS automatically detects which camera is plugged in during boot. However, a multiplexer physically blocks this auto-detection. On a cold boot (power loss), the multiplexer resets to a closed state, the OS assumes no camera is connected, and the camera drivers crash.

To fix this, we must:

1. Disable camera auto-detect.
2. Force the OS to load your specific camera driver.
3. Install a "self-healing" background service. If the Pi suffers a power loss and boots up blindly, this service detects the camera failure, automatically forces the multiplexer open to Channel 1, and performs a soft reboot to self-correct.

Run the following unified block of commands in your terminal. It may be easier to run as sudo su, and copy-paste the entire block at once to avoid any permission issues. The script is idempotent and safe to run multiple times.

> **Important:** If you are using a V3 or HQ camera, simply change `CAMERA_SENSOR="imx219"` at the top of the script to `imx708` or `imx477` before pressing Enter.

```bash
# ==========================================
# 1. DEFINE YOUR CAMERA MODEL HERE
# Options: imx219 (V2), imx708 (V3), imx477 (HQ)
# ==========================================
CAMERA_SENSOR="imx219"

echo "Configuring multiplexer and cold-boot fix for: $CAMERA_SENSOR"

# 2. Enable I2C interface (Required to switch multiplexer channels)
sudo raspi-config nonint do_i2c 0

# 3. Determine boot config location (Bookworm vs Bullseye)
CONFIG_TXT=$(ls /boot/firmware/config.txt /boot/config.txt 2>/dev/null | head -n 1)

# 4. Disable camera auto-detect
sudo sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/g' $CONFIG_TXT
sudo sed -i 's/^#camera_auto_detect=0/camera_auto_detect=0/g' $CONFIG_TXT

# 5. Remove any existing camera overlays and force the chosen one
sudo sed -i '/^dtoverlay=imx/d' $CONFIG_TXT
echo "dtoverlay=$CAMERA_SENSOR" | sudo tee -a $CONFIG_TXT > /dev/null

# 6. Create the Python-native self-healing script
cat << 'EOF' | sudo tee /usr/local/bin/chronoroot-mux-fix.sh > /dev/null
#!/bin/bash

FLAG_FILE="/etc/chronoroot_coldboot_flag"

# Ask Python's picamera2 if it successfully initialized the hardware
if python3 -c 'from picamera2 import Picamera2; Picamera2()' 2>/dev/null; then
    # Camera active. Delete flag.
    rm -f $FLAG_FILE
    exit 0
else
    # Camera failed.
    if [ -f $FLAG_FILE ]; then
        # Already tried rebooting. Hardware broken. Abort to prevent loop.
        rm -f $FLAG_FILE
        exit 0
    else
        # Cold boot detected. Force mux to Channel 1, set flag, and reboot.
        i2cset -y 1 0x70 0x01 2>/dev/null
        touch $FLAG_FILE
        reboot
    fi
fi
EOF

# Clean hidden Windows line endings (prevents Exec format errors from copy/pasting)
sudo sed -i 's/\r$//' /usr/local/bin/chronoroot-mux-fix.sh
sudo chmod +x /usr/local/bin/chronoroot-mux-fix.sh

# 7. Create and enable the systemd service
cat << 'EOF' | sudo tee /etc/systemd/system/chronoroot-mux-fix.service > /dev/null
[Unit]
Description=ChronoRoot Multiplexer Cold Boot Fix
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/chronoroot-mux-fix.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo sed -i 's/\r$//' /etc/systemd/system/chronoroot-mux-fix.service
sudo systemctl daemon-reload
sudo systemctl enable chronoroot-mux-fix.service
```

Now, you will need to reboot your Raspberry Pi to apply the changes after finishing the complete installation.

## 4. Networking & Field Mode

The ChronoRoot Mini is "offline-first." It is designed to work immediately upon power-up, regardless of whether a local Wi-Fi network is present.

### A. How the Network Behaves

* **Connected Mode:** On boot, the Pi looks for a saved network (Default: SSID `ChronoRootWifi` / Pass: `chronoroot`). If found, the app is available at the hostname you set during OS customization (e.g., `http://chronoroot-mini.local`).
* **Field Mode (Hotspot):** If no network is found, the Pi creates a hotspot named `comitup-<number>`. Connecting to this hotspot triggers a captive portal that opens the **ChronoRoot Control App** directly.

### B. Comitup Network Routing

To allow the ChronoRoot App to "host" the Wi-Fi settings, we move the Comitup engine to a background port.

**Move Comitup to Port 8081:**
Run this automated command to free up Port 80 for your application:

```bash
sudo sed -i 's/port=80,/port=8081,/g' /usr/share/comitup/web/comitupweb.py
sudo systemctl restart comitup-web

```

### C. The Integrated Wi-Fi Tab

The Wi-Fi settings are now handled inside the ChronoRoot application.

* **In the Field:** The Wi-Fi tab will show the Comitup network selector inside an iframe, allowing you to bridge the module to a local router.
* **In the Lab:** The Wi-Fi tab will show your current connection status and provide a **"Forget Network"** button to force the module back into Hotspot mode for reconfiguration.

## 5. Directory Structure & Permissions

ChronoRoot requires specific directories for data logging and application files.

```bash
# Data Directory
sudo mkdir -p /srv/ChronoRootData
sudo chmod a+rw /srv/ChronoRootData

# Application Directory
sudo mkdir -p /srv/ChronoRootControl
sudo chmod -R a+rw /srv/ChronoRootControl

```

## 6. Install ChronoRootControl

Clone the repository and set up the Python Virtual Environment.

```bash
cd /srv/ChronoRootControl
git clone https://github.com/ChronoRoot/ChronoRootControl.git .

# Setup Virtual Env
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --upgrade pip
CFLAGS="-fcommon" pip install -r requirements.txt

```

## 7. Web Server Configuration (Nginx + uWSGI)

### uWSGI Configuration

Link the application to uWSGI to handle Python execution:

```bash
sudo cp /srv/ChronoRootControl/server/uwsgi.ini /etc/uwsgi/apps-available/ChronoRootControl.ini
cd /etc/uwsgi/apps-enabled/
sudo ln -s ../apps-available/ChronoRootControl.ini .
sudo systemctl restart uwsgi

```

### Nginx Configuration

Set up Nginx as the reverse proxy:

```bash
sudo cp /srv/ChronoRootControl/server/nginx.conf /etc/nginx/sites-available/chronorootcontrol.conf
sudo rm -f /etc/nginx/sites-enabled/default
cd /etc/nginx/sites-enabled/
sudo ln -s ../sites-available/chronorootcontrol.conf .
sudo systemctl restart nginx

```

## 8. Optional: I2C OLED Status Display

This section is for modules equipped with a small I2C OLED screen (e.g., SSD1306) to monitor system status, IP address, and camera activity.

### A. Install Display System Dependencies

These libraries are required for the Python Imaging Library (Pillow) to render fonts and shapes on the Pi Zero 2 W.

```bash
sudo apt update && sudo apt install -y \
    libjpeg-dev zlib1g-dev libfreetype-dev \
    liblcms2-dev libopenjp2-7-dev libtiff-dev 
```

### B. Install Python Libraries in Venv

You must install the display drivers into the same virtual environment used by the ChronoRootControl application.

```bash
cd /srv/ChronoRootControl
source venv/bin/activate

# Install the OLED driver and the psutil helper
pip install luma.oled psutil

```

### C. Deploy the Status Script

Create the display script within your project directory:

```bash
mkdir -p /srv/ChronoRootControl/screen
# Now create your /srv/ChronoRootControl/screen/status.py file 
# using the code provided in the previous steps.

```

### D. Automated Scheduling (Cron)

To ensure the screen updates every minute (even after a reboot), add it to the system crontab.

1. Open the crontab editor:
```bash
crontab -e

```


2. Append the following lines to the end of the file:

```bash
   # Update OLED screen every minute using the project venv
   * * * * * /srv/ChronoRootControl/venv/bin/python3 /srv/ChronoRootControl/screen/status.py
   
   # Update screen immediately upon boot
   @reboot /srv/ChronoRootControl/venv/bin/python3 /srv/ChronoRootControl/screen/status.py

```

### E. Manual Test

To verify the screen is working without waiting for the next minute, run the script manually from within the `venv`:

```bash
/srv/ChronoRootControl/venv/bin/python3 /srv/ChronoRootControl/screen/status.py

```