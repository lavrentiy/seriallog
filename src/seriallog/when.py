"""Times on the command line: '30m', '2h', '3d', 'today', '2026-10-18', '2026-10-18T19:30'."""

import re
from datetime import datetime, timedelta

_REL = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|m|h|d|w)$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse(text, now=None):
    """A point in time; relative amounts count back from now. None for 'all'."""
    now = now or datetime.now()
    s = text.strip().lower()
    if s in ("all", "ever"):
        return None
    if s == "now":
        return now
    if s == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s == "yesterday":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    m = _REL.match(s)
    if m:
        return now - timedelta(seconds=float(m.group(1)) * _UNIT[m.group(2)])
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(
            f"can't read time {text!r}: use e.g. 30m, 2h, 3d, today, 2026-10-18 or 2026-10-18T19:30"
        ) from None
