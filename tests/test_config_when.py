import unittest
from datetime import datetime

from seriallog import config, when


class ConfigTest(unittest.TestCase):
    def test_full(self):
        c = config.parse({
            "log_dir": "/tmp/x", "retention_days": 30,
            "port": [{"name": "usb0", "port": 4001}, {"name": "acm0", "host": "h", "port": 4003}],
            "alert": [{"name": "crash", "pattern": "panic", "ignore_case": True, "ports": ["usb0"]}],
        })
        self.assertEqual([p.name for p in c.ports], ["usb0", "acm0"])
        self.assertEqual(c.port("acm0").host, "h")
        a = c.alerts[0]
        self.assertTrue(a.regex.search("KERNEL PANIC"))
        self.assertTrue(a.applies_to("usb0"))
        self.assertFalse(a.applies_to("acm0"))

    def test_errors(self):
        bad = [
            {"port": [{"name": "bad name", "port": 1}]},
            {"port": [{"name": "a", "port": 1}, {"name": "a", "port": 2}]},
            {"port": [{"name": "a"}]},
            {"alert": [{"name": "x", "pattern": "("}]},
            {"alert": [{"name": "x", "pattern": "p", "ports": ["nope"]}]},
            {"retention_days": 0},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(config.ConfigError):
                config.parse(raw)

    def test_unknown_port(self):
        c = config.parse({"port": [{"name": "usb0", "port": 4001}]})
        with self.assertRaises(config.ConfigError):
            c.port("usb9")


class WhenTest(unittest.TestCase):
    now = datetime(2026, 10, 18, 19, 30)

    def test_relative(self):
        self.assertEqual(when.parse("30m", self.now), datetime(2026, 10, 18, 19, 0))
        self.assertEqual(when.parse("2h", self.now), datetime(2026, 10, 18, 17, 30))
        self.assertEqual(when.parse("1d", self.now), datetime(2026, 10, 17, 19, 30))

    def test_words_and_dates(self):
        self.assertEqual(when.parse("today", self.now), datetime(2026, 10, 18))
        self.assertEqual(when.parse("yesterday", self.now), datetime(2026, 10, 17))
        self.assertEqual(when.parse("2026-10-01", self.now), datetime(2026, 10, 1))
        self.assertEqual(when.parse("2026-10-01T08:15", self.now), datetime(2026, 10, 1, 8, 15))
        self.assertIsNone(when.parse("all", self.now))

    def test_bad(self):
        with self.assertRaises(ValueError):
            when.parse("last tuesday", self.now)


if __name__ == "__main__":
    unittest.main()
