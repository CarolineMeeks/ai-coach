#!/usr/bin/env python3
"""Local web app for the Fitbit recovery-first coach."""

from __future__ import annotations

import argparse
import json
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fitbit_client import (
    FitbitClient,
    FitbitConfig,
    answer_chat,
    build_coach_report,
    build_fatloss_report,
    build_trends_report,
    build_zepbound_report,
    detect_topic,
)
from interaction_log import append_interaction, read_recent_interactions


STATIC_DIR = Path(__file__).with_name("web")


def load_status_payload(client: FitbitClient, target_date: str) -> dict:
    coach = build_coach_report(client, target_date)
    trends = build_trends_report(client, target_date, 7)
    fatloss = build_fatloss_report(client, target_date, 30)
    zepbound = build_zepbound_report(client, target_date)
    return {
        "date": target_date,
        "coach": coach,
        "trends": trends,
        "fatloss": fatloss,
        "zepbound": zepbound,
    }


def make_handler(client: FitbitClient):
    class CoachHandler(BaseHTTPRequestHandler):
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

            if route == "/":
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
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
                    self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json(payload)
                return
            if route == "/api/history":
                limit = int(query.get("limit", ["30"])[0])
                payload = {
                    "items": read_recent_interactions(client.config.interaction_log_path, limit=limit)
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
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            append_interaction(
                client.config.interaction_log_path,
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
