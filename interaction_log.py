#!/usr/bin/env python3
"""Simple JSONL interaction logging for the coach app."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
DEFAULT_LOG_PATH = Path(__file__).with_name("coach_interactions.jsonl")


def append_interaction(path: Path, record: dict[str, Any]) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **record,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def read_recent_interactions(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with _LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
    recent = lines[-limit:]
    records: list[dict[str, Any]] = []
    for line in recent:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
