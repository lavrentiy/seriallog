"""Log file format, line splitting, writing, reading and housekeeping.

One file per port per day, in local time:

    <log_dir>/<port>/2026-10-18.log         today and yesterday
    <log_dir>/<port>/2026-10-16.log.gz      older days, compressed

Each line is a timestamp, a kind and the text:

    2026-10-18T19:40:01.123+02:00 | boot ok, fw 1.4.2       data from the device
    2026-10-18T19:40:00.871+02:00 ! device connected        logger event

The timestamp is when the first byte of the line arrived, in local time with
its UTC offset, so lines stay in order across a clock change and logs from
machines in different zones can be compared. Files written before the offset
was added (no +hh:mm) are read as local time. Bytes that are not valid UTF-8
and control characters are written as \\xNN; ANSI colour
codes are removed.
"""

import gzip
import heapq
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA = "|"
EVENT = "!"
MAX_LINE = 4096

_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL = {c: f"\\x{c:02x}" for c in [*range(0x00, 0x09), *range(0x0a, 0x20), 0x7f]}
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log(\.gz)?$")


# ---------- Bytes to lines ----------

class LineSplitter:
    """Collects bytes from a port and hands back complete lines.

    A line ends at \\n, \\r\\n or a lone \\r. A partial line is handed back by
    flush(), which the caller runs after a quiet spell, so a prompt without a
    newline ("login: ") still shows up.
    """

    def __init__(self, max_len=MAX_LINE):
        self.max_len = max_len
        self.buf = bytearray()
        self.started = None   # arrival time of the first byte in buf

    def feed(self, data, now):
        if not data:
            return []
        if not self.buf:
            self.started = now
        self.buf += data
        out = []
        while True:
            n = _find_eol(self.buf)
            if n is None:
                break
            line, used = n
            if used is None:      # lone \r at the very end: \n may follow
                break
            out.append((self.started, bytes(self.buf[:line])))
            del self.buf[:used]
            self.started = now
        while len(self.buf) > self.max_len:
            out.append((self.started, bytes(self.buf[:self.max_len])))
            del self.buf[:self.max_len]
            self.started = now
        if not self.buf:
            self.started = None
        return out

    def flush(self):
        if not self.buf:
            return []
        data = bytes(self.buf)
        if data.endswith(b"\r"):
            data = data[:-1]
        out = [(self.started, data)]
        self.buf.clear()
        self.started = None
        return out

    @property
    def pending(self):
        return bool(self.buf)


def _find_eol(buf):
    """(end of line, bytes to consume), (end, None) if undecided, or None."""
    lf = buf.find(b"\n")
    cr = buf.find(b"\r")
    if cr == -1 or (lf != -1 and lf < cr):
        return None if lf == -1 else (lf, lf + 1)
    if cr + 1 == len(buf):
        return (cr, None)
    return (cr, cr + 2 if buf[cr + 1:cr + 2] == b"\n" else cr + 1)


def render(raw):
    """Device bytes to one line of printable text."""
    text = _ANSI.sub(b"", raw).decode("utf-8", errors="backslashreplace")
    return text.translate(_CONTROL)


def local(t):
    """Epoch seconds to an aware datetime in the machine's zone."""
    return datetime.fromtimestamp(t, timezone.utc).astimezone()

def localize(dt):
    """An aware datetime in the machine's zone; a naive one counts as local time."""
    return dt.astimezone()

def stamp(t):
    return local(t).isoformat(timespec="milliseconds")

def parse_stamp(text):
    """A timestamp as written by stamp(), or an older one without offset."""
    return localize(datetime.fromisoformat(text))


def format_line(t, kind, text):
    return f"{stamp(t)} {kind} {text}\n"


# ---------- Reading ----------

@dataclass(frozen=True)
class Entry:
    port: str
    time: datetime
    kind: str
    text: str

    def line(self):
        return f"{self.time.isoformat(timespec='milliseconds')} {self.kind} {self.text}"


def parse_line(port, line):
    ts, _, rest = line.rstrip("\r\n").partition(" ")   # \r: a Windows file read in binary mode
    kind = rest[:1]
    if kind not in (DATA, EVENT) or rest[1:2] not in (" ", ""):
        return None
    try:
        t = parse_stamp(ts)
    except ValueError:
        return None
    return Entry(port, t, kind, rest[2:])


