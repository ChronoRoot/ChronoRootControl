"""
REST API for ChronoRoot module control and telemetry.

All endpoints are mounted under the ``/api`` blueprint prefix (e.g. ``GET /api/status``).
The primary consumer is a Master Controller or fleet manager that polls live telemetry
from each Raspberry Pi module and syncs archived experiment summaries.

Fleet integration guide: ``app/doc/06_api.md`` (also linked from the in-app Help page).
"""

import os
import time
import datetime
import subprocess
from flask import Blueprint, abort, jsonify, make_response, request, send_from_directory
import uwsgi 
import json
import socket

# Local Application Imports
from config import Config
from app.experiment.models import Experiment
from app.experimentlist.models import ExperimentList
from app.options.schedulerstatus import SchedulerStatus
from app.options.config_manager import save_user_config, apply_system_time_config
from app.sync.manager import setup_rclone_remote, test_rclone_connection
from phototron.rpimodule import RpiModule

# Blueprint Definition
api_exp = Blueprint('api_exp', __name__, template_folder='templates', static_folder='static')

# =====================================================================
# 1. SYSTEM TELEMETRY & STATUS 
# =====================================================================

@api_exp.route('/status', methods=['GET'])
def get_system_status():
    """
    Primary heartbeat endpoint for Master Controller / fleet monitoring.

    **Endpoint:** ``GET /api/status``

    Reads the unified RAM-disk snapshot (``/run/chronoroot_scheduler_status.json``),
    enriches it with uptime and dynamically computed alerts, and returns the full
    live state. Safe to poll every 1--5 seconds; this handler is read-only.

    **Response headers:** ``X-Status-Read-Ms`` (load latency in ms);
    ``X-Status-Load-Retries`` (non-zero = file-lock contention during capture/sync).

    **Cameras (``cam_reports``):** Each port has ``health`` (last completed result)
    and ``activity`` (``IDLE`` or ``CAPTURING``). Count operational cameras with
    ``health == "OK"``. ``UNTESTED`` is neutral after boot. ``NOT DETECTED`` means
    no device on the I2C bus. ``ERROR`` means probe/capture failed for other reasons.
    While ``activity == "CAPTURING"``, ``health`` is unchanged — never alert on
    ``CAPTURING``.

    **Experiments (``jobs``):** Active scheduled experiments only. Entries disappear
    when an experiment is ``FINISHED`` or ``CANCELLED``. Each job has ``progress``
    (capture **rounds**, not per-camera files), ``next_run_time``, ``last_capture``,
    and RAM ``status`` (``SCHEDULED`` / ``RUNNING`` / ``IDLE`` / ``ERROR``). For
    authoritative disk status use ``GET /api/<expid>``. For archived file counts
    use ``GET /api/history``.

    **Alerts:** Inspect ``alerts.has_warnings`` and ``alerts.issues[]``. Also
    ``camera_gaps`` (per-camera file lag), ``all_cameras_failed``, ``watchdog``.

    Full integration guide with examples: ``app/doc/06_api.md``.

    Request body: none.

    Returns:
        200 OK -- full system state dictionary (schema below).

        Response schema::

            {
                "identity": {
                    "hostname": (String) Module hostname.
                    "ip":       (String) Active IPv4 address.
                    "mac":      (String) Hardware MAC address.
                },

                "system_health": {
                    "storage": {
                        "total_gb":     (Float) Partition size in GB.
                        "free_gb":      (Float) Free space in GB.
                        "percent_used": (Float) 0--100.
                        "last_check":   (String) Last daemon storage check timestamp.
                    }
                },

                "system_time": (String) Server clock now (YYYY-MM-DD HH:MM:SS).

                "status":  (String) "running" if APScheduler is active, else "waiting".
                "uptime":  (String) Formatted uptime, e.g. "2h 15m 10s".
                "last_picture": (String) Last camera actuation timestamp, or "Never".
                "next_picture": (String) Earliest next scheduled capture, or "None".

                "active_jobs_count": (Integer) Jobs with status RUNNING.

                "lock_info": {
                    "status":      (String) "FREE" or "LOCKED".
                    "owner":       (String|null) Experiment id or process name.
                    "details":     (String|null) e.g. "Exp myexp_...".
                    "acquired_at": (String|null) Lock acquisition timestamp.
                },

                "cam_reports": {
                    "<camera_id>": {
                        "health":     (String) UNTESTED | OK |
                                      NOT DETECTED | ERROR.
                        "activity":   (String) IDLE | CAPTURING.
                        "last_check": (String) Last **completed** action timestamp.
                        "path":       (String|null) Relative image path under WORKING_DIR.
                    }
                },

                "lights_info": {
                    "state": (String) "ON" or "OFF" (last known IR state).
                    "health_check": {
                        "status":   (String) UNTESTED | OK | NOT DETECTED.
                        "last_test": (String) Timestamp of last light diagnostic.
                        "path_on":  (String|null) Relative path to ON diagnostic image.
                        "path_off": (String|null) Relative path to OFF diagnostic image.
                        ... (statistical fields from last light test)
                    }
                },

                "last_diagnostic": {
                    "time":          (String) Last full hardware scan timestamp.
                    "global_result": (String) PASS | FAIL | PARTIAL/FAIL.
                    "message":       (String) Human-readable summary.
                    "cam_snapshot":  (Object) Per-camera state at end of scan.
                },

                "camera_gaps": [
                    {"expid": (String), "cam": (Integer), "behind_by": (Integer)}
                ],

                "all_cameras_failed": {
                    "expid": (String), "at": (String)
                } or null,

                "watchdog": {
                    "reboots_last_6h": (Integer),
                    "reboot_limit":    (Integer) Always 3.
                    "limit_reached":   (Boolean)
                },

                "jobs": {
                    "<expid>": {
                        "name":     (String),
                        "start":    (String),
                        "end":      (String),
                        "interval": (Integer) Minutes between capture rounds.
                        "status":   (String) RUNNING | SCHEDULED | ERROR | etc.
                        "next_run_time": (String|null),
                        "last_capture": {
                            "time":   (String),
                            "result": (String) SUCCESS | FAIL.
                        },
                        "progress": {
                            "taken":           (Integer) Completed capture rounds.
                            "expected":        (Integer) Total rounds for experiment.
                            "expected_so_far": (Integer) Rounds due by now.
                        }
                    }
                },

                "alerts": {
                    "has_warnings":       (Boolean),
                    "lock_stuck":         (Boolean),
                    "picture_overdue":    (Boolean),
                    "all_cameras_failed": (Boolean) Present when total capture failure.
                    "issues":             (Array of String) Human-readable warnings.
                },

                "sync": {
                    "sync_enabled": (Boolean),
                    "is_syncing":   (Boolean),
                    "status_msg":   (String),
                    "last_success": (String|null),
                    "next_sync":    (String|null)
                }
            }
    """
    # 1. Read the "shared whiteboard" with a lightweight, read-only loader
    #    (no network identity probe, no write-back) since this is a hot endpoint.
    started = time.perf_counter()
    status_manager = SchedulerStatus.for_read()
    
    # 2. Get the clean, processed dictionary (includes alerts, uptime, cameras).
    #    reload=False reuses the snapshot already loaded by for_read().
    info = status_manager.get_info(reload=False)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    
    # 3. Return it, exposing read latency and lock contention so fleet pollers
    #    can correlate slow responses with file-lock pressure during sync/captures.
    response = make_response(jsonify(info), 200)
    response.headers['X-Status-Read-Ms'] = str(elapsed_ms)
    response.headers['X-Status-Load-Retries'] = str(getattr(status_manager, 'last_load_retries', 0))
    return response

