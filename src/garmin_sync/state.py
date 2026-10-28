"""Sync watermark state: one last-synced date per data kind.

Stored in data/sync-state.json (gitignored). The state is updated
only after a kind's points have been written successfully, so a
failed kind is retried from its old watermark on the next run.
A dry run never touches state.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

DEFAULT_STATE_PATH = Path("data/sync-state.json")


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def get_last_synced(state: dict, kind: str):
    value = state.get(kind)
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def save_state(state: dict, path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
