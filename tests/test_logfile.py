import gzip
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from seriallog import logfile
from seriallog.logfile import DATA, EVENT, LineSplitter, PortLog


def ts(text):
    """Local datetime string to epoch seconds."""
    return datetime.fromisoformat(text).timestamp()


class LineSplitterTest(unittest.TestCase):
    def test_line_endings(self):
        s = LineSplitter()
        out = s.feed(b"a\nb\r\nc\rd", 1.0)
        self.assertEqual([l for _, l in out], [b"a", b"b", b"c"])
        self.assertTrue(s.pending)
        self.assertEqual(s.flush(), [(1.0, b"d")])
        self.assertFalse(s.pending)

    def test_crlf_split_across_chunks(self):
        s = LineSplitter()
        self.assertEqual(s.feed(b"one\r", 1.0), [])          # \n may still follow
        self.assertEqual(s.feed(b"\ntwo\n", 2.0), [(1.0, b"one"), (2.0, b"two")])

    def test_lone_cr_at_end_flushes_without_cr(self):
        s = LineSplitter()
        s.feed(b"prompt\r", 1.0)
        self.assertEqual(s.flush(), [(1.0, b"prompt")])

    def test_timestamp_is_first_byte(self):
        s = LineSplitter()
        s.feed(b"hel", 10.0)
        self.assertEqual(s.feed(b"lo\nnext", 11.0), [(10.0, b"hello")])
        self.assertEqual(s.flush(), [(11.0, b"next")])

    def test_empty_lines_kept(self):
        s = LineSplitter()
        self.assertEqual([l for _, l in s.feed(b"a\n\nb\n", 1.0)], [b"a", b"", b"b"])

    def test_long_line_split(self):
        s = LineSplitter(max_len=4)
        out = s.feed(b"abcdefghij", 1.0)
        self.assertEqual([l for _, l in out], [b"abcd", b"efgh"])
        self.assertEqual(s.flush(), [(1.0, b"ij")])


class RenderTest(unittest.TestCase):
    def test_ansi_removed(self):
        self.assertEqual(logfile.render(b"\x1b[0;32mI (123) ok\x1b[0m"), "I (123) ok")

    def test_bad_utf8_and_control(self):
        self.assertEqual(logfile.render(b"a\xffb\x07c\td"), "a\\xffb\\x07c\td")

    def test_utf8_kept(self):
        self.assertEqual(logfile.render("25 °C".encode()), "25 °C")


