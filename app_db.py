#!/usr/bin/env python3
"""SQLite-backed storage for coach app state."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(__file__).with_name("coach.db")


@dataclass
class CoachUser:
    id: int
    slug: str


class CoachDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fitbit_tokens (
                    user_id INTEGER NOT NULL UNIQUE,
                    fitbit_user_id TEXT,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS interaction_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    source TEXT,
                    topic TEXT,
                    date_context TEXT,
                    message TEXT NOT NULL,
                    reply TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS user_goals (
                    user_id INTEGER PRIMARY KEY,
                    step_goal INTEGER NOT NULL DEFAULT 5000,
                    zone_min_goal INTEGER NOT NULL DEFAULT 30,
                    weigh_in_required INTEGER NOT NULL DEFAULT 1,
                    shot_logging_required INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                """
            )

    def ensure_user(self, slug: str) -> CoachUser:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO users (slug, created_at) VALUES (?, ?)",
                (slug, now),
            )
            row = connection.execute(
                "SELECT id, slug FROM users WHERE slug = ?",
                (slug,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO user_goals (
                        user_id, step_goal, zone_min_goal, weigh_in_required, shot_logging_required, created_at, updated_at
                    ) VALUES (?, 5000, 30, 1, 1, ?, ?)
                    """,
                    (int(row["id"]), now, now),
                )
        if row is None:
            raise RuntimeError(f"Unable to ensure coach user {slug!r}.")
        return CoachUser(id=int(row["id"]), slug=str(row["slug"]))

    def get_fitbit_tokens(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM fitbit_tokens WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["payload_json"]))

    def save_fitbit_tokens(self, user_id: int, payload: dict[str, Any]) -> None:
        fitbit_user_id = payload.get("user_id")
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO fitbit_tokens (user_id, fitbit_user_id, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    fitbit_user_id = excluded.fitbit_user_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, fitbit_user_id, json.dumps(payload, sort_keys=True), updated_at),
            )

    def append_interaction(self, user_id: int, record: dict[str, Any]) -> None:
        timestamp = record.get("timestamp") or datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO interaction_history (
                    user_id, timestamp, source, topic, date_context, message, reply
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    timestamp,
                    record.get("source"),
                    record.get("topic"),
                    record.get("date_context"),
                    record.get("message", ""),
                    record.get("reply", ""),
                ),
            )

    def read_recent_interactions(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, source, topic, date_context, message, reply
                FROM interaction_history
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def interaction_count(self, user_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM interaction_history WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def get_user_goals(self, user_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT step_goal, zone_min_goal, weigh_in_required, shot_logging_required
                FROM user_goals
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"No goals found for user_id={user_id}.")
        return {
            "step_goal": int(row["step_goal"]),
            "zone_min_goal": int(row["zone_min_goal"]),
            "weigh_in_required": bool(row["weigh_in_required"]),
            "shot_logging_required": bool(row["shot_logging_required"]),
        }

    def migrate_legacy_token_file(self, user_id: int, token_path: Path) -> bool:
        if self.get_fitbit_tokens(user_id) is not None or not token_path.exists():
            return False
        payload = json.loads(token_path.read_text())
        self.save_fitbit_tokens(user_id, payload)
        return True

    def migrate_legacy_interaction_log(self, user_id: int, log_path: Path) -> int:
        if self.interaction_count(user_id) > 0 or not log_path.exists():
            return 0
        migrated = 0
        lines = log_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.append_interaction(user_id, record)
            migrated += 1
        return migrated
