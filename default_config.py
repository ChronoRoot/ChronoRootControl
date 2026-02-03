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

    IVPORT_MODULE_PATH = "/root/ivport-v2"
    SELECTOR_PRESENT = True # IVPORT MODULE PRESENCE
    IVPORT_VERSION = 2
    SELECTOR_TYPE = "TYPE_QUAD2" #possible values : TYPE_QUAD, TYPE_QUAD2, TYPE_DUAL, TYPE_DUAL2
    CAMERA_TYPE = "RPICAM"

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



    CAMS = (1, 2, 3, 4) # CAMERAS PRESENCE & DISPOSITION
                        # possible values:
                        # One cam :  (1, ), (2, ), (3, ), (4, )
                        # Two cams : (1,2),(1,3),(1,4),(2,3),(2,4)
                        # Three cams : (1,2,3),(1,2,4),(2,3,4)
                        # Four cams : (1,2,3,4)
                        # the order inside the list is not important
    CAM_WARMUP = 5
    CAM_ADJUST_TIME = 10

    CAM_RETRIES = 10
    CAM_WAIT_AFTER_RETRAY = 10

    MAX_WAIT = 100 # time to wait to another experience to terminate, min 5
    STREAM_RESOLUTION = (1640, 1232)
    IR_GPIO = 32
    IR_WARM_UP = 10

    CAM_PARAMS = {
        "format" : 'png',
        'resize' : None, # (1280, 720)
        'use_video_port' : False,
        'iso' : '',
        'awb_gains' : '', #(1.0,1.0)
        'awb_mode' : 'auto', #('off', 'auto', 'sunlight', 'cloudy', 'shade', 'tungsten', 'fluorescent', 'incandescent', 'flash', 'horizon' )
        'brightness' : 50, #(0 to 100)
        'color_effects' : (128, 128), # rot B&W, default None
        'contrast' : 0, # -100 to 100
        'drc_strength' : 'off', # ('off', 'low', 'medium', 'high') <- PiCamera.DRC_STRENGTHS
        'exposure_compensation' : 0, # -25 to 25
        'exposure_mode' : 'backlight', #PiCamera.EXPOSURE_MODES <- posible values
        'exposure_speed' : 0,
        'flash_mode': 'off', # PiCamera.FLASH_MODES
        'hflip' : False,
        'image_denoise' : True,
        'vflip' : False,
        'image_effect' : 'none', # PiCamera.IMAGE_EFFECTS
        'meter_mode' : 'average', #PiCamera.METER_MODES
        'resolution' : (3280, 2464),
        'saturation' : 0, # -100 to 100
        'sharpness' : 0, # -100 to 100
        'shutter_speed' : 0, # 0 -> auto, in µs
        'zoom' : (0.0, 0.0, 1.0, 1.0)
    }
