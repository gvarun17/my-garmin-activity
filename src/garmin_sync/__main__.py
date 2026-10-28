"""CLI for the Garmin sync service.

Examples, from the repo root:
  PYTHONPATH=src python -m garmin_sync --days 7
  PYTHONPATH=src python -m garmin_sync --days 30 --dry-run
  PYTHONPATH=src python -m garmin_sync --start 2026-10-01 --end 2026-10-28 --kinds daily,sleep
  PYTHONPATH=src python -m garmin_sync --days 7 --include-today

Use a modern Python (3.12+) in a venv, not macOS system Python 3.9.
See README troubleshooting for the LibreSSL TLS failure on 3.9.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from .sync import KINDS, run_sync


def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sync Garmin Personal API data to InfluxDB.")
    parser.add_argument("--days", type=int, default=7, help="Days to sync when no watermark/start is given (default 7).")
    parser.add_argument("--start", type=_parse_date, help="Start date YYYY-MM-DD (overrides watermark/days).")
    parser.add_argument("--end", type=_parse_date, help="End date YYYY-MM-DD (default yesterday, or today with --include-today).")
    parser.add_argument("--kinds", default=",".join(KINDS), help="Comma list: daily,activities,sleep,hrv.")
    parser.add_argument("--include-today", action="store_true", help="Include today, which is usually incomplete in Garmin.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and normalise, but do not write to InfluxDB or advance state.")
    parser.add_argument("--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        parser.error(f"Unknown kinds: {', '.join(bad)}. Choose from: {', '.join(KINDS)}")
    result = run_sync(
        days=args.days, start=args.start, end=args.end, kinds=kinds,
        include_today=args.include_today, dry_run=args.dry_run,
    )
    print(f"Fetched: {result['fetched']}  Written: {result['written']}  Skipped rows: {result['skipped']}")
    if result["failed"]:
        print(f"Failed kinds: {', '.join(result['failed'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
