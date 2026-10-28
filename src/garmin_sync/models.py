"""Normalise Garmin rows into InfluxDB points.

Design rules, all deliberate:
- Time is stored in UTC. Daily rows use midnight UTC on their
  calendar date. Activities use startTimeLocal interpreted in
  GARMIN_TIMEZONE (default Asia/Kolkata), then converted to UTC,
  because Garmin returns startTimeLocal without a timezone.
- Tags are low-cardinality only: source, activity type, sleep
  qualifier, HRV status. Activity id and name are fields, not tags,
  so tag cardinality does not grow with every workout.
- Every field is optional. A missing field is omitted from the
  point, never turned into zero, because zero steps or zero sleep
  would be a lie.
- Rows without a usable date are skipped and counted by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


@dataclass
class Point:
    measurement: str
    time: datetime
    tags: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nested(row: dict, *keys):
    current = row
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first(row: dict, *keys):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _parse_date(value):
    if value is None:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _midnight_utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _parse_local_datetime(value, tz_name: str):
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            naive = datetime.strptime(text[:19], fmt)
            break
        except ValueError:
            continue
    else:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def daily_to_point(row: dict) -> Point | None:
    day = _parse_date(_first(row, "calendarDate", "date", "day"))
    if day is None:
        return None
    fields = {}
    for name, value in {
        "steps": _first(row, "steps", "totalSteps"),
        "distance_m": _first(row, "distance", "totalDistance"),
        "calories": _first(row, "calories", "totalCalories", "activeKilocalories"),
        "resting_hr": _nested(row, "heartRate", "restingValue"),
        "min_hr": _nested(row, "heartRate", "minValue"),
        "max_hr": _nested(row, "heartRate", "maxValue"),
        "stress_avg": _nested(row, "stress", "avgLevel"),
        "body_battery_end": _nested(row, "bodyBattery", "endOfDayValue"),
        "body_battery_latest": _nested(row, "bodyBattery", "latestValue"),
        "body_battery_charged": _nested(row, "bodyBattery", "chargedValue"),
        "body_battery_drained": _nested(row, "bodyBattery", "drainedValue"),
    }.items():
        num = _num(value)
        if num is not None:
            fields[name] = num
    return Point("daily_summary", _midnight_utc(day), {"source": "garmin"}, fields)


def activity_to_point(row: dict, tz_name: str = "Asia/Kolkata") -> Point | None:
    start = _parse_local_datetime(
        _first(row, "startTimeLocal", "startTime"), tz_name
    )
    if start is None:
        # Fall back to calendar date so the activity is not lost.
        day = _parse_date(_first(row, "calendarDate", "date"))
        if day is None:
            return None
        start = _midnight_utc(day)
    activity_type = _nested(row, "activityType", "typeKey") or _first(
        row, "activityType"
    )
    if isinstance(activity_type, dict):
        activity_type = activity_type.get("typeKey")
    fields = {}
    for name, value in {
        "distance_m": _first(row, "distance"),
        "duration_s": _first(row, "duration"),
        "calories": _first(row, "calories"),
        "avg_hr": _first(row, "averageHR"),
        "max_hr": _first(row, "maxHR"),
    }.items():
        num = _num(value)
        if num is not None:
            fields[name] = num
    for name, value in {
        "activity_id": str(_first(row, "activityId", "id")) if _first(row, "activityId", "id") is not None else None,
        "activity_name": _first(row, "activityName", "name"),
    }.items():
        if value is not None:
            fields[name] = str(value)
    tags = {"source": "garmin"}
    if activity_type:
        tags["activity_type"] = str(activity_type)
    return Point("activity", start, tags, fields)


def sleep_to_point(row: dict) -> Point | None:
    day = _parse_date(_first(row, "calendarDate", "date", "day"))
    if day is None:
        return None
    scores = row.get("sleepScores") or {}
    overall = scores.get("overall") or {} if isinstance(scores, dict) else {}
    fields = {}
    for name, value in {
        "duration_s": _first(row, "sleepTimeSeconds", "sleepDuration"),
        "score": overall.get("value") if isinstance(overall, dict) else None,
        "deep_s": _first(row, "deepSleepSeconds"),
        "light_s": _first(row, "lightSleepSeconds"),
        "rem_s": _first(row, "remSleepSeconds"),
        "awake_count": _first(row, "awakeCount"),
    }.items():
        num = _num(value)
        if num is not None:
            fields[name] = num
    # Component percentages, when Garmin returns them in sleepScores.
    for name, key in {
        "deep_pct": "deepPercentage",
        "light_pct": "lightPercentage",
        "rem_pct": "remPercentage",
    }.items():
        if isinstance(scores, dict) and scores.get(key) is not None:
            num = _num(scores.get(key))
            if num is not None:
                fields[name] = num
    tags = {"source": "garmin"}
    qualifier = overall.get("qualifierKey") if isinstance(overall, dict) else None
    if qualifier:
        tags["sleep_qualifier"] = str(qualifier)
    return Point("sleep", _midnight_utc(day), tags, fields)


def hrv_to_point(row: dict) -> Point | None:
    # HRV rows may be nested under hrvSummaries, or be a summary itself.
    summary = row
    if isinstance(row.get("hrvSummaries"), list) and row["hrvSummaries"]:
        summary = row["hrvSummaries"][0]
    day = _parse_date(
        _first(summary, "calendarDate", "date", "day")
        or _first(row, "calendarDate", "date")
    )
    if day is None:
        return None
    baseline = summary.get("baseline") or {}
    fields = {}
    for name, value in {
        "last_night_avg": _first(summary, "lastNightAvg"),
        "weekly_avg": _first(summary, "weeklyAvg"),
        "baseline_low": baseline.get("balancedLow") if isinstance(baseline, dict) else None,
        "baseline_upper": baseline.get("balancedUpper") if isinstance(baseline, dict) else None,
    }.items():
        num = _num(value)
        if num is not None:
            fields[name] = num
    tags = {"source": "garmin"}
    # Status is Garmin's reading of weeklyAvg against baseline, not
    # last night alone. Keep it as a tag, with that meaning in mind.
    status = _first(summary, "status")
    if status:
        tags["hrv_status"] = str(status)
    return Point("hrv", _midnight_utc(day), tags, fields)


def rows_to_points(rows, converter, **kwargs):
    """Convert rows, skipping unusable ones. Returns (points, skipped)."""
    points, skipped = [], 0
    for row in rows or []:
        if not isinstance(row, dict):
            skipped += 1
            continue
        point = converter(row, **kwargs) if kwargs else converter(row)
        if point is None or not point.fields:
            skipped += 1
            continue
        points.append(point)
    return points, skipped
