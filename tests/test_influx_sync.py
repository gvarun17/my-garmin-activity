"""Tests for InfluxDB line protocol and sync watermark behaviour."""

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from garmin_sync import models
from garmin_sync.influx import InfluxWriter, point_to_line
from garmin_sync.sync import run_sync


class FakeClient:
    def get_daily_summaries(self, start, end):
        return [{"calendarDate": "2026-10-01", "steps": 1000}]

    def get_activities(self, start, end):
        return []

    def get_sleep(self, start, end):
        return []

    def get_hrv(self, start, end):
        return []


class FakeWriter:
    def __init__(self):
        self.written = []

    def write(self, points):
        self.written.extend(points)
        return len(points)


class InfluxTests(unittest.TestCase):
    def test_line_protocol_escaping_and_types(self):
        point = models.Point(
            "daily_summary",
            datetime(2026, 10, 1, tzinfo=timezone.utc),
            {"source": "garmin"},
            {"steps": 1000, "activity_name": 'Morning "Walk"', "ratio": 1.5},
        )
        line = point_to_line(point)
        self.assertIn("steps=1000i", line)
        self.assertIn('activity_name="Morning \\"Walk\\""', line)
        self.assertIn("ratio=1.5", line)


class SyncTests(unittest.TestCase):
    def test_dry_run_does_not_write_or_advance_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            writer = FakeWriter()
            result = run_sync(
                days=1, start=date(2026, 10, 1), end=date(2026, 10, 1),
                kinds=("daily",), dry_run=True, client=FakeClient(), writer=writer,
                sync_state_path=state_path, raw_dir=Path(tmp) / "raw",
            )
            self.assertEqual(writer.written, [])
            self.assertFalse(state_path.is_file())
            self.assertEqual(result["written"], 0)

    def test_success_advances_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            writer = FakeWriter()
            result = run_sync(
                days=1, start=date(2026, 10, 1), end=date(2026, 10, 1),
                kinds=("daily",), client=FakeClient(), writer=writer,
                sync_state_path=state_path, raw_dir=Path(tmp) / "raw",
            )
            self.assertEqual(len(writer.written), 1)
            self.assertEqual(state_path.read_text(), '{\n  "daily": "2026-10-01"\n}')


if __name__ == "__main__":
    unittest.main()
