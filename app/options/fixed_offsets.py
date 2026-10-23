"""Fixed UTC offsets for module clocks.

The value stored in config and sent over the API is ``UTC+02:00`` or
``UTC-03:00``. Linux zone ids under ``Etc/GMT`` use the opposite sign
(``Etc/GMT-2`` is UTC+2). Those ids are for timedatectl and APScheduler only.
"""

import re

_UTC_HOURS = re.compile(r"(?i)^UTC([+-])(\d{1,2})(?::(\d{2}))?$")
_ETC_HOURS = re.compile(r"(?i)^Etc/GMT([+-])(\d{1,2})$")


def utc_offset_choices():
    """(value, label) pairs from UTC-12:00 through UTC+14:00."""
    choices = []
    for hours in range(-12, 15):
        label = format_utc(hours)
        choices.append((label, label))
    return choices


def format_utc(hours):
    sign = "+" if hours >= 0 else "-"
    return "UTC%s%02d:00" % (sign, abs(hours))


def etc_zone_name(hours):
    """Linux zone id. The Etc/GMT sign is the reverse of the UTC sign."""
    if hours == 0:
        return "Etc/GMT"
    if hours > 0:
        return "Etc/GMT-%d" % hours
    return "Etc/GMT+%d" % abs(hours)


def _clean(value):
    if not isinstance(value, str):
        return ""
    return (
        value.strip()
        .replace("\u2212", "-")
        .replace("\u2013", "-")
        .replace(" ", "")
    )


def parse_offset_hours(value):
    """East-positive hours, or None when value is not a fixed offset."""
    text = _clean(value)
    if not text:
        return None
    if text.upper() in ("UTC", "ETC/GMT"):
        return 0
    match = _UTC_HOURS.match(text)
    if match:
        sign, hours, minutes = match.group(1), int(match.group(2)), match.group(3)
        if minutes not in (None, "00"):
            return None
        if sign == "+" and 0 <= hours <= 14:
            return hours
        if sign == "-" and 0 <= hours <= 12:
            return -hours
        return None
    match = _ETC_HOURS.match(text)
    if match:
        sign, hours = match.group(1), int(match.group(2))
        if sign == "-" and 1 <= hours <= 14:
            return hours
        if sign == "+" and 1 <= hours <= 12:
            return -hours
    return None


def display_zone(value):
    """UTC label for an offset, otherwise the saved name unchanged."""
    hours = parse_offset_hours(value)
    if hours is not None:
        return format_utc(hours)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "UTC+00:00"


def os_zone_name(value):
    """Zone name for timedatectl and APScheduler."""
    hours = parse_offset_hours(value)
    if hours is not None:
        return etc_zone_name(hours)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "Etc/GMT"


def resolve_time_zone(value, current=None):
    """
    Accept a UTC offset, or the zone already in use.

    Returns {"stored", "os_name"}, or None when the new name is not allowed.
    A city zone such as Europe/Paris is allowed only when it is already current.
    """
    hours = parse_offset_hours(value)
    if hours is not None:
        return {"stored": format_utc(hours), "os_name": etc_zone_name(hours)}

    if not isinstance(value, str) or not value.strip():
        return None
    submitted = value.strip()
    if current is None:
        return None
    current_text = current.strip() if isinstance(current, str) else ""
    if not current_text or submitted != current_text:
        return None
    current_hours = parse_offset_hours(current_text)
    if current_hours is not None:
        return {
            "stored": format_utc(current_hours),
            "os_name": etc_zone_name(current_hours),
        }
    return {"stored": submitted, "os_name": submitted}