@api_exp.route('/storage/usage', methods=['GET'])
def get_storage_usage():
    """
    Retrieve the current disk usage statistics for the working directory.
    
    This endpoint calculates the total, used, and free space (in Gigabytes) 
    for the partition hosting the active Config.WORKING_DIR. It also detects 
    if the directory is living on an external mount (like a USB drive) or 
    on the internal SD card.
    
    Expected JSON Payload: 
    None
    
    Returns:
        200 OK: A dictionary containing storage telemetry.
        
        Response Schema Breakdown:
        {
            "path":              (String) The absolute path currently active.
            "total_gb":          (Float) Total capacity of the partition in GB.
            "used_gb":           (Float) Space currently occupied in GB.
            "free_gb":           (Float) Space remaining in GB.
            "percent_used":      (Float) Percentage of disk occupied (0.0 to 100.0).
            "is_external_mount": (Boolean) True if the path crosses a filesystem boundary (e.g., USB).
        }
    """
    from config import Config
    from app.storage.stats import get_storage_stats

    try:
        return jsonify(get_storage_stats()), 200
    except FileNotFoundError:
        path = Config.WORKING_DIR
        return make_response(jsonify({'error': f'Working directory {path} not found.'}), 404)


@api_exp.route('/history', methods=['GET'])
def get_experiment_history():
    """
    Archived experiment summaries for Master Controller database sync.

    **Endpoint:** ``GET /api/history``

    Scans disk and returns only experiments with status ``FINISHED`` or
    ``CANCELLED``. Picture counts are computed from image files on disk at
    request time (``.png`` / ``.jpg`` / ``.jpeg`` per camera subdirectory), not
    from ``info.json``.

    Running, scheduled, or errored experiments are excluded. Use ``GET /api/``
    or ``GET /api/<expid>`` for live status; use ``GET /api/status`` → ``jobs``
    for round progress while active.

    **Field semantics:**

    - ``expected_pictures`` -- target count **per camera** for the full window.
    - ``per_camera`` -- actual file counts (string keys, e.g. ``"1": 97``).
    - ``taken_pictures`` -- legacy: count from the first camera in ``cameras``.
    - ``all_ok`` -- every configured camera has ``per_camera[cam] >= expected``.
    - ``any_taken`` -- at least one image exists in any camera folder.
    - ``message`` -- non-empty only if counting failed (missing workdir, etc.).

    Request body: none.

    Returns:
        200 OK -- dictionary keyed by experiment id. Example::

            {
                "growth_chamber_a_2026-06-01": {
                    "name": "growth_chamber_a_2026-06-01",
                    "expid": "growth_chamber_a_2026-06-01",
                    "status": "FINISHED",
                    "start": "2026-06-01 08:00:00",
                    "end": "2026-06-02 08:00:00",
                    "interval": 15,
                    "cameras": [1, 2, 3, 4],
                    "expected_pictures": 97,
                    "taken_pictures": 97,
                    "per_camera": {"1": 97, "2": 95, "3": 0, "4": 97},
                    "all_ok": false,
                    "any_taken": true,
                    "message": ""
                }
            }

    See ``app/doc/06_api.md`` for the full integration guide.
    """
    archived_data = ExperimentList.get_archived_history()
    return jsonify(archived_data), 200

