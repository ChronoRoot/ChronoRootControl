# ChronoRootControl configuration

After installation, ChronoRootControl application is ready to use on module using selected default.

You can adapt the configuration to your particular setup if you wish. We provided an example user config file that can be used to overwrite the default values.

First, copy the example file `user_config.py.example` to `user_config.py`, and then edit it with your favorite text editor.

```python
"""
Example user configuration file
Copy this file to 'user_config.py' and modify values as needed.
Only include the settings you want to override from default_config.py
"""

class Config(object):
    # Example overrides - uncomment and modify as needed
    
    # Site customization
    # SITE_NAME = "My Custom ChronoRoot"
    # SITE_DESC = "My customized ChronoRoot setup"
    
    # Working directory
    # WORKING_DIR = "/custom/path/to/data"
    
    # Camera configuration
    # CAMS = (1, 2)  # Only use cameras 1 and 2
    # CAM_WARMUP = 5  # Custom warmup time
    
    # Capture profiles (Picamera2 / libcamera controls)
    # DEFAULT_CAPTURE_PROFILE = "backlight_manual"
    # CAM_CAPTURE_PROFILES = {
    #     'backlight_manual': {
    #         'grayscale': True,
    #         'controls': {
    #             'AeEnable': False,
    #             'AwbEnable': False,
    #             'ColourGains': (1.0, 1.0),
    #             'NoiseReductionMode': 0,
    #             'ExposureTime': 30000,   # microseconds, tune for your chamber
    #             'AnalogueGain': 1.0,
    #         },
    #     },
    # }
    
    # Debug settings
    # DEBUG = True
```

Noteworthy configuration variables are

* `WORKING_DIR` that defines the root directory where the data are stored (`/srv/ChronoRootData` by default). To use the first USB drive plugged, set it to `/media/usb0`.

* `CAMS` the cameras available to be used with the multiplexer.

* `SELECTOR_PRESENT` that defines if a camera multiplexer is present on the module to be able to control several cameras.

* `PREVIEW_FPS` sets the live Focus preview rate. The default is 10 FPS. Picamera2
  is camera-paced at this rate and its completed-request queue is disabled, so the
  preview waits for a new sensor frame instead of replaying an older queued frame.

* `STREAM_STALL_TIMEOUT` is the no-frame interval before preview recovery starts
  (15 seconds by default). `STREAM_CLOSE_GRACE_TIMEOUT` is the additional time
  allowed for Picamera2 to close (5 seconds by default).

## Camera capture profiles

Since the migration to Picamera2 (libcamera), camera tuning is driven by named profiles in `CAM_CAPTURE_PROFILES`, not by the legacy `CAM_PARAMS` ISP keys. Each profile bundles a set of libcamera controls that are applied via `set_controls()` right before each capture, plus a `grayscale` flag controlling whether the saved PNG is converted to monochrome.

Three profiles ship by default:

* `backlight_manual` (the default, selected by `DEFAULT_CAPTURE_PROFILE`): for modules fitted with a high-pass infrared filter. The camera only sees the IR backlight, so auto exposure, auto white balance, and noise reduction are disabled and exposure/gain are locked. This preserves the contrast and sharp root edges that the auto algorithms otherwise destroy. Tune `ExposureTime` (microseconds) and `AnalogueGain` for your chamber.

* `backlight_auto`: also for the IR filter, but lets auto exposure and auto white balance run while keeping noise reduction off so fine root edges are preserved. The output is still converted to grayscale.

* `color_auto`: for when the IR filter is physically removed and you want normal color imaging. Auto exposure and auto white balance run normally with standard noise reduction.

The active profile applies to every capture (experiments, diagnostics, and the per-camera tests). The legacy `CAM_PARAMS` ISP settings (`contrast`, `brightness`, `awb_mode`, `exposure_mode`, etc.) are no longer applied; only the profile controls take effect on the hardware.

The easiest way to switch the active profile or tune the manual backlight (`ExposureTime`, `AnalogueGain`, denoise) is the **Camera Capture Profile** card under Settings -> Advanced Hardware Configuration. Saving there writes to `user_config.py` and restarts the services so the change takes effect on the next capture.

`CROP_TO_SQUARE` (toggled via the "Crop images to square" switch in the **Hardware Topology** card) makes both captures and the live preview request a square resolution (`side x side`, with `side` the smaller of the sensor's width/height, e.g. 2464x2464). libcamera then outputs a centered square crop, removing the backlight visible at the sides of square plates.

## Debug and recovery

The bottom of the Configuration page contains a **Debug & Recovery** panel. Diagnostics are loaded only when **Load / Refresh** is pressed.

The standard installation writes application logs to `/srv/ChronoRootControl/log/` (singular):

* `ChronoRootControl.log` contains Flask and experiment workflow messages.
* `ChronoRootControl_SHDL.log` contains camera, multiplexer, scheduler, and live-stream details.
* `ChronoRootControl_CRASH.log` receives Python fatal-signal tracebacks when the process can still write them.

The panel reads only these configured files and the watchdog reboot record; it is not a general filesystem browser. Log output is bounded to the newest portion so opening a large file does not exhaust Raspberry Pi memory.

Application logs cannot record a process that is killed by the kernel. The service and journal views therefore include uWSGI, nginx, the multiplexer boot fix, and kernel warnings. The default kernel view filters routine Raspberry Pi boot noise and keeps OOM kills, camera/I2C failures, filesystem faults, hangs, thermal events, and power/undervoltage warnings. **Raw kernel warnings** remains available when the complete boot journal is needed.

Log cleanup is manual. **Clear log** truncates the selected application log in place after confirmation, preserving the file used by active logger processes. The watchdog reboot record cannot be cleared from the interface because it enforces the automatic-reboot circuit breaker.

The Focus overlay reports **target FPS** (the configured camera rate), **actual
FPS** (a rolling in-memory measurement of delivered frames), and **last-frame
age**. Those metrics stay in the web worker; they are not written to disk every
frame. If no frame arrives for the configured stall timeout, the crash log
receives all Python thread stacks and a bounded Picamera2 close begins. If the
close grace also expires, only the stuck uWSGI web worker is terminated; the
uWSGI master replaces it and Linux releases that worker's `/tmp/cam.lock`. The
scheduler mule is not terminated, and the Pi is not rebooted. If the worker does
not come back, restart uWSGI after collecting journals from this panel.