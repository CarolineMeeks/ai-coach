#!/usr/bin/env python3
"""Local web app for the Fitbit recovery-first coach."""

from __future__ import annotations

import argparse
import json
import traceback
import secrets
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fitbit_client import FitbitClient, FitbitConfig, answer_chat, build_coach_report, build_fatloss_report, build_trends_report, build_zepbound_report, detect_topic


STATIC_DIR = Path(__file__).with_name("web")
OAUTH_STATES: set[str] = set()


def load_status_payload(client: FitbitClient, target_date: str) -> dict:
    client.reset_cache_events()
    coach = build_coach_report(client, target_date)
    trends = build_trends_report(client, target_date, 7)
    fatloss = build_fatloss_report(client, target_date, 30)
    zepbound = build_zepbound_report(client, target_date)
    cache_events = client.consume_cache_events()
    used_stale = any(event["kind"] == "stale" for event in cache_events)
    used_cache = any(event["kind"] == "cache" for event in cache_events)
    return {
        "date": target_date,
        "coach": coach,
        "trends": trends,
        "fatloss": fatloss,
        "zepbound": zepbound,
        "cache_status": {
            "used_cache": used_cache,
            "used_stale": used_stale,
            "message": (
                "Using cached Fitbit data because the API is temporarily rate-limited."
                if used_stale
                else "Using recently cached Fitbit data to keep the app fast and rate-limit friendly."
                if used_cache
                else "Using fresh Fitbit data."
            ),
        },
    }


def with_cache_status(client: FitbitClient, payload: dict) -> dict:
    cache_events = client.consume_cache_events()
    used_stale = any(event["kind"] == "stale" for event in cache_events)
    used_cache = any(event["kind"] == "cache" for event in cache_events)
    return {
        **payload,
        "cache_status": {
            "used_cache": used_cache,
            "used_stale": used_stale,
            "message": (
                "Using cached Fitbit data because the API is temporarily rate-limited."
                if used_stale
                else "Using recently cached Fitbit data to keep the app fast and rate-limit friendly."
                if used_cache
                else "Using fresh Fitbit data."
            ),
        },
    }


def make_handler(client: FitbitClient):
    class CoachHandler(BaseHTTPRequestHandler):
        def _log(self, message: str) -> None:
            print(message, flush=True)

        def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            route = parsed.path
            query = parse_qs(parsed.query)
            target_date = query.get("date", [str(date.today())])[0]
            self._log(f"GET {route} date={target_date}")

            if route == "/":
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            if route == "/connect-fitbit":
                state = secrets.token_urlsafe(24)
                OAUTH_STATES.add(state)
                auth_url = client.build_auth_url(state)
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", auth_url)
                self.end_headers()
                return
            if route == "/callback":
                code = query.get("code", [""])[0]
                state = query.get("state", [""])[0]
                if not code or state not in OAUTH_STATES:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid or missing OAuth state/code.")
                    return
                OAUTH_STATES.discard(state)
                try:
                    payload = client.exchange_code(code)
                    user_id = payload.get("user_id", "unknown")
                    body = (
                        "<!doctype html><html><head><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                        "<title>Fitbit Connected</title>"
                        "<style>body{font-family:Georgia,serif;background:#f3eadf;color:#1f2933;"
                        "display:grid;place-items:center;min-height:100vh;margin:0;padding:24px}"
                        "main{max-width:620px;background:#fffaf3;border:1px solid #e7d7bf;"
                        "border-radius:24px;padding:32px;box-shadow:0 18px 40px rgba(31,41,51,.08)}"
                        "a{color:#0f766e}</style></head><body><main>"
                        f"<h1>Fitbit connected</h1><p>Tokens saved for Fitbit user {user_id}.</p>"
                        "<p>You can close this tab and go back to the coach app.</p>"
                        "<p><a href='/'>Return to coach</a></p></main></body></html>"
                    ).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if route == "/app.js":
                self._send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
                return
            if route == "/styles.css":
                self._send_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
                return
            if route == "/api/status":
                try:
                    payload = load_status_payload(client, target_date)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"ERROR /api/status: {exc}")
                    self._log(traceback.format_exc())
                    self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json(payload)
                return
            if route == "/api/today":
                try:
                    client.reset_cache_events()
                    payload = with_cache_status(
                        client,
                        {
                            "date": target_date,
                            "coach": build_coach_report(client, target_date),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log(f"ERROR /api/today: {exc}")
                    self._log(traceback.format_exc())
                    self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json(payload)
                return
            if route == "/api/trends":
                try:
                    client.reset_cache_events()
                    payload = with_cache_status(
                        client,
                        {
                            "date": target_date,
                            "trends": build_trends_report(client, target_date, 7),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log(f"ERROR /api/trends: {exc}")
                    self._log(traceback.format_exc())
                    self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json(payload)
                return
            if route == "/api/fatloss":
                try:
                    client.reset_cache_events()
                    payload = with_cache_status(
                        client,
                        {
                            "date": target_date,
                            "fatloss": build_fatloss_report(client, target_date, 30),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log(f"ERROR /api/fatloss: {exc}")
                    self._log(traceback.format_exc())
                    self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json(payload)
                return
            if route == "/api/zepbound":
                try:
                    client.reset_cache_events()
                    payload = with_cache_status(
                        client,
                        {
                            "date": target_date,
                            "zepbound": build_zepbound_report(client, target_date),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log(f"ERROR /api/zepbound: {exc}")
                    self._log(traceback.format_exc())
                    self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json(payload)
                return
            if route == "/api/history":
                limit = int(query.get("limit", ["30"])[0])
                payload = {
                    "items": client.read_recent_interactions(limit=limit)
                }
                self._send_json(payload)
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/chat":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON body."}, HTTPStatus.BAD_REQUEST)
                return

            prompt = str(payload.get("message", ""))
            target_date = str(payload.get("date", date.today()))
            if not prompt.strip():
                self._send_json({"error": "Message is required."}, HTTPStatus.BAD_REQUEST)
                return

            try:
                reply = answer_chat(client, prompt, target_date)
            except Exception as exc:  # noqa: BLE001
                self._log(f"ERROR /api/chat: {exc}")
                self._log(traceback.format_exc())
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            client.append_interaction(
                {
                    "source": "web-chat",
                    "topic": detect_topic(prompt),
                    "date_context": target_date,
                    "message": prompt,
                    "reply": reply,
                },
            )
            self._send_json({"reply": reply})

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    return CoachHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local web UI for the Fitbit coach")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = FitbitClient(FitbitConfig.from_env())
    server = ThreadingHTTPServer((args.host, args.port), make_handler(client))
    print(f"Coach web app running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
