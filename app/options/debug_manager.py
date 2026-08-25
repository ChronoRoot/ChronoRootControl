"""Bounded, whitelisted diagnostics used by the Configuration Debug panel."""
from concurrent.futures import ThreadPoolExecutor
import datetime
import os
import re
import resource
import subprocess
import tempfile
import threading
import time

from config import Config


MAX_TAIL_BYTES = 512 * 1024
DEFAULT_TAIL_BYTES = 128 * 1024
MAX_COMMAND_CHARS = 256 * 1024
DIAGNOSTIC_CACHE_SECONDS = 15
_diagnostic_lock = threading.Lock()
_cache_lock = threading.Lock()
_diagnostic_cache = None
_diagnostic_cache_time = 0
_diagnostic_generation = 0
_journal_lock = threading.Lock()
_journal_cache = {}

SERVICE_UNITS = {
    "uwsgi": "uwsgi.service",
    "nginx": "nginx.service",
    "mux_fix": "chronoroot-mux-fix.service",
    "comitup": "comitup.service",
    "comitup_web": "comitup-web.service",
    "network_manager": "NetworkManager.service",
    "time_sync": "systemd-timesyncd.service",
}

JOURNAL_SOURCES = {
    "uwsgi": ["journalctl", "--no-pager", "-n", "200", "-o", "short-iso", "-u", "uwsgi.service"],
    "nginx": ["journalctl", "--no-pager", "-n", "200", "-o", "short-iso", "-u", "nginx.service"],
    "mux_fix": [
        "journalctl", "--no-pager", "-n", "200", "-o", "short-iso",
        "-u", "chronoroot-mux-fix.service",
    ],
    # Kernel warnings capture OOM kills, camera-driver errors, I2C faults and
    # undervoltage messages that cannot reach Python file handlers.
    "kernel": ["journalctl", "--no-pager", "-n", "250", "-o", "short-iso", "-k", "-p", "warning..alert"],
    "kernel_raw": ["journalctl", "--no-pager", "-n", "250", "-o", "short-iso", "-k", "-p", "warning..alert"],
}

KERNEL_RELEVANT_PATTERN = re.compile(
    r"(?:"
    r"out of memory|oom(?:-killer)?|killed process|"
    r"under.?voltage|voltage normal|throttl|thermal|overheat|"
    r"kernel panic|panic|segfault|general protection fault|"
    r"watchdog|hung task|blocked for more than|"
    r"imx\d*|libcamera|unicam|camera|csi|i2c|remote i/o|"
    r"mmc.*(?:error|fail|timeout)|ext4.*(?:error|warning)|"
    r"read-only file system|usb.*(?:error|fail|timeout|reset)"
    r")",
    re.IGNORECASE,
)


def _log_definitions():
    return {
        "application": {
            "label": "Application",
            "path": Config.LOGFILE,
            "clearable": True,
        },
        "hardware": {
            "label": "Hardware / Stream (SHDL)",
            "path": Config.SHDL_LOG_FILE,
            "clearable": True,
        },
        "crash": {
            "label": "Fatal Crash",
            "path": Config.CRASH_LOG_FILE,
            "clearable": True,
        },
        "watchdog": {
            "label": "Watchdog Reboot Record",
            "path": os.path.join(Config.WORKING_DIR, "watchdog_reboot_dates.txt"),
            # Clearing this would reset the reboot circuit breaker.
            "clearable": False,
        },
    }


def _run(command, timeout=8):
    """Run a fixed argv command and return bounded output."""
    env = dict(os.environ)
    env["SYSTEMD_COLORS"] = "0"
    try:
        # Write command output to a temporary file so subprocess pipes never
        # buffer an unexpectedly large journal in the uWSGI worker's RAM.
        with tempfile.TemporaryFile(mode="w+b") as output_file:
            result = subprocess.run(
                command,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=env,
            )
            output_file.seek(0, os.SEEK_END)
            size = output_file.tell()
            output_file.seek(max(0, size - MAX_COMMAND_CHARS))
            output = output_file.read(MAX_COMMAND_CHARS).decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": None,
            "output": "%s is not installed." % command[0],
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "output": "Command timed out after %s seconds." % timeout,
        }

    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "output": output,
    }


