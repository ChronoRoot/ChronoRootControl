"""
The default configuration settings
    Default means : RaspberryPi 3 with IVPort2 4 cam multiplexer & 4 cameras connected
"""
import os
import logging

class Config(object):
    DEBUG = False

    WTF_CSRF_ENABLED = True
    SECRET_KEY = 'YUYLMOZmyFP1WykCBTlrR7daubECzxnP0HLqsStigqskc9kD6nq2FBpDXKN5H1p8'

    SITE_NAME = "ChronoRootControl"
    SITE_DESC = "A web interface to control a ChronoRoot module"

    WORKING_DIR = "/srv/ChronoRootData"
    
    # --- Camera & Multiplexer Hardware ---    
    # Hardware multiplexer type. 
    # Valid options: "SINGLE", "TYPE_QUAD2"
    SELECTOR_TYPE = "SINGLE" 
    
    CAMS = (1, ) # CAMERAS PRESENCE & DISPOSITION
                        # possible values (if we have ivport)
                        # One cam :  (1, ), (2, ), (3, ), (4, )
                        # Two cams : (1,2),(1,3),(1,4),(2,3),(2,4)
                        # Three cams : (1,2,3),(1,2,4),(2,3,4)
                        # Four cams : (1,2,3,4)
                        # the order inside the list is not important
    CAM_WARMUP = 5
    CAM_ADJUST_TIME = 10

    CAM_RETRIES = 10
    CAM_WAIT_AFTER_RETRAY = 10
    PER_CAMERA_ALLOWANCE = 5 # Minutes to wait per camera for an experiment to finish before flagging an alert. 

    # --- HARDWARE PROFILES ---
    CAMERA_TYPE = "RPICAM_V2" # Change to "RPICAM_V3" or "RPICAM_V3_WIDE" as needed

    CAMERA_PROFILES = {
        "RPICAM_V2": {
            "name": "Raspberry Pi Camera Module V2",
            "resolution": (3280, 2464),       # Native 4:3 max resolution
            "stream_resolution": (800, 600),  # 4:3 streaming
            "autofocus": False
        },
        "RPICAM_V3_V2COMP": {
            "name": "Raspberry Pi Camera Module V3 (V2 compatible mode)",
            "resolution": (3280, 2464),       
            "stream_resolution": (800, 600),  
            "autofocus": True
        },
        "RPICAM_V3": {
            "name": "Raspberry Pi Camera Module V3",
            "resolution": (4608, 2592),       # Native 16:9 max resolution
            "stream_resolution": (800, 450),  # 16:9 streaming (prevents squishing)
            "autofocus": True
        },
        "RPICAM_V3_WIDE": {
            "name": "Raspberry Pi Camera Module V3 Wide",
            "resolution": (4608, 2592),
            "stream_resolution": (800, 450),
            "autofocus": True
        }
    }
    
    KEEP_AUTOFOCUS = False
    FOCUS_DISTANCES = {}
    
    # --- TIME & NETWORK DEFAULTS ---
    USE_NTP = False
    TIME_ZONE = "Europe/Paris"
    NTP_SERVER = "pool.ntp.org"

    # Unified Date Format
    DATE_FORMAT = '%Y-%m-%d_%H-%M-%S'
    PRETTY_FORMAT = '%Y-%m-%d %H:%M:%S' # For UI/Logs
    
    MULE_NO = 1

    APP_ROOT = os.path.dirname(os.path.realpath(__file__))
    LOGFILE = os.path.join(
                    APP_ROOT,
                    'log/%s.log' % SITE_NAME.replace(' ', '_')
                    )

    SHDL_LOG_FILE = os.path.join(
                        APP_ROOT,
                        'log/%s_SHDL.log' % SITE_NAME.replace(' ', '_')
                    )
    LOG_FORMAT = '[%(asctime)s] [%(levelname)s] [pid/%(process)d] %(message)s'
    LOG_LEVEL = logging.DEBUG

    LOCK_FILE = "/tmp/cam.lock"
    LOCK_TIMEOUT = 5

    MAX_WAIT = 100 # time to wait to another experience to terminate, min 5
    IR_GPIO = 32
    IR_WARM_UP = 5

    # Default capture parameters carried on each Experiment (Experiment.img_params).
    # Picamera2 hardware controls now live in CAM_CAPTURE_PROFILES below. We do NOT
    # pin a 'capture_profile' here: leaving it unset lets every capture fall back to
    # the live DEFAULT_CAPTURE_PROFILE, so changing the active profile in the UI
    # actually takes effect. Add 'capture_profile' to a specific call's params only
    # if you need to override the default for that single capture.
    CAM_PARAMS = {
        "format" : 'png',
        'resize' : None, # (1280, 720)
    }

    # --- PICAMERA2 CAPTURE PROFILES ---
    # Named bundles of libcamera controls applied via picam2.set_controls() right
    # before each capture. Select one per capture via params["capture_profile"];
    # callers fall back to DEFAULT_CAPTURE_PROFILE when unset.
    DEFAULT_CAPTURE_PROFILE = "backlight_manual"

    CAM_CAPTURE_PROFILES = {
        # IR backlight, fully manual: the sensor (behind a high-pass IR filter)
        # only sees the IR backlight, so AE/AWB/denoise are locked to keep
        # contrast and sharp root edges. Tune ExposureTime / AnalogueGain.
        "backlight_manual": {
            "grayscale": True,
            "controls": {
                "AeEnable": False,
                "AwbEnable": False,
                "ColourGains": (1.0, 1.0),  # neutral R/B so gray conversion is unbiased
                "NoiseReductionMode": 0,    # off, preserve sharp root edges
                "ExposureTime": 40000,      # µs, tune for your chamber
                "AnalogueGain": 1.0,
            },
        },
        # IR backlight, automatic: let AE/AWB run but keep denoise off so fine
        # root edges are preserved. Output is still converted to grayscale.
        "backlight_auto": {
            "grayscale": True,
            "controls": {
                "AeEnable": True,
                "AwbEnable": True,
                "NoiseReductionMode": 0,    # off, preserve sharp root edges
            },
        },
        # Visible light (IR filter physically removed): let libcamera run normally.
        "color_auto": {
            "grayscale": False,
            "controls": {
                "AeEnable": True,
                "AwbEnable": True,
                "NoiseReductionMode": 1,    # fast/standard
            },
        },
    }

    # When True, captures and the live preview request a square resolution
    # (side x side, side = min(width, height)), so libcamera outputs a centered
    # square crop. This removes the backlight visible at the sides of square
    # plates. Cropping is done by resolution only (no ScalerCrop), as proven by
    # the RPICAM_V3_V2COMP profile.
    CROP_TO_SQUARE = False

    # --- DATA SYNCHRONIZATION ---
    SYNC_ENABLED = False
    SYNC_MODE = 'copy'       # 'copy' (keep local) or 'move' (delete local after upload)
    SYNC_DESTINATION = ''    # e.g., '/media/pi/usb_drive' OR 'my_remote:/backup'
    SYNC_INTERVAL = 60       # Run every X minutes