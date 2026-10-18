"""Times on the command line: '30m', '2h', '3d', 'today', '2026-10-18', '2026-10-18T19:30'.

Results are aware datetimes in the machine's zone, like the log timestamps.
"""

import re
from datetime import datetime, time, timedelta

from seriallog.logfile import localize

_REL = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|m|h|d|w)$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse(text, now=None):
    """A point in time; relative amounts count back from now. None for 'all'."""
    now = localize(now) if now else datetime.now().astimezone()
    s = text.strip().lower()
    if s in ("all", "ever"):
        return None
    if s == "now":
        return now
    if s == "today":
        return midnight(now)
    if s == "yesterday":
        return midnight(now - timedelta(days=1))
    m = _REL.match(s)
    if m:
        return localize(now - timedelta(seconds=float(m.group(1)) * _UNIT[m.group(2)]))
    try:
        return localize(datetime.fromisoformat(text.strip()))
    except ValueError:
        raise ValueError(
            f"can't read time {text!r}: use e.g. 30m, 2h, 3d, today, 2026-10-18 or 2026-10-18T19:30"
        ) from None

def midnight(dt):
    """Start of dt's day, with the UTC offset in force at that moment."""
    return localize(datetime.combine(dt.date(), time()))
