# REST API — Master Controller Integration Guide

This document explains how a **Master Controller** (or any fleet manager) integrates with a single ChronoRoot module: one Raspberry Pi that controls up to four cameras and runs timed imaging experiments. Each module exposes a JSON REST API at `http://<module-ip>/api`. There is no authentication; deploy modules on a trusted lab network.

Timestamps use the module's local clock in the format `YYYY-MM-DD HH:MM:SS`.

---

## Core concepts

Before polling endpoints, understand three ideas that recur throughout the API.

**Module.** One Raspberry Pi running ChronoRoot. It has a hostname, IP, and MAC address (`identity` in `/api/status`). It may have one to four camera ports on an IVPort multiplexer.

**Experiment.** A scheduled imaging run with a start time, end time, interval (minutes between capture rounds), and a list of camera ports to use. Configuration and authoritative status are stored on disk in `workdir/<expid>/info.json`. Image files live in `workdir/<expid>/<camera_id>/`.

**Capture round.** One scheduler tick. The module tries every camera selected for the experiment. A round counts as successful in RAM progress if **at least one** camera produces an image. Per-camera file counts are counted separately on disk.

**Two sources of truth.** Fleet software must know which endpoint to use for which question:

| Question | Authoritative source |
|----------|---------------------|
| Is an experiment still running? What is its configured status? | Disk: `GET /api/<expid>` or `GET /api/` |
| Live progress, next capture time, last round result | RAM: `GET /api/status` → `jobs` |
| Final per-camera file counts after completion | Disk scan: `GET /api/history` |

RAM state lives in `/run/chronoroot_scheduler_status.json`. It is volatile and fast to read. When an experiment reaches `FINISHED` or `CANCELLED`, it is **removed from `jobs`** in `/api/status`. Do not poll `/status` expecting to find completed experiments there — switch to `/history` or `/api/<expid>`.

---

## Recommended polling

Poll `GET /api/status` every 1–5 seconds per module for heartbeat, camera health, active jobs, and alerts. Poll `GET /api/history` on a slower schedule (every 5–15 minutes) or immediately after you detect that an experiment has finished, to sync archived summaries into a central database. Use `GET /api/` when you need metadata for experiments that are still active (`RUNNING`, `ERROR`, `SCHEDULED`).

Response headers on `/status` help diagnose slow reads:

- `X-Status-Read-Ms` — milliseconds spent loading the RAM snapshot.
- `X-Status-Load-Retries` — non-zero means the reader retried acquiring the file lock (common during capture or sync).

---

## Cameras: health and activity

Each camera appears in `cam_reports` under `GET /api/status`. Two independent fields describe it:

**`health`** — the result of the last **completed** probe or capture. It does not change while a shot is in progress. Use this for fleet inventory ("how many cameras work?") and operator alerts.

**`activity`** — what the camera is doing **right now**. Only two values exist: `IDLE` and `CAPTURING`. When `activity` is `CAPTURING`, `health` is intentionally left at its previous value (for example `OK`). Never treat `CAPTURING` as a failure or offline state.

### Health values

**`UNTESTED`** — No probe or capture has finished since the module booted (or since this camera port was added to configuration). This is neutral: do not alert. Run `POST /api/diagnostic` or wait for the first experiment capture to populate real health.

**`OK`** — The last completed action succeeded. The camera responded on the I2C bus and produced an image (or passed a diagnostic capture). Count this camera as operational.

**`NOT DETECTED`** — The multiplexer probe found no camera on this port ("not detected on bus"). Typical causes: dead camera, loose ribbon cable, wrong port assignment. The camera cannot capture until hardware is fixed. Alert the operator; exclude from "cameras online" count.

**`ERROR`** — The camera was detected but the last capture failed for another reason (driver error, timeout, filesystem issue, etc.). Distinct from `NOT DETECTED`: the bus saw something but the shot did not complete. Alert the operator; the next scheduled round may succeed.

### Activity values

**`IDLE`** — Camera is not currently taking a picture.

**`CAPTURING`** — A probe or capture is in progress on this port. Poll until `activity` returns to `IDLE`, then read `health` and `path` for the outcome.

### Example: camera 1 through a capture cycle

Before capture (already healthy):

