"""Garmin Personal API client: fetch only, no storage here.

Responsibilities: authenticated GETs, chunking, activity-window
splitting, retries for 429/5xx, and treating 204 as empty. Parsing
into dashboard points lives in models.py. Writing to InfluxDB lives
in influx.py. Orchestration lives in sync.py.

All fields from Garmin are treated as optional. Rows without a
usable date are skipped, never allowed to fail a whole sync, because
community reports include at least one sleep row with a null
calendarDate in an otherwise normal window.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from . import endpoints
from .auth import auth_headers

log = logging.getLogger(__name__)


class GarminApiError(Exception):
    """Garmin API failure. Never includes a token or key."""


def chunk_windows(start: date, end: date, chunk_days: int = endpoints.CHUNK_DAYS):
    """Yield inclusive (window_start, window_end) date pairs."""
    if end < start:
        return
    cursor = start
    step = timedelta(days=chunk_days - 1)
    while cursor <= end:
        window_end = min(cursor + step, end)
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def _extract_list(payload):
    """Normalise Garmin's varied response shapes to a list of rows."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in (
            "activities",
            "dailySummaries",
            "hrvSummaries",
            "dailySleepSummaries",
            "sleepSummaries",
            "summaries",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return []


class GarminClient:
    def __init__(self, token: str, http_get=None, delay: float = endpoints.REQUEST_DELAY_SECONDS):
        self._token = token
        self._http_get = http_get or self._default_http_get
        self._delay = delay
        self._last_call = 0.0

    @staticmethod
    def _default_http_get(url, headers, params, timeout):
        import requests

        return requests.get(url, headers=headers, params=params, timeout=timeout)

    def _get(self, path: str, params: dict | None = None):
        """GET one URL with retries. Returns parsed JSON, or None for 204."""
        url = endpoints.BASE_URL + path
        headers = {**auth_headers(self._token), "Accept": "application/json"}
        attempts = 0
        while True:
            attempts += 1
            # Polite spacing, one request at a time.
            wait = self._delay - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self._http_get(url, headers=headers, params=params or {}, timeout=30)
            except Exception as exc:
                if attempts >= 3:
                    raise GarminApiError(f"Garmin request failed: {type(exc).__name__}") from exc
                time.sleep(2 ** attempts)
                continue
            self._last_call = time.monotonic()
            status = getattr(response, "status_code", 200)
            if status == 204:
                return None
            if status == 401:
                raise GarminApiError("Garmin returned 401: access token is invalid or expired.")
            if status == 404:
                raise GarminApiError(f"Garmin returned 404 for {path}. Check the endpoint in Swagger.")
            if status == 429 or status >= 500:
                retry_after = 0
                try:
                    retry_after = int(response.headers.get("Retry-After", "0"))
                except (ValueError, AttributeError):
                    retry_after = 0
                if attempts >= 3:
                    raise GarminApiError(f"Garmin returned HTTP {status} after retries for {path}.")
                time.sleep(max(retry_after, 2 ** attempts))
                continue
            if status != 200:
                raise GarminApiError(f"Garmin returned HTTP {status} for {path}.")
            body = response.text if hasattr(response, "text") else ""
            if not body:
                return None
            import json

            try:
                return json.loads(body)
            except ValueError as exc:
                raise GarminApiError(f"Garmin returned non-JSON for {path}.") from exc

    def _get_list(self, path: str, params: dict | None = None):
        return _extract_list(self._get(path, params))

    def get_daily_summaries(self, start: date, end: date):
        rows = []
        for ws, we in chunk_windows(start, end):
            rows.extend(
                self._get_list(
                    endpoints.DAILY_SUMMARIES_PATH,
                    {"startDate": ws.isoformat(), "endDate": we.isoformat()},
                )
            )
        return rows

    def get_sleep(self, start: date, end: date):
        rows = []
        for ws, we in chunk_windows(start, end):
            rows.extend(
                self._get_list(
                    endpoints.SLEEP_PATH,
                    {"startDate": ws.isoformat(), "endDate": we.isoformat()},
                )
            )
        return rows

    def get_hrv(self, start: date, end: date):
        rows = []
        for ws, we in chunk_windows(start, end):
            path = endpoints.HRV_PATH.format(start=ws.isoformat(), end=we.isoformat())
            rows.extend(self._get_list(path))
        return rows

    def _search_activities_window(self, start: date, end: date):
        """One activity window, paged. Raises GarminApiError on 500."""
        rows = []
        offset = 0
        while True:
            page = self._get_list(
                endpoints.ACTIVITIES_PATH,
                {
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "start": offset,
                    "limit": endpoints.ACTIVITY_PAGE_SIZE,
                },
            )
            rows.extend(page)
            if len(page) < endpoints.ACTIVITY_PAGE_SIZE:
                break
            offset += len(page)
        return rows

    def get_activities(self, start: date, end: date):
        """Activities, splitting windows on 500 down to a single day."""
        rows = []
        for ws, we in chunk_windows(start, end, endpoints.ACTIVITY_WINDOW_DAYS):
            rows.extend(self._activities_recursive(ws, we))
        return rows

    def _activities_recursive(self, start: date, end: date):
        try:
            return self._search_activities_window(start, end)
        except GarminApiError as exc:
            if "500" not in str(exc) or start >= end:
                raise
            mid = start + (end - start) // 2
            return self._activities_recursive(start, mid) + self._activities_recursive(
                mid + timedelta(days=1), end
            )