def day_files(log_dir, port):
    """[(date, path)] for a port, oldest first."""
    folder = Path(log_dir) / port
    found = {}
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    for name in names:
        m = _DAY_FILE.match(name)
        if m:
            day = date.fromisoformat(m.group(1))
            # A plain file wins over a .gz of the same day (compression in progress).
            if day not in found or not name.endswith(".gz"):
                found[day] = folder / name
    return sorted(found.items())


def _open(path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def read_port(log_dir, port, since=None, until=None):
    """Entries of one port in time order, between since and until (datetimes)."""
    since = localize(since) if since else None
    until = localize(until) if until else None
    for day, path in day_files(log_dir, port):
        if since and day < since.date():
            continue
        if until and day > until.date():
            break
        try:
            f = _open(path)
        except FileNotFoundError:     # compressed away while we were listing
            continue
        with f:
            for line in f:
                e = parse_line(port, line)
                if e is None:
                    continue
                if since and e.time < since:
                    continue
                if until and e.time > until:
                    return
                yield e


def read(log_dir, ports, since=None, until=None):
    """Entries of several ports, merged in time order."""
    streams = [read_port(log_dir, p, since, until) for p in ports]
    return heapq.merge(*streams, key=lambda e: e.time)


def last_lines(log_dir, port, n):
    """The last n entries of a port, reading back day by day."""
    out = []
    for _, path in reversed(day_files(log_dir, port)):
        with _open(path) as f:
            entries = [e for e in (parse_line(port, l) for l in f) if e]
        out[:0] = entries[-(n - len(out)):]
        if len(out) >= n:
            break
    return out


def follow(log_dir, port, poll=0.3, today=date.today):
    """New entries of a port as they are written, moving to the next day file at midnight.

    Starts at the end of today's file, since the caller has shown the history
    with last_lines(), and yields None after each quiet poll so the caller can
    notice that nobody is listening any more. A line that is still being
    written is left until it is complete. The daemon files a line under the
    day its first byte arrived, so yesterday's file can still grow after
    midnight; it is switched away from only once today's file exists, after
    reading what landed in it since the previous poll.
    """
    folder = Path(log_dir) / port
    day = f = None
    skip = True

    def entry(line):
        return parse_line(port, line.decode("utf-8", "replace")) if line.endswith(b"\n") else None

    try:
        while True:
            now = today()
            if now != day and (folder / f"{now.isoformat()}.log").exists():
                if f:
                    yield from filter(None, map(entry, f))
                    f.close()
                f = open(folder / f"{now.isoformat()}.log", "rb")
                if skip:
                    f.seek(0, os.SEEK_END)
                day = now
            skip = False
            line = f.readline() if f else b""
            e = entry(line)
            if e:
                yield e
                continue
            if line and not line.endswith(b"\n"):        # partial line: wait for the rest
                f.seek(-len(line), os.SEEK_CUR)
            yield None
            time.sleep(poll)
    finally:
        if f:
            f.close()


# ---------- Writing ----------

class PortLog:
    """Appends lines to the day file of one port, switching at midnight."""

    def __init__(self, log_dir, port):
        self.folder = Path(log_dir) / port
        self.day = None
        self.f = None

    def write(self, t, kind, text):
        day = local(t).date()
        if day != self.day:
            self.close()
            self.folder.mkdir(parents=True, exist_ok=True)
            self.f = open(self.folder / f"{day.isoformat()}.log", "a",
                          encoding="utf-8", buffering=1)
            self.day = day
        self.f.write(format_line(t, kind, text))

    def close(self):
        if self.f:
            self.f.close()
            self.f = None
            self.day = None


# ---------- Housekeeping ----------

def housekeeping(log_dir, ports, retention_days, today=None):
    """Compress day files older than yesterday; delete those past retention.

    Yesterday stays plain: a line that started just before midnight may still
    be written to it. Returns (compressed, deleted) counts.
    """
    today = today or date.today()
    oldest_kept = today - timedelta(days=retention_days - 1)
    compressed = deleted = 0
    for port in ports:
        for day, path in day_files(log_dir, port):
            if day < oldest_kept:
                for p in (path, path.with_name(f"{day.isoformat()}.log.gz")):
                    if p.exists():
                        p.unlink()
                        deleted += 1
            elif day < today - timedelta(days=1) and path.suffix == ".log":
                gz = path.with_name(path.name + ".gz")
                tmp = path.with_name(path.name + ".gz.tmp")
                with open(path, "rb") as src, gzip.open(tmp, "wb") as dst:
                    while chunk := src.read(1 << 16):
                        dst.write(chunk)
                os.replace(tmp, gz)
                path.unlink()
                compressed += 1
    return compressed, deleted
