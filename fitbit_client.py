#!/usr/bin/env python3
"""Minimal Fitbit API client for personal coaching workflows."""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests

from interaction_log import DEFAULT_LOG_PATH, append_interaction


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
    token_path: Path
    zepbound_sheet_url: str | None = None
    interaction_log_path: Path = DEFAULT_LOG_PATH

    @classmethod
    def from_env(cls) -> "FitbitConfig":
        client_id = os.getenv("FITBIT_CLIENT_ID", "").strip()
        client_secret = os.getenv("FITBIT_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("FITBIT_REDIRECT_URI", "http://127.0.0.1:8765/callback").strip()
        token_path = Path(os.getenv("FITBIT_TOKEN_PATH", ".fitbit_tokens.json")).expanduser()
        zepbound_sheet_url = os.getenv("ZEPBOUND_SHEET_URL", "").strip() or None
        interaction_log_path = Path(
            os.getenv("COACH_INTERACTION_LOG_PATH", str(DEFAULT_LOG_PATH))
        ).expanduser()

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
            token_path=token_path,
            zepbound_sheet_url=zepbound_sheet_url,
            interaction_log_path=interaction_log_path,
        )


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise FitbitConfigError(
                f"Token file not found at {self.path}. Run the 'auth' command first."
            )
        return json.loads(self.path.read_text())

    def save(self, payload: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))


class FitbitClient:
    def __init__(self, config: FitbitConfig) -> None:
        self.config = config
        self.tokens = TokenStore(config.token_path)

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
        return fresh_payload

    def access_token(self) -> str:
        payload = self.refresh_access_token()
        token = payload.get("access_token")
        if not token:
            raise FitbitConfigError("No access_token returned by Fitbit.")
        return token

    def get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.access_token()
        response = requests.get(
            f"{API_BASE_URL}{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_text(self, url: str) -> str:
        response = requests.get(
            url,
            timeout=30,
        )
        response.raise_for_status()
        return response.text


def get_day_snapshot(client: FitbitClient, target_date: str) -> dict[str, Any]:
    activity = client.get_json(f"/1/user/-/activities/date/{target_date}.json")
    sleep = client.get_json(f"/1.2/user/-/sleep/date/{target_date}.json")
    profile = client.get_json("/1/user/-/profile.json")
    active_zone = client.get_json(f"/1/user/-/activities/active-zone-minutes/date/{target_date}/1d.json")
    return {
        "date": target_date,
        "daily_activity": activity,
        "active_zone_minutes": active_zone,
        "sleep": sleep,
        "profile": profile,
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
    azm_entry = (
        snapshot.get("active_zone_minutes", {})
        .get("activities-active-zone-minutes", [{}])[0]
        .get("value", {})
    )
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


def build_coach_report(client: FitbitClient, target_date: str) -> dict[str, Any]:
    day = summarize_day(get_day_snapshot(client, target_date))
    coaching = coach_day(day)
    return {
        "date": target_date,
        "readiness": coaching["readiness"],
        "prescription": coaching["prescription"],
        "stats": {
            "steps": day["steps"],
            "step_goal": day["step_goal"],
            "step_goal_pct": day["step_goal_pct"],
            "active_minutes": day["active_minutes"],
            "light_minutes": day["light_minutes"],
            "movement_minutes": day["movement_minutes"],
            "sedentary_minutes": day["sedentary_minutes"],
            "resting_hr": day["resting_hr"],
            "zone_minutes": day["zone_minutes"],
            "fat_burn_zone_minutes": day["fat_burn_zone_minutes"],
            "cardio_zone_minutes": day["cardio_zone_minutes"],
            "peak_zone_minutes": day["peak_zone_minutes"],
            "sleep": format_minutes(day["sleep_minutes"]),
            "time_in_bed": format_minutes(day["time_in_bed_minutes"]),
            "sleep_efficiency": day["sleep_efficiency"],
        },
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
    hit_step_goal_days = sum(1 for item in snapshots if (item["step_goal_pct"] or 0) >= 100)
    avg_steps = round(sum(steps) / len(steps), 1) if steps else 0
    avg_sleep = round(sum(sleep_minutes) / len(sleep_minutes)) if sleep_minutes else 0
    avg_rhr = round(sum(resting_hrs) / len(resting_hrs), 1) if resting_hrs else None
    consistency = "strong" if hit_step_goal_days >= 5 else "fair" if hit_step_goal_days >= 3 else "needs work"

    coach_notes: list[str] = []
    if avg_sleep < 390:
        coach_notes.append("Average sleep is too thin to support great recovery, appetite control, and muscle retention.")
    if avg_steps < 5000:
        coach_notes.append("Movement volume is low for a fat-loss phase, so daily walking is the highest-return lever.")
    if avg_rhr is not None and snapshots[-1]["resting_hr"] is not None and snapshots[-1]["resting_hr"] >= avg_rhr + 3:
        coach_notes.append("Today’s resting heart rate is meaningfully above your 7-day average, so recovery gets veto power.")
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
                "active_minutes": item["active_minutes"],
            }
            for item in snapshots
        ],
        "coach_notes": coach_notes,
    }


