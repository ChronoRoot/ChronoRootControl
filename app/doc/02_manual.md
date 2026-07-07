
# ChronoRoot User Manual

Welcome to ChronoRoot. This interface is designed to run completely autonomously once an experiment is set up. The system is split into several dedicated tabs to help you calibrate hardware, manage storage, and track your plant growth experiments.

## 1. Main Page (Dashboard)

The Dashboard is your home screen. It provides a real-time, bird's-eye view of what the robot is currently doing.

* **System Vitals:** Four quick-glance cards show your System Time, Hardware Status (whether a camera is currently taking a picture), Storage capacity, and Cloud Sync status.
* **System Alerts:** If something requires your attention—such as a delayed picture, a disconnected camera, or a nearly full hard drive—a red banner will appear at the top of the screen with recommendations.
* **Active Operations:** Displays currently running experiments. You can track progress bars, see exactly which camera is firing, and watch a live countdown to the next scheduled picture.
* **Up Next (Queue):** A list of experiments programmed to start in the future.
* **Archive:** Your finished or cancelled experiments. You can click here to quickly browse the generated data.

## 2. Device Configuration

Before starting a new experiment, you should prepare your hardware here. This page is divided into three logical steps:

* **Routine Initialization:** Ensure your system clock is correct (crucial for accurate timestamps) and run a **Hardware Scan**. The scan probes the multiplexer and ensures all connected cameras are responding.
* **Camera Calibration:** Turn the IR Backlight ON or OFF to match your growth chamber conditions. Use the **Live Focus** button to stream a real-time feed from a specific camera, allowing you to manually adjust the physical lens until the plate is perfectly sharp.
* **Advanced Hardware Configuration:** Set your multiplexer type and camera sensor model (e.g., V2, V3). Incorrect settings here will cause the hardware scan to fail.
* **Software Update:** Pulls the latest ChronoRoot code from the repository (a `git pull` against `/srv/ChronoRootControl`). It needs an internet connection and reports the outcome clearly — already up to date, updated, no internet, or blocked by local changes. Restart the services or reboot afterwards to run the new version. This same action is exposed to the Fleet Commander via `POST /api/update`.
* **Device Hostname:** Rename the module on the network. The change is staged safely through `raspi-config` (which updates both `/etc/hostname` and `/etc/hosts`, so the Pi can always resolve its own name and `sudo` never hangs) and only takes effect after a reboot, which is triggered automatically once you confirm. After about a minute the module comes back at `http://<new-name>.local`.

## 3. Creating an Experiment

Clicking "New Experiment" from the Dashboard opens the setup wizard. The system has built-in safety checks to ensure your parameters are physically possible.

* **Time Window:** Define the exact Start and End times.
* **Interval:** Set how often pictures should be taken. The minimum allowed interval is 5 minutes to prevent hardware overheating and storage bottlenecks.
* **Camera & Lighting:** Select which cameras (1 through 4) to activate for this run, and choose whether the IR backlight should turn on during capture.
* **Validation Check:** When you hit save, ChronoRoot calculates exactly how many pictures will be taken. If your hard drive does not have enough free space to hold the experiment, the system will block the creation and warn you.

## 4. Storage Management

ChronoRoot generates a massive amount of high-resolution images. This tab helps you manage where that data goes.

* **Active Storage:** Displays whether you are saving to the internal SD card or an external drive, alongside a clear capacity gauge.
* **Mount New Disks:** If you plug in a fresh USB drive, it will appear here automatically. Type a simple name (like "usb_data") and click Mount. The system will format permissions and ensure the drive automatically reconnects if the power goes out.
* **Change Location:** Allows you to swap the active working directory to any mounted USB drive.
* **Data Explorer:** A built-in file browser. You can click into any experiment folder, view the number of files, and directly open images in your browser without needing to download them first.

## 5. Data Synchronization (Sync)

For modules deployed in remote growth chambers, this tab allows you to automatically back up your data to a network server or cloud provider.

* **Live Status:** Shows what the background sync engine is doing. You can manually trigger a sync or cancel an active transfer at any time.
* **Connection Type:** Supports sending data to another local hard drive, an SFTP/FTP server, or advanced Cloud Providers (like Google Drive or AWS).
* **Server Credentials:** Enter your host IP, username, and password. You can use the **Test Connection** button to verify your credentials before saving.
* **Auto-Sync:** Toggle background syncing ON and set an interval (e.g., every 60 minutes). The system will quietly copy new pictures in the background without interrupting your ongoing experiments.

## 6. Wi-Fi & Networking

ChronoRoot is "Offline-First," meaning it does not need the internet to function, but it provides a smart networking interface for when you need to access it.

* **Field Mode (Hotspot):** If you plug the robot into a wall and it cannot find a known Wi-Fi network, it creates its own hotspot named comitup-. Connect your laptop or phone to this hotspot to access the ChronoRoot interface directly.
* **Lab Mode:** If the robot detects your lab's Wi-Fi, it connects automatically and displays its IP address here.
* **Forget Network:** If you move the robot to a new building, click this button. It will wipe the saved Wi-Fi credentials and immediately reboot back into Field Mode (Hotspot) so you don't lose access.