# =====================================================================
# 2. EXPERIMENT ORCHESTRATION (CRUD)
# =====================================================================

@api_exp.route('/', methods=['GET'])
def get_experiment_list():
    """
    List all experiments on disk (every status).

    **Endpoint:** ``GET /api/``

    Returns ``info.json`` metadata for each experiment folder. Disk ``status`` values
    include ``SETUP``, ``NEW``, ``SCHEDULED``, ``RUNNING``, ``ERROR``, ``FINISHED``,
    ``CANCELLED``. ``ERROR`` is recoverable until the experiment ``end`` passes.

    For live round progress while scheduled, use ``GET /api/status`` → ``jobs``.
    For archived per-camera file counts, use ``GET /api/history``.

    Request body: none.

    Returns:
        200 OK -- dict keyed by ``expid``, each value is a full experiment object.
    """
    exps = ExperimentList()
    return jsonify(exps.to_dict())


@api_exp.route('/', methods=['POST'])
def create_experiment():
    """
    Create and launch a newly scheduled experiment.
    
    This endpoint accepts a configuration payload, validates it against internal 
    business logic (e.g., minimum durations, intervals), checks for scheduling 
    overlaps with existing experiments, and if clear, provisions the workdir and 
    alerts the background scheduler.
    
    Expected JSON Payload:
    {
        "desc": "Testing plant growth",       # Optional: String description
        "start": "2024-05-17 14:00:00",       # Required: String (YYYY-MM-DD HH:MM:SS)
        "end": "2024-05-18 14:00:00",         # Required: String (YYYY-MM-DD HH:MM:SS)
        "interval": 15,                       # Required: Integer (minimum 5)
        "ir": true,                           # Optional: Boolean for infrared backlight
        "cameras": [1, 2]                     # Required: Array of integers representing camera IDs
    }
    
    Returns:
        201 Created: The full generated experiment JSON dictionary.
        400 Bad Request: {"error": "Validation Failed", "message": "<reason>"}
        409 Conflict: {"error": "Scheduling Conflict", "message": "Overlaps with experiment: <id>"}
    """
    if not request.json:
        abort(400, description="Missing JSON body")

    exp = Experiment()
    exp.from_dict(request.json)

    # 1. Check internal math (interval, duration)
    is_valid, error_msg = exp.validate_rules(is_new=True)
    if not is_valid:
        return make_response(jsonify({'error': 'Validation Failed', 'message': error_msg}), 400)

    # 2. Check scheduling overlaps
    exp_list = ExperimentList()
    conflict = exp_list.find_conflict(exp.start, exp.end)
    if conflict:
        msg = f"Overlaps with experiment: {conflict.expid}"
        return make_response(jsonify({'error': 'Scheduling Conflict', 'message': msg}), 409)

    # 3. Launch
    exp.create()
    return jsonify(exp.to_dict()), 201