```json
"1": {
  "health": "OK",
  "activity": "IDLE",
  "last_check": "2026-07-02 10:00:00",
  "path": "myexp_2026-07-02/1/20260702_100000_1.png"
}
```

During capture (health unchanged):

```json
"1": {
  "health": "OK",
  "activity": "CAPTURING",
  "last_check": "2026-07-02 10:00:00",
  "path": "myexp_2026-07-02/1/20260702_100000_1.png"
}
```

After successful capture:

```json
"1": {
  "health": "OK",
  "activity": "IDLE",
  "last_check": "2026-07-02 10:15:00",
  "path": "myexp_2026-07-02/1/20260702_101500_1.png"
}
```

After boot, never tested:

```json
"1": {
  "health": "UNTESTED",
  "activity": "IDLE",
  "last_check": "N/A",
  "path": null
}
```

---

## IR lights: health_check.status

`lights_info.health_check.status` in `GET /api/status` reports the result of the last **completed** IR light diagnostic (`POST /api/camera/<id>/test_lights` or the lights step inside `POST /api/diagnostic`). `lights_info.state` (`ON` / `OFF`) is the live backlight state and is independent of diagnostic health.

### Status values

**`UNTESTED`** — No light diagnostic has finished since boot. Neutral: do not alert.

**`OK`** — The last diagnostic captured OFF/ON image pairs and the statistical effect size exceeded the pass threshold (> 1.0). IR illumination is working.

**`NOT DETECTED`** — The last diagnostic failed: images could not be captured, or the effect size did not exceed the threshold. Alert the operator.

---

## Experiments: disk status and lifecycle

Every experiment folder contains `info.json`, exposed by `GET /api/` and `GET /api/<expid>`. The `status` field is the **authoritative experiment state** on disk.

### Status values

**`SETUP`** — Draft created in the web UI only. Not yet submitted. Not visible to the scheduler.

**`NEW`** — Created via API or UI. The web app has notified the background scheduler to register the job.

**`SCHEDULED`** — Start time is in the future. The job is registered but capture has not begun.

**`RUNNING`** — The experiment window is active and captures are occurring (or should be). Set when the first capture round executes successfully after `NEW`, `SCHEDULED`, or recovery from `ERROR`.

**`ERROR`** — The last capture round failed entirely (every selected camera failed) or the scheduler reported a job error. The experiment **can recover**: the next successful round sets status back to `RUNNING`. Do not treat `ERROR` as terminal until `end` has passed.

**`FINISHED`** — The experiment window ended. `message` contains a human-readable completion summary (success, partial, or zero captures).

**`CANCELLED`** — Stopped by the user via `GET/POST /api/<expid>/cancel`.

**`DIAGNOSTICS`** — Reserved for the transient `system` job during `POST /api/diagnostic`. Not a normal experiment.

### Typical transitions

```
SETUP → NEW → SCHEDULED → RUNNING ⇄ ERROR → FINISHED
                    ↘ CANCELLED
```

After a reboot, the scheduler mule runs `resync_with_disk`: it reloads non-finished experiments, counts existing image files, logs any missed frames, and re-registers jobs in RAM.

### Disk fields (`info.json` / `GET /api/<expid>`)

| Field | Meaning |
|-------|---------|
| `expid` | Unique experiment identifier (folder name). |
| `name` | Auto-generated display name. |
| `desc` | User description. |
| `status` | One of the statuses above. |
| `message` | Human-readable status text (errors, completion summary). |
| `start` / `end` | Scheduled window. |
| `interval` | Minutes between capture rounds. |
| `expected_pictures` | Images **per camera** expected for the full window. |
| `cameras` | List of camera port integers, e.g. `[1, 2, 3]`. |
| `ir` | Whether infrared backlight is used. |
| `workdir` | Absolute path to experiment folder. |
| `creation` / `modification` | Timestamps of create/update. |

`expected_pictures` is computed from `(end - start) / interval`, rounded up, plus one for the shot at minute zero.

---

## Experiments: RAM telemetry (`jobs` in `/api/status`)

While an experiment is actively scheduled, a corresponding entry appears in `jobs` on `GET /api/status`. This is live telemetry, not persisted to `info.json`.

### When `jobs` entries exist

