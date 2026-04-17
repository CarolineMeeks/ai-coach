#!/usr/bin/env python3
"""Minimal Fitbit API client for personal coaching workflows."""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests

from app_db import DEFAULT_DB_PATH, CoachDB, CoachUser


APP_TIMEZONE = "America/New_York"
if hasattr(time, "tzset"):
    os.environ["TZ"] = os.getenv("COACH_TIMEZONE", APP_TIMEZONE)
    time.tzset()

AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
TOKEN_URL = "https://api.fitbit.com/oauth2/token"
API_BASE_URL = "https://api.fitbit.com"
DEFAULT_SCOPES = [
    "activity",
    "heartrate",
    "sleep",
    "profile",
    "weight",
]


class FitbitConfigError(RuntimeError):
    """Raised when required environment variables or tokens are missing."""


@dataclass
class FitbitConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    database_path: Path
    user_slug: str
    token_path: Path
    zepbound_sheet_url: str | None = None
    interaction_log_path: Path = Path("coach_interactions.jsonl")
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.4-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    sms_to_number: str | None = None
    scheduler_enabled: bool = False
    scheduler_poll_seconds: int = 60

    @classmethod
    def from_env(cls) -> "FitbitConfig":
        client_id = os.getenv("FITBIT_CLIENT_ID", "").strip()
        client_secret = os.getenv("FITBIT_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("FITBIT_REDIRECT_URI", "http://127.0.0.1:8765/callback").strip()
        database_path = Path(os.getenv("COACH_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()
        user_slug = os.getenv("COACH_USER_SLUG", "default").strip() or "default"
        token_path = Path(os.getenv("FITBIT_TOKEN_PATH", ".fitbit_tokens.json")).expanduser()
        zepbound_sheet_url = os.getenv("ZEPBOUND_SHEET_URL", "").strip() or None
        interaction_log_path = Path(
            os.getenv("COACH_INTERACTION_LOG_PATH", "coach_interactions.jsonl")
        ).expanduser()
        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip() or None
        openai_model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"
        openai_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip() or "https://api.openai.com/v1"
        twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip() or None
        twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip() or None
        twilio_from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip() or None
        sms_to_number = os.getenv("SMS_TO_NUMBER", "").strip() or None
        scheduler_enabled = os.getenv("COACH_ENABLE_SCHEDULER", "").strip().lower() in {"1", "true", "yes", "on"}
        scheduler_poll_seconds = int(os.getenv("COACH_SCHEDULER_POLL_SECONDS", "60").strip() or "60")

        missing = [
            name
            for name, value in {
                "FITBIT_CLIENT_ID": client_id,
                "FITBIT_CLIENT_SECRET": client_secret,
            }.items()
            if not value
        ]
        if missing:
            raise FitbitConfigError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            database_path=database_path,
            user_slug=user_slug,
            token_path=token_path,
            zepbound_sheet_url=zepbound_sheet_url,
            interaction_log_path=interaction_log_path,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            openai_base_url=openai_base_url,
            twilio_account_sid=twilio_account_sid,
            twilio_auth_token=twilio_auth_token,
            twilio_from_number=twilio_from_number,
            sms_to_number=sms_to_number,
            scheduler_enabled=scheduler_enabled,
            scheduler_poll_seconds=scheduler_poll_seconds,
        )


class TokenStore:
    def __init__(self, db: CoachDB, user: CoachUser, legacy_path: Path) -> None:
        self.db = db
        self.user = user
        self.legacy_path = legacy_path

    def load(self) -> dict[str, Any]:
        payload = self.db.get_fitbit_tokens(self.user.id)
        if payload is None:
            raise FitbitConfigError("Fitbit tokens not found in the app database. Run the auth command first.")
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        self.db.save_fitbit_tokens(self.user.id, payload)


class FitbitClient:
    def __init__(self, config: FitbitConfig) -> None:
        self.config = config
        self.db = CoachDB(config.database_path)
        self.user = self.db.ensure_user(config.user_slug)
        self.db.migrate_legacy_token_file(self.user.id, config.token_path)
        self.db.migrate_legacy_interaction_log(self.user.id, config.interaction_log_path)
        self.tokens = TokenStore(self.db, self.user, config.token_path)
        self._access_token_payload: dict[str, Any] | None = None
        self._response_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
        self._cache_events: list[dict[str, str]] = []

    def _basic_auth_header(self) -> str:
        raw = f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
        encoded = base64.b64encode(raw).decode("ascii")
        return f"Basic {encoded}"

    def build_auth_url(self, state: str, scopes: list[str] | None = None) -> str:
        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "scope": " ".join(scopes or DEFAULT_SCOPES),
            "redirect_uri": self.config.redirect_uri,
            "state": state,
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        response = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "client_id": self.config.client_id,
                "grant_type": "authorization_code",
                "redirect_uri": self.config.redirect_uri,
                "code": code,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        payload["saved_at"] = int(time.time())
        self.tokens.save(payload)
        return payload

    def refresh_access_token(self) -> dict[str, Any]:
        payload = self.tokens.load()
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise FitbitConfigError("Stored token payload is missing a refresh_token.")

        response = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        response.raise_for_status()
        fresh_payload = response.json()
        fresh_payload["saved_at"] = int(time.time())
        self.tokens.save(fresh_payload)
        self._access_token_payload = fresh_payload
        return fresh_payload

    def access_token(self) -> str:
        payload = self._access_token_payload or self.tokens.load()
        saved_at = int(payload.get("saved_at", 0) or 0)
        expires_in = int(payload.get("expires_in", 0) or 0)
        now = int(time.time())
        refresh_needed = not payload.get("access_token")
        if expires_in and saved_at:
            refresh_needed = refresh_needed or now >= (saved_at + expires_in - 60)
        if refresh_needed:
            payload = self.refresh_access_token()
        else:
            self._access_token_payload = payload
        token = payload.get("access_token")
        if not token:
            raise FitbitConfigError("No access_token returned by Fitbit.")
        return token

    def _cache_key(self, kind: str, target: str, params: dict[str, Any] | None = None) -> tuple[str, str, str]:
        serialized = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
        return (kind, target, serialized)

    def _get_cached(self, key: tuple[str, str, str], allow_stale: bool = False) -> Any | None:
        cached = self._response_cache.get(key)
        if not cached:
            return None
        expires_at, payload = cached
        if not allow_stale and time.time() >= expires_at:
            return None
        return deepcopy(payload)

    def reset_cache_events(self) -> None:
        self._cache_events = []

    def consume_cache_events(self) -> list[dict[str, str]]:
        events = self._cache_events[:]
        self._cache_events = []
        return events

    def _set_cached(self, key: tuple[str, str, str], payload: Any, ttl_seconds: int) -> Any:
        self._response_cache[key] = (time.time() + ttl_seconds, deepcopy(payload))
        return deepcopy(payload)

    def get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        key = self._cache_key("json", endpoint, params)
        cached = self._get_cached(key)
        if cached is not None:
            self._cache_events.append({"kind": "cache", "target": endpoint})
            return cached
        token = self.access_token()
        try:
            response = requests.get(
                f"{API_BASE_URL}{endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=30,
            )
            response.raise_for_status()
        except requests.HTTPError:
            if response.status_code == 429:
                stale = self._get_cached(key, allow_stale=True)
                if stale is not None:
                    self._cache_events.append({"kind": "stale", "target": endpoint})
                    return stale
            raise
        payload = response.json()
        return self._set_cached(key, payload, ttl_seconds=300)

    def get_text(self, url: str) -> str:
        key = self._cache_key("text", url)
        cached = self._get_cached(key)
        if cached is not None:
            self._cache_events.append({"kind": "cache", "target": url})
            return cached
        try:
            response = requests.get(
                url,
                timeout=30,
            )
            response.raise_for_status()
        except requests.HTTPError:
            if response.status_code == 429:
                stale = self._get_cached(key, allow_stale=True)
                if stale is not None:
                    self._cache_events.append({"kind": "stale", "target": url})
                    return stale
            raise
        return self._set_cached(key, response.text, ttl_seconds=900)

    def openai_response(self, system_prompt: str, user_prompt: str) -> str:
        if not self.config.openai_api_key:
            raise FitbitConfigError("Missing OPENAI_API_KEY in the environment.")

        response = requests.post(
            f"{self.config.openai_base_url.rstrip('/')}/responses",
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.openai_model,
                "reasoning": {"effort": "low"},
                "input": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": system_prompt}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_prompt}],
                    },
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("output_text"):
            return str(payload["output_text"]).strip()

        outputs = payload.get("output", [])
        for item in outputs:
            for content in item.get("content", []):
                text = content.get("text")
                if text:
                    return str(text).strip()
        raise FitbitConfigError("OpenAI response did not include output text.")

    def append_interaction(self, record: dict[str, Any]) -> None:
        self.db.append_interaction(self.user.id, record)

    def read_recent_interactions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.read_recent_interactions(self.user.id, limit=limit)

    def get_user_goals(self) -> dict[str, Any]:
        return self.db.get_user_goals(self.user.id)

    def add_water_intake(self, log_date: str, amount_oz: float, source: str = "manual", note: str | None = None) -> None:
        self.db.add_water_intake(self.user.id, log_date, amount_oz, source=source, note=note)

    def get_water_intake_logs(self, log_date: str) -> list[dict[str, Any]]:
        return self.db.get_water_intake_logs(self.user.id, log_date)

    def get_water_total(self, log_date: str) -> float:
        return self.db.get_water_total(self.user.id, log_date)

    def send_sms(self, body: str, to_number: str | None = None) -> dict[str, Any]:
        if not (self.config.twilio_account_sid and self.config.twilio_auth_token and self.config.twilio_from_number):
            raise FitbitConfigError("Twilio is not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM_NUMBER.")
        destination = to_number or self.config.sms_to_number
        if not destination:
            raise FitbitConfigError("Missing destination phone number. Set SMS_TO_NUMBER or provide a to_number.")

        response = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{self.config.twilio_account_sid}/Messages.json",
            auth=(self.config.twilio_account_sid, self.config.twilio_auth_token),
            data={
                "From": self.config.twilio_from_number,
                "To": destination,
                "Body": body,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def reminder_already_run(self, reminder_key: str, run_date: str) -> bool:
        return self.db.reminder_already_run(self.user.id, reminder_key, run_date)

    def record_reminder_run(
        self,
        reminder_key: str,
        run_date: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.db.record_reminder_run(self.user.id, reminder_key, run_date, status, payload)

    def add_workout_log(
        self,
        workout_date: str,
        workout_name: str,
        workout_category: str | None = None,
        source: str = "manual",
        note: str | None = None,
    ) -> None:
        self.db.add_workout_log(
            self.user.id,
            workout_date,
            workout_name,
            workout_category=workout_category,
            source=source,
            note=note,
        )

    def get_recent_workouts(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.get_recent_workouts(self.user.id, limit=limit)


def get_day_snapshot(client: FitbitClient, target_date: str) -> dict[str, Any]:
    activity = client.get_json(f"/1/user/-/activities/date/{target_date}.json")
    sleep = client.get_json(f"/1.2/user/-/sleep/date/{target_date}.json")
    profile = client.get_json("/1/user/-/profile.json")
    active_zone = client.get_json(f"/1/user/-/activities/active-zone-minutes/date/{target_date}/1d.json")
    try:
        # Inference from Fitbit's web API patterns: some accounts expose daily HRV here.
        hrv = client.get_json(f"/1/user/-/hrv/date/{target_date}.json")
    except Exception:  # noqa: BLE001
        hrv = {}
    return {
        "date": target_date,
        "daily_activity": activity,
        "active_zone_minutes": active_zone,
        "sleep": sleep,
        "profile": profile,
        "hrv": hrv,
    }


def summarize_day(snapshot: dict[str, Any]) -> dict[str, Any]:
    summary = snapshot.get("daily_activity", {}).get("summary", {})
    goals = snapshot.get("daily_activity", {}).get("goals", {})
    sleep_entries = snapshot.get("sleep", {}).get("sleep", [])
    main_sleep = next((entry for entry in sleep_entries if entry.get("isMainSleep")), None)
    profile_user = snapshot.get("profile", {}).get("user", {})

    minutes_asleep = 0
    time_in_bed = 0
    sleep_efficiency = None
    if main_sleep:
        minutes_asleep = int(main_sleep.get("minutesAsleep", 0))
        time_in_bed = int(main_sleep.get("timeInBed", 0))
        sleep_efficiency = main_sleep.get("efficiency")
        if not minutes_asleep and main_sleep.get("duration"):
            time_in_bed = int(main_sleep["duration"] / 60000)

    steps = int(summary.get("steps", 0))
    step_goal = int(goals.get("steps", 0) or 0)
    active_minutes = int(summary.get("fairlyActiveMinutes", 0)) + int(summary.get("veryActiveMinutes", 0))
    light_minutes = int(summary.get("lightlyActiveMinutes", 0))
    sedentary_minutes = int(summary.get("sedentaryMinutes", 0))
    resting_hr = summary.get("restingHeartRate")
    hrv_items = snapshot.get("hrv", {}).get("hrv", [])
    hrv_entry = hrv_items[0].get("value", {}) if hrv_items else {}
    hrv_daily_rmssd = hrv_entry.get("dailyRmssd")
    hrv_deep_rmssd = hrv_entry.get("deepRmssd")
    azm_items = snapshot.get("active_zone_minutes", {}).get("activities-active-zone-minutes", [])
    azm_entry = azm_items[0].get("value", {}) if azm_items else {}
    zone_minutes = int(azm_entry.get("activeZoneMinutes", 0) or 0)
    fat_burn_zone_minutes = int(azm_entry.get("fatBurnActiveZoneMinutes", 0) or 0)
    cardio_zone_minutes = int(azm_entry.get("cardioActiveZoneMinutes", 0) or 0)
    peak_zone_minutes = int(azm_entry.get("peakActiveZoneMinutes", 0) or 0)

    return {
        "date": snapshot.get("date"),
        "steps": steps,
        "step_goal": step_goal,
        "step_goal_pct": round((steps / step_goal) * 100, 1) if step_goal else None,
        "active_minutes": active_minutes,
        "light_minutes": light_minutes,
        "movement_minutes": light_minutes + active_minutes,
        "sedentary_minutes": sedentary_minutes,
        "resting_hr": resting_hr,
        "hrv_daily_rmssd": round_or_none(hrv_daily_rmssd, 1) if hrv_daily_rmssd is not None else None,
        "hrv_deep_rmssd": round_or_none(hrv_deep_rmssd, 1) if hrv_deep_rmssd is not None else None,
        "zone_minutes": zone_minutes,
        "fat_burn_zone_minutes": fat_burn_zone_minutes,
        "cardio_zone_minutes": cardio_zone_minutes,
        "peak_zone_minutes": peak_zone_minutes,
        "sleep_minutes": minutes_asleep,
        "time_in_bed_minutes": time_in_bed,
        "sleep_efficiency": sleep_efficiency,
        "weight": profile_user.get("weight"),
        "age": profile_user.get("age"),
    }


def build_recovery_baseline(client: FitbitClient, end_date: str, days: int = 7) -> dict[str, Any]:
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    snapshots = []
    for offset in range(days - 1, -1, -1):
        current = end - timedelta(days=offset)
        try:
            snapshots.append(summarize_day(get_day_snapshot(client, current.isoformat())))
        except Exception:  # noqa: BLE001
            continue

    resting_hrs = [item["resting_hr"] for item in snapshots if item.get("resting_hr") is not None]
    hrv_values = [item["hrv_daily_rmssd"] for item in snapshots if item.get("hrv_daily_rmssd") is not None]

    avg_rhr = round(sum(resting_hrs) / len(resting_hrs), 1) if resting_hrs else None
    avg_hrv = round(sum(hrv_values) / len(hrv_values), 1) if hrv_values else None
    latest = snapshots[-1] if snapshots else None

    resting_hr_elevated = bool(
        latest
        and avg_rhr is not None
        and latest.get("resting_hr") is not None
        and latest["resting_hr"] >= avg_rhr + 3
    )
    hrv_suppressed = bool(
        latest
        and avg_hrv is not None
        and latest.get("hrv_daily_rmssd") is not None
        and latest["hrv_daily_rmssd"] <= avg_hrv * 0.85
    )

    return {
        "days": days,
        "avg_resting_hr": avg_rhr,
        "avg_hrv_daily_rmssd": avg_hrv,
        "resting_hr_elevated": resting_hr_elevated,
        "hrv_suppressed": hrv_suppressed,
        "pair_flag": resting_hr_elevated and hrv_suppressed,
    }


def format_minutes(total_minutes: int | None) -> str:
    if not total_minutes:
        return "0m"
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def calculate_trend(values: list[float], alpha: float = 0.1) -> list[float]:
    if not values:
        return []
    trend = [values[0]]
    for value in values[1:]:
        trend.append(trend[-1] + alpha * (value - trend[-1]))
    return trend


def round_or_none(value: float | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_sheet_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%a, %b %d, %Y").date()


def get_profile_time_context(client: FitbitClient) -> dict[str, Any]:
    profile = client.get_json("/1/user/-/profile.json")
    user = profile.get("user", {})
    timezone_name = user.get("timezone") or os.getenv("COACH_TIMEZONE", APP_TIMEZONE)

    offset_millis = (
        user.get("offsetFromUTCMillis")
        or user.get("offsetFromUTCMillis")
        or user.get("offsetFromUtcMillis")
    )
    if offset_millis is None and user.get("offsetFromUTC") is not None:
        try:
            offset_millis = int(float(user.get("offsetFromUTC")) * 3600000)
        except (TypeError, ValueError):
            offset_millis = None

    offset_minutes = 0
    if offset_millis is not None:
        try:
            offset_minutes = int(int(offset_millis) / 60000)
        except (TypeError, ValueError):
            offset_minutes = 0

    return {
        "timezone": timezone_name,
        "offset_minutes": offset_minutes,
    }


def current_date_for_client(client: FitbitClient, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    try:
        time_context = get_profile_time_context(client)
    except Exception:  # noqa: BLE001
        return date.today().isoformat()

    offset = timedelta(minutes=int(time_context.get("offset_minutes", 0) or 0))
    return (current + offset).date().isoformat()


def build_google_csv_url(sheet_url: str) -> str:
    if "/export?" in sheet_url and "format=csv" in sheet_url:
        return sheet_url
    if "/edit" in sheet_url:
        base, _, query = sheet_url.partition("/edit")
        gid = "0"
        if "gid=" in query:
            gid = query.split("gid=")[-1].split("&")[0].split("#")[0]
        elif "#gid=" in sheet_url:
            gid = sheet_url.split("#gid=")[-1].split("&")[0]
        return f"{base}/export?format=csv&gid={gid}"
    raise FitbitConfigError("ZEPBOUND_SHEET_URL must be a Google Sheets edit or CSV export URL.")


def parse_water_oz(text: str) -> float | None:
    lowered = text.lower().strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:oz|ounces?)\b", lowered)
    if match:
        return round(float(match.group(1)), 1)
    if re.search(r"\b(?:water|hydration)\s+(?:at|is)\s+(\d+(?:\.\d+)?)\b", lowered):
        return round(float(re.search(r"\b(?:water|hydration)\s+(?:at|is)\s+(\d+(?:\.\d+)?)\b", lowered).group(1)), 1)
    if re.search(r"\bat\s+(\d+(?:\.\d+)?)\b", lowered) and ("water" in lowered or "hydration" in lowered):
        return round(float(re.search(r"\bat\s+(\d+(?:\.\d+)?)\b", lowered).group(1)), 1)
    if lowered.isdigit():
        return round(float(lowered), 1)
    return None


def parse_relative_date(text: str, target_date: str) -> str:
    base = datetime.strptime(target_date, "%Y-%m-%d").date()
    lowered = text.lower()
    if "yesterday" in lowered:
        return (base - timedelta(days=1)).isoformat()
    if "tomorrow" in lowered:
        return (base + timedelta(days=1)).isoformat()
    return target_date


def categorize_workout(name: str) -> str:
    lowered = name.lower()
    if "strength" in lowered or "lift" in lowered or "weights" in lowered:
        return "strength"
    if "dance" in lowered:
        return "dance"
    if "bike" in lowered or "cycling" in lowered:
        return "bike"
    if "hike" in lowered or "walk" in lowered:
        return "walk"
    if "sail" in lowered:
        return "sailing"
    return "other"


def parse_workout_log(text: str, target_date: str) -> dict[str, str] | None:
    lowered = text.lower()
    if "i did" not in lowered and "i took" not in lowered and "i went to" not in lowered:
        return None
    if "workout" not in lowered and "class" not in lowered and "strength" not in lowered and "dance" not in lowered:
        return None

    workout_date = parse_relative_date(text, target_date)
    phrase = text.strip().split(".")[0].strip()
    for prefix in ["I did ", "i did ", "I took ", "i took ", "I went to ", "i went to "]:
        if phrase.startswith(prefix):
            phrase = phrase[len(prefix):]
            break
    phrase = re.sub(r"\b(yesterday|today|this morning|this evening|tonight)\b", "", phrase, flags=re.IGNORECASE)
    workout_name = re.sub(r"\s+", " ", phrase).strip(" .,-")
    if not workout_name:
        return None
    return {
        "workout_date": workout_date,
        "workout_name": workout_name,
        "workout_category": categorize_workout(workout_name),
    }


def detect_symptom_flags(text: str) -> dict[str, bool]:
    lowered = text.lower()
    return {
        "very_sore": "very sore" in lowered or "really sore" in lowered or "extremely sore" in lowered,
        "stairs_pain": "painful to walk up stairs" in lowered or "stairs" in lowered and "pain" in lowered,
        "tired": "tired" in lowered or "exhausted" in lowered or "fatigued" in lowered,
        "missed_activity": "didn't go" in lowered or "did not go" in lowered or "skipped" in lowered,
    }


def interpret_water_entry(text: str, current_total_oz: float) -> dict[str, Any] | None:
    amount_oz = parse_water_oz(text)
    if amount_oz is None:
        return None

    lowered = text.strip().lower()
    incremental_markers = ["another", "more", "plus", "add ", "added", "extra", "just drank", "had another"]
    total_markers = ["total", "so far", "for today", "all day", "actually", "correction", "correct", "reset", "no "]
    is_increment = any(marker in lowered for marker in incremental_markers)
    is_total = any(marker in lowered for marker in total_markers)

    if is_increment:
        return {
            "entry_type": "increment",
            "logged_amount_oz": amount_oz,
            "reported_total_oz": round(current_total_oz + amount_oz, 1),
        }

    delta = round(amount_oz - current_total_oz, 1)
    if delta <= 0 and not is_total:
        return {
            "entry_type": "total",
            "logged_amount_oz": 0.0,
            "reported_total_oz": amount_oz,
        }

    return {
        "entry_type": "total",
        "logged_amount_oz": delta,
        "reported_total_oz": amount_oz,
    }


def coach_day(day: dict[str, Any]) -> dict[str, Any]:
    recovery_score = 0
    movement_score = 0
    notes: list[str] = []

    sleep_minutes = day["sleep_minutes"]
    sleep_efficiency = day["sleep_efficiency"]
    step_goal_pct = day["step_goal_pct"] or 0
    active_minutes = day["active_minutes"]
    sedentary_minutes = day["sedentary_minutes"]
    zone_minutes = day["zone_minutes"]
    movement_minutes = day["movement_minutes"]
    completed_exercise = zone_minutes >= 30

    if sleep_minutes >= 420:
        recovery_score += 2
    elif sleep_minutes >= 360:
        recovery_score += 1
    else:
        notes.append("Sleep was light on true recovery time, so intensity should stay modest.")

    if sleep_efficiency is not None:
        if sleep_efficiency >= 85:
            recovery_score += 2
        elif sleep_efficiency >= 78:
            recovery_score += 1
        else:
            notes.append("Sleep efficiency was choppy, which argues for a recovery-first day.")

    if step_goal_pct >= 100:
        movement_score += 2
    elif step_goal_pct >= 80:
        movement_score += 1
    else:
        notes.append("Daily movement is below target, so the easiest win is more easy walking.")

    if zone_minutes >= 45:
        movement_score += 2
    elif zone_minutes >= 20:
        movement_score += 1
    else:
        notes.append("There has not been much true exercise dose yet by zone-minute standards, which is fine if recovery is the priority.")

    if sedentary_minutes > 600:
        notes.append("Sedentary time crept high, so sprinkle short movement snacks through the day.")

    if day.get("rhr_hrv_pair_flag"):
        recovery_score -= 1
        notes.append("Resting HR is up while HRV is suppressed versus your recent baseline, which is a real recovery caution flag.")
    elif day.get("resting_hr_elevated"):
        notes.append("Resting HR is above your recent baseline, so I want a little more respect for recovery.")
    elif day.get("hrv_suppressed"):
        notes.append("HRV is softer than your recent baseline, so I do not want to pretend recovery is perfect.")

    if completed_exercise:
        if recovery_score >= 2:
            readiness = "trained"
            prescription = "You already got meaningful exercise in. The job now is recovery: protein, hydration, a little walking, and no bonus nonsense."
        else:
            readiness = "trained-but-watch-recovery"
            prescription = "You already trained today, so the smart move is to shift into recovery mode and avoid stacking extra intensity on tired tissue."
    elif recovery_score >= 3 and movement_score >= 2:
        readiness = "green"
        prescription = "You are clear for a normal training day: strength work or a solid bike/hike effort is reasonable."
    elif recovery_score >= 2:
        readiness = "yellow"
        prescription = "This is a build-the-base day: strength technique, brisk walking, easy biking, or dancing without chasing intensity."
    else:
        readiness = "amber"
        prescription = "Keep today restorative: walking, mobility, easy movement, and protein on purpose. Heroics are cancelled."

    if not notes:
        notes.append("The basics are in place. Keep the day boring and consistent, which is how fat loss actually wins.")
    if completed_exercise:
        notes.append(
            f"Today already includes {zone_minutes} zone minutes, which counts as real exercise rather than background movement."
        )
    elif movement_minutes >= 120:
        notes.append("There is still plenty of general movement on the board, even if zone-minute exercise is modest.")

    return {
        "readiness": readiness,
        "prescription": prescription,
        "notes": notes,
    }


def build_water_report(client: FitbitClient, target_date: str, warm_day: bool = False) -> dict[str, Any]:
    goals = client.get_user_goals()
    coach = build_coach_report(client, target_date)
    stats = coach["stats"]
    total_oz = client.get_water_total(target_date)
    logs = client.get_water_intake_logs(target_date)

    active_bonus = goals["water_goal_active_bonus_oz"] if stats["zone_minutes"] >= 30 or stats["movement_minutes"] >= 120 else 0
    warm_bonus = goals["water_goal_warm_bonus_oz"] if warm_day else 0
    minimum_target = goals["water_goal_min_oz"] + active_bonus + warm_bonus
    ideal_target = goals["water_goal_max_oz"] + active_bonus + warm_bonus

    if total_oz >= ideal_target:
        status = "met"
    elif total_oz >= minimum_target:
        status = "good"
    elif total_oz >= minimum_target * 0.65:
        status = "close"
    else:
        status = "behind"

    notes: list[str] = []
    if active_bonus:
        notes.append("Activity bumped the hydration target up a bit today.")
    if warm_bonus:
        notes.append("Warm-day bonus is on, so the hydration target is higher.")
    if status == "met":
        notes.append("Hydration goal is already handled. Nicely boring, exactly how we like it.")
    elif status == "good":
        notes.append("You are in the solid range already; a little more would polish it off.")
    elif status == "close":
        notes.append("You are close enough that one focused refill would change the story.")
    else:
        notes.append("Hydration is lagging, and that gets more expensive on active or recovery-sensitive days.")

    return {
        "date": target_date,
        "total_oz": total_oz,
        "minimum_target_oz": minimum_target,
        "ideal_target_oz": ideal_target,
        "active_bonus_oz": active_bonus,
        "warm_bonus_oz": warm_bonus,
        "status": status,
        "logs": logs,
        "coach_notes": notes,
    }


def format_water_reply(report: dict[str, Any]) -> str:
    notes = " ".join(report["coach_notes"])
    return (
        f"Water so far today is {report['total_oz']} oz. "
        f"The target range is {report['minimum_target_oz']} to {report['ideal_target_oz']} oz today. "
        f"Status: {report['status']}. {notes}"
    )


def build_water_sms_prompt(client: FitbitClient, target_date: str, window: str, warm_day: bool = False) -> dict[str, Any]:
    report = build_water_report(client, target_date, warm_day=warm_day)
    if window == "noon":
        return {
            "send": True,
            "message": (
                f"Coach check-in: how much water have you had so far today? "
                f"You are at {report['total_oz']} oz. Today's target range is {report['minimum_target_oz']}-{report['ideal_target_oz']} oz. "
                "Reply with a number like '24 oz'."
            ),
            "reason": "midday_check_in",
        }
    if window == "evening":
        if report["status"] == "met":
            return {
                "send": False,
                "message": None,
                "reason": "goal_already_met",
            }
        return {
            "send": True,
            "message": (
                f"9:45 hydration check: you are at {report['total_oz']} oz so far, with a target range of "
                f"{report['minimum_target_oz']}-{report['ideal_target_oz']} oz today. Reply with your total for today, "
                "like '82 oz'."
            ),
            "reason": "goal_still_open",
        }
    raise ValueError(f"Unsupported water reminder window: {window}")


def due_water_reminder_window(now: datetime) -> str | None:
    minute_of_day = now.hour * 60 + now.minute
    if 12 * 60 <= minute_of_day < 12 * 60 + 5:
        return "noon"
    if 21 * 60 + 45 <= minute_of_day < 21 * 60 + 50:
        return "evening"
    return None


def run_due_scheduler_cycle(
    client: FitbitClient,
    now: datetime | None = None,
    send: bool = True,
    warm_day: bool = False,
) -> dict[str, Any]:
    current = now or datetime.now().astimezone()
    target_date = current.date().isoformat()
    window = due_water_reminder_window(current)
    if window is None:
        return {
            "date": target_date,
            "timestamp": current.isoformat(),
            "status": "idle",
            "reason": "outside reminder windows",
        }

    reminder_key = f"water_{window}"
    if client.reminder_already_run(reminder_key, target_date):
        return {
            "date": target_date,
            "timestamp": current.isoformat(),
            "status": "already_ran",
            "window": window,
        }

    reminder = build_water_sms_prompt(client, target_date, window=window, warm_day=warm_day)
    payload: dict[str, Any] = {
        "date": target_date,
        "timestamp": current.isoformat(),
        "window": window,
        "status": "skipped" if not reminder["send"] else "pending_send",
        "reason": reminder["reason"],
        "message": reminder["message"],
        "sent": False,
    }

    if not reminder["send"]:
        client.record_reminder_run(reminder_key, target_date, "skipped", payload)
        return payload

    if send and reminder["message"]:
        sms_payload = client.send_sms(reminder["message"])
        payload["status"] = "sent"
        payload["sent"] = True
        payload["sid"] = sms_payload.get("sid")
        client.record_reminder_run(reminder_key, target_date, "sent", payload)
        return payload

    payload["status"] = "dry_run"
    client.record_reminder_run(reminder_key, target_date, "dry_run", payload)
    return payload


def handle_water_sms_reply(client: FitbitClient, body: str, target_date: str) -> str:
    log_date = parse_relative_date(body, target_date)
    current_total = client.get_water_total(log_date)
    interpreted = interpret_water_entry(body, current_total)
    if interpreted is None:
        return "I could not parse the water amount. Reply with something like '24 oz'."
    if interpreted["logged_amount_oz"] != 0:
        client.add_water_intake(log_date, interpreted["logged_amount_oz"], source="sms", note=body.strip())
    report = build_water_report(client, log_date)
    date_phrase = (
        "for yesterday"
        if log_date == parse_relative_date("yesterday", target_date)
        else "for tomorrow"
        if log_date == parse_relative_date("tomorrow", target_date)
        else "for today"
    )
    if interpreted["entry_type"] == "total" and interpreted["logged_amount_oz"] == 0:
        return (
            f"Got it. I already had you at {current_total} oz, so I did not add more. "
            f"Current total stays {report['total_oz']} oz {date_phrase}."
        )
    if interpreted["entry_type"] == "total" and interpreted["logged_amount_oz"] < 0:
        return (
            f"Corrected. I reset your total to {report['total_oz']} oz {date_phrase}."
        )
    if report["status"] == "met":
        return (
            f"Logged {interpreted['logged_amount_oz']} oz. You are now at {report['total_oz']} oz {date_phrase}, which clears the hydration target. Nice work."
        )
    return (
        f"Logged {interpreted['logged_amount_oz']} oz. You are now at {report['total_oz']} oz {date_phrase}, with a goal range of "
        f"{report['minimum_target_oz']}-{report['ideal_target_oz']} oz."
    )


def get_weight_logs(client: FitbitClient, start_date: str, end_date: str) -> dict[str, Any]:
    return client.get_json(f"/1/user/-/body/log/weight/date/{start_date}/{end_date}.json")


def build_bodycomp_report(client: FitbitClient, end_date: str, days: int) -> dict[str, Any]:
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    start = end - timedelta(days=days - 1)
    payload = get_weight_logs(client, start.isoformat(), end.isoformat())
    entries = payload.get("weight", [])

    normalized = []
    for entry in entries:
        weight = entry.get("weight")
        fat_pct = entry.get("fat")
        if weight is None:
            continue
        fat_mass = None
        lean_mass = None
        if fat_pct is not None:
            fat_mass = round(weight * (fat_pct / 100), 2)
            lean_mass = round(weight - fat_mass, 2)
        normalized.append(
            {
                "date": entry.get("date"),
                "time": entry.get("time"),
                "weight_kg": weight,
                "fat_pct": round_or_none(fat_pct, 1),
                "fat_mass_kg": round_or_none(fat_mass, 1),
                "lean_mass_kg": round_or_none(lean_mass, 1),
                "source": entry.get("source"),
            }
        )

    normalized.sort(key=lambda item: f"{item['date']}T{item.get('time') or '00:00:00'}")
    weights = [item["weight_kg"] for item in normalized]
    fat_pcts = [item["fat_pct"] for item in normalized if item["fat_pct"] is not None]
    lean_masses = [item["lean_mass_kg"] for item in normalized if item["lean_mass_kg"] is not None]
    weight_trend = calculate_trend(weights)
    fat_trend = calculate_trend(fat_pcts) if fat_pcts else []
    lean_trend = calculate_trend(lean_masses) if lean_masses else []

    latest = normalized[-1] if normalized else None
    coach_notes: list[str] = []
    if latest and latest["fat_pct"] is not None:
        coach_notes.append("Body fat percentage is available, so we can track fat mass and estimated lean mass instead of obsessing over scale noise.")
    if len(normalized) >= 2 and latest is not None:
        delta_weight = round(normalized[-1]["weight_kg"] - normalized[0]["weight_kg"], 2)
        if abs(delta_weight) >= 0.8:
            coach_notes.append("Short-term scale changes can still be water, sodium, and glycogen, so trend beats drama.")
    if latest and latest["lean_mass_kg"] is not None:
        coach_notes.append("For sarcopenia prevention, the mission is simple: protect lean mass with protein and strength while fat mass trends down slowly.")

    return {
        "window": {
            "days": days,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "entries": len(normalized),
        },
        "latest": latest,
        "trend": {
            "weight_kg": round(weight_trend[-1], 2) if weight_trend else None,
            "fat_pct": round_or_none(fat_trend[-1], 1) if fat_trend else None,
            "lean_mass_kg": round(lean_trend[-1], 2) if lean_trend else None,
        },
        "daily": normalized,
        "coach_notes": coach_notes,
    }


def run_bodycomp(client: FitbitClient, end_date: str, days: int) -> None:
    print(json.dumps(build_bodycomp_report(client, end_date, days), indent=2))


def build_primary_goal(client: FitbitClient, target_date: str, day: dict[str, Any], goals: dict[str, Any]) -> dict[str, Any]:
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    yesterday_date = (target - timedelta(days=1)).isoformat()
    yesterday: dict[str, Any] | None = None
    try:
        yesterday = summarize_day(get_day_snapshot(client, yesterday_date))
    except Exception:  # noqa: BLE001
        yesterday = None

    yesterday_trained = bool(yesterday and yesterday["zone_minutes"] >= 30)
    recovery_biased_day = day["readiness"] in {"amber", "trained-but-watch-recovery", "yellow"}

    if yesterday_trained and recovery_biased_day and day["zone_minutes"] < 30:
        target_minutes = 30
        status = "met" if day["movement_minutes"] >= target_minutes else "close" if day["movement_minutes"] >= 20 else "not_met"
        return {
            "kind": "recovery_walk",
            "label": "Recovery walk",
            "target": target_minutes,
            "unit": "movement_minutes",
            "actual": day["movement_minutes"],
            "status": status,
            "reason": "Yesterday already had real training, so today’s smart goal is restorative movement rather than more intensity.",
        }

    step_target = goals["step_goal"]
    step_status = "met" if day["steps"] >= step_target else "close" if day["steps"] >= step_target * 0.8 else "not_met"
    return {
        "kind": "steps",
        "label": "Daily movement",
        "target": step_target,
        "unit": "steps",
        "actual": day["steps"],
        "status": step_status,
        "reason": "Base movement is still the easiest high-return lever for fat loss and recovery.",
    }


def build_exercise_goal(day: dict[str, Any], goals: dict[str, Any], primary_goal: dict[str, Any]) -> dict[str, Any]:
    zone_target = goals["zone_min_goal"]
    if primary_goal["kind"] == "recovery_walk":
        return {
            "label": "Recovery movement",
            "target": primary_goal["target"],
            "unit": primary_goal["unit"],
            "actual": primary_goal["actual"],
            "status": primary_goal["status"],
            "reason": "On recovery days, purposeful walking counts as the exercise win instead of chasing heart-rate zones.",
        }

    zone_minutes = day["zone_minutes"]
    movement_minutes = day["movement_minutes"]
    steps = day["steps"]

    if zone_minutes >= zone_target or movement_minutes >= 45 or (zone_minutes >= 20 and steps >= goals["step_goal"] * 0.8):
        status = "met"
    elif zone_minutes >= max(10, zone_target * 0.5) or movement_minutes >= 25 or steps >= goals["step_goal"] * 0.6:
        status = "close"
    else:
        status = "not_met"

    return {
        "label": "Combined exercise",
        "target": zone_target,
        "unit": "zone-minute equivalent",
        "actual": zone_minutes,
        "status": status,
        "reason": "Zone minutes count most, but purposeful walking and meaningful step volume still count on lighter days.",
    }


def build_training_recommendation(client: FitbitClient, target_date: str) -> dict[str, str]:
    coach = build_coach_report(client, target_date)
    stats = coach["stats"]
    recent_workouts = client.get_recent_workouts(limit=3)
    recent_strength = any(
        (item.get("workout_category") == "strength") or ("strength" in (item.get("workout_name") or "").lower())
        for item in recent_workouts
    )
    pair_flag = bool(coach.get("recovery_baseline", {}).get("pair_flag"))
    rhr_elevated = bool(coach.get("recovery_baseline", {}).get("resting_hr_elevated"))
    hrv_suppressed = bool(coach.get("recovery_baseline", {}).get("hrv_suppressed"))
    already_moved_well = stats["steps"] >= 10000 or stats["movement_minutes"] >= 90

    if pair_flag:
        return {
            "primary": "PT or recovery",
            "why": "your resting HR is up and HRV is down versus baseline, which is a real recovery caution flag",
            "prompt": "I recommend PT or recovery today, not a hard class. What sounds more realistic: PT, a walk, or full recovery?",
        }

    if coach["readiness"] == "amber":
        return {
            "primary": "Recovery",
            "why": "recovery is not convincing enough for a real training push",
            "prompt": "I recommend recovery today. If you want to do something useful, make it PT or an easy walk rather than a real workout.",
        }

    if coach["readiness"] == "yellow":
        if already_moved_well and (rhr_elevated or hrv_suppressed):
            return {
                "primary": "PT or enough-for-today",
                "why": "you already banked solid movement and recovery is giving a yellow light, not a green one",
                "prompt": "You already banked enough movement that I do not think you need more. If you still want something useful, I recommend PT over a harder session.",
            }
        if recent_strength:
            return {
                "primary": "PT or VR strength only if moderate",
                "why": "you have recent strength load already, so I want a lower-drama second option",
                "prompt": "I would lean PT today. If you really want training, make it a moderate VR strength session, not a prove-something workout.",
            }
        return {
            "primary": "Class or VR strength",
            "why": "recovery is decent enough and there is no strong veto from recent load",
            "prompt": "I think a real workout is reasonable today. My bias is class or VR strength if you want training, with PT as the lower-load backup.",
        }

    return {
        "primary": "Real workout",
        "why": "readiness is good and I do not see a strong recovery veto",
        "prompt": "I recommend a real workout today. Class or VR strength are both good options unless life logistics make PT the smarter fit.",
    }


def build_coach_report(client: FitbitClient, target_date: str) -> dict[str, Any]:
    day = summarize_day(get_day_snapshot(client, target_date))
    recovery_baseline = build_recovery_baseline(client, target_date, 7)
    day["resting_hr_baseline"] = recovery_baseline["avg_resting_hr"]
    day["hrv_baseline"] = recovery_baseline["avg_hrv_daily_rmssd"]
    day["resting_hr_elevated"] = recovery_baseline["resting_hr_elevated"]
    day["hrv_suppressed"] = recovery_baseline["hrv_suppressed"]
    day["rhr_hrv_pair_flag"] = recovery_baseline["pair_flag"]
    coaching = coach_day(day)
    goals = client.get_user_goals()
    day["readiness"] = coaching["readiness"]

    goal_status = {
        "steps": {
            "target": goals["step_goal"],
            "actual": day["steps"],
            "status": "met" if day["steps"] >= goals["step_goal"] else "close" if day["steps"] >= goals["step_goal"] * 0.8 else "not_met",
        },
        "zone_minutes": {
            "target": goals["zone_min_goal"],
            "actual": day["zone_minutes"],
            "status": "met" if day["zone_minutes"] >= goals["zone_min_goal"] else "close" if day["zone_minutes"] >= goals["zone_min_goal"] * 0.67 else "not_met",
        },
    }
    primary_goal = build_primary_goal(client, target_date, day, goals)
    exercise_goal = build_exercise_goal(day, goals, primary_goal)

    return {
        "date": target_date,
        "readiness": coaching["readiness"],
        "prescription": coaching["prescription"],
        "goal_status": goal_status,
        "primary_goal": primary_goal,
        "exercise_goal": exercise_goal,
        "stats": {
            "steps": day["steps"],
            "step_goal": day["step_goal"],
            "step_goal_pct": day["step_goal_pct"],
            "active_minutes": day["active_minutes"],
            "light_minutes": day["light_minutes"],
            "movement_minutes": day["movement_minutes"],
            "sedentary_minutes": day["sedentary_minutes"],
            "resting_hr": day["resting_hr"],
            "resting_hr_baseline": day["resting_hr_baseline"],
            "hrv_daily_rmssd": day["hrv_daily_rmssd"],
            "hrv_baseline": day["hrv_baseline"],
            "zone_minutes": day["zone_minutes"],
            "fat_burn_zone_minutes": day["fat_burn_zone_minutes"],
            "cardio_zone_minutes": day["cardio_zone_minutes"],
            "peak_zone_minutes": day["peak_zone_minutes"],
            "sleep_minutes": day["sleep_minutes"],
            "sleep": format_minutes(day["sleep_minutes"]),
            "time_in_bed": format_minutes(day["time_in_bed_minutes"]),
            "sleep_efficiency": day["sleep_efficiency"],
        },
        "recovery_baseline": recovery_baseline,
        "notes": coaching["notes"],
    }


def build_trends_report(client: FitbitClient, end_date: str, days: int) -> dict[str, Any]:
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    snapshots = []
    for offset in range(days - 1, -1, -1):
        current = end - timedelta(days=offset)
        snapshots.append(summarize_day(get_day_snapshot(client, current.isoformat())))

    steps = [item["steps"] for item in snapshots]
    sleep_minutes = [item["sleep_minutes"] for item in snapshots if item["sleep_minutes"] > 0]
    resting_hrs = [item["resting_hr"] for item in snapshots if item["resting_hr"] is not None]
    hrv_values = [item["hrv_daily_rmssd"] for item in snapshots if item.get("hrv_daily_rmssd") is not None]
    hit_step_goal_days = sum(1 for item in snapshots if (item["step_goal_pct"] or 0) >= 100)
    avg_steps = round(sum(steps) / len(steps), 1) if steps else 0
    avg_sleep = round(sum(sleep_minutes) / len(sleep_minutes)) if sleep_minutes else 0
    avg_rhr = round(sum(resting_hrs) / len(resting_hrs), 1) if resting_hrs else None
    avg_hrv = round(sum(hrv_values) / len(hrv_values), 1) if hrv_values else None
    consistency = "strong" if hit_step_goal_days >= 5 else "fair" if hit_step_goal_days >= 3 else "needs work"

    coach_notes: list[str] = []
    if avg_sleep < 390:
        coach_notes.append("Average sleep is too thin to support great recovery, appetite control, and muscle retention.")
    if avg_steps < 5000:
        coach_notes.append("Movement volume is low for a fat-loss phase, so daily walking is the highest-return lever.")
    if avg_rhr is not None and snapshots[-1]["resting_hr"] is not None and snapshots[-1]["resting_hr"] >= avg_rhr + 3:
        coach_notes.append("Today’s resting heart rate is meaningfully above your 7-day average, so recovery gets veto power.")
    if avg_hrv is not None and snapshots[-1].get("hrv_daily_rmssd") is not None and snapshots[-1]["hrv_daily_rmssd"] <= avg_hrv * 0.85:
        coach_notes.append("Today’s HRV is meaningfully below your 7-day average, so I want more humility about recovery.")
    if (
        avg_rhr is not None
        and snapshots[-1]["resting_hr"] is not None
        and snapshots[-1]["resting_hr"] >= avg_rhr + 3
        and avg_hrv is not None
        and snapshots[-1].get("hrv_daily_rmssd") is not None
        and snapshots[-1]["hrv_daily_rmssd"] <= avg_hrv * 0.85
    ):
        coach_notes.append("High resting HR plus low HRV together is a stronger recovery caution signal than either one alone.")
    if not coach_notes:
        coach_notes.append("The weekly trend is stable enough to keep progressing with consistency over drama.")

    return {
        "window": {
            "days": days,
            "start_date": snapshots[0]["date"] if snapshots else None,
            "end_date": snapshots[-1]["date"] if snapshots else None,
        },
        "averages": {
            "steps": avg_steps,
            "sleep": format_minutes(avg_sleep),
            "resting_hr": avg_rhr,
            "hrv_daily_rmssd": avg_hrv,
        },
        "consistency": {
            "step_goal_hit_days": hit_step_goal_days,
            "step_goal_consistency": consistency,
        },
        "daily": [
            {
                "date": item["date"],
                "steps": item["steps"],
                "step_goal_pct": item["step_goal_pct"],
                "sleep": format_minutes(item["sleep_minutes"]),
                "resting_hr": item["resting_hr"],
                "hrv_daily_rmssd": item.get("hrv_daily_rmssd"),
                "active_minutes": item["active_minutes"],
            }
            for item in snapshots
        ],
        "coach_notes": coach_notes,
    }


def build_fatloss_report(client: FitbitClient, end_date: str, days: int) -> dict[str, Any]:
    bodycomp = build_bodycomp_report(client, end_date, days)
    daily = bodycomp["daily"]
    if not daily:
        return {
            "window": bodycomp["window"],
            "latest": bodycomp["latest"],
            "trend": bodycomp["trend"],
            "changes": {
                "weight_kg": None,
                "fat_mass_kg": None,
                "lean_mass_kg": None,
            },
            "verdict": "insufficient data",
            "summary": "There are no body-composition rows in this window yet, so fat-loss guidance is limited.",
            "coach_notes": [
                "Keep logging weigh-ins consistently so the coach can separate fat loss from water noise."
            ],
        }
    valid_fat = [item for item in daily if item["fat_mass_kg"] is not None]
    valid_lean = [item for item in daily if item["lean_mass_kg"] is not None]

    fat_start = valid_fat[0]["fat_mass_kg"] if valid_fat else None
    fat_end = valid_fat[-1]["fat_mass_kg"] if valid_fat else None
    lean_start = valid_lean[0]["lean_mass_kg"] if valid_lean else None
    lean_end = valid_lean[-1]["lean_mass_kg"] if valid_lean else None
    weight_start = daily[0]["weight_kg"] if daily else None
    weight_end = daily[-1]["weight_kg"] if daily else None

    fat_change = round_or_none((fat_end - fat_start), 1) if fat_start is not None and fat_end is not None else None
    lean_change = round_or_none((lean_end - lean_start), 1) if lean_start is not None and lean_end is not None else None
    weight_change = round_or_none((weight_end - weight_start), 1) if weight_start is not None and weight_end is not None else None

    if fat_change is None:
        verdict = "insufficient data"
        summary = "Body-fat data is too thin to call the trend yet."
    elif fat_change <= -0.4 and (lean_change is None or lean_change >= -0.3):
        verdict = "fat loss with lean mass protected"
        summary = "This window looks like actual fat loss, not just scale theater."
    elif fat_change <= -0.2 and lean_change is not None and lean_change < -0.3:
        verdict = "mixed loss"
        summary = "Some fat appears to be coming off, but lean mass may be slipping too."
    elif abs(fat_change) < 0.2 and weight_change is not None and abs(weight_change) >= 0.6:
        verdict = "mostly water noise"
        summary = "Scale movement is outpacing body-fat movement, which smells like fluid and glycogen."
    else:
        verdict = "slow or unclear"
        summary = "The trend is moving, but not enough to declare a clean fat-loss win yet."

    coach_notes: list[str] = []
    if lean_change is not None and lean_change < -0.3:
        coach_notes.append("Lean mass trend is soft, so protein and strength training need to stop being optional.")
    if fat_change is not None and fat_change >= 0:
        coach_notes.append("Fat mass is not trending down yet, so the lever is consistency, not punishment.")
    if fat_change is not None and fat_change < 0 and (lean_change is None or lean_change >= -0.3):
        coach_notes.append("The current pattern supports the goal: fat trending down while lean mass stays relatively intact.")
    if not coach_notes:
        coach_notes.append("Give the trend another week before making a big decision off a small wobble.")

    return {
        "window": bodycomp["window"],
        "latest": bodycomp["latest"],
        "trend": bodycomp["trend"],
        "changes": {
            "weight_kg": weight_change,
            "fat_mass_kg": fat_change,
            "lean_mass_kg": lean_change,
        },
        "verdict": verdict,
        "summary": summary,
        "coach_notes": coach_notes,
    }


def build_zepbound_report(client: FitbitClient, target_date: str) -> dict[str, Any]:
    goals = client.get_user_goals()
    sheet_url = client.config.zepbound_sheet_url
    if not sheet_url:
        raise FitbitConfigError("Missing ZEPBOUND_SHEET_URL in the environment.")

    csv_url = build_google_csv_url(sheet_url)
    csv_text = client.get_text(csv_url)
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        raise FitbitConfigError("Zepbound sheet returned no data.")

    headers = rows[0]
    note_index = 3 if len(headers) > 3 else None
    target = datetime.strptime(target_date, "%Y-%m-%d").date()

    entries = []
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        try:
            row_date = parse_sheet_date(row[0])
        except ValueError:
            continue
        if row_date > target:
            break
        estimated = parse_float(row[1] if len(row) > 1 else None)
        administered = parse_float(row[2] if len(row) > 2 else None)
        note = row[note_index].strip() if note_index is not None and len(row) > note_index else ""
        entries.append(
            {
                "date": row_date.isoformat(),
                "estimated_amount_mg": round_or_none(estimated, 2),
                "dose_administered_mg": round_or_none(administered, 2),
                "note": note or None,
            }
        )

    if not entries:
        raise FitbitConfigError("No Zepbound rows were available on or before the requested date.")

    latest = entries[-1]
    dose_entries = [entry for entry in entries if entry["dose_administered_mg"]]
    last_dose = dose_entries[-1] if dose_entries else None
    previous_dose = dose_entries[-2] if len(dose_entries) >= 2 else None
    days_since_last_dose = None
    days_between_last_two = None
    if last_dose:
        days_since_last_dose = (target - datetime.strptime(last_dose["date"], "%Y-%m-%d").date()).days
    if last_dose and previous_dose:
        days_between_last_two = (
            datetime.strptime(last_dose["date"], "%Y-%m-%d").date()
            - datetime.strptime(previous_dose["date"], "%Y-%m-%d").date()
        ).days
    cadence_days = days_between_last_two or 7
    next_due_date = None
    if last_dose:
        next_due_date = (
            datetime.strptime(last_dose["date"], "%Y-%m-%d").date() + timedelta(days=cadence_days)
        ).isoformat()

    coach_notes: list[str] = []
    if last_dose and days_since_last_dose == 0:
        coach_notes.append("Dose day today. Respect appetite changes, hydration, and any GI drama before pushing intensity.")
    elif last_dose and days_since_last_dose in {1, 2}:
        coach_notes.append("You are in the early post-shot window, so recovery and protein execution matter more than intensity cosplay.")
    elif last_dose and days_since_last_dose >= 6:
        coach_notes.append("You are late in the shot cycle, so hunger may rise and planning beats willpower.")
    if latest["estimated_amount_mg"] is not None and latest["estimated_amount_mg"] < 2.5:
        coach_notes.append("Modeled medication-in-system is on the lower side, so appetite support habits need to do more of the work.")
    if not coach_notes:
        coach_notes.append("Your dosing data is available, so we can line up appetite, recovery, and training around the shot cycle.")

    shot_logged_today = bool(last_dose and last_dose["date"] == target_date)
    is_current_day = target_date == current_date_for_client(client)
    expected_shot_day = bool(next_due_date and target_date >= next_due_date)
    if shot_logged_today:
        shot_status = "met"
    elif is_current_day and expected_shot_day:
        shot_status = "pending"
    else:
        shot_status = "not_met"
    shot_summary = (
        "Shot logged today"
        if shot_status == "met"
        else "Shot still open for later today"
        if shot_status == "pending"
        else f"Next shot likely due {next_due_date}"
        if next_due_date and target_date < next_due_date
        else "No shot logged for this date"
    )

    return {
        "date": target_date,
        "latest_entry": latest,
        "last_dose": last_dose,
        "days_since_last_dose": days_since_last_dose,
        "days_between_last_two_doses": days_between_last_two,
        "next_due_date": next_due_date,
        "goal_status": {
            "shot_logged": {
                "required": goals["shot_logging_required"],
                "actual": shot_logged_today,
                "status": "met" if not goals["shot_logging_required"] else shot_status,
                "summary": shot_summary,
            }
        },
        "coach_notes": coach_notes,
        "source_csv_url": csv_url,
    }


def build_daily_wins(client: FitbitClient, target_date: str) -> list[dict[str, Any]]:
    coach = build_coach_report(client, target_date)
    zepbound = build_zepbound_report(client, target_date)
    bodycomp = build_bodycomp_report(client, target_date, 1)

    wins: list[dict[str, Any]] = []
    stats = coach["stats"]
    primary_goal = coach["primary_goal"]
    if primary_goal["status"] == "met":
        if primary_goal["kind"] == "recovery_walk":
            wins.append(
                {
                    "kind": "recovery_walk",
                    "label": f"Recovery walk goal met: {primary_goal['actual']} / {primary_goal['target']} movement minutes",
                }
            )
        else:
            wins.append(
                {
                    "kind": "primary_goal",
                    "label": f"{primary_goal['label']} goal met: {primary_goal['actual']} / {primary_goal['target']} {primary_goal['unit']}",
                }
            )

    step_goal = coach["goal_status"]["steps"]
    if step_goal["status"] == "met" and primary_goal["kind"] != "steps":
        wins.append({"kind": "steps", "label": f"Step goal met: {step_goal['actual']} / {step_goal['target']}"})
    elif step_goal["status"] == "close":
        wins.append({"kind": "steps_close", "label": f"Close on steps: {step_goal['actual']} / {step_goal['target']}"})

    if stats["movement_minutes"] >= 30 and primary_goal["kind"] != "recovery_walk":
        wins.append({"kind": "walk", "label": f"Walk win banked: {stats['movement_minutes']} movement minutes"})

    exercise_goal = coach["exercise_goal"]
    if exercise_goal["status"] == "met":
        wins.append({"kind": "exercise_goal", "label": f"{exercise_goal['label']} goal met"})
    elif exercise_goal["status"] == "close":
        wins.append({"kind": "exercise_goal_close", "label": f"Close on {exercise_goal['label'].lower()} goal"})

    if zepbound["goal_status"]["shot_logged"]["status"] == "met":
        wins.append({"kind": "shot_logged", "label": "Shot logged today"})

    if bodycomp["latest"] and bodycomp["latest"]["date"] == target_date:
        wins.append({"kind": "weigh_in", "label": "Weigh-in logged today"})

    return wins


def build_weekly_goal_summary(client: FitbitClient, end_date: str, days: int = 7) -> dict[str, Any]:
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    daily: list[dict[str, Any]] = []
    primary_goal_met_days = 0
    exercise_goal_met_days = 0
    recovery_goal_days = 0
    build_goal_days = 0
    ambitious_goal_days = 0

    for offset in range(days - 1, -1, -1):
        current = end - timedelta(days=offset)
        report = build_coach_report(client, current.isoformat())
        primary_status = report["primary_goal"]["status"]
        exercise_status = report["exercise_goal"]["status"]
        primary_kind = report["primary_goal"]["kind"]
        readiness = report["readiness"]
        if primary_status == "met":
            primary_goal_met_days += 1
        if exercise_status == "met":
            exercise_goal_met_days += 1
        if primary_kind == "recovery_walk" or readiness in {"amber", "trained-but-watch-recovery"}:
            recovery_goal_days += 1
        elif readiness == "yellow":
            build_goal_days += 1
        elif readiness == "green":
            ambitious_goal_days += 1
        daily.append(
            {
                "date": current.isoformat(),
                "primary_goal": report["primary_goal"]["label"],
                "primary_kind": primary_kind,
                "primary_status": primary_status,
                "exercise_goal": report["exercise_goal"]["label"],
                "exercise_status": exercise_status,
                "readiness": readiness,
            }
        )

    if primary_goal_met_days >= 5:
        consistency = "strong"
    elif primary_goal_met_days >= 3:
        consistency = "decent"
    else:
        consistency = "shaky"

    return {
        "days": days,
        "primary_goal_met_days": primary_goal_met_days,
        "exercise_goal_met_days": exercise_goal_met_days,
        "recovery_goal_days": recovery_goal_days,
        "build_goal_days": build_goal_days,
        "ambitious_goal_days": ambitious_goal_days,
        "consistency": consistency,
        "daily": daily,
    }


def build_previous_window_fatloss_report(client: FitbitClient, end_date: str, days: int) -> dict[str, Any] | None:
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    previous_end = end - timedelta(days=days)
    try:
        return build_fatloss_report(client, previous_end.isoformat(), days)
    except Exception:  # noqa: BLE001
        return None


def build_readiness_reasons(report: dict[str, Any]) -> list[str]:
    stats = report["stats"]
    reasons: list[str] = []

    if report.get("recovery_baseline", {}).get("pair_flag"):
        reasons.append("resting HR is up and HRV is down versus your recent baseline")

    sleep_minutes = stats.get("sleep_minutes") or 0
    sleep_text = stats.get("sleep")
    sleep_efficiency = stats.get("sleep_efficiency")
    step_goal_pct = stats.get("step_goal_pct") or 0
    zone_minutes = stats.get("zone_minutes") or 0
    movement_minutes = stats.get("movement_minutes") or 0

    if sleep_minutes and sleep_minutes < 420 and sleep_text and sleep_text != "n/a":
        reasons.append(f"sleep was only {sleep_text}")
    if sleep_efficiency is not None and sleep_efficiency < 85:
        reasons.append(f"sleep efficiency was {sleep_efficiency}")
    if zone_minutes < 20:
        reasons.append(f"true exercise dose is still low at {zone_minutes} zone minutes")
    if step_goal_pct < 80:
        reasons.append(f"daily movement is only {step_goal_pct}% of your step goal")
    elif movement_minutes >= 120 and not reasons:
        reasons.append(f"you do have plenty of base movement at {movement_minutes} movement minutes")
    if not reasons and sleep_text and sleep_text != "n/a":
        reasons.append(f"recovery is decent but not so convincing that I want to force intensity just because I can")

    return reasons


def detect_manual_sleep_context(text: str) -> dict[str, bool]:
    lowered = text.lower()
    forgot_fitbit = any(
        phrase in lowered
        for phrase in [
            "forgot to wear my fitbit to bed",
            "didn't wear my fitbit to bed",
            "did not wear my fitbit to bed",
            "forgot my fitbit at night",
            "fitbit to bed",
        ]
    )
    slept_well = any(
        phrase in lowered
        for phrase in [
            "slept well",
            "good night sleep",
            "good night's sleep",
            "good nights sleep",
            "got good sleep",
            "slept great",
        ]
    )
    return {
        "forgot_fitbit": forgot_fitbit,
        "slept_well": slept_well,
    }


def recent_manual_sleep_note(client: FitbitClient, target_date: str) -> dict[str, bool] | None:
    for item in reversed(client.read_recent_interactions(limit=12)):
        if item.get("date_context") != target_date:
            continue
        flags = detect_manual_sleep_context(item.get("message", ""))
        if flags["forgot_fitbit"] or flags["slept_well"]:
            return flags
    return None


def apply_recent_sleep_context(client: FitbitClient, reply: str, target_date: str) -> str:
    flags = recent_manual_sleep_note(client, target_date)
    if not flags:
        return reply
    if flags["forgot_fitbit"] and flags["slept_well"]:
        return (
            f"{reply} You told me Fitbit missed last night but you actually slept well, "
            "so I do not want to treat the missing sleep data as automatic bad recovery."
        )
    if flags["slept_well"]:
        return f"{reply} You also told me sleep was better than the device captured, so I want to keep that in mind."
    return reply


def format_coach_reply(report: dict[str, Any]) -> str:
    stats = report["stats"]
    notes = " ".join(report["notes"])
    reasons = build_readiness_reasons(report)
    why_line = f"My main reason is that {reasons[0]}." if reasons else "My main reason is that recovery does not quite justify a green light."
    hrv_line = (
        f" HRV is {stats['hrv_daily_rmssd']} ms against a 7-day baseline of {stats['hrv_baseline']} ms."
        if stats.get("hrv_daily_rmssd") is not None and stats.get("hrv_baseline") is not None
        else ""
    )
    return (
        f"{report['date']}: readiness is {report['readiness']}. {why_line} {report['prescription']} "
        f"Steps are {stats['steps']} of {stats['step_goal']} ({stats['step_goal_pct']}%), "
        f"zone minutes are {stats['zone_minutes']} "
        f"({stats['fat_burn_zone_minutes']} fat burn, {stats['cardio_zone_minutes']} cardio, {stats['peak_zone_minutes']} peak), "
        f"sleep is {stats['sleep']} with efficiency {stats['sleep_efficiency']}, and resting HR is {stats['resting_hr']}.{hrv_line} "
        f"{notes}"
    )


def format_sleep_context_reply(client: FitbitClient, prompt: str, target_date: str) -> str:
    flags = detect_manual_sleep_context(prompt)
    coach = build_coach_report(client, target_date)
    if flags["forgot_fitbit"] and flags["slept_well"]:
        return (
            "That helps. I do not want to confuse missing Fitbit sleep data with bad sleep. "
            f"I’ll treat today’s recovery picture as better than the device alone suggests. Right now I still read the day as {coach['readiness']}, "
            "but I would lean less conservative than if you had actually slept badly."
        )
    if flags["forgot_fitbit"]:
        return (
            "That matters. If Fitbit missed the night, I do not want to overreact to the missing sleep data. "
            "Tell me whether you slept well, badly, or somewhere in the middle so I can coach today more honestly."
        )
    return (
        "Thanks, that gives me useful context. Sleep quality matters more than device completeness, so I want to factor your report in."
    )


def format_trends_reply(report: dict[str, Any]) -> str:
    averages = report["averages"]
    consistency = report["consistency"]
    notes = " ".join(report["coach_notes"])
    return (
        f"From {report['window']['start_date']} to {report['window']['end_date']}, average steps were {averages['steps']}, "
        f"average sleep was {averages['sleep']}, and average resting HR was {averages['resting_hr']}. "
        f"You hit the step goal {consistency['step_goal_hit_days']} of {report['window']['days']} days, which is {consistency['step_goal_consistency']} consistency. "
        f"{notes}"
    )


def format_fatloss_reply(report: dict[str, Any]) -> str:
    latest = report["latest"] or {}
    changes = report["changes"]
    notes = " ".join(report["coach_notes"])
    return (
        f"The fat-loss verdict is: {report['verdict']}. {report['summary']} "
        f"Latest weigh-in is {latest.get('weight_kg')} kg at {latest.get('fat_pct')}% body fat, "
        f"with estimated fat mass {latest.get('fat_mass_kg')} kg and lean mass {latest.get('lean_mass_kg')} kg. "
        f"Over this window, weight changed {changes['weight_kg']} kg, fat mass changed {changes['fat_mass_kg']} kg, "
        f"and lean mass changed {changes['lean_mass_kg']} kg. {notes}"
    )


def kg_to_lb(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 2.20462, 1)


def format_weekly_summary_reply(client: FitbitClient, target_date: str) -> str:
    trends = build_trends_report(client, target_date, 7)
    fatloss = build_fatloss_report(client, target_date, 7)
    previous_fatloss = build_previous_window_fatloss_report(client, target_date, 7)
    goal_summary = build_weekly_goal_summary(client, target_date, 7)
    latest = fatloss.get("latest") or {}
    changes = fatloss.get("changes") or {}
    latest_weight_lb = kg_to_lb(latest.get("weight_kg"))
    fat_change_lb = kg_to_lb(changes.get("fat_mass_kg"))
    lean_change_lb = kg_to_lb(changes.get("lean_mass_kg"))
    weight_change_lb = kg_to_lb(changes.get("weight_kg"))
    summary_parts = [
        f"Here’s the 7-day read: you averaged about {round(trends['averages']['steps'])} steps and {trends['averages']['sleep']} of sleep a night."
    ]

    if latest_weight_lb is not None:
        summary_parts.append(
            f"Your latest weigh-in was {latest_weight_lb} lb"
            + (
                f" at {latest.get('fat_pct')}% body fat."
                if latest.get("fat_pct") is not None
                else "."
            )
        )

    if weight_change_lb is not None:
        summary_parts.append(f"Scale change over the week was {weight_change_lb:+.1f} lb.")
    if fat_change_lb is not None and lean_change_lb is not None:
        summary_parts.append(
            f"Estimated fat mass changed {fat_change_lb:+.1f} lb and lean mass changed {lean_change_lb:+.1f} lb."
        )
    elif fat_change_lb is not None:
        summary_parts.append(f"Estimated fat mass changed {fat_change_lb:+.1f} lb.")

    if previous_fatloss and previous_fatloss.get("changes"):
        previous_changes = previous_fatloss["changes"]
        previous_fat = previous_changes.get("fat_mass_kg")
        previous_lean = previous_changes.get("lean_mass_kg")
        fat_change = changes.get("fat_mass_kg")
        lean_change = changes.get("lean_mass_kg")
        if fat_change is not None and previous_fat is not None:
            if fat_change < previous_fat:
                summary_parts.append("This week looks a little better than the previous one on fat-loss direction.")
            elif fat_change > previous_fat:
                summary_parts.append("This week looks a little softer than the previous one on fat-loss direction, so I care more about consistency than drama.")
        if lean_change is not None and previous_lean is not None and lean_change > previous_lean:
            summary_parts.append("Lean-mass protection looks a bit better this week than last week.")

    if fatloss["verdict"] == "mostly water noise":
        summary_parts.append("This still looks more like consistency work than dramatic fat-loss theater, which is fine.")
    else:
        verdict_map = {
            "fat loss with lean mass protected": "I’d call this a decent week for fat loss while keeping lean mass reasonably intact.",
            "mixed loss": "I see some fat-loss movement here, but I also want more attention on lean-mass protection.",
            "slow or unclear": "I would call this steady but not dramatic yet, which is still real progress if the direction holds.",
            "insufficient data": "We still need a little more body-comp history before I want to sound too certain.",
        }
        summary_parts.append(verdict_map.get(fatloss["verdict"], f"My fat-loss read this week is {fatloss['verdict']}."))

    if goal_summary["primary_goal_met_days"] >= 3:
        summary_parts.append(
            f"You hit your coach-set main goal {goal_summary['primary_goal_met_days']} out of 7 days, which is {goal_summary['consistency']} consistency."
        )
    else:
        summary_parts.append(
            f"You hit your coach-set main goal {goal_summary['primary_goal_met_days']} out of 7 days, so the improvement lever is still follow-through on the daily plan."
        )

    if goal_summary["exercise_goal_met_days"] >= 1:
        summary_parts.append(
            f"You fully hit the exercise goal on {goal_summary['exercise_goal_met_days']} of 7 days."
        )

    if goal_summary["recovery_goal_days"] >= 4:
        summary_parts.append(
            "This was a more recovery-biased week, so those goal hits were solid follow-through on a conservative plan rather than a big ambitious training week."
        )
    elif goal_summary["ambitious_goal_days"] >= 3:
        summary_parts.append(
            "This was a more ambitious training week, so the consistency is worth extra credit."
        )
    else:
        summary_parts.append(
            "This looked like more of a build-the-base week than a swing-for-the-fences week."
        )

    challenge_bits: list[str] = []
    avg_sleep_text = trends["averages"]["sleep"]
    avg_sleep_minutes = 0
    if avg_sleep_text and avg_sleep_text != "0m":
        hours_match = re.search(r"(\d+)h", avg_sleep_text)
        minutes_match = re.search(r"(\d+)m", avg_sleep_text)
        avg_sleep_minutes = (int(hours_match.group(1)) * 60 if hours_match else 0) + (int(minutes_match.group(1)) if minutes_match else 0)
    if avg_sleep_minutes and avg_sleep_minutes < 420:
        challenge_bits.append("sleep was not especially generous")
    short_sleep_days = 0
    elevated_rhr_days = 0
    softer_hrv_days = 0
    avg_rhr = trends["averages"].get("resting_hr")
    avg_hrv = trends["averages"].get("hrv_daily_rmssd")
    for item in trends.get("daily", []):
        sleep_text = item.get("sleep") or ""
        hours_match = re.search(r"(\d+)h", sleep_text)
        minutes_match = re.search(r"(\d+)m", sleep_text)
        sleep_minutes = (int(hours_match.group(1)) * 60 if hours_match else 0) + (int(minutes_match.group(1)) if minutes_match else 0)
        if sleep_minutes and sleep_minutes < 420:
            short_sleep_days += 1
        if avg_rhr is not None and item.get("resting_hr") is not None and item["resting_hr"] >= avg_rhr + 3:
            elevated_rhr_days += 1
        if avg_hrv is not None and item.get("hrv_daily_rmssd") is not None and item["hrv_daily_rmssd"] <= avg_hrv * 0.85:
            softer_hrv_days += 1

    if short_sleep_days >= 2:
        challenge_bits.append(f"there were {short_sleep_days} shorter-sleep nights")
    if elevated_rhr_days >= 2:
        challenge_bits.append("resting heart rate ran a little hot on multiple days")
    if softer_hrv_days >= 2:
        challenge_bits.append("HRV was softer than usual on multiple days")
    if fatloss["verdict"] in {"slow or unclear", "mixed loss", "mostly water noise"}:
        challenge_bits.append("body-comp progress was still a little muddy")
    if goal_summary["primary_goal_met_days"] < 7:
        challenge_bits.append("daily follow-through was not perfect")

    if challenge_bits:
        summary_parts.append(
            "The main challenge this week was that " + ", and ".join(challenge_bits) + "."
        )

    return " ".join(summary_parts)


def format_zepbound_reply(report: dict[str, Any]) -> str:
    latest = report["latest_entry"]
    last_dose = report["last_dose"] or {}
    notes = " ".join(report["coach_notes"])
    if report["days_since_last_dose"] == 0:
        timing = "Today is shot day."
    else:
        timing = f"That was {report['days_since_last_dose']} days ago."
    return (
        f"As of {report['date']}, your modeled Zepbound amount in system is {latest.get('estimated_amount_mg')} mg. "
        f"Last recorded dose was {last_dose.get('dose_administered_mg')} mg on {last_dose.get('date')}, "
        f"{timing} "
        f"The last logged note was {latest.get('note') or 'none'}. {notes}"
    )


def format_activity_observation_reply(client: FitbitClient, target_date: str) -> str:
    coach = build_coach_report(client, target_date)
    wins = build_daily_wins(client, target_date)
    stats = coach["stats"]
    step_goal = coach["goal_status"]["steps"]
    exercise_goal = coach["exercise_goal"]
    primary_goal = coach["primary_goal"]

    if primary_goal["kind"] == "recovery_walk":
        if primary_goal["status"] == "met":
            wins_line = f" Wins today: {', '.join(item['label'] for item in wins)}." if wins else ""
            return (
                f"Yes, and this is exactly the kind of win I want to notice. Yesterday already had real training, "
                f"so today’s right goal was a recovery walk, not more intensity. You got {primary_goal['actual']} "
                f"movement minutes, which clears the goal cleanly.{wins_line}"
            )
        if primary_goal["status"] == "close":
            return (
                f"Yes. Yesterday was already a training day, so today’s smart goal is a recovery walk. "
                f"You are close with {primary_goal['actual']} of {primary_goal['target']} movement minutes."
            )

    if stats["movement_minutes"] >= 30 or step_goal["status"] in {"met", "close"}:
        opener = "Yes, I noticed."
    elif stats["zone_minutes"] > 0:
        opener = "Yes, your data shows exercise."
    else:
        opener = "Not really yet."

    walk_line = (
        f"You logged {stats['movement_minutes']} movement minutes and {stats['steps']} steps, which looks like a real walk rather than accidental shuffling."
        if stats["movement_minutes"] >= 20
        else f"You have {stats['steps']} steps and {stats['movement_minutes']} movement minutes so far."
    )

    if step_goal["status"] == "met":
        goal_line = f"You met your step goal today: {step_goal['actual']} of {step_goal['target']}."
    elif stats["movement_minutes"] >= 30:
        goal_line = "That absolutely counts as a walking win today, even if it is not the full step goal."
    elif step_goal["status"] == "close":
        goal_line = f"You are close on the step goal at {step_goal['actual']} of {step_goal['target']}."
    else:
        goal_line = "It counts as useful movement, but not a formal goal hit yet."

    exercise_line = (
        f"Your combined exercise goal is also {exercise_goal['status'].replace('_', ' ')}, so the day counts even if the walk did not spike zone minutes."
        if exercise_goal["status"] in {"met", "close"}
        else "This still looks more like base movement than a full exercise hit, which is fine on a lighter day."
    )
    wins_line = f" Wins today: {', '.join(item['label'] for item in wins)}." if wins else ""
    return f"{opener} {walk_line} {goal_line} {exercise_line}{wins_line}"


def format_goal_check_reply(client: FitbitClient, target_date: str) -> str:
    coach = build_coach_report(client, target_date)
    wins = build_daily_wins(client, target_date)
    step_goal = coach["goal_status"]["steps"]
    exercise_goal = coach["exercise_goal"]
    primary_goal = coach["primary_goal"]

    if wins:
        win_line = ", ".join(item["label"] for item in wins)
        primary_line = (
            f"I set your main goal today as {primary_goal['label'].lower()} because {primary_goal['reason'].lower()}"
            if primary_goal["status"] == "met"
            else f"I want your main goal today to be {primary_goal['label'].lower()} because {primary_goal['reason'].lower()}"
        )
        return (
            f"Yes. {primary_line} I can already give you credit for these: {win_line}. "
            f"Steps are {step_goal['actual']} of {step_goal['target']}, and your {exercise_goal['label'].lower()} status is {exercise_goal['status'].replace('_', ' ')}."
        )

    return (
        f"Not yet. I want your main goal today to be {primary_goal['label'].lower()}, and the current score is "
        f"{primary_goal['actual']} of {primary_goal['target']} {primary_goal['unit']}. "
        f"Steps are {step_goal['actual']} of {step_goal['target']}, and your {exercise_goal['label'].lower()} status is "
        f"{exercise_goal['status'].replace('_', ' ')}."
    )


def format_goal_focus_reply(client: FitbitClient, target_date: str) -> str:
    coach = build_coach_report(client, target_date)
    primary_goal = coach["primary_goal"]
    exercise_goal = coach["exercise_goal"]
    step_goal = coach["goal_status"]["steps"]
    water = build_water_report(client, target_date)
    zepbound = build_zepbound_report(client, target_date)

    lines = [
        f"Your main goal today is {primary_goal['label'].lower()}: {primary_goal['reason']}",
        f"Exercise goal: {exercise_goal['label']} is {exercise_goal['status'].replace('_', ' ')}.",
        f"Step goal: {step_goal['actual']} of {step_goal['target']}.",
        f"Water goal: {water['total_oz']} oz of a {water['minimum_target_oz']}-{water['ideal_target_oz']} oz range.",
        f"Shot log: {zepbound['goal_status']['shot_logged']['summary']}.",
    ]
    if water["status"] == "behind":
        lines.append(
            "Water is still an active goal today. How is your drinking going so far?"
        )
    elif water["status"] == "close":
        lines.append(
            "Water is still open, but you are close. Tell me your total so far if it has changed."
        )
    return " ".join(lines)


def format_workout_log_reply(client: FitbitClient, prompt: str, target_date: str) -> str:
    parsed = parse_workout_log(prompt, target_date)
    if parsed is None:
        return "I couldn't cleanly tell what workout to log yet."
    client.add_workout_log(
        parsed["workout_date"],
        parsed["workout_name"],
        workout_category=parsed["workout_category"],
        source="chat",
        note=prompt.strip(),
    )
    recent = client.get_recent_workouts(limit=1)[0]
    return (
        f"Logged it. I have {recent['workout_name']} on {recent['workout_date']} as a "
        f"{recent.get('workout_category') or 'workout'} session. That will help me notice patterns like "
        f"whether this kind of workout stops wrecking you over time."
    )


def format_symptom_override_reply(client: FitbitClient, prompt: str, target_date: str) -> str:
    flags = detect_symptom_flags(prompt)
    recent_workouts = client.get_recent_workouts(limit=3)
    latest_workout = recent_workouts[0] if recent_workouts else None
    workout_line = (
        f" The most recent logged workout was {latest_workout['workout_name']} on {latest_workout['workout_date']}."
        if latest_workout
        else ""
    )
    if flags["very_sore"] or flags["stairs_pain"]:
        return (
            "This is a recovery day, not a character-building contest. "
            "If stairs are painful and you are very sore, I should not be nudging brisk walking or strength technique tonight. "
            "I want the goal now to be food, hydration, sleep, and only gentle movement if it actually makes you feel better."
            f"{workout_line}"
        )
    if flags["tired"] and flags["missed_activity"]:
        return (
            "You already have enough evidence that recovery gets veto power today. "
            "Missing the dance because you were wiped out is not a motivation problem; it is feedback. "
            "Tonight should be about recovery, not trying to earn your way back into the green."
            f"{workout_line}"
        )
    return (
        "Your symptoms matter more than me trying to sound brave. "
        "If soreness and fatigue are high, I want the plan to downgrade toward recovery even if your Fitbit numbers look respectable."
        f"{workout_line}"
    )


def format_today_plan_reply(client: FitbitClient, target_date: str) -> str:
    coach = build_coach_report(client, target_date)
    fatloss = build_fatloss_report(client, target_date, 30)
    zepbound = build_zepbound_report(client, target_date)
    wins = build_daily_wins(client, target_date)
    recommendation = build_training_recommendation(client, target_date)
    primary_goal = coach["primary_goal"]
    win_line = f" Wins today: {', '.join(item['label'] for item in wins)}." if wins else ""
    shot_line = zepbound["goal_status"]["shot_logged"]["summary"]
    return (
        f"Today is a {coach['readiness']} day. {coach['prescription']} "
        f"The main goal is {primary_goal['label'].lower()}: {primary_goal['reason']} "
        f"My stronger recommendation is {recommendation['primary'].lower()} because {recommendation['why']}. "
        f"Your 30-day fat-loss read is {fatloss['verdict']}, so the priority is protecting lean mass while staying consistent. "
        f"{shot_line}. You are {zepbound['days_since_last_dose']} days past your last Zepbound dose, with about "
        f"{zepbound['latest_entry']['estimated_amount_mg']} mg modeled in your system. "
        f"Action list: hit protein early, do some easy walking, and only push training if your body feels cooperative rather than negotiable.{win_line}"
    )


def extract_requested_future_activity(prompt: str) -> str | None:
    lowered = prompt.lower()
    patterns = [
        r"(trx class)",
        r"(strength class)",
        r"(dance class)",
        r"(orange theory(?: strength 50)?)",
        r"(bike ride)",
        r"(hike)",
        r"(dance)",
        r"(strength workout)",
        r"(workout)",
        r"(class)",
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return match.group(1)
    return None


def extract_requested_activity(prompt: str) -> str | None:
    return extract_requested_future_activity(prompt)


def detect_reentry_gap_days(client: FitbitClient, target_date: str) -> int | None:
    recent = client.read_recent_interactions(limit=5)
    if not recent:
        return None
    latest = recent[-1]
    latest_date = latest.get("date_context")
    if latest_date:
        try:
            return (
                datetime.strptime(target_date, "%Y-%m-%d").date()
                - datetime.strptime(latest_date, "%Y-%m-%d").date()
            ).days
        except ValueError:
            pass
    timestamp = latest.get("timestamp")
    if timestamp:
        try:
            latest_dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            current_dt = datetime.now(timezone.utc)
            return (current_dt.date() - latest_dt.date()).days
        except ValueError:
            return None
    return None


def format_reentry_reply(client: FitbitClient, prompt: str, target_date: str) -> str:
    lowered = prompt.lower()
    gap_days = detect_reentry_gap_days(client, target_date)
    coach = build_coach_report(client, target_date)
    reasons = build_readiness_reasons(coach)
    main_reason = reasons[0] if reasons else "recovery does not quite justify a green light"

    if "vacation" in lowered or "travel" in lowered or "traveling" in lowered:
        return (
            f"I’m glad you’re back. I’m reading today as {coach['readiness']} mostly because {main_reason}. "
            "If the sleep dip was vacation sleep instead of a real life-collapse situation, I do not want to overreact to it. "
            "If you’re home now, make tonight a catch-up sleep night and let me reassess tomorrow."
        )

    if "sick" in lowered or "ill" in lowered:
        return (
            f"I’m glad you’re back. I’m reading today as {coach['readiness']} mostly because {main_reason}. "
            "If the gap was illness, I want to be conservative until your energy and legs stop sounding suspicious. "
            "Tell me whether you were sick, traveling, on vacation, or just off-routine so I can coach the re-entry better."
        )

    if "yellow" in lowered or "why" in lowered:
        gap_line = (
            f" You were away for a few days, so before I get bossy I want to know whether that was vacation, travel, sickness, or just life."
            if gap_days is not None and gap_days >= 3
            else ""
        )
        sleep_line = (
            " If the short sleep was vacation-related and you’re home now, I want catch-up sleep tonight more than I want extra ambition."
            if coach["stats"].get("sleep_minutes", 0) < 420
            else ""
        )
        return (
            f"I’m glad you’re back. Today is {coach['readiness']} mostly because {main_reason}. "
            f"{coach['prescription']}{gap_line}{sleep_line}"
        )

    if gap_days is not None and gap_days >= 3:
        return (
            "I’m glad you’re back. Before I start pretending continuity, tell me whether you were sick, traveling, on vacation, or just off-routine. "
            "That changes how I should read the last few days and what I should ask from you tonight."
        )

    return (
        f"I’m glad you’re back. Today looks {coach['readiness']} to me, mostly because {main_reason}. "
        "If the recent gap had a reason like travel, sickness, or vacation sleep, tell me that and I’ll coach the re-entry like a grown-up."
    )


def format_tomorrow_plan_reply(client: FitbitClient, target_date: str) -> str:
    coach = build_coach_report(client, target_date)
    trends = build_trends_report(client, target_date, 7)
    fatloss = build_fatloss_report(client, target_date, 30)
    zepbound = build_zepbound_report(client, target_date)

    zone_minutes = coach["stats"]["zone_minutes"]
    sleep_efficiency = coach["stats"]["sleep_efficiency"] or 0
    days_since_shot = zepbound["days_since_last_dose"]

    if zone_minutes >= 45 and sleep_efficiency < 78:
        plan = "Aim for a recovery-biased day tomorrow: walking, mobility, and maybe light strength technique, but not a second hard hit."
    elif zone_minutes >= 45:
        plan = "Tomorrow can be a normal training day if you wake up feeling decent, but it does not need to be a hero day."
    elif coach["readiness"] in {"amber", "trained-but-watch-recovery"}:
        plan = "Tomorrow should focus on base building: protein, walking, and the simplest useful training option."
    else:
        plan = "Tomorrow is a good candidate for strength work or a purposeful aerobic session if recovery feels normal."

    hunger_note = (
        "You will also be moving farther from your Zepbound shot, so appetite may creep up and planning meals will matter more."
        if days_since_shot is not None and days_since_shot >= 4
        else "You are still fairly close to your Zepbound dose, so keep protein first and avoid under-eating after today's training."
    )

    trend_note = (
        "Your fat-loss trend still reads as mostly water noise, so tomorrow's win is consistency and lean-mass protection, not slash-and-burn dieting."
        if fatloss["verdict"] == "mostly water noise"
        else "Your trend is clearer, so tomorrow is mostly about staying steady rather than getting dramatic."
    )

    weekly_note = (
        f"Seven-day consistency is {trends['consistency']['step_goal_consistency']}, with average sleep at {trends['averages']['sleep']}."
    )

    return f"{plan} {hunger_note} {trend_note} {weekly_note}"


def format_tomorrow_activity_reply(client: FitbitClient, prompt: str, target_date: str) -> str:
    activity = extract_requested_future_activity(prompt) or "that session"
    coach = build_coach_report(client, target_date)
    trends = build_trends_report(client, target_date, 7)

    sleep_minutes = coach["stats"]["sleep_minutes"] or 0
    zone_minutes = coach["stats"]["zone_minutes"] or 0
    sleep_reason = f"sleep was only {coach['stats']['sleep']}" if coach["stats"]["sleep"] != "n/a" else "recovery still looks incomplete"

    if sleep_minutes < 420:
        return (
            f"Maybe, but I would treat {activity} as conditional, not automatic. "
            f"My hesitation is mostly that {sleep_reason}. If tonight is a real catch-up sleep night and you wake up feeling normal, "
            f"I’m okay with {activity} tomorrow. If you still feel wrung out, I’d swap it for walking or easier movement instead."
        )

    if zone_minutes >= 45:
        return (
            f"Probably not another hard {activity} tomorrow unless you wake up feeling unusually fresh. "
            "You already have a decent training load on the board, so I’d rather protect recovery than stack intensity for sport."
        )

    consistency = trends["consistency"]["step_goal_consistency"]
    return (
        f"Yes, {activity} is a reasonable plan for tomorrow if your body feels normal in the morning. "
        f"My read is that today is more yellow than red, and the main caution flag is {sleep_reason}. "
        f"If sleep improves tonight, I’m comfortable with it. Seven-day movement consistency is {consistency}, so the bigger game is staying steady, not perfect."
    )


def format_today_activity_reply(client: FitbitClient, prompt: str, target_date: str) -> str:
    activity = extract_requested_activity(prompt) or "that session"
    coach = build_coach_report(client, target_date)
    stats = coach["stats"]
    reasons = build_readiness_reasons(coach)
    caution_reasons = [
        reason
        for reason in reasons
        if "true exercise dose is still low" not in reason and "base movement" not in reason
    ]
    main_reason = caution_reasons[0] if caution_reasons else "I do not have a strong recovery argument against training today"
    lowered = activity.lower()
    higher_intensity = any(keyword in lowered for keyword in ["orange theory", "trx", "strength 50"])

    if coach["readiness"] in {"trained", "trained-but-watch-recovery"}:
        return (
            f"No, I would skip {activity} today. You already have enough exercise on the board, and I’d rather protect recovery than pile on another session."
        )

    if higher_intensity and coach["readiness"] == "yellow":
        if not caution_reasons:
            return (
                f"Yes, {activity} is reasonable today. "
                f"You have decent sleep, plenty of movement, and no strong recovery veto showing up. "
                "I would still treat it like a normal training day, not an excuse to chase your own funeral."
            )
        return (
            f"Maybe, but I would treat {activity} as optional rather than the smart default. "
            f"My hesitation is mostly that {main_reason}. With {stats['steps']} steps already and only {stats['zone_minutes']} zone minutes, "
            "I’m more comfortable with a moderate or technique-biased version than with an all-out effort."
        )

    if higher_intensity and coach["readiness"] == "amber":
        return (
            f"No, I would skip {activity} today. My main reason is {main_reason}, and amber is not the day for proving you have character."
        )

    return (
        f"Yes, {activity} is reasonable today if your body feels cooperative. "
        f"My main caution flag is that {main_reason}. I’d keep the effort honest but not theatrical."
    )


def format_strength_technique_reply() -> str:
    return (
        "When I say strength technique, I mean a lighter, cleaner strength session instead of a go-to-war session. "
        "Think: fewer sets, more rest, solid form, stopping with a rep or two in reserve, and treating the workout like practice rather than a test. "
        "In real life that means no hero weights, no chasing failure, and leaving the gym feeling better organized, not flattened."
    )


def format_recovery_plan_reply(client: FitbitClient, target_date: str) -> str:
    coach = build_coach_report(client, target_date)
    stats = coach["stats"]
    recommendation = build_training_recommendation(client, target_date)
    walk_line = (
        "You already have enough movement banked, so I do not need another formal walk target."
        if stats["steps"] >= 10000 or stats["movement_minutes"] >= 90
        else "If you want movement, keep it easy: a relaxed walk, gentle stretching, light yoga, or easy mobility is enough."
    )
    return (
        f"Recovery today would look like this: no hard class, no proving-a-point workout, protein on purpose, hydration handled, and an early-enough night to help tomorrow. "
        f"{walk_line} If you still want something useful, I would choose PT over a real training session. "
        f"My reason is that {recommendation['why']}."
    )


def format_time_crunch_reply(client: FitbitClient, target_date: str) -> str:
    recommendation = build_training_recommendation(client, target_date)
    coach = build_coach_report(client, target_date)
    stats = coach["stats"]

    if recommendation["primary"] == "Recovery":
        return (
            "You are short on time, so I would make this gloriously simple: skip the workout, eat protein on purpose, handle hydration, "
            "and set yourself up for a better night of sleep. That is the highest-return move today."
        )

    if recommendation["primary"] in {"PT or recovery", "PT or enough-for-today"}:
        return (
            "Since time is tight, I would choose PT or call the walk enough. You already have solid movement banked, and I do not think today needs a full second training stimulus."
        )

    if stats["steps"] >= 10000:
        return (
            "Because time is tight and you already have over 10K steps, I would either do a short PT session or call that enough for today. "
            "A full workout is optional, not mandatory."
        )

    return (
        "If you want the highest return on limited time, I would do the simplest useful thing: PT if you want structured work, or a real class/VR strength session only if you genuinely have the energy for it."
    )


def build_llm_context(client: FitbitClient, prompt: str, target_date: str) -> dict[str, Any]:
    topic = detect_topic(prompt)
    context: dict[str, Any] = {"date": target_date, "topic_hint": topic}

    try:
        context["coach"] = build_coach_report(client, target_date)
    except Exception as exc:  # noqa: BLE001
        context["coach_error"] = str(exc)

    try:
        context["zepbound"] = build_zepbound_report(client, target_date)
    except Exception as exc:  # noqa: BLE001
        context["zepbound_error"] = str(exc)

    if topic in {"water", "other"}:
        try:
            context["water"] = build_water_report(client, target_date)
        except Exception as exc:  # noqa: BLE001
            context["water_error"] = str(exc)

    if topic in {"fatloss", "tomorrow_plan", "today_plan", "other"}:
        try:
            context["fatloss"] = build_fatloss_report(client, target_date, 30)
        except Exception as exc:  # noqa: BLE001
            context["fatloss_error"] = str(exc)

    if topic in {"trends", "tomorrow_plan", "weekly_summary", "other"}:
        try:
            context["trends"] = build_trends_report(client, target_date, 7)
        except Exception as exc:  # noqa: BLE001
            context["trends_error"] = str(exc)

    if topic in {"weekly_summary", "other"}:
        try:
            context["weekly_summary_preview"] = format_weekly_summary_reply(client, target_date)
        except Exception as exc:  # noqa: BLE001
            context["weekly_summary_error"] = str(exc)

    try:
        context["recent_workouts"] = client.get_recent_workouts(limit=3)
    except Exception as exc:  # noqa: BLE001
        context["recent_workouts_error"] = str(exc)

    try:
        context["recent_interactions"] = client.read_recent_interactions(limit=5)
    except Exception as exc:  # noqa: BLE001
        context["recent_interactions_error"] = str(exc)

    return context


def llm_route_intent(client: FitbitClient, prompt: str, target_date: str) -> dict[str, Any]:
    context = build_llm_context(client, prompt, target_date)
    system_prompt = (
        "You are an intent router for a recovery-first coaching app. "
        "Classify the user's message into exactly one internal topic from this list: "
        "sleep_context, reentry, tomorrow_plan, today_plan, zepbound, fatloss, trends, weekly_summary, coach, "
        "activity_observation, goal_check, goal_focus, workout_log, symptom_override, water, "
        "recovery_plan, strength_technique, help, other. "
        "Return only compact JSON with keys: topic, activity, confidence, reason. "
        "activity may be null. confidence must be a number from 0 to 1. "
        "Use the provided structured context, but do not answer the user question."
    )
    user_prompt = (
        f"User question: {prompt}\n"
        f"Reference date: {target_date}\n"
        "Structured context:\n"
        f"{json.dumps(context, indent=2)}"
    )
    raw = client.openai_response(system_prompt, user_prompt)
    try:
        routed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise FitbitConfigError("OpenAI intent router did not return valid JSON.")
        routed = json.loads(match.group(0))
    return {
        "topic": str(routed.get("topic") or "other"),
        "activity": routed.get("activity"),
        "confidence": float(routed.get("confidence") or 0),
        "reason": str(routed.get("reason") or ""),
    }


def llm_answer_chat(client: FitbitClient, prompt: str, target_date: str) -> str:
    context = build_llm_context(client, prompt, target_date)
    system_prompt = (
        "You are an elite recovery-first personal trainer and longevity coach for women’s health, "
        "with special focus on fat loss, sarcopenia prevention over age 60, and GLP-1 support. "
        "Speak in first person and sound like one coach with a real personality, not a dashboard. "
        "Be supportive, decisive, witty, and data-driven. Use only the provided context. "
        "If data is missing or rate-limited, say so plainly. Do not invent metrics or medical claims. "
        "Prefer practical coaching actions over generic advice."
    )
    user_prompt = (
        f"User question: {prompt}\n"
        f"Reference date: {target_date}\n"
        "Structured context:\n"
        f"{json.dumps(context, indent=2)}"
    )
    return client.openai_response(system_prompt, user_prompt)


def answer_chat(client: FitbitClient, prompt: str, target_date: str) -> str:
    text = prompt.strip().lower()
    if detect_topic(text) == "weekly_summary":
        return format_weekly_summary_reply(client, target_date)
    routed_activity = None
    if client.config.openai_api_key:
        try:
            routed = llm_route_intent(client, prompt, target_date)
            topic = routed["topic"]
            routed_activity = routed.get("activity")
        except Exception:
            topic = detect_topic(text)
    else:
        topic = detect_topic(text)
    if topic == "sleep_context":
        return format_sleep_context_reply(client, prompt, target_date)
    if topic == "reentry":
        return format_reentry_reply(client, prompt, target_date)
    if topic == "tomorrow_plan":
        if "ok to" in text or "okay to" in text or "can i" in text or routed_activity:
            routed_prompt = f"{prompt}\nActivity: {routed_activity}" if routed_activity else prompt
            return apply_recent_sleep_context(client, format_tomorrow_activity_reply(client, routed_prompt, target_date), target_date)
        return apply_recent_sleep_context(client, format_tomorrow_plan_reply(client, target_date), target_date)
    if topic == "today_plan":
        if "ok to" in text or "okay to" in text or "can i" in text or "should i go to" in text or "should i try to make it to" in text or routed_activity:
            routed_prompt = f"{prompt}\nActivity: {routed_activity}" if routed_activity else prompt
            return apply_recent_sleep_context(client, format_today_activity_reply(client, routed_prompt, target_date), target_date)
        return apply_recent_sleep_context(client, format_today_plan_reply(client, target_date), target_date)
    if topic == "time_crunch":
        return apply_recent_sleep_context(client, format_time_crunch_reply(client, target_date), target_date)
    if topic == "recovery_plan":
        return apply_recent_sleep_context(client, format_recovery_plan_reply(client, target_date), target_date)
    if topic == "strength_technique":
        return format_strength_technique_reply()
    if topic == "zepbound":
        return format_zepbound_reply(build_zepbound_report(client, target_date))
    if topic == "fatloss":
        return format_fatloss_reply(build_fatloss_report(client, target_date, 30))
    if topic == "weekly_summary":
        return format_weekly_summary_reply(client, target_date)
    if topic == "trends":
        return format_trends_reply(build_trends_report(client, target_date, 7))
    if topic == "coach":
        return apply_recent_sleep_context(client, format_coach_reply(build_coach_report(client, target_date)), target_date)
    if topic == "activity_observation":
        return format_activity_observation_reply(client, target_date)
    if topic == "goal_check":
        return format_goal_check_reply(client, target_date)
    if topic == "goal_focus":
        return format_goal_focus_reply(client, target_date)
    if topic == "workout_log":
        return format_workout_log_reply(client, prompt, target_date)
    if topic == "symptom_override":
        workout_reply = ""
        if parse_workout_log(prompt, target_date) is not None:
            workout_reply = format_workout_log_reply(client, prompt, target_date) + " "
        return workout_reply + format_symptom_override_reply(client, prompt, target_date)
    if topic == "water":
        amount_oz = parse_water_oz(text)
        if amount_oz is not None:
            return handle_water_sms_reply(client, prompt.strip(), target_date)
        return format_water_reply(build_water_report(client, target_date))
    if topic == "help":
        return (
            "Ask me things like 'What should I do today?', 'What should I aim to do tomorrow?', "
            "'How is my 7-day trend?', 'Am I losing fat or lean mass?', 'Did you notice I took a walk?', "
            "'Did I meet my goals today?', 'What goals am I working on?', or 'How is water going?'"
        )
    if topic == "empty":
        return "Ask about training readiness, weekly trends, fat loss, body composition, water, or today."
    if client.config.openai_api_key:
        try:
            return llm_answer_chat(client, prompt, target_date)
        except Exception:
            pass
    return "I didn't map that cleanly yet. Ask me about today, training readiness, weekly trends, fat loss, water, or body composition."


def detect_topic(text: str) -> str:
    text = text.strip().lower()
    if not text:
        return "empty"
    sleep_flags = detect_manual_sleep_context(text)
    if sleep_flags["forgot_fitbit"] or ("sleep" in text and sleep_flags["slept_well"]):
        return "sleep_context"
    if any(
        phrase in text
        for phrase in [
            "i'm back",
            "im back",
            "hi i'm back",
            "hi im back",
            "i was away",
            "i've been away",
            "ive been away",
            "back from vacation",
            "back from travel",
            "i was on vacation",
            "i was traveling",
            "i was sick",
            "i am home now",
            "i'm home now",
        ]
    ):
        return "reentry"
    if any(
        phrase in text
        for phrase in [
            "did you notice i took a walk",
            "did you notice my walk",
            "did you see my walk",
            "did you notice i went for a walk",
            "did you notice i exercised",
            "did you notice i worked out",
            "did you notice i was active",
            "did you see i was active",
            "did you notice i took a class",
            "did you notice i did an exercise class",
        ]
    ):
        return "activity_observation"
    if any(
        phrase in text
        for phrase in [
            "did i meet my goals",
            "have i met my goals",
            "did i hit my goals",
            "what goals did i meet",
            "what goals am i working on",
            "what goals am i working toward",
            "what are my goals",
            "what am i working on",
            "what should i be working on",
            "what have i already done well",
            "what wins do i have",
            "wins today",
        ]
    ):
        if any(
            phrase in text
            for phrase in [
                "what goals am i working on",
                "what goals am i working toward",
                "what are my goals",
                "what am i working on",
                "what should i be working on",
            ]
        ):
            return "goal_focus"
        return "goal_check"
    if any(word in text for word in ["sore", "stairs", "painful", "exhausted", "fatigued"]) and any(
        word in text for word in ["i did", "i took", "workout", "class", "dance", "tired", "didn't go", "did not go", "skipped"]
    ):
        return "symptom_override"
    if parse_workout_log(text, date.today().isoformat()) is not None:
        return "workout_log"
    if any(
        phrase in text
        for phrase in [
            "what should i aim to do tomorrow",
            "what should i do tomorrow",
            "plan for tomorrow",
            "tomorrow plan",
            "what about tomorrow",
            "how about tomorrow",
            "so coach, how about tomorrow",
            "coach, how about tomorrow",
            "for tomorrow",
            "what is tomorrows plan",
            "what's tomorrows plan",
            "what is tomorrow's plan",
            "what's tomorrow's plan",
            "am i ok to",
            "am i okay to",
            "can i go to",
            "can i do",
            "should i do",
        ]
    ):
        if "tomorrow" in text:
            return "tomorrow_plan"
    if "tomorrow" in text and any(word in text for word in ["class", "trx", "dance", "workout", "train", "strength", "bike", "hike"]):
        return "tomorrow_plan"
    if any(phrase in text for phrase in ["what should i do today", "today plan", "plan for today", "what now"]):
        return "today_plan"
    if "today" in text and any(word in text for word in ["orange theory", "trx", "class", "workout"]):
        return "today_plan"
    if any(phrase in text for phrase in ["should i go to", "should i try to make it to", "can i go to", "can i do", "am i ok to", "am i okay to"]) and (
        "today" in text or any(word in text for word in ["orange theory", "trx", "class", "workout"])
    ):
        return "today_plan"
    if "should i try to make it" in text and any(word in text for word in ["orange theory", "trx", "class", "workout"]):
        return "today_plan"
    if "strength technique" in text:
        return "strength_technique"
    if any(
        phrase in text
        for phrase in [
            "running short on time",
            "short on time",
            "not much time today",
            "pressed for time",
            "what should i do if i'm short on time",
            "only have a few minutes",
            "only have a few min",
            "i only have a few minutes",
            "i only have a few min",
            "don't have much time",
            "do not have much time",
        ]
    ):
        return "time_crunch"
    if any(
        phrase in text
        for phrase in [
            "what would recovery look like",
            "what does recovery look like",
            "what should recovery look like",
            "what does a recovery day look like",
            "what would a recovery day look like",
            "what do you suggest for recovery",
            "what do you suggest for a recovery day",
            "what should i do for recovery",
            "what do you recommend for recovery",
            "what sort of recovery",
            "what kind of recovery",
            "what kind of recovery day",
        ]
    ):
        return "recovery_plan"
    if "water" in text or "hydration" in text or parse_water_oz(text) is not None:
        return "water"
    if any(word in text for word in ["zepbound", "shot", "dose", "dosing", "glp", "medication"]):
        return "zepbound"
    if any(word in text for word in ["fat", "lean", "body comp", "bodycomp", "muscle"]):
        return "fatloss"
    if any(
        phrase in text
        for phrase in [
            "weekly summary",
            "summary of my progress for the last 7 days",
            "summary of my progress for the past 7 days",
            "summary for my weightloss group",
            "summary for my weight loss group",
            "weekly report for my facebook weightloss group",
            "weekly report for my facebook weight loss group",
            "writing my weekly report for my facebook weightloss group",
            "writing my weekly report for my facebook weight loss group",
            "weekly report for my weightloss group",
            "weekly report for my weight loss group",
            "facebook summary",
        ]
    ):
        return "weekly_summary"
    if any(word in text for word in ["trend", "week", "7 day", "consistency", "average"]):
        return "trends"
    if any(word in text for word in ["train", "today", "recovery", "ready", "workout"]):
        return "coach"
    if any(word in text for word in ["help", "options", "what can you do"]):
        return "help"
    return "other"


def run_chat(client: FitbitClient, target_date: str) -> None:
    print("Fitbit coach chat is live. Ask about today, trends, or fat loss. Type 'exit' to leave.")
    while True:
        try:
            prompt = input("you> ").strip()
        except EOFError:
            print()
            break
        if prompt.lower() in {"exit", "quit"}:
            break
        reply = answer_chat(client, prompt, target_date)
        client.append_interaction(
            {
                "source": "terminal-chat",
                "topic": detect_topic(prompt.strip().lower()),
                "date_context": target_date,
                "message": prompt,
                "reply": reply,
            },
        )
        print(f"coach> {reply}")


def run_zepbound(client: FitbitClient, target_date: str) -> None:
    print(json.dumps(build_zepbound_report(client, target_date), indent=2))


def callback_server() -> tuple[HTTPServer, dict[str, str]]:
    state: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            state["code"] = query.get("code", [""])[0]
            state["state"] = query.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Fitbit authorization received.</h1><p>You can close this tab.</p></body></html>"
            )

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    server = HTTPServer(("127.0.0.1", 8765), Handler)
    return server, state


def run_auth(client: FitbitClient, open_browser: bool) -> None:
    expected_state = secrets.token_urlsafe(24)
    auth_url = client.build_auth_url(expected_state)
    print(f"Open this URL to authorize Fitbit access:\n{auth_url}\n")

    server, server_state = callback_server()
    worker = threading.Thread(target=server.handle_request, daemon=True)
    worker.start()

    if open_browser:
        webbrowser.open(auth_url)

    worker.join(timeout=180)
    server.server_close()

    code = server_state.get("code")
    returned_state = server_state.get("state")
    if not code:
        raise RuntimeError("Authorization code was not received before timeout.")
    if returned_state != expected_state:
        raise RuntimeError("OAuth state mismatch. Refusing to continue.")

    payload = client.exchange_code(code)
    user_id = payload.get("user_id", "unknown")
    print(f"Authorization complete. Tokens saved for Fitbit user {user_id}.")


def run_refresh(client: FitbitClient) -> None:
    payload = client.refresh_access_token()
    print(json.dumps({"user_id": payload.get("user_id"), "saved_at": payload.get("saved_at")}, indent=2))


def run_summary(client: FitbitClient, target_date: str) -> None:
    print(json.dumps(get_day_snapshot(client, target_date), indent=2))


def run_intraday(client: FitbitClient, metric: str, target_date: str) -> None:
    if metric == "steps":
        endpoint = f"/1/user/-/activities/steps/date/{target_date}/1d/1min.json"
    elif metric == "heartrate":
        endpoint = f"/1/user/-/activities/heart/date/{target_date}/1d/1min.json"
    else:
        raise ValueError(f"Unsupported metric: {metric}")
    print(json.dumps(client.get_json(endpoint), indent=2))


def run_coach(client: FitbitClient, target_date: str) -> None:
    print(json.dumps(build_coach_report(client, target_date), indent=2))


def run_trends(client: FitbitClient, end_date: str, days: int) -> None:
    print(json.dumps(build_trends_report(client, end_date, days), indent=2))


def run_fatloss(client: FitbitClient, end_date: str, days: int) -> None:
    print(json.dumps(build_fatloss_report(client, end_date, days), indent=2))


def run_water(client: FitbitClient, target_date: str, warm_day: bool) -> None:
    print(json.dumps(build_water_report(client, target_date, warm_day=warm_day), indent=2))


def run_log_water(client: FitbitClient, target_date: str, amount_oz: float, source: str, note: str | None) -> None:
    client.add_water_intake(target_date, amount_oz, source=source, note=note)
    print(json.dumps(build_water_report(client, target_date), indent=2))


def run_water_reminder(client: FitbitClient, target_date: str, window: str, warm_day: bool, send: bool) -> None:
    reminder = build_water_sms_prompt(client, target_date, window=window, warm_day=warm_day)
    payload: dict[str, Any] = {
        "message": reminder["message"],
        "window": window,
        "date": target_date,
        "sent": False,
        "send": reminder["send"],
        "reason": reminder["reason"],
    }
    if send and reminder["send"] and reminder["message"]:
        sms_payload = client.send_sms(reminder["message"])
        payload["sent"] = True
        payload["sid"] = sms_payload.get("sid")
    print(json.dumps(payload, indent=2))


def run_water_reply(client: FitbitClient, target_date: str, body: str) -> None:
    reply = handle_water_sms_reply(client, body, target_date)
    print(json.dumps({"reply": reply, "report": build_water_report(client, target_date)}, indent=2))


def run_scheduler(client: FitbitClient, send: bool, at_iso: str | None) -> None:
    now = datetime.fromisoformat(at_iso) if at_iso else datetime.now().astimezone()
    print(json.dumps(run_due_scheduler_cycle(client, now=now, send=send), indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fitbit CLI for recovery-first coaching workflows")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="Run OAuth flow and save tokens locally")
    auth_parser.add_argument("--no-browser", action="store_true", help="Print URL only, do not open a browser")

    subparsers.add_parser("refresh", help="Refresh access token and update local token store")

    summary_parser = subparsers.add_parser("summary", help="Fetch daily activity, sleep, and profile data")
    summary_parser.add_argument("--date", default=str(date.today()), help="Target date in YYYY-MM-DD format")

    intraday_parser = subparsers.add_parser("intraday", help="Fetch intraday steps or heart-rate data")
    intraday_parser.add_argument("metric", choices=["steps", "heartrate"])
    intraday_parser.add_argument("--date", default=str(date.today()), help="Target date in YYYY-MM-DD format")

    coach_parser = subparsers.add_parser("coach", help="Return a plain-English daily coaching summary")
    coach_parser.add_argument("--date", default=str(date.today()), help="Target date in YYYY-MM-DD format")

    trends_parser = subparsers.add_parser("trends", help="Return a rolling trend report for coaching signals")
    trends_parser.add_argument("--date", default=str(date.today()), help="End date in YYYY-MM-DD format")
    trends_parser.add_argument("--days", type=int, default=7, help="Number of days to include")

    bodycomp_parser = subparsers.add_parser("bodycomp", help="Return weight, body fat percentage, and estimated lean mass trends")
    bodycomp_parser.add_argument("--date", default=str(date.today()), help="End date in YYYY-MM-DD format")
    bodycomp_parser.add_argument("--days", type=int, default=30, help="Number of days to include")

    fatloss_parser = subparsers.add_parser("fatloss", help="Interpret fat-loss versus lean-mass trends")
    fatloss_parser.add_argument("--date", default=str(date.today()), help="End date in YYYY-MM-DD format")
    fatloss_parser.add_argument("--days", type=int, default=30, help="Number of days to include")

    zepbound_parser = subparsers.add_parser("zepbound", help="Read Zepbound dosing history from a public Google Sheet")
    zepbound_parser.add_argument("--date", default=str(date.today()), help="Reference date in YYYY-MM-DD format")

    chat_parser = subparsers.add_parser("chat", help="Start a conversational coach interface in the terminal")
    chat_parser.add_argument("--date", default=str(date.today()), help="Reference date in YYYY-MM-DD format")

    water_parser = subparsers.add_parser("water", help="Show hydration progress and target range")
    water_parser.add_argument("--date", default=str(date.today()), help="Reference date in YYYY-MM-DD format")
    water_parser.add_argument("--warm-day", action="store_true", help="Apply the warm-weather hydration bonus")

    log_water_parser = subparsers.add_parser("log-water", help="Log a water intake entry")
    log_water_parser.add_argument("amount_oz", type=float, help="Water amount in ounces")
    log_water_parser.add_argument("--date", default=str(date.today()), help="Reference date in YYYY-MM-DD format")
    log_water_parser.add_argument("--source", default="manual", help="Source label for this entry")
    log_water_parser.add_argument("--note", default=None, help="Optional note stored with the entry")

    water_reminder_parser = subparsers.add_parser("water-reminder", help="Build or send a water reminder SMS")
    water_reminder_parser.add_argument("--date", default=str(date.today()), help="Reference date in YYYY-MM-DD format")
    water_reminder_parser.add_argument("--window", choices=["noon", "evening"], required=True)
    water_reminder_parser.add_argument("--warm-day", action="store_true", help="Apply the warm-weather hydration bonus")
    water_reminder_parser.add_argument("--send", action="store_true", help="Send the reminder via Twilio instead of only printing it")

    water_reply_parser = subparsers.add_parser("water-reply", help="Parse an SMS-style water reply and log it")
    water_reply_parser.add_argument("body", help="Incoming message body, for example '24 oz'")
    water_reply_parser.add_argument("--date", default=str(date.today()), help="Reference date in YYYY-MM-DD format")

    scheduler_parser = subparsers.add_parser("run-scheduler", help="Run one scheduler cycle for due reminders")
    scheduler_parser.add_argument("--send", action="store_true", help="Actually send due reminders instead of dry-run logging")
    scheduler_parser.add_argument("--at", default=None, help="Optional ISO datetime for testing, for example 2026-04-09T12:00:00-04:00")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = FitbitConfig.from_env()
    client = FitbitClient(config)

    if args.command == "auth":
        run_auth(client, open_browser=not args.no_browser)
    elif args.command == "refresh":
        run_refresh(client)
    elif args.command == "summary":
        run_summary(client, args.date)
    elif args.command == "intraday":
        run_intraday(client, args.metric, args.date)
    elif args.command == "coach":
        run_coach(client, args.date)
    elif args.command == "trends":
        run_trends(client, args.date, args.days)
    elif args.command == "bodycomp":
        run_bodycomp(client, args.date, args.days)
    elif args.command == "fatloss":
        run_fatloss(client, args.date, args.days)
    elif args.command == "zepbound":
        run_zepbound(client, args.date)
    elif args.command == "chat":
        run_chat(client, args.date)
    elif args.command == "water":
        run_water(client, args.date, args.warm_day)
    elif args.command == "log-water":
        run_log_water(client, args.date, args.amount_oz, args.source, args.note)
    elif args.command == "water-reminder":
        run_water_reminder(client, args.date, args.window, args.warm_day, args.send)
    elif args.command == "water-reply":
        run_water_reply(client, args.date, args.body)
    elif args.command == "run-scheduler":
        run_scheduler(client, args.send, args.at)
    else:
        raise ValueError(f"Unknown command {args.command}")


if __name__ == "__main__":
    main()