@api_exp.route('/<expid>', methods=['GET'])
def get_experiment(expid):
    """
    Single experiment metadata from disk (``info.json``).

    **Endpoint:** ``GET /api/<expid>``

    Authoritative for experiment ``status``, ``message``, schedule, and camera list.
    Does not include live RAM fields (``progress``, ``next_run_time``) — use
    ``GET /api/status`` → ``jobs[expid]`` while the experiment is active.

    Request body: none.

    Returns:
        200 OK -- full experiment JSON dictionary.
        404 Not Found -- unknown ``expid``.
    """
    try:
        # USE NEW FACTORY METHOD
        exp = Experiment.load_from_id(expid)
    except FileNotFoundError:
        abort(404)
    return jsonify(exp.to_dict())


@api_exp.route('/<expid>', methods=['PUT'])
def update_experiment(expid):
    """
    Safely modify an existing experiment.
    
    This endpoint loads the target experiment, applies the new JSON parameters, 
    and performs the same rigorous date/interval validation as creation. It handles 
    conflict detection intelligently by ignoring the target experiment's own 
    previous timeslot block.
    
    Expected JSON Payload:
    (Same as POST /)
    {
        "desc": "Updated description",
        "start": "2024-05-17 15:00:00",
        "end": "2024-05-19 15:00:00",
        "interval": 30,
        "cameras": [1]
    }
    
    Returns:
        200 OK: The updated experiment JSON dictionary.
        400 Bad Request: {"error": "Validation Failed", "message": "<reason>"}
        404 Not Found: If the requested expid does not exist.
        409 Conflict: {"error": "Scheduling Conflict", "message": "Overlaps with experiment: <id>"}
    """
    try:
        exp = Experiment.load_from_id(expid)
    except FileNotFoundError:
        abort(404)
    
    # Protect immutable fields
    original_name = exp.name
    
    exp.from_dict(request.json)
    
    # Restore immutable fields
    exp.name = original_name 

    # 1. Check internal math (is_new=False because it already exists)
    is_valid, error_msg = exp.validate_rules(is_new=False)
    if not is_valid:
        return make_response(jsonify({'error': 'Validation Failed', 'message': error_msg}), 400)

    # 2. Check scheduling overlaps (ignoring itself)
    exp_list = ExperimentList()
    conflict = exp_list.find_conflict(exp.start, exp.end, ignore_id=exp.expid)
    if conflict:
        msg = f"Overlaps with experiment: {conflict.expid}"
        return make_response(jsonify({'error': 'Scheduling Conflict', 'message': msg}), 409)

    # 3. Safely update without resetting to 'NEW'
    exp.update() 
    return jsonify(exp.to_dict())

@api_exp.route('/<expid>', methods=['DELETE'])
def delete_experiment(expid):
    """
    Permanently delete an experiment.
    
    This endpoint wipes the experiment from the system and recursively deletes 
    its working directory (including all logs and captured images) from the disk.
    Note: The underlying model restricts this to CANCELLED or FINISHED experiments.
    
    Expected JSON Payload: 
    None
    
    Returns:
        200 OK: {"result": True}
        404 Not Found: If the requested expid does not exist.
    """
    try:
        exp = Experiment.load_from_id(expid)
    except FileNotFoundError:
        abort(404)
    exp.delete()
    return jsonify({'result': True})


@api_exp.route('/<expid>/cancel', methods=['GET', 'POST']) 
def cancel_experiment(expid):
    """
    Interrupt and cancel a running or scheduled experiment.
    
    Signals the uWSGI backend to drop the task from the scheduler queue and 
    updates the experiment's status flags to prevent future executions.
    
    Expected JSON Payload: 
    None
    
    Returns:
        200 OK: The updated experiment JSON dictionary showing status="CANCELLED".
        404 Not Found: If the requested expid does not exist.
    """
    print(f"DEBUG: Cancel requested for {expid}") 
    try:
        exp = Experiment.load_from_id(expid)
        print(f"DEBUG: Current status before cancel: {exp.status}")
    except FileNotFoundError:
        abort(404)
    
    exp.cancel()
    print(f"DEBUG: Status after cancel(): {exp.status}")
    return jsonify(exp.to_dict())

