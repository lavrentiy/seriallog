"""seriallog: timestamped logs of serial ports shared by ser2net.

  seriallog ports                  which ports are logged, and whether a device is on them
  seriallog tail usb0 [-f]         last lines of a port, optionally following new ones
  seriallog grep PANIC --since 2h  search the logs (all ports unless -p is given)
  seriallog alerts                 alerts from the last 24 hours
  seriallog notify                 desktop notification for each new alert (run in the session)
  seriallog daemon                 the logger itself (runs as a service)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from seriallog import __version__, config as cfg, logfile, when
from seriallog.daemon import ALERTS_FILE, STATUS_FILE

STALE_STATUS = 30  # seconds; older status means the daemon isn't running


# ---------- Colour ----------

class Paint:
    def __init__(self, on):
        self.on = on

    def __call__(self, code, text):
        return f"\x1b[{code}m{text}\x1b[0m" if self.on else text

    def red(self, t):  return self("1;31", t)
    def dim(self, t):  return self("2", t)
    def bold(self, t): return self("1", t)
    def green(self, t): return self("32", t)
    def amber(self, t): return self("33", t)


def paint_for(stream):
    return Paint(stream.isatty() and "NO_COLOR" not in os.environ
                 and os.environ.get("TERM") != "dumb")


# ---------- Helpers ----------

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def entry_dict(e):
    return {"port": e.port, "time": e.time.isoformat(timespec="milliseconds"),
            "kind": "event" if e.kind == logfile.EVENT else "data", "text": e.text}


def show_entry(e, p, port_width=0, highlight=None):
    text = e.text
    if highlight and p.on:
        text = highlight.sub(lambda m: p.red(m.group(0)), text)
    if e.kind == logfile.EVENT:
        text = p.amber(f"! {text}")
    port = f"{p.red(e.port.ljust(port_width))} " if port_width else ""
    return f"{port}{p.dim(e.time.strftime('%Y-%m-%d %H:%M:%S.') + f'{e.time.microsecond // 1000:03d}')} {text}"


def ports_named(conf, names):
    return [conf.port(n).name for n in names] if names else [p.name for p in conf.ports]


# ---------- Commands ----------

def cmd_ports(conf, a):
    status = load_json(conf.log_dir / STATUS_FILE, {})
    ports = status.get("ports", {})
    if a.json:
        print(json.dumps(status, indent=1, ensure_ascii=False))
        return 0
    p = paint_for(sys.stdout)
    updated = status.get("updated")
    if not updated:
        print(p.amber("No status yet: is the seriallog service running?"))
    elif (datetime.now().astimezone() - logfile.parse_stamp(updated)).total_seconds() > STALE_STATUS:
        print(p.amber(f"Status last written {updated}: the seriallog service may be stopped."))
    for port in conf.ports:
        s = ports.get(port.name, {})
        state = s.get("state") or "unknown"
        colour = {"connected": p.green, "absent": p.dim}.get(state, p.amber)
        since = (s.get("since") or "")[:19].replace("T", " ")
        print(f"{p.red(port.name):<8} {colour(state):<12} since {since}  "
              f"{p.dim(s.get('endpoint', f'{port.host}:{port.port}'))}")
        if s.get("last_line_at"):
            last = (s.get("last_line") or "")[:100]
            print(f"         last line {s['last_line_at'][:19].replace('T', ' ')}: {last}")
    return 0


def cmd_tail(conf, a):
    name = conf.port(a.port).name
    p = paint_for(sys.stdout)
    for e in logfile.last_lines(conf.log_dir, name, a.lines):
        print(json.dumps(entry_dict(e), ensure_ascii=False) if a.json else show_entry(e, p))
    if not a.follow:
        return 0
    sys.stdout.flush()
    return follow(conf.log_dir / name, name, p, a.json)


def follow(folder, name, p, as_json):
    """Print new lines of today's file as they arrive, moving on at midnight."""
    day, f = None, None
    try:
        while True:
            today = datetime.now().date()
            if today != day:
                path = folder / f"{today.isoformat()}.log"
                if path.exists():
                    if f:
                        f.close()
                    f = open(path, "rb")
                    if day is None:
                        f.seek(0, os.SEEK_END)     # tail already printed the history
                    day = today
            line = f.readline() if f else b""
            if line.endswith(b"\n"):
                e = logfile.parse_line(name, line.decode("utf-8", "replace"))
                if e:
                    print(json.dumps(entry_dict(e), ensure_ascii=False) if as_json
                          else show_entry(e, p), flush=True)
                continue
            if line:                                # partial line: wait for the rest
                f.seek(-len(line), os.SEEK_CUR)
            time.sleep(0.3)
    except KeyboardInterrupt:
        return 0
    finally:
        if f:
            f.close()


