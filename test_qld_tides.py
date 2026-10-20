#!/usr/bin/env python3
"""
Queensland tide predictions: parsing, the curve, site corrections, downloads.

- the MSQ high/low CSV is read (rows only, not the header block);
- the level between a high and a low follows a half cosine;
- the time and height corrections move every reading;
- a year not in data/ is found through the portal's dataset, saved, and not
  downloaded again; a failure is retried after an hour, not every update;
- the plugin turns it into today's highs and lows in the board's timezone.

Run: python3 test_qld_tides.py   (the plugin test needs LEDMATRIX_CORE)
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qld_tides  # noqa: E402
from qld_tides import AEST, QldTideSource, level_at, parse_hilo_csv  # noqa: E402

BUNDLED = (HERE / "data" / "burnett-heads-2026.csv").read_text(encoding="utf-8")


def at(day, hour, minute=0, year=2026, month=10):
    return datetime(year, month, day, hour, minute, tzinfo=AEST)


class _Response:
    def __init__(self, text="", json_body=None, status=200):
        self.text, self._json, self.status_code = text, json_body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise qld_tides.requests.HTTPError(str(self.status_code))

    def json(self):
        return self._json


class _Portal:
    """data.qld.gov.au: package_show, then the CSV."""

    CSV_URL = ("https://www.data.qld.gov.au/dataset/x/resource/y/download/"
               "q051010a_burnett-heads-storm-surge_2027_hilo.csv")

    def __init__(self, csv_text, years=("2027",)):
        self.csv_text, self.years, self.calls = csv_text, years, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url.endswith("/package_show"):
            resources = [{"name": "Description", "url": "http://example/desc.txt"}]
            resources += [{"name": f"{y} — Burnett Heads tide gauge predicted high/low data",
                           "url": self.CSV_URL.replace("2027", y)} for y in self.years]
            return _Response(json_body={"result": {"resources": resources}})
        if self.csv_text is None:
            return _Response(status=503)
        return _Response(text=self.csv_text)


class TestParse(unittest.TestCase):
    def test_reads_the_bundled_year(self):
        events = parse_hilo_csv(BUNDLED)
        self.assertEqual(len(events), 1411, "the file says 1411 valid readings")
        self.assertEqual(events[0], (at(1, 0, month=1), 0.67, "L"))
        today = [e for e in events if e[0].date() == at(20, 0).date()]
        self.assertEqual([(e[0].strftime("%H:%M"), e[1], e[2]) for e in today],
                         [("04:03", 2.02, "H"), ("09:40", 1.27, "L"),
                          ("16:25", 2.53, "H"), ("23:05", 1.08, "L")])

    def test_header_lines_are_not_readings(self):
        self.assertEqual(parse_hilo_csv("Number of days in the file =,365\n"
                                        "Date and time of first reading =,01 01 2026 00:00\n"), [])


class TestCurve(unittest.TestCase):
    events = [(at(20, 4, 3), 2.02, "H"), (at(20, 9, 40), 1.27, "L")]

    def test_ends_and_middle(self):
        self.assertAlmostEqual(level_at(self.events, at(20, 4, 3)), 2.02)
        self.assertAlmostEqual(level_at(self.events, at(20, 9, 40)), 1.27)
        mid = at(20, 4, 3) + (at(20, 9, 40) - at(20, 4, 3)) / 2
        self.assertAlmostEqual(level_at(self.events, mid), (2.02 + 1.27) / 2, places=3)

    def test_slow_near_the_turn(self):
        a = level_at(self.events, at(20, 4, 33))
        b = level_at(self.events, at(20, 6, 51))
        self.assertLess(2.02 - a, 0.05, "slack water at the high")
        self.assertGreater(abs(b - level_at(self.events, at(20, 7, 21))), 0.1, "fastest mid-tide")

    def test_outside_the_readings(self):
        self.assertIsNone(level_at(self.events, at(20, 12)))


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(HERE / "data" / "burnett-heads-2026.csv", self.dir)

    def source(self, portal=None, **kw):
        return QldTideSource("burnett-heads", session=portal or _Portal(None),
                             data_dir=self.dir, **kw)


class TestSource(_Sandbox):
    def test_bundled_year_needs_no_network(self):
        portal = _Portal(None)
        events = self.source(portal).events_around(at(20, 14))
        self.assertTrue(events)
        self.assertEqual(portal.calls, [])

    def test_corrections_move_every_reading(self):
        plain = self.source().events_around(at(20, 14))
        moved = self.source(time_offset_minutes=-10, height_offset_m=-0.1).events_around(at(20, 14))
        first = next(e for e in plain if e[0] == at(20, 16, 25))
        shifted = next(e for e in moved if e[0] == at(20, 16, 15))
        self.assertEqual(shifted[1], round(first[1] - 0.1, 3))
        self.assertEqual(shifted[2], "H")

    def test_window(self):
        events = self.source().events_around(at(20, 14), days_before=1, days_after=2)
        self.assertGreaterEqual(events[0][0], at(19, 14))
        self.assertLessEqual(events[-1][0], at(22, 14))
        self.assertEqual(len(events), 12)

    def test_next_year_is_downloaded_once_and_kept(self):
        new_year = BUNDLED.replace("/2026 ,", "/2027 ,")
        portal = _Portal(new_year)
        src = self.source(portal)
        events = src.events_around(datetime(2026, 12, 31, 20, tzinfo=AEST))
        self.assertTrue(any(e[0].year == 2027 for e in events), "runs into January")
        self.assertEqual([c[0].rsplit("/", 1)[-1] for c in portal.calls],
                         ["package_show", portal.CSV_URL.rsplit("/", 1)[-1]])
        self.assertEqual(portal.calls[0][1], {"id": "burnett-heads-tide-gauge-predicted-high-low-data"})
        self.assertTrue((self.dir / "burnett-heads-2027.csv").is_file())
        portal.calls.clear()
        QldTideSource("burnett-heads", session=portal, data_dir=self.dir).year(2027)
        self.assertEqual(portal.calls, [], "read from data/ next time")

    def test_a_year_not_yet_published_is_retried_hourly(self):
        portal = _Portal(None, years=())
        src = self.source(portal)
        self.assertEqual(src.year(2027), [])
        self.assertEqual(src.year(2027), [])
        self.assertEqual(len(portal.calls), 1, "backs off")
        with mock.patch.object(qld_tides.time, "time", return_value=qld_tides.time.time() + 3601):
            src.year(2027)
        self.assertEqual(len(portal.calls), 2)

    def test_a_failed_download_saves_nothing(self):
        src = self.source(_Portal(None))
        self.assertEqual(src.year(2027), [])
        self.assertFalse((self.dir / "burnett-heads-2027.csv").exists())

    def test_gauge_name_is_cleaned(self):
        self.assertEqual(QldTideSource(" Burnett-Heads/../x ").gauge, "burnett-headsx")


def _find_core():
    for c in (os.environ.get("LEDMATRIX_CORE", ""), str(HERE.parent / "LEDMatrix"),
              str(Path.home() / "LEDMatrix")):
        if c and (Path(c) / "src" / "plugin_system").is_dir():
            return c
    return None


@unittest.skipIf(_find_core() is None, "no LEDMatrix core checkout")
class TestPlugin(_Sandbox):
    def plugin(self, **cfg):
        sys.path.insert(0, _find_core())
        from src.plugin_system.testing import MockCacheManager, MockPluginManager
        from src.plugin_system.testing.visual_display_manager import VisualTestDisplayManager
        from manager import TidesPlugin
        config = {"enabled": True, "gauge": "burnett-heads", **cfg}
        with mock.patch.object(qld_tides, "DATA_DIR", self.dir):
            p = TidesPlugin("tides", config, VisualTestDisplayManager(width=192, height=32),
                            MockCacheManager(), MockPluginManager())
        p.source.data_dir = self.dir
        p.source.session = _Portal(None)
        p._get_global_timezone = lambda: "Australia/Brisbane"
        return p

    def update_at(self, p, when):
        class _Now(datetime):
            @classmethod
            def now(cls, tz=None):
                return when.astimezone(tz) if tz else when.replace(tzinfo=None)
        with mock.patch("manager.datetime", _Now):
            p.update()

    def test_todays_tides_in_brisbane_time(self):
        p = self.plugin()
        self.update_at(p, at(20, 14))
        self.assertEqual([(e["dt"][11:16], e["type"], e["height"]) for e in p.hilo],
                         [("04:03", "H", 2.02), ("09:40", "L", 1.27),
                          ("16:25", "H", 2.53), ("23:05", "L", 1.08)])
        self.assertEqual(len(p.hourly), 24)
        self.assertTrue(1.27 < p.live < 2.53, "rising between the low and the high")

    def test_defaults_are_woodgate_in_metres(self):
        p = self.plugin()
        self.assertEqual((p.station_name, p.units, p.station_id),
                         ("Woodgate Beach", "metric", "burnett-heads"))
        self.assertEqual(p.modes, ["tides_current", "tides_schedule", "tides_chart", "tides_stats"])

    def test_feet(self):
        p = self.plugin(units="imperial")
        self.update_at(p, at(20, 14))
        self.assertEqual(p.hilo[0]["height"], round(2.02 * 3.28084, 2))

    def test_next_tide_after_the_days_last(self):
        p = self.plugin()
        self.update_at(p, at(20, 23, 30))
        with mock.patch.object(p, "_now", return_value=datetime(2026, 10, 20, 23, 30)):
            nxt = p._next_tides(1)
        self.assertEqual(nxt[0]["dt"][:16], "2026-10-21T04:56")

    def test_a_screen_switched_off_does_not_draw(self):
        p = self.plugin(show_stats=False)
        self.update_at(p, at(20, 14))
        self.assertFalse(p.display("tides_stats"))
        self.assertNotIn("tides_stats", p.modes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