@api_exp.errorhandler(404)
def not_found(error):
    return make_response(jsonify({'error': 'Not found'}), 404)

# =====================================================================
# 3. HARDWARE CONTROL & DIAGNOSTICS
# =====================================================================

@api_exp.route('/diagnostic', methods=['POST'])
def diagnostic():
    """
    Trigger a full hardware diagnostic scan (asynchronous).

    **Endpoint:** ``POST /api/diagnostic``

    Queues a mule job that probes every camera, runs an IR light test, and
    updates ``cam_reports`` / ``last_diagnostic`` in the RAM state.

    Request body: none.

    Returns:
        200 OK: ``{"result": true, "expid": "system"}``

    Fleet integrators should poll ``GET /api/status`` until ``lock_info.status``
    returns to ``FREE`` and ``last_diagnostic.time`` updates. Per-camera results
    appear in ``cam_reports`` (``health`` + ``activity``).
    """
    # 1. Create a transient experiment object for system tasks
    exp = Experiment()
    exp.expid = 'system'
    exp.workdir = os.path.join(Config.WORKING_DIR, 'system')
    
    # Ensure directory exists (create manually since we aren't calling save())
    if not os.path.exists(exp.workdir):
        os.makedirs(exp.workdir)

    exp.diagnostic()
    return jsonify({'result': True, 'expid': 'system'})


# =====================================================================
# 6. INDIVIDUAL HARDWARE TESTING
# =====================================================================

@api_exp.route('/camera/<int:cam_id>/test', methods=['POST'])
def test_single_camera(cam_id):
    """
    Queue a single test capture for one camera (asynchronous).

    **Endpoint:** ``POST /api/camera/<cam_id>/test``

    Optional JSON body: ``{"ir": true}`` or ``{"ir": false}`` to force backlight
    state; omit to keep current IR setting.

    Returns:
        202 Accepted: ``{"result": true, "queued": true}``

    Poll ``GET /api/status`` until ``cam_reports[<cam_id>].activity`` is ``IDLE``
    and ``path`` points to a new image (or ``health`` is ERROR / NOT DETECTED).
    Do not treat ``activity == "CAPTURING"`` as a failure.
    """
    req_data = request.json or {}
    use_ir = req_data.get('ir') if 'ir' in req_data else None

    uwsgi.mule_msg(json.dumps({
        'id': 'system',
        'action': 'TEST_CAMERA',
        'cam_id': cam_id,
        'use_ir': use_ir
    }), Config.MULE_NO)

    return jsonify({"result": True, "queued": True}), 202


@api_exp.route('/camera/<int:cam_id>/test_lights', methods=['POST'])
def test_camera_lights(cam_id):
    """
    Queue an IR light diagnostic for a specific camera on the mule.

    Runs asynchronously so the web worker never blocks on the OFF/ON capture
    pair; results land in the system telemetry which the UI polls.
    """
    uwsgi.mule_msg(json.dumps({
        'id': 'system',
        'action': 'TEST_LIGHTS',
        'cam_id': cam_id
    }), Config.MULE_NO)

    return jsonify({"result": True, "queued": True}), 202

@api_exp.route('/camera/<int:cam_id>/focus', methods=['POST'])
def save_camera_focus(cam_id):
    """
    Saves the manual focus diopter value for a specific camera.
    """
    if not request.json or 'focus_value' not in request.json:
        return make_response(jsonify({'error': 'Missing focus_value in payload'}), 400)

    focus_value = float(request.json['focus_value'])
    from config import Config
    
    # 1. Fetch existing distances dictionary
    current_distances = getattr(Config, 'FOCUS_DISTANCES', {})
    if not isinstance(current_distances, dict):
        current_distances = {}
        
    # 2. THE FIX: Create a completely independent copy of the dictionary
    new_distances = current_distances.copy()
    
    # 3. Update the new dictionary
    new_distances[str(cam_id)] = focus_value
    
    # 4. Save the explicitly new object to disk
    success, msg = save_user_config({'FOCUS_DISTANCES': new_distances})
    
    if success:
        # Force the live Config to point to the new dictionary
        setattr(Config, 'FOCUS_DISTANCES', new_distances)
        return jsonify({'result': True, 'message': f'Focus locked at {focus_value}'}), 200
    else:
        return make_response(jsonify({'error': msg}), 500)