An entry is created when the scheduler registers the experiment (`register_job_metadata`). It is **deleted** when the experiment is cancelled or finished (`remove_experiment`). Cancelled experiment IDs are also listed internally in `cancelled_experiments` and excluded from the `jobs` object returned by the API.

If you poll `/status` and an experiment you know is running does not appear in `jobs`, check disk status via `GET /api/<expid>` — it may have finished, been cancelled, or failed to reschedule after an outage.

### RAM job fields

| Field | Meaning |
|-------|---------|
| `name` | Display name (mirrors disk). |
| `start` / `end` | Scheduled window strings. |
| `interval` | Minutes between rounds. |
| `next_run_time` | Next APScheduler fire time, or `null` if none scheduled. |
| `status` | Scheduler view: `SCHEDULED`, `RUNNING`, `IDLE`, or `ERROR`. |
| `trigger` | APScheduler trigger description (for debugging). |
| `progress.taken` | Completed capture **rounds** (not per-camera files). |
| `progress.expected` | Total rounds for the full experiment lifespan. |
| `progress.expected_so_far` | Rounds that should have occurred by `system_time`. |
| `last_capture.time` | Timestamp of the last round attempt. |
| `last_capture.result` | `SUCCESS` if ≥1 camera succeeded; `FAIL` otherwise. |

### RAM `status` vs disk `status`

These can differ briefly. Disk `status` reflects the experiment model (`RUNNING`, `ERROR`, etc.) and is updated when capture rounds complete. RAM `jobs[].status` is driven by the scheduler: `RUNNING` when `next_run_time` is set; `ERROR` after a scheduler job failure; protected from being overwritten to `IDLE` while in `ERROR`. For operator-facing experiment state, prefer disk `status` from `GET /api/<expid>`. For "when is the next shot?" and round counters, use RAM `jobs`.

### Progress semantics

`progress.taken` increments once per successful **round**, not once per image file. If an experiment uses cameras `[1, 2, 3]` and camera 2 fails but cameras 1 and 3 succeed, `taken` still increments by 1 and `last_capture.result` is `SUCCESS`.

To detect per-camera lag during a live experiment, use `camera_gaps` (see below) or count files on disk under `workdir/<expid>/<cam>/`.

### Related RAM fields

**`camera_gaps`** — Array of `{expid, cam, behind_by}`. The health daemon compares each camera's on-disk file count to `progress.expected_so_far`. If a camera is behind, it appears here. `behind_by` is the number of missing image files.

**`all_cameras_failed`** — `{expid, at}` when the last round failed on every selected camera; cleared on the next successful round. May contribute to watchdog reboot logic.

**`lock_info`** — Hardware mutex. When `status` is `LOCKED` and `details` contains `Exp <expid>`, that experiment holds the camera bus.

**`next_picture`** — Earliest `next_run_time` among non-cancelled jobs (human-readable string, or `"None"`).

**`active_jobs_count`** — Count of jobs whose RAM `status` is `RUNNING` (not the same as counting all experiments on disk).

**`last_picture`** — Timestamp of the last time any camera completed a shot (any experiment or diagnostic).

---

## `GET /api/status`

Primary heartbeat. Returns the full live telemetry object described above. Safe to poll frequently; the handler is read-only.

### Interpreting alerts

The `alerts` object is computed on each request from RAM state and clock:

- `has_warnings` — Master flag. If `true`, read `issues[]` for human-readable strings.
- `lock_stuck` — Hardware lock held longer than allowed for the configured camera count.
- `picture_overdue` — Next scheduled capture is late by more than `PER_CAMERA_ALLOWANCE` minutes.
- `all_cameras_failed` — Set when the last round failed on every camera (including effective mux/I2C outage). Surfaces in `issues[]` and may trigger the hardware watchdog reboot.

Also check `watchdog.limit_reached` (true after 3 auto-reboots in 6 hours).

Do **not** raise alerts for `health == "UNTESTED"` or for `activity == "CAPTURING"` on an otherwise healthy camera.

### Example response (abbreviated)

