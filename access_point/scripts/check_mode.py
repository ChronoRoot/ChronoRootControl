#!/usr/bin/env python3
# /etc/init.d/check_mode.py
### BEGIN INIT INFO
# Provides:          sample.py
# Required-Start:    $local_fs $syslog
# Required-Stop:     $local_fs $syslog
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: Activate access point or satellite modes according to GPIO21 status.
# Description:       Check for the presence of a jumper on GPIO between the pin 21 and the ground. If present call the access_point_mode.sh script otherwise the satellite_mode.sh
### END INIT INFO


import RPi.GPIO as GPIO
import datetime
import os, sys
import subprocess

logfile='/var/log/automode.log'
access_point_mode_script = "/etc/ChronoRoot/access_point_mode.sh"
satellite_mode_script = "/etc/ChronoRoot/satellite_mode.sh"
bin_bash="/bin/bash"

def logit(myfile, msg):
    """
    Appends the current time to a message for logging
    """
    current_time = '{0:%Y-%m-%d %H:%M:%S}'.format(datetime.datetime.now())
    with open(myfile, "a") as logf:
        logf.writelines("AUTOMODE - %s - %s \n" % (current_time, msg))

# Check that access_point_mode script exist
if not os.path.exists(access_point_mode_script):
    logit(logfile, "%s not exist" % access_point_mode_script)
    sys.exit(1)

# Check that satellite_mode script exist
if not os.path.exists(satellite_mode_script):
    logit(logfile, "%s not exist" % satellite_mode_script)
    sys.exit(1)

# Collect jumpre status
GPIO.setmode(GPIO.BCM)
GPIO.setup(21, GPIO.IN, pull_up_down=GPIO.PUD_UP)
jumper_absent = GPIO.input(21)

# According to jumper status run the correct script
with open(logfile, "a") as logf:
    if jumper_absent:
        subprocess.call("%s %s" % (bin_bash , satellite_mode_script), shell=True, stdout=logf, stderr=logf)
    else:
        subprocess.call("%s %s" % (bin_bash , access_point_mode_script), shell=True,  stdout=logf, stderr=logf)