@api_exp.route('/toggle_light', methods=['POST'])
def toggle_light():
    """Background endpoint to toggle the IR light without reloading the page."""
    rpi = RpiModule()
    light = rpi.light
    status_mgr = SchedulerStatus()
    
    # Get the requested state from the JSON payload
    data = request.get_json()
    turn_on = data.get('ir_state', False)
    
    try:
        if turn_on:
            light.state = light.ON
            status_mgr.update_lights_state("ON")
            new_state = True
        else:
            light.state = light.OFF
            status_mgr.update_lights_state("OFF")
            new_state = False
            
        return jsonify({"success": True, "light_state": new_state})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@api_exp.route('/system_images/<path:filename>', methods=['GET'])
def get_system_image(filename):
    """
    Serve diagnostic and system-generated images from the working directory.
    
    This endpoint allows the frontend to safely request images generated during 
    hardware diagnostics without exposing the full internal file system.
    
    Expected JSON Payload: 
    None (Filename is passed via URL path)
    
    Returns:
        200 OK: The requested image file (e.g., image/jpeg).
        404 Not Found: If the requested file does not exist in the working directory.
    """
    return send_from_directory(Config.WORKING_DIR, filename)


@api_exp.route('/reboot', methods=['POST'])
def reboot():
    """
    Trigger a graceful hardware reboot of the Raspberry Pi.
    
    This endpoint spawns a detached background process to issue the `sudo shutdown -r now` 
    command with a slight delay, ensuring Flask has enough time to return the 200 OK 
    response to the frontend before the system goes down.
    
    Expected JSON Payload: 
    None
    
    Returns:
        200 OK: {"result": True, "message": "System is rebooting..."}
        500 Internal Server Error: {"error": "<exception details>"}
    """
    try:
        os.system('(sleep 1; sudo shutdown -r now) &')
        return jsonify({'result': True, 'message': 'System is rebooting...'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
# =====================================================================
# 4. SYSTEM CONFIGURATION & SERVICE MANAGEMENT
# =====================================================================

@api_exp.route('/config', methods=['GET'])
def get_system_config():
    """Returns the current user_config.py variables."""
    from config import Config
    keys = [
        'TIME_ZONE', 'NTP_SERVER', 'USE_NTP', 'CAMS', 'CAMERA_TYPE', 
        'SELECTOR_TYPE', 'SYNC_ENABLED', 'SYNC_INTERVAL', 'SYNC_REMOTE_TYPE', 
        'FOCUS_DISTANCES', 'KEEP_AUTOFOCUS' 
    ]
    return jsonify({k: getattr(Config, k, None) for k in keys}), 200

@api_exp.route('/config', methods=['PUT'])
def update_system_config():
    """
    Remotely update global system variables in `user_config.py`.
    
    This endpoint securely applies headless configuration changes. Note that some 
    changes (like Hardware mappings) may require a service restart to take effect.
    
    Expected JSON Payload:
    {
        "TIME_ZONE": "Europe/Paris",      # Optional: String
        "NTP_SERVER": "pool.ntp.org",     # Optional: String
        "USE_NTP": true,                  # Optional: Boolean
        "CAMS": ["cam1", "cam2"],         # Optional: List of strings
        "CAMERA_TYPE": "RPICAM_V2",       # Optional: String
        "SELECTOR_TYPE": "SINGLE"         # Optional: String
        "FOCUS_DISTANCES": []             # Optional: Dict of floats, 1 per CAMS value
        "KEEP_AUTOFOCUS": false,          # Optional: Boolean       
    }
    
    Returns:
        200 OK: {"result": True, "message": "Config saved successfully."}
        400 Bad Request: {"error": "Missing JSON body"}
        500 Internal Server Error: {"error": "<file write failure details>"}
    """
    if not request.json:
        return make_response(jsonify({'error': 'Missing JSON body'}), 400)

    # Validate keys
    allowed_keys = [
        'TIME_ZONE', 'NTP_SERVER', 'USE_NTP', 'CAMERA_TYPE', 
        'SELECTOR_TYPE', 'FOCUS_DISTANCES', 'KEEP_AUTOFOCUS' 
    ]
    new_settings = {k: v for k, v in request.json.items() if k in allowed_keys}
    
    success, msg = save_user_config(new_settings)
    
    if success:
        return jsonify({'result': True, 'message': msg}), 200
    else:
        return make_response(jsonify({'error': msg}), 500)


@api_exp.route('/config/check_ntp', methods=['GET'])
def check_ntp_server():
    data = request.json or {}
    ntp_server = data.get('ntp_server', getattr(Config, 'NTP_SERVER', None))
    target_server = ntp_server if ntp_server else "pool.ntp.org"
            
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(2.0) # 2-second timeout
        # 0x1B is a standard 48-byte NTP client request packet
        client.sendto(b'\x1b' + 47 * b'\0', (target_server, 123))
        client.recvfrom(1024)
        client.close()
        return jsonify({'result': True, 'message': f"NTP server {target_server} is reachable."}), 200    
    except OSError:
        if 'client' in locals():
            client.close()
        return jsonify({'result': False, 'message': f"Robot is offline or cannot reach NTP server on UDP port 123: {target_server}"}), 500

@api_exp.route('/config/time', methods=['POST'])
def api_sync_time():
    """
    Force a system-level time synchronization across the fleet.
    
    Interfaces directly with Linux OS commands (`timedatectl`, `date`, `timesyncd`) 
    to apply the requested time configuration, completely bypassing the web UI.
    
    Expected JSON Payload (Network Mode):
    {
        "mode": "network",             # Required: 'network' or 'manual'
        "timezone": "Europe/Paris",    # Optional: Defaults to system config
        "ntp_server": "pool.ntp.org"   # Optional: Defaults to system config
    }
    
    Expected JSON Payload (Manual Mode):
    {
        "mode": "manual",              # Required
        "date": "2024-05-17 14:00:00"  # Required for manual mode
    }
    
    Returns:
        200 OK: {"result": True, "message": "Time configuration applied successfully.", "server_time": "2024-05-17 14:00:05"}
        500 Internal Server Error: {"error": "Robot is offline or cannot reach NTP server..."}
    """
    data = request.json or {}
    mode = data.get('mode', 'network')
    date_str = data.get('date')
    timezone = data.get('timezone', getattr(Config, 'TIME_ZONE', 'UTC'))
    ntp_server = data.get('ntp_server', getattr(Config, 'NTP_SERVER', None))

    success, msg = apply_system_time_config(mode, date_str, timezone, ntp_server)

    if success:
        return jsonify({'result': True, 'message': msg, 'server_time': datetime.now().strftime(Config.PRETTY_FORMAT)}), 200
    else:
        return make_response(jsonify({'error': msg}), 500)


@api_exp.route('/restart_service', methods=['GET'])
def restart_service():
    """
    Trigger a restart of the underlying uWSGI application service.
    
    This is typically called via AJAX by a loading/restarting screen. It uses a delayed 
    subprocess so the browser successfully receives the HTTP 200 response immediately 
    before the Python daemon actually kills and resurrects itself.
    
    Expected JSON Payload: 
    None
    
    Returns:
        200 OK: "Restarting..." (Plaintext response to facilitate simple AJAX handling)
    """
    subprocess.Popen('(sleep 1; sudo systemctl restart uwsgi) &', shell=True)
    return "Restarting..."

# =====================================================================
# 5. DATA SYNCHRONIZATION (BACKGROUND WORKERS)
# =====================================================================

@api_exp.route('/sync/config', methods=['PUT', 'POST'])
def api_sync_config():
    """
    API to configure the synchronization backend and Rclone network profiles.
    
    Accepts a JSON payload to update local settings and, if using a network protocol, 
    provisions the secure Rclone remote dynamically.
    
    Expected JSON Payload:
    {
        "remote_type": "sftp",            # Required: 'local', 'sftp', 'ftp', or 'advanced'
        "host": "192.168.1.100",          # Required for sftp/ftp: IP or hostname
        "user": "admin",                  # Required for sftp/ftp: Username
        "password": "my_password",        # Required for new sftp/ftp connections
        "port": 22,                       # Optional: Custom network port
        "destination_path": "/backups",   # Required: Where data should be sent
        "sync_enabled": true,             # Optional: Boolean to enable background auto-sync
        "sync_interval": 60               # Optional: Integer in minutes
    }
    
    Returns:
        200 OK: {"result": True, "message": "Sync configuration saved successfully"}
        400 Bad Request: If JSON is missing or required network fields are absent.
        500 Internal Server Error: If Rclone fails to build the profile or the config fails to write.
    """
    if not request.json:
        return make_response(jsonify({'error': 'Missing JSON body'}), 400)
    
    data = request.json
    rtype = data.get('remote_type', getattr(Config, 'SYNC_REMOTE_TYPE', 'local'))
    
    # Configure the remote if dealing with network storage
    if rtype in ['sftp', 'ftp']:
        host = data.get('host')
        user = data.get('user')
        password = data.get('password')
        port = data.get('port')
        
        if not host or not user:
            return make_response(jsonify({'error': 'Host and Username are required for SSH/FTP'}), 400)
        if not password:
            return make_response(jsonify({'error': 'Password is required to configure a new connection'}), 400)
        
        success, msg = setup_rclone_remote(rtype, host, user, password, port)
        if not success:
            return make_response(jsonify({'error': msg}), 500)
    
    # Save standard sync settings to user_config.py
    config_payload = {}
    if 'sync_enabled' in data:
        config_payload['SYNC_ENABLED'] = bool(data['sync_enabled'])
    if 'sync_interval' in data:
        config_payload['SYNC_INTERVAL'] = int(data['sync_interval'])
    if 'remote_type' in data:
        config_payload['SYNC_REMOTE_TYPE'] = rtype
    if 'destination_path' in data:
        config_payload['SYNC_DESTINATION'] = data['destination_path']
        
    if config_payload:
        success, msg = save_user_config(config_payload)
        if not success:
            return make_response(jsonify({'error': f"Error saving config: {msg}"}), 500)
            
    return jsonify({'result': True, 'message': 'Sync configuration saved successfully'})


@api_exp.route('/sync/test', methods=['POST'])
def api_sync_test():
    """
    API to test SSH/FTP network credentials without altering the active configuration.
    
    This route provisions a temporary sandbox remote, tests the connection with a 
    strict timeout, and returns the exact error string if it fails.
    
    Expected JSON Payload:
    {
        "remote_type": "sftp",     # Required: 'sftp' or 'ftp'
        "host": "192.168.1.100",   # Required: Target IP or domain
        "user": "admin",           # Required: Username
        "password": "my_password", # Required: Connection password
        "port": 22                 # Optional: Target port
    }
    
    Returns:
        200 OK: {"result": True, "message": "Connection successful! Credentials are valid."}
        400 Bad Request: {"error": "<Rclone specific error output>"} (e.g., dial tcp timeout, auth failed)
    """
    if not request.json:
        return make_response(jsonify({'error': 'Missing JSON body'}), 400)
        
    data = request.json
    rtype = data.get('remote_type')
    
    if rtype not in ['sftp', 'ftp']:
        return make_response(jsonify({"error": "Only SSH/FTP connections can be tested."}), 400)
        
    success, msg = test_rclone_connection(
        rtype, data.get('host'), data.get('user'), data.get('password'), data.get('port')
    )
    
    if success:
        return jsonify({"result": True, "message": msg}), 200
    else:
        # Returning 400 here so the frontend can easily catch network rejections vs success
        return make_response(jsonify({"error": msg}), 400)

@api_exp.route('/sync/trigger', methods=['POST'])
def api_sync_trigger():
    if not getattr(Config, 'SYNC_DESTINATION', '').strip():
        return make_response(jsonify({"error": "Cannot sync. Destination path is empty in config."}), 400)

    status = SchedulerStatus().get_info().get("sync", {})
    if status.get("is_syncing"):
        return make_response(jsonify({"error": "Sync is already in progress"}), 409)
        
    msg_payload = {
        'id': 'system_sync',
        'action': 'SYNC'
    }
    uwsgi.mule_msg(json.dumps(msg_payload))
    
    return jsonify({"result": True, "message": "Sync queued successfully"}), 200

@api_exp.route('/sync/cancel', methods=['POST'])
def api_sync_cancel():
    """
    Emergency kill switch. Aborts transfer and turns off auto-sync.
    """
    msg_payload = {
        'id': 'system_sync',
        'action': 'CANCEL_SYNC'
    }
    uwsgi.mule_msg(json.dumps(msg_payload))
    
    return jsonify({"result": True, "message": "Cancel signal sent."}), 200