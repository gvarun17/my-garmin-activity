"""InfluxDB 2.7 writer, via its HTTP write API (no client library).

Line protocol rules applied here:
- measurement, tag keys/values and field keys escape commas, equals
  and spaces; string field values escape quotes and backslashes
- int fields get an ``i`` suffix, floats are plain, bools are t/f
- timestamps are seconds since epoch, matching precision=s
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from .models import Point

log = logging.getLogger(__name__)


def _escape_key(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _escape_tag(value: str) -> str:
    return _escape_key(value)


def _escape_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def point_to_line(point: Point) -> str:
    parts = [_escape_key(point.measurement)]
    for key in sorted(point.tags):
        parts[0] += f",{_escape_key(key)}={_escape_tag(point.tags[key])}"
    field_parts = []
    for key in sorted(point.fields):
        value = point.fields[key]
        if isinstance(value, bool):
            field_parts.append(f"{_escape_key(key)}={'t' if value else 'f'}")
        elif isinstance(value, int):
            field_parts.append(f"{_escape_key(key)}={value}i")
        elif isinstance(value, float):
            field_parts.append(f"{_escape_key(key)}={value}")
        else:
            field_parts.append(f'{_escape_key(key)}="{_escape_string(value)}"')
    if not field_parts:
        raise ValueError("Point has no fields, cannot write to InfluxDB.")
    timestamp = int(point.time.timestamp())
    return f"{parts[0]} {','.join(field_parts)} {timestamp}"


def points_to_lines(points) -> str:
    return "\n".join(point_to_line(p) for p in points)


class InfluxWriter:
    def __init__(self, url: str, org: str, bucket: str, token: str, http_post=None):
        self.url = url.rstrip("/")
        self.org = org
        self.bucket = bucket
        self.token = token
        self._http_post = http_post or self._default_http_post

    @staticmethod
    def _default_http_post(url, headers, data, timeout):
        import requests

        return requests.post(url, headers=headers, data=data, timeout=timeout)

    def write(self, points) -> int:
        points = list(points)
        if not points:
            return 0
        body = points_to_lines(points)
        url = (
            f"{self.url}/api/v2/write?org={quote(self.org)}"
            f"&bucket={quote(self.bucket)}&precision=s"
        )
        response = self._http_post(
            url,
            headers={"Authorization": f"Token {self.token}", "Content-Type": "text/plain"},
            data=body,
            timeout=30,
        )
        status = getattr(response, "status_code", 204)
        if status not in (200, 204):
            raise RuntimeError(f"InfluxDB write failed with HTTP {status}.")
        log.info("Wrote %d points to InfluxDB bucket %s.", len(points), self.bucket)
        return len(points)
