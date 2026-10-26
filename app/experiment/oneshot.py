"""
One-shot captures: a single round, outside the repeating schedule.

A shot is refused when the next scheduled round is less than five minutes away,
so it does not collide with an experiment that is about to take pictures.
"""
from datetime import datetime, timedelta

from config import Config
from app.options.schedulerstatus import SchedulerStatus

CLEARANCE = timedelta(minutes=5)
LOCK_WAIT_SECONDS = 180


def scheduled_capture_within(minutes=None):
    """
    Return (blocked, when).

    blocked is True when a scheduled round is sooner than `minutes` from now,
    including a round that is already due. when is that datetime, or None when
    nothing is scheduled.
    """
    info = SchedulerStatus.for_read().get_info(reload=False)
    raw = info.get("next_picture")
    if not raw or raw == "None":
        return False, None
    try:
        when = datetime.strptime(str(raw), Config.PRETTY_FORMAT)
    except ValueError:
        return False, None
    window = CLEARANCE if minutes is None else timedelta(minutes=minutes)
    if when - datetime.now() < window:
        return True, when
    return False, when


def clearance_message(when):
    """Explain why a one-shot was refused."""
    if when is None:
        return (
            "A scheduled capture is less than 5 minutes away. "
            "Wait until that round is further off."
        )
    stamp = when.strftime(Config.PRETTY_FORMAT)
    return (
        "A scheduled capture is at %s, less than 5 minutes away. "
        "Wait until that round is further off."
    ) % stamp
