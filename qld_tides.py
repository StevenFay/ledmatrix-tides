"""
Queensland tide predictions from Maritime Safety Queensland (MSQ).

MSQ publishes a year of predicted high and low waters for each of its tide
gauges on the Queensland Government open data portal (data.qld.gov.au), one
CSV per gauge per year, in metres above Lowest Astronomical Tide (LAT) and in
AEST (UTC+10, no daylight saving). A reading row looks like::

    20/10/2026 , 04:03 ,  1 ,   2.020        (1 = high water, -1 = low water)

Each year's file is downloaded once and kept in ``data/``; predictions do not
change, so the board runs offline after that. Burnett Heads 2026 ships with
the plugin.

Between a high and a low the water follows a half cosine, the standard way to
fill in the curve from high/low times (the "rule of twelfths" is the same
shape). Good to a few centimetres for a display.
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

AEST = timezone(timedelta(hours=10))
CKAN = "https://www.data.qld.gov.au/api/3/action"
PACKAGE_SUFFIX = "-tide-gauge-predicted-high-low-data"
DATA_DIR = Path(__file__).resolve().parent / "data"

#: Seconds before a year that could not be downloaded is tried again.
RETRY_SECONDS = 60 * 60

_ROW = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*,\s*(\d{1,2}):(\d{2})\s*,\s*(-?1)\s*,\s*(-?[\d.]+)")

Event = Tuple[datetime, float, str]     # (aware time, metres LAT, 'H' or 'L')


def parse_hilo_csv(text: str) -> List[Event]:
    """The high/low readings in an MSQ predicted high/low CSV, oldest first."""
    events: List[Event] = []
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        day, month, year, hour, minute, kind, height = m.groups()
        try:
            when = datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=AEST)
            events.append((when, float(height), "H" if kind == "1" else "L"))
        except ValueError:
            continue
    events.sort(key=lambda e: e[0])
    return events


def level_at(events: List[Event], when: datetime) -> Optional[float]:
    """Water level at ``when``: a half cosine between the readings either side."""
    for (t0, h0, _), (t1, h1, _) in zip(events, events[1:]):
        if t0 <= when <= t1:
            span = (t1 - t0).total_seconds()
            if span <= 0:
                return h0
            frac = (when - t0).total_seconds() / span
            return h0 + (h1 - h0) * (1 - math.cos(math.pi * frac)) / 2
    return None


class QldTideSource:
    """One gauge's predictions, with the site's time and height corrections.

    ``gauge`` is the dataset name on data.qld.gov.au without
    ``-tide-gauge-predicted-high-low-data`` (e.g. ``burnett-heads``).
    """

    def __init__(self, gauge: str, time_offset_minutes: float = 0.0,
                 height_offset_m: float = 0.0, session=None, data_dir: Path = DATA_DIR,
                 log: Optional[logging.Logger] = None):
        self.gauge = re.sub(r"[^a-z0-9-]", "", (gauge or "").strip().lower())
        self.time_offset = timedelta(minutes=float(time_offset_minutes or 0))
        self.height_offset = float(height_offset_m or 0)
        self.session = session or requests.Session()
        self.data_dir = Path(data_dir)
        self.log = log or logger
        self._years: Dict[int, List[Event]] = {}
        self._retry_at: Dict[int, float] = {}

    # ── Files ───────────────────────────────────────────────────────────────

    def _path(self, year: int) -> Path:
        return self.data_dir / f"{self.gauge}-{year}.csv"

    def year(self, year: int, download: bool = True) -> List[Event]:
        """A year's readings: from memory, the data folder, or the portal."""
        if year in self._years:
            return self._years[year]
        path = self._path(year)
        if path.is_file():
            events = parse_hilo_csv(path.read_text(encoding="utf-8", errors="replace"))
            if events:
                self._years[year] = events
                return events
        if not download or time.time() < self._retry_at.get(year, 0):
            return []
        text = self._download(year)
        events = parse_hilo_csv(text) if text else []
        if not events:
            self._retry_at[year] = time.time() + RETRY_SECONDS
            return []
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            self.log.warning("Could not save %s: %s", path.name, e)
        self._years[year] = events
        self.log.info("Tides: %d readings for %s %d", len(events), self.gauge, year)
        return events

    def _download(self, year: int) -> Optional[str]:
        url = self._resource_url(year)
        if not url:
            return None
        try:
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except (requests.RequestException, OSError) as e:
            self.log.warning("Tides: download of %s %d failed: %s", self.gauge, year, e)
            return None

    def _resource_url(self, year: int) -> Optional[str]:
        """The CSV for ``year`` in the gauge's high/low dataset."""
        try:
            r = self.session.get(f"{CKAN}/package_show",
                                 params={"id": self.gauge + PACKAGE_SUFFIX}, timeout=20)
            r.raise_for_status()
            package = r.json().get("result") or {}
        except (requests.RequestException, OSError, ValueError) as e:
            self.log.warning("Tides: no dataset for gauge %r (%s)", self.gauge, e)
            return None
        for res in package.get("resources") or []:
            name = res.get("name") or ""
            url = res.get("url") or ""
            if re.match(rf"\s*{year}\b", name) and url.lower().endswith(".csv"):
                return url
        self.log.info("Tides: %s has no %d predictions published yet", self.gauge, year)
        return None

    # ── Readings for the site ───────────────────────────────────────────────

    def events_around(self, now: datetime, days_before: int = 1,
                      days_after: int = 2) -> List[Event]:
        """Corrected readings from ``days_before`` to ``days_after`` around now."""
        now_aest = now.astimezone(AEST)
        start = now_aest - timedelta(days=days_before)
        end = now_aest + timedelta(days=days_after)
        events: List[Event] = []
        for year in sorted({start.year, now_aest.year, end.year}):
            # Next year's file is only looked for once it is needed.
            events.extend(self.year(year))
        out = []
        for when, height, kind in events:
            when = when + self.time_offset
            if start <= when <= end:
                out.append((when, round(height + self.height_offset, 3), kind))
        return out