def cmd_grep(conf, a):
    flags = re.IGNORECASE if a.ignore_case else 0
    try:
        rx = re.compile(re.escape(a.pattern) if a.fixed else a.pattern, flags)
    except re.error as e:
        print(f"seriallog: bad pattern: {e}", file=sys.stderr)
        return 2
    since, until = when.parse(a.since), when.parse(a.until) if a.until else None
    names = ports_named(conf, a.port)
    p = paint_for(sys.stdout)
    width = max(len(n) for n in names) if len(names) > 1 else 0
    found = []
    for e in logfile.read(conf.log_dir, names, since, until):
        if e.kind == logfile.EVENT and not a.events:
            continue
        if rx.search(e.text):
            found.append(e)
            if not a.json:
                print(show_entry(e, p, width, rx))
            if len(found) >= a.max:
                if not a.json:
                    print(p.amber(f"... stopped after {a.max} matches (--max)"), file=sys.stderr)
                break
    if a.json:
        print(json.dumps([entry_dict(e) for e in found], ensure_ascii=False))
    return 0 if found else 1


def read_alerts(conf, since):
    out = []
    try:
        lines = (conf.log_dir / ALERTS_FILE).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
            t = logfile.parse_stamp(r["time"])
        except (ValueError, KeyError, TypeError):
            continue
        if since is None or t >= since:
            out.append(r)
    return out


def cmd_alerts(conf, a):
    records = read_alerts(conf, when.parse(a.since))
    if a.json:
        print(json.dumps(records, ensure_ascii=False))
        return 0
    p = paint_for(sys.stdout)
    if not records:
        print(p.dim(f"No alerts since {a.since}."))
    for r in records:
        print(f"{p.dim(r['time'][:19].replace('T', ' '))} {p.red(r['port'])} "
              f"{p.amber(r['alert'])}: {r['text']}")
    return 0


def cmd_notify(conf, a):
    """Follow the alerts file and show each new alert as a desktop notification."""
    path = conf.log_dir / ALERTS_FILE
    sender = shutil.which("notify-send")
    pos = path.stat().st_size if path.exists() else 0
    try:
        while True:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size < pos:                 # trimmed by housekeeping
                pos = 0
            if size > pos:
                with open(path, encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                complete = chunk[:chunk.rfind("\n") + 1]
                pos += len(complete.encode("utf-8"))
                for line in complete.splitlines():
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    title = f"serial {r['port']}: {r['alert']}"
                    if sender:
                        subprocess.run([sender, "-u", "critical", "-a", "seriallog",
                                        title, r["text"][:300]], check=False)
                    else:
                        print(f"{title}: {r['text']}", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def cmd_daemon(conf, a):
    from seriallog import daemon
    return daemon.run(conf)


# ---------- Arguments ----------

def build_parser():
    ap = argparse.ArgumentParser(prog="seriallog", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", help=f"config file (default {cfg.DEFAULT_CONFIG} "
                    "or $SERIALLOG_CONFIG)")
    ap.add_argument("--version", action="version", version=f"seriallog {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("ports", help="logged ports and device state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ports)

    s = sub.add_parser("tail", help="last lines of a port")
    s.add_argument("port")
    s.add_argument("-n", "--lines", type=int, default=20)
    s.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    s.add_argument("--json", action="store_true", help="one JSON object per line")
    s.set_defaults(fn=cmd_tail)

    s = sub.add_parser("grep", help="search the logs")
    s.add_argument("pattern", help="Python regular expression (or text with -F)")
    s.add_argument("-p", "--port", action="append", default=[], help="only this port (repeatable)")
    s.add_argument("--since", default="7d", help="start: 30m, 2h, 3d, today, 2026-10-18 or all (default 7d)")
    s.add_argument("--until", help="end, same formats")
    s.add_argument("-i", "--ignore-case", action="store_true")
    s.add_argument("-F", "--fixed", action="store_true", help="pattern is plain text")
    s.add_argument("--events", action="store_true", help="also search connect/disconnect events")
    s.add_argument("--max", type=int, default=1000, help="stop after this many matches")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_grep)

    s = sub.add_parser("alerts", help="recent alerts")
    s.add_argument("--since", default="24h")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("notify", help="desktop notification for each new alert")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("daemon", help="run the logger")
    s.set_defaults(fn=cmd_daemon)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        conf = cfg.load(a.config)
        return a.fn(conf, a)
    except (cfg.ConfigError, ValueError) as e:
        print(f"seriallog: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:            # seriallog grep ... | head
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