def get_log_catalog():
    logs = []
    for log_id, definition in _log_definitions().items():
        path = definition["path"]
        item = {
            "id": log_id,
            "label": definition["label"],
            "path": path,
            "clearable": definition["clearable"],
            "exists": os.path.isfile(path),
            "size_bytes": 0,
            "modified_at": None,
            "error": None,
        }
        try:
            stat = os.stat(path)
            item["size_bytes"] = stat.st_size
            item["modified_at"] = datetime.datetime.fromtimestamp(stat.st_mtime).strftime(
                Config.PRETTY_FORMAT
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            item["error"] = str(exc)
        logs.append(item)
    return logs


def tail_log(log_id, max_bytes=DEFAULT_TAIL_BYTES):
    definitions = _log_definitions()
    if log_id not in definitions:
        raise ValueError("Unknown log.")

    try:
        max_bytes = int(max_bytes)
    except (TypeError, ValueError):
        max_bytes = DEFAULT_TAIL_BYTES
    max_bytes = max(1024, min(max_bytes, MAX_TAIL_BYTES))

    path = definitions[log_id]["path"]
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            content = handle.read(max_bytes)
    except FileNotFoundError:
        return {
            "id": log_id,
            "content": "",
            "size_bytes": 0,
            "truncated": False,
            "message": "Log file does not exist yet.",
        }

    return {
        "id": log_id,
        "content": content.decode("utf-8", errors="replace"),
        "size_bytes": size,
        "truncated": start > 0,
        "message": None,
    }


def clear_log(log_id):
    global _diagnostic_cache, _diagnostic_generation
    definitions = _log_definitions()
    definition = definitions.get(log_id)
    if definition is None:
        raise ValueError("Unknown log.")
    if not definition["clearable"]:
        raise PermissionError("This record cannot be cleared because it protects the reboot limit.")

    path = definition["path"]
    try:
        # Truncate the existing inode so active logging.FileHandler instances
        # continue writing to the same file.
        with open(path, "r+b") as handle:
            handle.truncate(0)
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        return "Log file was already empty or absent."
    with _cache_lock:
        _diagnostic_generation += 1
        _diagnostic_cache = None
    return "%s log cleared." % definition["label"]


def _get_service_status(service_id, unit):
    properties = (
        "LoadState,ActiveState,SubState,UnitFileState,NRestarts,"
        "ExecMainStatus,ActiveEnterTimestamp"
    )
    result = _run(
        ["systemctl", "show", unit, "--no-pager", "--property=%s" % properties],
        timeout=5,
    )
    values = {}
    for line in result["output"].splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return {
        "id": service_id,
        "unit": unit,
        "available": values.get("LoadState") != "not-found" and bool(values),
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "unit_file_state": values.get("UnitFileState", "unknown"),
        "restarts": values.get("NRestarts", "0"),
        "exit_status": values.get("ExecMainStatus", "unknown"),
        "active_since": values.get("ActiveEnterTimestamp") or None,
        "error": None if result["ok"] or values else result["output"],
    }


def get_service_statuses():
    # systemctl calls are independent; running them in parallel caps total
    # request latency at one timeout instead of one timeout per unit.
    with ThreadPoolExecutor(max_workers=min(4, len(SERVICE_UNITS))) as executor:
        futures = [
            executor.submit(_get_service_status, service_id, unit)
            for service_id, unit in SERVICE_UNITS.items()
        ]
        return [future.result() for future in futures]


def get_failed_units():
    result = _run(
        ["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"],
        timeout=5,
    )
    return {
        "ok": result["ok"],
        "content": result["output"] or "No failed units.",
    }


def get_journal(source):
    command = JOURNAL_SOURCES.get(source)
    if command is None:
        raise ValueError("Unknown journal source.")

    cached = _journal_cache.get(source)
    now = time.monotonic()
    if cached and now - cached["cached_at"] < DIAGNOSTIC_CACHE_SECONDS:
        return cached["result"]
    if not _journal_lock.acquire(blocking=False):
        if cached:
            return cached["result"]
        return {
            "source": source,
            "ok": False,
            "busy": True,
            "content": "Another journal is being collected. Try again shortly.",
        }
    try:
        result = _run(command, timeout=10)
        content = result["output"]
        if source == "kernel":
            content = "\n".join(
                line for line in content.splitlines()
                if KERNEL_RELEVANT_PATTERN.search(line)
            )
        response = {
            "source": source,
            "ok": result["ok"],
            "busy": False,
            "content": content or (
                "No relevant kernel problems found. Use Raw kernel warnings "
                "to inspect all boot-time warnings."
                if source == "kernel"
                else "No journal entries."
            ),
        }
        _journal_cache[source] = {"cached_at": time.monotonic(), "result": response}
        return response
    finally:
        _journal_lock.release()


def get_system_snapshot():
    snapshot = {
        "board_model": "unknown",
        "process_max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "mem_total_kb": None,
        "mem_available_kb": None,
        "swap_free_kb": None,
        "temperature_c": None,
        "throttled": None,
    }
    try:
        with open("/proc/device-tree/model", "rb") as handle:
            snapshot["board_model"] = handle.read().replace(b"\x00", b"").decode(
                "utf-8", errors="replace"
            )
    except OSError:
        pass

    try:
        with open("/proc/meminfo", "r") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                mapping = {
                    "MemTotal": "mem_total_kb",
                    "MemAvailable": "mem_available_kb",
                    "SwapFree": "swap_free_kb",
                }
                if key in mapping:
                    snapshot[mapping[key]] = int(value.strip().split()[0])
    except (OSError, ValueError):
        pass

    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as handle:
            snapshot["temperature_c"] = round(float(handle.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass

    throttled = _run(["vcgencmd", "get_throttled"], timeout=2)
    if throttled["ok"]:
        snapshot["throttled"] = throttled["output"]
    return snapshot


def get_debug_snapshot():
    """
    Collect diagnostics once, with a short cache and non-blocking concurrency guard.

    Only one uWSGI request performs subprocess work. Concurrent clicks return the
    last snapshot immediately (or a bounded busy response before the first one).
    """
    global _diagnostic_cache, _diagnostic_cache_time
    now = time.monotonic()
    with _cache_lock:
        collection_generation = _diagnostic_generation
        if _diagnostic_cache is not None and now - _diagnostic_cache_time < DIAGNOSTIC_CACHE_SECONDS:
            return _diagnostic_cache

    if not _diagnostic_lock.acquire(blocking=False):
        with _cache_lock:
            if _diagnostic_cache is not None:
                return _diagnostic_cache
        return {"busy": True, "error": "Diagnostics are already being collected. Try again shortly."}

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            services = executor.submit(get_service_statuses)
            failed_units = executor.submit(get_failed_units)
            system = executor.submit(get_system_snapshot)
            snapshot = {
                "busy": False,
                "logs": get_log_catalog(),
                "services": services.result(),
                "failed_units": failed_units.result(),
                "system": system.result(),
                "collected_at": datetime.datetime.now().strftime(Config.PRETTY_FORMAT),
            }
        with _cache_lock:
            # A log may have been cleared while this collection was in progress.
            # Do not republish its stale pre-clear metadata into the cache.
            if _diagnostic_generation == collection_generation:
                _diagnostic_cache = snapshot
                _diagnostic_cache_time = time.monotonic()
        return snapshot
    finally:
        _diagnostic_lock.release()