class FormatTest(unittest.TestCase):
    def test_roundtrip(self):
        t = ts("2026-10-18T19:40:01.123")
        line = logfile.format_line(t, DATA, "boot | ok")
        when = datetime(2026, 10, 18, 19, 40, 1, 123000).astimezone()
        self.assertEqual(line, f"{when.isoformat(timespec='milliseconds')} | boot | ok\n")
        self.assertRegex(line, r"^2026-10-18T19:40:01\.123[+-]\d\d:\d\d \| ")
        e = logfile.parse_line("usb0", line)
        self.assertEqual((e.port, e.kind, e.text), ("usb0", DATA, "boot | ok"))
        self.assertEqual(e.time, when)

    def test_other_zone(self):
        e = logfile.parse_line("usb0", "2026-10-18T17:40:01.123+00:00 | x\n")
        self.assertEqual(e.time, datetime(2026, 10, 18, 17, 40, 1, 123000, tzinfo=timezone.utc))
        self.assertEqual(e.time.utcoffset(), e.time.astimezone().utcoffset())   # shown in local time

    def test_line_without_offset(self):
        """Files written before the offset was added are local time."""
        e = logfile.parse_line("usb0", "2026-10-18T19:40:01.123 | x\n")
        self.assertEqual(e.time, datetime(2026, 10, 18, 19, 40, 1, 123000).astimezone())

    def test_empty_text(self):
        e = logfile.parse_line("usb0", logfile.format_line(ts("2026-10-18T00:00:00"), DATA, ""))
        self.assertEqual(e.text, "")

    def test_garbage_ignored(self):
        self.assertIsNone(logfile.parse_line("usb0", "not a log line\n"))
        self.assertIsNone(logfile.parse_line("usb0", "2026-10-18T19:40:01.123 ? x\n"))


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, port, *lines):
        log = PortLog(self.dir, port)
        for when, kind, text in lines:
            log.write(ts(when), kind, text)
        log.close()

    def test_midnight_switch(self):
        self.write("usb0",
                   ("2026-10-17T23:59:59.900", DATA, "late"),
                   ("2026-10-18T00:00:00.100", DATA, "early"))
        names = sorted(p.name for p in (self.dir / "usb0").iterdir())
        self.assertEqual(names, ["2026-10-17.log", "2026-10-18.log"])

    def test_read_merges_ports_in_time_order(self):
        self.write("a", ("2026-10-18T10:00:00", DATA, "a1"), ("2026-10-18T10:00:02", DATA, "a2"))
        self.write("b", ("2026-10-18T10:00:01", EVENT, "device connected"))
        got = [(e.port, e.text) for e in logfile.read(self.dir, ["a", "b"])]
        self.assertEqual(got, [("a", "a1"), ("b", "device connected"), ("a", "a2")])

    def test_since_until(self):
        self.write("a", ("2026-10-16T10:00:00", DATA, "old"),
                   ("2026-10-18T09:00:00", DATA, "in"),
                   ("2026-10-18T11:00:00", DATA, "late"))
        got = [e.text for e in logfile.read(
            self.dir, ["a"], datetime(2026, 10, 18), datetime(2026, 10, 18, 10))]
        self.assertEqual(got, ["in"])

    def test_housekeeping(self):
        today = date(2026, 10, 18)
        for d in range(5):
            day = today - timedelta(days=d)
            self.write("a", (f"{day.isoformat()}T12:00:00", DATA, f"day-{d}"))
        compressed, deleted = logfile.housekeeping(self.dir, ["a"], retention_days=4, today=today)
        names = sorted(p.name for p in (self.dir / "a").iterdir())
        self.assertEqual(names, ["2026-10-15.log.gz", "2026-10-16.log.gz",
                                 "2026-10-17.log", "2026-10-18.log"])
        self.assertEqual((compressed, deleted), (2, 1))
        with gzip.open(self.dir / "a" / "2026-10-16.log.gz", "rt") as f:
            self.assertIn("day-2", f.read())
        # compressed days are still searchable
        texts = [e.text for e in logfile.read(self.dir, ["a"])]
        self.assertEqual(texts, ["day-3", "day-2", "day-1", "day-0"])

    def test_follow_across_midnight(self):
        days = [date(2026, 10, 17)]
        gen = logfile.follow(self.dir, "a", poll=0, today=lambda: days[-1])
        log = PortLog(self.dir, "a")
        try:
            log.write(ts("2026-10-17T23:00:00"), DATA, "old")        # already shown by tail
            self.assertIsNone(next(gen))
            log.write(ts("2026-10-17T23:59:59"), DATA, "late")
            self.assertEqual(next(gen).text, "late")
            self.assertIsNone(next(gen))
            days.append(date(2026, 10, 18))                           # midnight passes
            log.write(ts("2026-10-17T23:59:59.900"), DATA, "later")   # still in yesterday's file
            log.write(ts("2026-10-18T00:00:01"), DATA, "early")
            self.assertEqual([next(gen).text, next(gen).text], ["later", "early"])
        finally:
            gen.close()
            log.close()

    def test_last_lines_across_days(self):
        self.write("a", ("2026-10-17T10:00:00", DATA, "x"), ("2026-10-17T11:00:00", DATA, "y"),
                   ("2026-10-18T10:00:00", DATA, "z"))
        self.assertEqual([e.text for e in logfile.last_lines(self.dir, "a", 2)], ["y", "z"])
        self.assertEqual([e.text for e in logfile.last_lines(self.dir, "a", 10)], ["x", "y", "z"])
        self.assertEqual(logfile.last_lines(self.dir, "missing", 5), [])


if __name__ == "__main__":
    unittest.main()
