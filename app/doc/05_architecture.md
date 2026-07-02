# App architecture

The application is built on a decoupled architecture to ensure stability, hardware safety, and precise timing for long-term imaging experiments. It comprises several primary execution contexts running in parallel:

1. **The Web Interface:** A Flask web app serving the UI and providing a comprehensive RESTful API for system telemetry.
2. **The uWSGI Mule:** A background process handling the core automation loop, acting as the system's "Chief Operator."
3. **The Scheduler:** An instance of BackgroundScheduler (from the apscheduler module), managed by the Mule to trigger precise image captures.
4. **The Background Daemons:** Standalone threaded workers (SystemHealthDaemon and AutoSyncDaemon) that monitor system vitals and manage external cloud backups.

## The uWSGI Mule & Background Daemons

From the uWSGI documentation:

> *Mules are worker processes living in the uWSGI stack but not reachable via socket connections, they are used as a generic subsystem to offload tasks. You can see them as a more primitive spooler. They can access the entire uWSGI API and can manage signals and be communicated with through a simple string-based message system.*

The Mule serves as the messenger between the web app and the scheduler. It operates an infinite loop that listens for Inter-Process Communication (IPC) messages from the Flask application to create, cancel, or modify experiments.

Upon startup, the Mule performs a highly resilient synchronization routine (resync_with_disk). It scans physical storage for valid experiments, cross-references actual captured images against the expected timeline to detect missed frames during downtime, and automatically resumes jobs in the scheduler, ensuring continuity after a power loss or reboot.

Alongside the scheduler, the Mule launches two critical background threads:

* **SystemHealthDaemon:** Wakes up periodically to monitor storage capacity, calculate disk usage, and dynamically recalculate the expected progress of all running experiments to feed the anomaly detection engine.
* **AutoSyncDaemon:** An isolated worker that handles data offloading via rclone. It reads the configuration state and executes network transfers (SFTP/FTP) entirely independent of the main camera workflow, preventing network latency from delaying a scheduled picture.

## Phototron Module

This module implements all Raspberry Pi hardware features and safety constraints.

The RpiModule class, implemented in phototron/rpimodule.py, handles image capture with or without LED lighting. It utilizes the **Singleton** design pattern to serialize access to the GPIO pins and implements a system-wide FileLock. This strict locking mechanism prevents resource contention between the scheduled "Take Picture" routine and immediate "Check Cameras" diagnostics requested by the user.

The module implements a **Dual Logging System**:

* **Hardware Log (SHDL):** Tracks deep system diagnostics, I2C probe failures, and multiplexer faults.
* **Experiment Log:** Tracks the logical flow of the experiment, logging successful captures and individual camera skips directly to the experiment's timeline.

The module supports two distinct operations:

1. **Scheduled Capture:** Iterates through the configured camera ports, executing a fault-tolerant loop where each camera is probed and captured in isolation. If a single camera fails, it is logged and bypassed without crashing the overall experiment.
2. **Check Cameras:** A real-time hardware validation triggered manually by the user. It scans the entire multiplexer array to verify I2C responsiveness and reports the immediate health status of each port.

To interface with the hardware, the RpiModule relies on factory patterns to load the correct drivers based on the user_config.py settings:

* The **Camera Selector** (phototron/camera_selector.py) manages the **IVPort Multiplexer**, enforcing a protocol where the I2C bus is actively probed before data transmission to prevent kernel panics.
* The **Camera Driver** utilizes the modern **Picamera2** library. It dynamically applies hardware profiles (e.g., RPICAM_V2, RPICAM_V3) to set correct native resolutions, stream aspect ratios, and autofocus behaviors.

## System Interfaces & Management Modules

Beyond experiment scheduling and camera control, ChronoRoot provides standalone modules to handle system-level operations directly from the web interface.

### Storage Management

The system is designed to handle large volumes of image data safely, supporting seamless transitions between the internal SD card and external USB drives.

* **Dynamic Disk Detection:** The storage module uses lsblk to locate unmounted partitions and /proc/mounts to identify active drives.
* **Automated Mounting & Permissions:** When a user mounts a new USB drive via the UI, the backend automatically generates the mount point, applies universal write permissions (using umask=000 for FAT32/NTFS or chmod 777 for Ext4), and persists the drive via its UUID in /etc/fstab so it survives a reboot.
* **Data Explorer:** A built-in web file browser allows researchers to explore experiment folders, check file sizes, and view raw image captures directly.

### Data Synchronization

Because ChronoRoot modules are often deployed in the field or in isolated growth chambers, the system features an asynchronous backup engine powered by rclone.

