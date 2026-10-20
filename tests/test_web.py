"""The web dashboard, served from a temporary log folder."""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from seriallog import config, logfile, web


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        now = datetime.now().replace(microsecond=0).timestamp()
        for port, offset, text in [("usb0", -30, "boot ok"), ("acm0", -20, "HardFault at 0x08001234"),
                                   ("usb0", -10, "temp 41.5 C")]:
            log = logfile.PortLog(self.dir, port)
            log.write(now + offset, logfile.DATA, text)
            log.close()
        (self.dir / "status.json").write_text(json.dumps({
            "updated": logfile.stamp(now),
            "ports": {"usb0": {"endpoint": "127.0.0.1:4001", "state": "connected",
                               "since": logfile.stamp(now - 60), "last_line": "temp 41.5 C"}},
        }), encoding="utf-8")
        alerts = [{"time": logfile.stamp(now - 20), "port": "acm0", "alert": "crash",
                   "text": "HardFault at 0x08001234"},
                  {"time": logfile.stamp(now - 3 * 86400), "port": "usb0", "alert": "crash",
                   "text": "old one"}]
        (self.dir / "alerts.jsonl").write_text(
            "".join(json.dumps(a) + "\n" for a in alerts), encoding="utf-8")
        conf = config.parse({"log_dir": str(self.dir),
                             "port": [{"name": "usb0", "port": 4001}, {"name": "acm0", "port": 4003}]})
        self.threads = set(threading.enumerate())
        self.server = web.Server(("127.0.0.1", 0), conf)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for t in set(threading.enumerate()) - self.threads:   # a stream still closing its file
            t.join(timeout=2)
        self.tmp.cleanup()

    def get(self, path):
        """(status code, body, content type)."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.read().decode("utf-8"), r.headers.get_content_type()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8"), e.headers.get_content_type()

    def get_json(self, path):
        code, body, ctype = self.get(path)
        self.assertEqual((code, ctype), (200, "application/json"), body)
        return json.loads(body)

    def test_page_is_self_contained(self):
        code, body, ctype = self.get("/")
        self.assertEqual((code, ctype), (200, "text/html"))
        self.assertIn("<title>seriallog</title>", body)
        self.assertNotIn("://", body)                   # no CDN, no external script
        self.assertEqual(self.get("/nothing")[0], 404)

    def test_status_lists_configured_ports(self):
        s = self.get_json("/api/status")
        self.assertFalse(s["stale"])
        self.assertEqual(list(s["ports"]), ["usb0", "acm0"])
        self.assertEqual(s["ports"]["usb0"]["state"], "connected")
        self.assertEqual(s["ports"]["acm0"], {"endpoint": "127.0.0.1:4003", "state": None})

    def test_status_without_daemon(self):
        (self.dir / "status.json").unlink()
        s = self.get_json("/api/status")
        self.assertTrue(s["stale"])
        self.assertEqual(list(s["ports"]), ["usb0", "acm0"])

    def test_tail(self):
        rows = self.get_json("/api/tail?port=usb0&n=1")
        self.assertEqual([(r["port"], r["kind"], r["text"]) for r in rows],
                         [("usb0", "data", "temp 41.5 C")])
        self.assertEqual(len(self.get_json("/api/tail?port=usb0")), 2)
        code, body, _ = self.get("/api/tail?port=usb9")
        self.assertEqual(code, 400)
        self.assertIn("unknown port 'usb9'", body)
        self.assertEqual(self.get("/api/tail?port=usb0&n=lots")[0], 400)

    def test_grep(self):
        rows = self.get_json("/api/grep?q=fault%7Ctemp&i=1&since=1h")
        self.assertEqual([(r["port"], r["text"]) for r in rows],
                         [("acm0", "HardFault at 0x08001234"), ("usb0", "temp 41.5 C")])
        self.assertEqual([r["text"] for r in self.get_json("/api/grep?q=o&ports=usb0")], ["boot ok"])
        self.assertEqual(len(self.get_json("/api/grep?q=.&max=1")), 1)
        self.assertEqual(self.get_json("/api/grep?q=.&since=now"), [])
        code, body, _ = self.get("/api/grep?q=%5B")          # an unterminated character class
        self.assertEqual(code, 400)
        self.assertIn("bad request", body)
        self.assertEqual(self.get("/api/grep?q=x&since=soon")[0], 400)
        self.assertEqual(self.get("/api/grep?q=x&ports=usb0,usb9")[0], 400)

    def test_alerts(self):
        recent = self.get_json("/api/alerts?since=24h")
        self.assertEqual([(r["port"], r["alert"]) for r in recent], [("acm0", "crash")])
        self.assertEqual(len(self.get_json("/api/alerts?since=all")), 2)
        self.assertEqual(self.get("/api/alerts?since=whenever")[0], 400)

    def test_follow_delivers_new_lines(self):
        resp = urllib.request.urlopen(self.base + "/api/follow?port=usb0", timeout=5)
        self.assertEqual(resp.headers.get_content_type(), "text/event-stream")
        log = logfile.PortLog(self.dir, "usb0")
        log.write(time.time(), logfile.EVENT, "device disconnected")
        log.write(time.time(), logfile.DATA, "rebooting")
        log.close()
        got = []
        while len(got) < 2:
            line = resp.readline()
            if line.startswith(b"data: "):
                got.append(json.loads(line[6:]))
        resp.close()
        self.assertEqual([(r["kind"], r["text"]) for r in got],
                         [("event", "device disconnected"), ("data", "rebooting")])
        self.assertEqual(self.get("/api/follow?port=usb9")[0], 400)

    def test_bind(self):
        self.assertEqual(web.parse_bind("0.0.0.0:80"), ("0.0.0.0", 80))
        self.assertEqual(web.parse_bind("[::1]:8777"), ("::1", 8777))
        with self.assertRaises(ValueError):
            web.parse_bind("8777")


if __name__ == "__main__":
    unittest.main()