```json
{
  "identity": {
    "hostname": "chronoroot-bench-01",
    "ip": "192.168.1.42",
    "mac": "dc:a6:32:ab:cd:ef"
  },
  "system_time": "2026-07-02 10:15:30",
  "system_health": {
    "storage": {
      "total_gb": 58.2,
      "free_gb": 41.7,
      "percent_used": 28.4,
      "last_check": "2026-07-02 10:10:00"
    }
  },
  "status": "running",
  "uptime": "3d 4h 22m",
  "last_picture": "2026-07-02 10:15:00",
  "next_picture": "2026-07-02 10:30:00",
  "active_jobs_count": 1,
  "lock_info": {
    "status": "FREE",
    "owner": null,
    "details": null,
    "acquired_at": null
  },
  "cam_reports": {
    "1": { "health": "OK", "activity": "IDLE", "last_check": "2026-07-02 10:15:00", "path": "exp1/1/..." },
    "2": { "health": "OK", "activity": "IDLE", "last_check": "2026-07-02 10:15:00", "path": "exp1/2/..." },
    "3": { "health": "NOT DETECTED", "activity": "IDLE", "last_check": "2026-07-02 09:00:00", "path": null },
    "4": { "health": "UNTESTED", "activity": "IDLE", "last_check": "N/A", "path": null }
  },
  "lights_info": {
    "state": "OFF",
    "health_check": {
      "status": "OK",
      "last_test": "2026-07-02 09:00:00"
    }
  },
  "last_diagnostic": {
    "time": "2026-07-02 09:00:00",
    "global_result": "PARTIAL/FAIL",
    "message": "2/4 cameras OK"
  },
  "camera_gaps": [
    { "expid": "myexp_2026-07-02", "cam": 3, "behind_by": 2 }
  ],
  "all_cameras_failed": null,
  "watchdog": {
    "reboots_last_6h": 0,
    "reboot_limit": 3,
    "limit_reached": false
  },
  "jobs": {
    "myexp_2026-07-02": {
      "name": "myexp_2026-07-02",
      "start": "2026-07-02 08:00:00",
      "end": "2026-07-03 08:00:00",
      "interval": 15,
      "status": "RUNNING",
      "next_run_time": "2026-07-02 10:30:00",
      "trigger": "interval[0:15:00]",
      "progress": {
        "taken": 10,
        "expected": 97,
        "expected_so_far": 10
      },
      "last_capture": {
        "time": "2026-07-02 10:15:00",
        "result": "SUCCESS"
      }
    }
  },
  "alerts": {
    "has_warnings": true,
    "lock_stuck": false,
    "picture_overdue": false,
    "all_cameras_failed": false,
    "issues": [
      "Camera 3 on myexp_2026-07-02 is 2 pictures behind schedule."
    ]
  },
  "sync": {
    "sync_enabled": true,
    "is_syncing": false,
    "status_msg": "Idle",
    "last_success": "2026-07-02 09:45:00",
    "next_sync": "2026-07-02 10:45:00"
  }
}
```

---

## `GET /api/history`

Returns archived experiment summaries for database sync. Only experiments with disk status `FINISHED` or `CANCELLED` are included. Running, scheduled, or errored experiments are excluded — use `GET /api/` or `GET /api/<expid>` for those.

Picture counts are computed from image files on disk at request time (`.png`, `.jpg`, `.jpeg` in each camera subdirectory). They are not cached in `info.json`.

### Response shape

Top-level keys are experiment IDs. Each value contains:

| Field | Meaning |
|-------|---------|
| `name` | Display name. |
| `expid` | Experiment ID (same as key). |
| `status` | `FINISHED` or `CANCELLED`. |
| `start` / `end` | Scheduled window. |
| `interval` | Minutes between capture rounds. |
| `cameras` | Configured camera ports (integers). |
| `expected_pictures` | Target count **per camera** for the full window. |
| `per_camera` | Actual file counts per port (string keys → integers). |
| `taken_pictures` | File count from the **first** camera in `cameras` (legacy; prefer `per_camera`). |
| `all_ok` | `true` if every configured camera has `per_camera[cam] >= expected_pictures`. |
| `any_taken` | `true` if at least one image file exists in any camera folder. |
| `message` | Empty on success; non-empty if counting failed (missing workdir, no cameras configured, etc.). |

### How `all_ok` and `any_taken` are used