def build_fatloss_report(client: FitbitClient, end_date: str, days: int) -> dict[str, Any]:
    bodycomp = build_bodycomp_report(client, end_date, days)
    daily = bodycomp["daily"]
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

    return {
        "date": target_date,
        "latest_entry": latest,
        "last_dose": last_dose,
        "days_since_last_dose": days_since_last_dose,
        "days_between_last_two_doses": days_between_last_two,
        "coach_notes": coach_notes,
        "source_csv_url": csv_url,
    }


def format_coach_reply(report: dict[str, Any]) -> str:
    stats = report["stats"]
    notes = " ".join(report["notes"])
    return (
        f"{report['date']}: readiness is {report['readiness']}. {report['prescription']} "
        f"Steps are {stats['steps']} of {stats['step_goal']} ({stats['step_goal_pct']}%), "
        f"zone minutes are {stats['zone_minutes']} "
        f"({stats['fat_burn_zone_minutes']} fat burn, {stats['cardio_zone_minutes']} cardio, {stats['peak_zone_minutes']} peak), "
        f"sleep is {stats['sleep']} with efficiency {stats['sleep_efficiency']}, and resting HR is {stats['resting_hr']}. "
        f"{notes}"
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


def format_zepbound_reply(report: dict[str, Any]) -> str:
    latest = report["latest_entry"]
    last_dose = report["last_dose"] or {}
    notes = " ".join(report["coach_notes"])
    return (
        f"As of {report['date']}, your modeled Zepbound amount in system is {latest.get('estimated_amount_mg')} mg. "
        f"Last recorded dose was {last_dose.get('dose_administered_mg')} mg on {last_dose.get('date')}, "
        f"which is {report['days_since_last_dose']} days ago. "
        f"The last logged note was {latest.get('note') or 'none'}. {notes}"
    )


def format_today_plan_reply(client: FitbitClient, target_date: str) -> str:
    coach = build_coach_report(client, target_date)
    fatloss = build_fatloss_report(client, target_date, 30)
    zepbound = build_zepbound_report(client, target_date)
    return (
        f"Today is a {coach['readiness']} day. {coach['prescription']} "
        f"Your 30-day fat-loss read is {fatloss['verdict']}, so the priority is protecting lean mass while staying consistent. "
        f"You are {zepbound['days_since_last_dose']} days past your last Zepbound dose, with about "
        f"{zepbound['latest_entry']['estimated_amount_mg']} mg modeled in your system. "
        "Action list: hit protein early, do some easy walking, and only push training if your body feels cooperative rather than negotiable."
    )


def answer_chat(client: FitbitClient, prompt: str, target_date: str) -> str:
    text = prompt.strip().lower()
    topic = detect_topic(text)
    if topic == "today_plan":
        return format_today_plan_reply(client, target_date)
    if topic == "zepbound":
        return format_zepbound_reply(build_zepbound_report(client, target_date))
    if topic == "fatloss":
        return format_fatloss_reply(build_fatloss_report(client, target_date, 30))
    if topic == "trends":
        return format_trends_reply(build_trends_report(client, target_date, 7))
    if topic == "coach":
        return format_coach_reply(build_coach_report(client, target_date))
    if topic == "help":
        return (
            "Try: 'Can I train today?', 'How is my 7-day trend?', "
            "'Am I losing fat or lean mass?', or 'Give me the body comp read.'"
        )
    if topic == "empty":
        return "Ask about training readiness, weekly trends, fat loss, body composition, or today."
    return (
        "I didn't map that cleanly yet. Ask about today, training readiness, weekly trends, fat loss, or body composition."
    )


def detect_topic(text: str) -> str:
    text = text.strip().lower()
    if not text:
        return "empty"
    if any(phrase in text for phrase in ["what should i do today", "today plan", "plan for today", "what now"]):
        return "today_plan"
    if any(word in text for word in ["zepbound", "shot", "dose", "dosing", "glp", "medication"]):
        return "zepbound"
    if any(word in text for word in ["fat", "lean", "body comp", "bodycomp", "muscle"]):
        return "fatloss"
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
        append_interaction(
            client.config.interaction_log_path,
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
    else:
        raise ValueError(f"Unknown command {args.command}")


if __name__ == "__main__":
    main()
