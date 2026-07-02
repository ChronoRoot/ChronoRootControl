import os
import shutil

from config import Config


def get_storage_stats(path=None):
    """Return disk usage for the working directory (or an explicit path)."""
    path = path or Config.WORKING_DIR
    total, used, free = shutil.disk_usage(path)
    return {
        "path": path,
        "total_gb": round(total / (2**30), 2),
        "used_gb": round(used / (2**30), 2),
        "free_gb": round(free / (2**30), 2),
        "percent_used": round((used / total) * 100, 1),
        "is_external_mount": os.stat('/').st_dev != os.stat(path).st_dev,
    }
