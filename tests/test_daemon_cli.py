"""The daemon against a fake ser2net, and the CLI on the files it writes."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from seriallog import cli, config, daemon, logfile

OPEN_FAILURE = b"Device open failure: Value or file not found\r\n"


class FakeSer2net:
    """Answers each connection with the next scripted behaviour.

    A script is a list of byte strings to send (None = pause 0.3 s), after
    which the connection is closed. When the scripts run out, every further
    connection gets the open failure, like ser2net with nothing plugged in.
    """

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.connections = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def handle(self, reader, writer):
        self.connections += 1
        script = self.scripts.pop(0) if self.scripts else [OPEN_FAILURE]
        for part in script:
            if part is None:
                await asyncio.sleep(0.3)
            else:
                writer.write(part)
                await writer.drain()
        writer.close()


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = daemon.POLL_ABSENT, daemon.STATUS_EVERY
        daemon.POLL_ABSENT, daemon.STATUS_EVERY = 0.05, 0.05

    def tearDown(self):
        daemon.POLL_ABSENT, daemon.STATUS_EVERY = self.saved
        self.tmp.cleanup()

    def run_daemon(self, scripts, seconds=1.5):
        async def go():
            fake = FakeSer2net(scripts)
            port = await fake.start()
            conf = config.parse({
                "log_dir": str(self.dir), "flush_after": 0.1,
                "port": [{"name": "usb0", "port": port}],
                "alert": [{"name": "crash", "pattern": "panic", "ignore_case": True}],
            })
            task = asyncio.create_task(daemon.Daemon(conf).run())
            await asyncio.sleep(seconds)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            fake.server.close()
            return fake

        return asyncio.run(go())

    def entries(self):
        return [(e.kind, e.text) for e in logfile.read(self.dir, ["usb0"])]

    def test_device_session(self):
        fake = self.run_daemon([
            [OPEN_FAILURE],                                     # not plugged in yet
            [b"boot\r\nKERNEL PANIC 1\r\nKERNEL ", b"PANIC 2\r\nlogin: ", None],
        ])
        self.assertGreaterEqual(fake.connections, 3)            # kept polling afterwards
        self.assertEqual(self.entries(), [
            ("!", "device connected"),
            ("|", "boot"),
            ("|", "KERNEL PANIC 1"),
            ("|", "KERNEL PANIC 2"),
            ("|", "login: "),                                    # partial line flushed
            ("!", "device disconnected"),
        ])
        alerts = [json.loads(l) for l in (self.dir / "alerts.jsonl").read_text().splitlines()]
        self.assertEqual([(a["alert"], a["text"]) for a in alerts],
                         [("crash", "KERNEL PANIC 1")])         # second one inside cooldown
        status = json.loads((self.dir / "status.json").read_text())["ports"]["usb0"]
        self.assertEqual(status["state"], "absent")
        self.assertEqual(status["lines"], 4)
        self.assertEqual(status["last_line"], "login: ")

    def test_absent_device_writes_nothing(self):
        self.run_daemon([], seconds=0.5)
        self.assertEqual(self.entries(), [])
        self.assertFalse((self.dir / "alerts.jsonl").exists())

    def test_ser2net_down(self):
        async def go():
            conf = config.parse({"log_dir": str(self.dir),
                                 "port": [{"name": "usb0", "port": 9}]})   # nothing listens
            task = asyncio.create_task(daemon.Daemon(conf).run())
            await asyncio.sleep(0.3)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        asyncio.run(go())
        status = json.loads((self.dir / "status.json").read_text())["ports"]["usb0"]
        self.assertEqual(status["state"], "unreachable")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conf = self.dir / "seriallog.toml"
        self.conf.write_text(
            f'log_dir = "{(self.dir / "logs").as_posix()}"\n'
            '[[port]]\nname = "usb0"\nport = 4001\n'
            '[[port]]\nname = "acm0"\nport = 4003\n', encoding="utf-8")
        from datetime import datetime
        now = datetime.now().replace(microsecond=0).timestamp()
        for port, offset, text in [("usb0", -30, "boot ok"), ("acm0", -20, "HardFault at 0x08001234"),
                                   ("usb0", -10, "temp 41.5 C")]:
            log = logfile.PortLog(self.dir / "logs", port)
            log.write(now + offset, logfile.DATA, text)
            log.close()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["-c", str(self.conf), *args])
        return code, out.getvalue()

    def test_grep_all_ports(self):
        code, out = self.run_cli("grep", "fault|temp", "-i")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("acm0 ") and lines[0].endswith("HardFault at 0x08001234"))
        self.assertTrue(lines[1].startswith("usb0 ") and lines[1].endswith("temp 41.5 C"))

    def test_grep_json_one_port(self):
        code, out = self.run_cli("grep", "o", "-p", "usb0", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([r["text"] for r in json.loads(out)], ["boot ok"])

    def test_grep_no_match(self):
        self.assertEqual(self.run_cli("grep", "nothing-like-this")[0], 1)

    def test_tail(self):
        code, out = self.run_cli("tail", "usb0", "-n", "1")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("temp 41.5 C"))

    def test_unknown_port(self):
        self.assertEqual(self.run_cli("tail", "usb9")[0], 2)

    def test_ports_without_daemon(self):
        code, out = self.run_cli("ports")
        self.assertEqual(code, 0)
        self.assertIn("is the seriallog service running", out)


if __name__ == "__main__":
    unittest.main()
