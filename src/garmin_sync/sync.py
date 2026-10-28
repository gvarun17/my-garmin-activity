"""Sync orchestrator: Garmin -> raw landing -> InfluxDB, with watermark.

Flow per kind (daily, activities, sleep, hrv):
1. Work out the window: from watermark+1 day, or from --days ago,
   up to yesterday by default (today is incomplete in Garmin).
2. Fetch from Garmin, save the raw JSON for that window under
   data/raw/<kind>-<start>-<end>.json (atomic, overwriting the same
   window is idempotent).
3. Normalise to InfluxDB points, skipping unusable rows.
4. Write points, unless --dry-run.
5. Advance the watermark for that kind, unless --dry-run.

A kind that fails does not stop the other kinds, and does not
advance its watermark. The run exits non-zero if any kind failed,
so a scheduler can see it, but partial progress is kept.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from . import auth, models, state
from .client import GarminClient
from .influx import InfluxWriter

log = logging.getLogger(__name__)

KINDS = ("daily", "activities", "sleep", "hrv")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _raw_path(kind: str, start: date, end: date) -> Path:
    return Path("data/raw") / f"{kind}-{start.isoformat()}-{end.isoformat()}.json"


def _save_raw(kind: str, start: date, end: date, rows, raw_dir: Path | None = None) -> None:
    path = (Path(raw_dir) / f"{kind}-{start.isoformat()}-{end.isoformat()}.json") if raw_dir else _raw_path(kind, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    tmp.replace(path)


def _window(kind: str, sync_state: dict, days: int, start: date | None, end: date | None, include_today: bool):
    today = date.today()
    window_end = end or (today if include_today else today - timedelta(days=1))
    if start:
        window_start = start
    else:
        last = state.get_last_synced(sync_state, kind)
        window_start = (last + timedelta(days=1)) if last else (window_end - timedelta(days=days - 1))
    return window_start, window_end


def run_sync(days: int = 7, start: date | None = None, end: date | None = None,
             kinds=KINDS, include_today: bool = False, dry_run: bool = False,
             client=None, writer=None, sync_state_path: Path | None = None,
             raw_dir: Path | None = None):
    """Run a sync. Returns counts and failed kinds.

    client/writer are injectable for tests. When omitted, they are
    built from .env: GARMIN_API_KEY via auth, and INFLUXDB_URL,
    INFLUXDB_ORG, INFLUXDB_BUCKET, INFLUXDB_TOKEN.
    """
    auth.load_dotenv(os.environ.get("GARMIN_ENV_FILE"))
    sync_state = state.load_state(sync_state_path or state.DEFAULT_STATE_PATH)
    if client is None:
        token, _exp, _source = auth.get_access_token()
        client = GarminClient(token, delay=float(_env("GARMIN_REQUEST_DELAY_SECONDS", "0.8")))
    if writer is None and not dry_run:
        writer = InfluxWriter(
            url=_env("INFLUXDB_URL", "http://localhost:8086"),
            org=_env("INFLUXDB_ORG", "garmin"),
            bucket=_env("INFLUXDB_BUCKET", "fitness"),
            token=_env("INFLUXDB_TOKEN", ""),
        )
    tz_name = _env("GARMIN_TIMEZONE", "Asia/Kolkata")
    fetched = written = skipped = 0
    failed = []
    for kind in kinds:
        ws, we = _window(kind, sync_state, days, start, end, include_today)
        if we < ws:
            log.info("Kind %s: window empty (%s to %s), skipping.", kind, ws, we)
            continue
        try:
            if kind == "daily":
                rows = client.get_daily_summaries(ws, we)
                points, skipped_kind = models.rows_to_points(rows, models.daily_to_point)
            elif kind == "activities":
                rows = client.get_activities(ws, we)
                points, skipped_kind = models.rows_to_points(
                    rows, models.activity_to_point, tz_name=tz_name
                )
            elif kind == "sleep":
                rows = client.get_sleep(ws, we)
                points, skipped_kind = models.rows_to_points(rows, models.sleep_to_point)
            else:
                rows = client.get_hrv(ws, we)
                points, skipped_kind = models.rows_to_points(rows, models.hrv_to_point)
            _save_raw(kind, ws, we, rows, raw_dir=raw_dir)
            fetched += len(rows)
            skipped += skipped_kind
            if dry_run:
                log.info("Kind %s: dry run, %d rows -> %d points (not written).", kind, len(rows), len(points))
            else:
                written += writer.write(points)
                sync_state[kind] = we.isoformat()
                state.save_state(sync_state, sync_state_path or state.DEFAULT_STATE_PATH)
            log.info("Kind %s: %s to %s, %d rows, %d points, %d skipped.", kind, ws, we, len(rows), len(points), skipped_kind)
        except Exception as exc:
            log.exception("Kind %s failed: %s", kind, exc)
            failed.append(kind)
    return {"fetched": fetched, "written": written, "skipped": skipped, "failed": failed}
