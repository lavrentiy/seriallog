"""The logging daemon: one silent ser2net client per port.

ser2net opens the serial device when a client connects. If the device is
not plugged in it sends "Device open failure: ..." and hangs up, so the
worker polls every few seconds and logs an event when the device appears or
goes away. ser2net itself being unreachable, and coming back, is logged as an
event too, so a gap in a log file explains itself. Nothing is ever written to
the device.
"""

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

from seriallog import logfile
from seriallog.logfile import DATA, EVENT, LineSplitter, PortLog, parse_stamp, render, stamp

log = logging.getLogger("seriallog")

SER2NET_OPEN_FAILURE = b"Device open failure"
CONNECT_TIMEOUT = 5.0      # seconds to reach ser2net
POLL_ABSENT = 3.0          # seconds between tries while the device is unplugged
FIRST_DATA_WAIT = 0.5      # ser2net reports an open failure within this time
STATUS_EVERY = 2.0
HOUSEKEEPING_EVERY = 3600.0
ALERTS_FILE = "alerts.jsonl"
STATUS_FILE = "status.json"

CONNECTED, ABSENT, UNREACHABLE = "connected", "absent", "unreachable"


class Daemon:
    def __init__(self, config, clock=time.time):
        self.config = config
        self.clock = clock
        self.log_dir = Path(config.log_dir)
        self.status = {}
        self.dirty = True
        self.last_alert = {}          # (alert name, port) -> time
        self.workers = [PortWorker(self, p) for p in config.ports]

    # ----- shared state -----

    def set_state(self, port, state, detail=""):
        s = self.status.setdefault(port.name, {
            "endpoint": f"{port.host}:{port.port}", "state": None, "since": None,
            "detail": "", "lines": 0, "bytes": 0, "last_line": None, "last_line_at": None,
        })
        if s["state"] != state or s["detail"] != detail:
            s.update(state=state, since=stamp(self.clock()), detail=detail)
            self.dirty = True

    def count(self, port, nbytes, t=None, text=None):
        s = self.status[port.name]
        s["bytes"] += nbytes
        if text is not None:
            s["lines"] += 1
            s["last_line"] = text
            s["last_line_at"] = stamp(t)
        self.dirty = True

    def check_alerts(self, port, t, text):
        for alert in self.config.alerts:
            if not alert.applies_to(port.name) or not alert.regex.search(text):
                continue
            key = (alert.name, port.name)
            if t - self.last_alert.get(key, float("-inf")) < alert.cooldown:
                continue
            self.last_alert[key] = t
            record = {"time": stamp(t), "port": port.name, "alert": alert.name, "text": text}
            with open(self.log_dir / ALERTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.warning("alert %s on %s: %s", alert.name, port.name, text)

    def write_status(self):
        if not self.dirty:
            return
        doc = {"updated": stamp(self.clock()), "ports": self.status}
        tmp = self.log_dir / (STATUS_FILE + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.log_dir / STATUS_FILE)
        self.dirty = False

    def housekeeping(self):
        names = [p.name for p in self.config.ports]
        done = logfile.housekeeping(self.log_dir, names, self.config.retention_days)
        trim_alerts(self.log_dir / ALERTS_FILE, self.config.retention_days, self.clock())
        if any(done):
            log.info("housekeeping: %d day files compressed, %d deleted", *done)

    # ----- running -----

    async def run(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        for w in self.workers:
            self.set_state(w.port, UNREACHABLE, "starting")
        await asyncio.to_thread(self.housekeeping)
        tasks = [asyncio.create_task(w.run(), name=w.port.name) for w in self.workers]
        tasks.append(asyncio.create_task(self._status_loop()))
        tasks.append(asyncio.create_task(self._housekeeping_loop()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            for w in self.workers:
                w.close()
            self.write_status()

    async def _status_loop(self):
        while True:
            self.write_status()
            await asyncio.sleep(STATUS_EVERY)

    async def _housekeeping_loop(self):
        while True:
            await asyncio.sleep(HOUSEKEEPING_EVERY)
            await asyncio.to_thread(self.housekeeping)


class PortWorker:
    def __init__(self, daemon, port):
        self.d = daemon
        self.port = port
        self.out = PortLog(daemon.log_dir, port.name)
        self.split = LineSplitter()
        self.state = None

    def event(self, text, level=logging.INFO):
        self.out.write(self.d.clock(), EVENT, text)
        log.log(level, "%s: %s", self.port.name, text)

    def lines(self, items):
        for t, raw in items:
            text = render(raw)
            self.out.write(t, DATA, text)
            self.d.count(self.port, 0, t, text)
            self.d.check_alerts(self.port, t, text)

    def became(self, state, detail=""):
        if state != UNREACHABLE and self.state == UNREACHABLE:
            self.event("ser2net reachable")
        if state == CONNECTED and self.state != CONNECTED:
            self.event("device connected")
        elif state != CONNECTED and self.state == CONNECTED:
            self.lines(self.split.flush())
            self.event("device disconnected")
        if state == UNREACHABLE and self.state != UNREACHABLE:
            self.event(f"ser2net unreachable at {self.port.host}:{self.port.port}: {detail}",
                       logging.WARNING)
        self.state = state
        self.d.set_state(self.port, state, detail)

    async def run(self):
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.port.host, self.port.port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as e:
                self.became(UNREACHABLE, str(e) or type(e).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            try:
                await self.session(reader)
            finally:
                writer.close()
            if self.state != CONNECTED:
                await asyncio.sleep(POLL_ABSENT)

    async def session(self, reader):
        try:
            first = await asyncio.wait_for(reader.read(4096), FIRST_DATA_WAIT)
        except asyncio.TimeoutError:
            first = None                   # quiet device, but it is open
        except OSError:
            self.became(ABSENT, "connection reset")
            return
        if first is not None and (first == b"" or first.startswith(SER2NET_OPEN_FAILURE)):
            reason = first.decode("utf-8", "replace").strip() or "closed by ser2net"
            self.became(ABSENT, reason)
            return
        self.became(CONNECTED)
        if first:
            self.feed(first)
        while True:
            timeout = self.d.config.flush_after if self.split.pending else None
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout)
            except asyncio.TimeoutError:
                self.lines(self.split.flush())
                continue
            except OSError:
                chunk = b""
            if not chunk:
                self.became(ABSENT, "device closed")
                return
            self.feed(chunk)

    def feed(self, data):
        self.d.count(self.port, len(data))
        self.lines(self.split.feed(data, self.d.clock()))

    def close(self):
        self.lines(self.split.flush())
        self.out.close()


def trim_alerts(path, retention_days, now):
    """Drop alert records older than the retention period."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    cutoff = now - retention_days * 86400

    def recent(line):
        try:
            return parse_stamp(json.loads(line)["time"]).timestamp() >= cutoff
        except (ValueError, KeyError, TypeError):
            return False

    keep = [l for l in lines if recent(l)]
    if len(keep) != len(lines):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(l + "\n" for l in keep), encoding="utf-8")
        os.replace(tmp, path)


def run(config):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    os.umask(0o022)                       # logs readable by the CLI and the dashboard
    if not config.ports:
        log.warning("no ports configured; nothing to log")
    daemon = Daemon(config)

    async def main():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, task.cancel)
            except (NotImplementedError, AttributeError):
                pass
        try:
            await daemon.run()
        except asyncio.CancelledError:
            log.info("stopping")

    asyncio.run(main())
    return 0
