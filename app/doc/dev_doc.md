# App architecture

The application is built on a decoupled architecture to ensure stability and precise timing for long-term imaging experiments. It comprises two primary execution contexts:

1. The web interface, which is a Flask web app handling user interaction.
2. The uWSGI Mule module, a background process handling automation.
3. The Scheduler (`BackgroundScheduler` from the `apscheduler` module), managed by the Mule.

## uWSGI Mule

From uWSGI documentation:

*Mules are worker processes living in the uWSGI stack but not reachable via socket connections, they are used as a generic subsystem to offload tasks. You can see them as a more primitive spooler. They can access the entire uWSGI API and can manage signals and be communicated with through a simple string-based message system.*

The Mule acts as the system's "Chief Operator," serving as the messenger between the web app and the scheduler. Implemented in the `uwsgiMules/shutting_director_mule.py` file, it runs independently of the web server's request cycle. It hosts an instance of the `BackgroundScheduler` which triggers image capture jobs at user-defined intervals. The Mule operates an infinite loop that listens for Inter-Process Communication (IPC) messages from the Flask application to create, cancel, or modify experiments. Upon startup, it performs a synchronization routine (`resync_with_disk`) that scans storage for valid experiments and automatically resumes them, ensuring continuity after a system reboot.

## Phototron module

This module implements all Raspberry Pi hardware features.

The `RpiModule` class, implemented in `phototron/rpimodule.py`, implements all ChronoRoot robot features, primarily image capture with or without LED lighting. It utilizes the **Singleton** design pattern to serialize access to the GPIO pins and implements a system-wide `FileLock`. This locking mechanism prevents resource contention between the two main workflows: the scheduled "Take Picture" routine and the immediate "Check Cameras" diagnostic.

The module supports two distinct operations. The **Scheduled Capture** iterates through the configured camera ports, executing a fault-tolerant loop where each camera is probed and captured in isolation. The **Check Cameras** operation is a real-time hardware validation triggered manually by the user; it scans the entire multiplexer array to verify I2C responsiveness and reports the immediate health status of each port.

To control the hardware, the `RpiModule` calls `SelectorFactory` and `CameraFactory` classes. These are **Factory** design pattern implementations. Each provides the specific driver object specified in the configuration file:

* The **Camera Selector** (`phototron/camera_selector.py`) manages the **IVMech Multiplexer**, enforcing a protocol where the I2C bus is probed before data transmission.
* The **Camera Driver** (`phototron/camera.py`) wraps the `picamera` library, using context managers to ensure the GPU connection is opened and closed strictly for each frame.

## Data Persistence & State Management

The application manages state through two distinct mechanisms.

The **Experiment Models**, implemented in `app/experiment/models.py`, handle the long-term storage of experiment data. This class serializes configuration, logs, and image paths into `info.json` files located on the SD card, ensuring scientific data is preserved across reboots.

The **Scheduler Status**, implemented in `app/options/schedulerstatus.py`, manages real-time system monitoring. It writes the current system state—such as the lock owner, next scheduled run time, and live camera health—to a JSON file located in the system's RAM (`/run/chronoroot_scheduler_status.json`). 