`any_taken == false` means zero images were saved — a failed or empty run. `all_ok == false` with `any_taken == true` means a **partial** capture (some cameras or rounds missing). The module's completion `message` in `info.json` uses the same logic to produce text like "Cam1: 95/97, Cam2: 97/97".

### Example response

```json
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
    "per_camera": {
      "1": 97,
      "2": 95,
      "3": 0,
      "4": 97
    },
    "all_ok": false,
    "any_taken": true,
    "message": ""
  },
  "quick_test_2026-06-03": {
    "name": "quick_test_2026-06-03",
    "expid": "quick_test_2026-06-03",
    "status": "CANCELLED",
    "start": "2026-06-03 14:00:00",
    "end": "2026-06-03 18:00:00",
    "interval": 30,
    "cameras": [1, 2],
    "expected_pictures": 9,
    "taken_pictures": 0,
    "per_camera": {
      "1": 0,
      "2": 0
    },
    "all_ok": false,
    "any_taken": false,
    "message": ""
  }
}
```

---

## Experiment endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/` | All experiments (every disk status). |
| POST | `/api/` | Create and schedule a new experiment. |
| GET | `/api/<expid>` | Single experiment `info.json`. |
| PUT | `/api/<expid>` | Update parameters (validates overlaps). |
| DELETE | `/api/<expid>` | Delete folder (only `CANCELLED` or `FINISHED`). |
| GET/POST | `/api/<expid>/cancel` | Cancel a running or scheduled experiment. |
| GET | `/api/history` | Archived summaries with file counts. |

### Creating an experiment (`POST /api/`)

```json
{
  "desc": "Testing plant growth",
  "start": "2026-07-02 14:00:00",
  "end": "2026-07-03 14:00:00",
  "interval": 15,
  "ir": true,
  "cameras": [1, 2, 3]
}
```

Returns `201` with the full experiment object. Validation failures return `400`; scheduling overlaps return `409`.

---

## Hardware actions (asynchronous)

These return immediately. Poll `GET /api/status` for results.

| Method | Path | Returns | Poll for |
|--------|------|---------|----------|
| POST | `/api/diagnostic` | `200 {"result": true, "expid": "system"}` | `last_diagnostic`, `cam_reports` |
| POST | `/api/camera/<id>/test` | `202 {"result": true, "queued": true}` | `activity` → `IDLE`, then `health` / `path` |
| POST | `/api/camera/<id>/test_lights` | `202` | `lights_info.health_check` |

During any of these, `lock_info.status` may be `LOCKED` and affected cameras show `activity: CAPTURING`.

---

## Fleet workflow

A typical Master Controller loop:

1. **Discover** modules (DHCP, static list, or mDNS).
2. **Poll** `GET /api/status` every few seconds. Track `identity`, count cameras with `health == "OK"`, surface `alerts.issues`.
3. **Track active experiments** via `jobs`. When an `expid` disappears from `jobs`, fetch `GET /api/<expid>` to read final disk `status` and `message`.
4. **Sync archives** via `GET /api/history`. Ingest new `expid` keys not yet in your database. Use `per_camera` for accurate per-port counts.
5. **React** to `camera_gaps` and `all_cameras_failed` before the experiment ends if partial capture is unacceptable.

### Example poller (Python)

```python
import requests

def poll_module(base_url: str) -> dict:
    r = requests.get(f"{base_url}/api/status", timeout=10)
    r.raise_for_status()
    data = r.json()

    cams = data.get("cam_reports", {})
    ok_count = sum(1 for c in cams.values() if c.get("health") == "OK")

    return {
        "hostname": data["identity"]["hostname"],
        "ip": data["identity"]["ip"],
        "cameras_ok": ok_count,
        "cameras_total": len(cams),
        "active_experiments": list(data.get("jobs", {}).keys()),
        "alerts": data.get("alerts", {}).get("issues", []),
        "read_ms": r.headers.get("X-Status-Read-Ms"),
    }
```

---

## Other endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/storage/usage` | Disk stats for the working partition. |
| GET | `/api/config` | Read module configuration. |
| PUT | `/api/config` | Update configuration. |
| POST | `/api/sync/trigger` | Manual rclone sync. |
| POST | `/api/reboot` | Reboot module (use with care). |

Endpoint-level schema detail also appears in the docstrings of `app/api.py` in the source tree.