* **The AutoSyncDaemon:** Network transfers are entirely decoupled from the Flask application and camera hardware. The daemon reads the target interval, executes the rclone sync, and parses the terminal output stream to push real-time progress percentages back to the UI via the RAM-disk state.
* **Protocol Support:** Natively supports SFTP and FTP, with a configuration wrapper that safely encrypts and obfuscates passwords within rclone.conf. It also supports "Advanced" cloud providers (Google Drive, AWS S3) requiring terminal-based OAuth setup.
* **Non-Blocking Feedback:** Users can trigger manual syncs, cancel active transfers via IPC signals, and view connection test results without freezing the web interface.

### Network & Wi-Fi Operations

The networking architecture is "Offline-First," built on top of the **Comitup** service.

* **Field Mode (Hotspot):** If the Raspberry Pi boots and cannot find a known Wi-Fi network, Comitup automatically spawns a local Access Point (e.g., comitup-1234). Connecting to this AP triggers a captive portal that loads the ChronoRoot interface.
* **Integrated Setup:** The Wi-Fi tab dynamically detects its operating mode by checking the wlan0 IP address. If the Pi is hosting the hotspot, the UI serves an iframe of the Comitup portal (running silently on port 8081) to allow bridging to a local lab router.
* **Lab Mode:** If connected to a standard network, the UI provides system IP telemetry and a "Forget Network" action (sudo comitup-cli d), which instantly wipes the connection profile and forces the Pi back into Hotspot mode for reconfiguration.

## Master Controller / Fleet Integration

Multiple ChronoRoot modules in a growth chamber or lab are typically supervised by a **Master Controller** that polls each module over HTTP. Integration is entirely through the REST API mounted at `/api` on each Raspberry Pi.

**Live telemetry** — `GET /api/status` reads the unified RAM-disk state (`/run/chronoroot_scheduler_status.json`) and returns identity, per-camera reports, active experiments (`jobs`), alerts, and sync status. Poll every few seconds.

**Cameras** — Each port exposes `health` (last completed result: `UNTESTED`, `OK`, `NOT DETECTED`, `ERROR`) and `activity` (`IDLE` or `CAPTURING`). Count only `health == "OK"` for operational cameras. Never alert on `CAPTURING` or `UNTESTED`.

**IR lights** — `lights_info.health_check.status` uses the same style: `UNTESTED`, `OK`, or `NOT DETECTED` (last completed diagnostic). `lights_info.state` is the live `ON`/`OFF` backlight state.

**Experiments — two sources of truth** — Disk `info.json` (via `GET /api/<expid>`) holds authoritative `status` (`RUNNING`, `ERROR`, `FINISHED`, etc.). RAM `jobs` in `/api/status` holds live round progress (`progress.taken` / `expected`), `next_run_time`, and `last_capture`. When an experiment finishes or is cancelled, it is **removed from `jobs`**; use `GET /api/history` for per-camera file counts.

**Alerts** — The status payload includes a computed `alerts` object (`lock_stuck`, `picture_overdue`, `camera_gaps`, `all_cameras_failed`, watchdog limits). Notify operators when `alerts.has_warnings` is true.

**Archived sync** — `GET /api/history` returns `FINISHED` and `CANCELLED` experiments with `per_camera` file counts for central database ingestion.

See the **[REST API (Fleet)](/help/api)** documentation page for the full integration guide: experiment lifecycle, RAM field reference, JSON examples, and fleet workflow.

## Data Persistence & State Management

The application manages state through two distinct, decoupled mechanisms to ensure the web interface never blocks hardware operations.

### The Experiment Models (Long-term Storage)

Implemented in app/experiment/models.py, this handles the long-term storage of experiment data. This class serializes the user configuration, interval metadata, and physical image paths into info.json files located on the active storage partition, ensuring scientific data is preserved permanently across reboots.

### The Unified RAM-Disk State (Real-time Telemetry)

Implemented in app/options/schedulerstatus.py, this manages real-time system monitoring. Instead of hitting the database or disk continuously, the backend writes its exact state to a volatile JSON file located in the system's RAM (/run/chronoroot_scheduler_status.json).

This unified dictionary serves as the "Single Source of Truth" for the entire system and powers the RESTful API and the frontend dashboard. It tracks:

* **System Identity:** Hostname, active IP address, and MAC address.
* **Hardware Telemetry:** Per-camera `health` (last completed result, including `UNTESTED` at boot) and `activity` (`IDLE` / `CAPTURING`), plus hardware lock owner and timestamp.
* **Job Progress:** Dynamic counters showing capture rounds taken versus the expected timeline (`jobs[expid].progress`). Removed from RAM when an experiment finishes or is cancelled.
* **Anomaly Detection:** An active alert engine that flags warnings if a hardware lock is held longer than the allowed threshold (indicating a hung bus) or if an experiment misses a scheduled capture window.