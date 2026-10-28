"""Tests for client chunking/splitting and model normalisation. Mocked."""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from garmin_sync import models
from garmin_sync.client import GarminClient, chunk_windows


class FakeResponse:
    def __init__(self, status_code, text, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class ClientTests(unittest.TestCase):
    def test_chunk_windows(self):
        windows = list(chunk_windows(date(2026, 10, 1), date(2026, 11, 5), 28))
        self.assertEqual(windows[0], (date(2026, 10, 1), date(2026, 10, 28)))
        self.assertEqual(windows[1], (date(2026, 10, 29), date(2026, 11, 5)))

    def test_204_is_empty(self):
        client = GarminClient("token", http_get=lambda *args, **kw: FakeResponse(204, ""), delay=0)
        self.assertEqual(client.get_daily_summaries(date(2026, 10, 1), date(2026, 10, 2)), [])

    def test_activities_split_on_500(self):
        calls = []

        def fake_get(url, headers, params, timeout):
            calls.append(params)
            # Fail the first 30-day window, succeed on single days.
            if params["startDate"] != params["endDate"]:
                return FakeResponse(500, "")
            return FakeResponse(200, '[{"activityId": 1, "startTimeLocal": "2026-10-02 06:00:00"}]')

        client = GarminClient("token", http_get=fake_get, delay=0)
        rows = client.get_activities(date(2026, 10, 1), date(2026, 10, 2))
        self.assertEqual(len(rows), 2)

    def test_429_retries_then_succeeds(self):
        attempts = {"n": 0}

        def fake_get(url, headers, params, timeout):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return FakeResponse(429, "", {"Retry-After": "0"})
            return FakeResponse(200, '[{"calendarDate": "2026-10-01", "steps": 100}]')

        client = GarminClient("token", http_get=fake_get, delay=0)
        rows = client.get_daily_summaries(date(2026, 10, 1), date(2026, 10, 1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(attempts["n"], 3)


class ModelTests(unittest.TestCase):
    def test_daily_point_skips_missing_fields_not_zero(self):
        point = models.daily_to_point({"calendarDate": "2026-10-01", "steps": 12345})
        self.assertEqual(point.fields["steps"], 12345)
        self.assertNotIn("resting_hr", point.fields)

    def test_sleep_row_without_date_skipped(self):
        points, skipped = models.rows_to_points(
            [{"calendarDate": None, "sleepTimeSeconds": 100}], models.sleep_to_point
        )
        self.assertEqual(points, [])
        self.assertEqual(skipped, 1)

    def test_sleep_score_from_map(self):
        point = models.sleep_to_point({
            "calendarDate": "2026-10-01",
            "sleepTimeSeconds": 27000,
            "sleepScores": {"overall": {"value": 81, "qualifierKey": "GOOD"}},
        })
        self.assertEqual(point.fields["score"], 81)
        self.assertEqual(point.tags["sleep_qualifier"], "GOOD")

    def test_hrv_point(self):
        point = models.hrv_to_point({
            "calendarDate": "2026-10-01",
            "lastNightAvg": 52,
            "weeklyAvg": 49,
            "status": "BALANCED",
            "baseline": {"balancedLow": 42, "balancedUpper": 59},
        })
        self.assertEqual(point.fields["last_night_avg"], 52)
        self.assertEqual(point.fields["baseline_low"], 42)

    def test_activity_id_is_field_not_tag(self):
        point = models.activity_to_point({
            "activityId": 999,
            "activityName": "Morning Walk",
            "startTimeLocal": "2026-10-01 06:00:00",
            "activityType": {"typeKey": "walking"},
            "distance": 5000,
        })
        self.assertEqual(point.fields["activity_id"], "999")
        self.assertNotIn("activity_id", point.tags)
        self.assertEqual(point.tags["activity_type"], "walking")


if __name__ == "__main__":
    unittest.main()
