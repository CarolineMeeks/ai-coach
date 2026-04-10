# Fitbit Coach Pages

Minimal static site for Fitbit app registration, plus a small Python connector for personal Fitbit data access.

## Repository contents

- `index.html` - homepage for the Fitbit app
- `privacy.html` - privacy policy page
- `terms.html` - terms of service page
- `fitbit_client.py` - local Python CLI for Fitbit OAuth and data pulls
- `.env.example` - environment variable template
- `requirements.txt` - Python dependency list

## Publish with GitHub Pages

1. Create a new public GitHub repository, ideally named `fitbit-coach`.
2. Add these files to the repository root.
3. In GitHub, go to `Settings` -> `Pages`.
4. Set `Source` to `Deploy from a branch`.
5. Set the branch to `main` and folder to `/ (root)`.
6. Save and wait for the site to publish.

If the repository is named `fitbit-coach`, your Pages URLs will be:

- `https://carolinemeeks.github.io/fitbit-coach/`
- `https://carolinemeeks.github.io/fitbit-coach/privacy.html`
- `https://carolinemeeks.github.io/fitbit-coach/terms.html`

## Fitbit registration values

- `Application Website URL`: `https://carolinemeeks.github.io/fitbit-coach/`
- `Organization URL`: `https://github.com/CarolineMeeks`
- `Privacy Policy URL`: `https://carolinemeeks.github.io/fitbit-coach/privacy.html`
- `Terms of Service URL`: `https://carolinemeeks.github.io/fitbit-coach/terms.html`
- `Callback URL`: `http://127.0.0.1:8765/callback`

## Local Fitbit connector setup

1. Create a Fitbit developer app at [dev.fitbit.com](https://dev.fitbit.com/).
2. Copy `.env.example` to `.env` and add your Fitbit app credentials.
3. Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4. Authenticate:

```bash
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
python3 fitbit_client.py auth
```

5. Pull data:

```bash
python3 fitbit_client.py summary --date 2026-04-08
python3 fitbit_client.py intraday steps --date 2026-04-08
python3 fitbit_client.py intraday heartrate --date 2026-04-08
python3 fitbit_client.py coach --date 2026-04-08
python3 fitbit_client.py trends --date 2026-04-08 --days 7
python3 fitbit_client.py bodycomp --date 2026-04-08 --days 30
python3 fitbit_client.py fatloss --date 2026-04-08 --days 30
python3 fitbit_client.py zepbound --date 2026-04-08
python3 fitbit_client.py water --date 2026-04-08
python3 fitbit_client.py log-water 24 --date 2026-04-08
python3 fitbit_client.py water-reminder --date 2026-04-08 --window noon
python3 fitbit_client.py water-reply "18 oz" --date 2026-04-08
python3 fitbit_client.py chat --date 2026-04-08
python3 coach_web.py --host 127.0.0.1 --port 8000
```

## Browser coach

Launch the local web app:

```bash
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
python3 coach_web.py --host 127.0.0.1 --port 8000
```

Then open:

- `http://127.0.0.1:8000`

The browser UI shows:

- a daily readiness panel
- body composition and Zepbound snapshot metrics
- hydration progress for the day
- quick question buttons
- a plain-English chat interface backed by the same coaching logic as the CLI
- a recent interaction history panel loaded from the SQLite app database

## App database

The app now stores its primary state in SQLite:

- Fitbit tokens
- interaction history
- the default coach user record

Default database path:

- `coach.db`

Recommended env vars:

- `COACH_TIMEZONE=America/New_York`
- `COACH_DB_PATH=coach.db`
- `COACH_USER_SLUG=default`

Legacy file paths are still supported as one-time migration inputs:

- `FITBIT_TOKEN_PATH`
- `COACH_INTERACTION_LOG_PATH`

If those legacy files exist and the database is empty, the app imports them into SQLite on startup.

The app also uses short-lived in-memory caching to avoid hammering Fitbit:

- OAuth access tokens are reused until near expiry instead of refreshing every request
- Fitbit API responses are cached for about 5 minutes
- the public Zepbound sheet is cached for about 15 minutes

The app defaults to `America/New_York` for "today" unless you override `COACH_TIMEZONE`.

## Hosted Fitbit OAuth

For a deployed app, set:

- `FITBIT_REDIRECT_URI=https://your-app.onrender.com/callback`
- `COACH_DB_PATH=/var/data/coach.db` on a platform with persistent disk

Then visit:

- `https://your-app.onrender.com/connect-fitbit`

That route starts Fitbit OAuth and saves tokens when Fitbit redirects back to `/callback`.

## Interaction history

Every terminal or browser chat exchange is now stored in the SQLite app database.

Each record includes:

- timestamp
- source (`terminal-chat` or `web-chat`)
- topic tag
- date context
- your message
- the coach reply

## Water goal support

The coach now supports a daily hydration target:

- base range: `80-100 oz`
- active-day bonus: added automatically when exercise or movement volume is high
- warm-day bonus: supported by the water-report/reminder commands via `--warm-day`

Useful commands:

- `python3 fitbit_client.py water --date 2026-04-08`
- `python3 fitbit_client.py log-water 24 --date 2026-04-08`
- `python3 fitbit_client.py water-reminder --date 2026-04-08 --window noon`
- `python3 fitbit_client.py water-reply "18 oz" --date 2026-04-08`

The browser API also exposes:

- `GET /api/water?date=YYYY-MM-DD`
- `POST /api/water` with `{ "date": "YYYY-MM-DD", "amount_oz": 24 }`

## SMS reminders

The app now includes Twilio-ready SMS helpers for hydration reminders and reply logging.

Set these optional env vars:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER`
- `SMS_TO_NUMBER`

Useful commands:

- preview a noon reminder:
  - `python3 fitbit_client.py water-reminder --date 2026-04-08 --window noon`
- preview an evening reminder:
  - `python3 fitbit_client.py water-reminder --date 2026-04-08 --window evening`
- send one through Twilio:
  - `python3 fitbit_client.py water-reminder --date 2026-04-08 --window noon --send`
- dry-run a scheduler check:
  - `python3 fitbit_client.py run-scheduler --at 2026-04-09T12:00:00-04:00`
- send a due reminder through the scheduler:
  - `python3 fitbit_client.py run-scheduler --at 2026-04-09T12:00:00-04:00 --send`

For inbound SMS, the web app exposes:

- `POST /sms/webhook`

That webhook currently parses water replies like `24 oz`, logs the intake, and returns a short confirmation message.

## Reminder scheduler

The web app can optionally run a small background scheduler for hydration reminders.

Recommended env vars:

- `COACH_ENABLE_SCHEDULER=1`
- `COACH_SCHEDULER_POLL_SECONDS=60`

Behavior:

- checks for a noon hydration reminder
- checks for a 9:45 PM hydration reminder
- records reminder runs in SQLite so the same reminder is not sent twice on the same day
- skips the 9:45 PM hydration text if the water goal is already met

Important:

- enable the scheduler in only one running instance, or you risk duplicate texts
- for local testing, leave it off and use `run-scheduler`

## Optional OpenAI conversation layer

If you set `OPENAI_API_KEY`, the coach can use an OpenAI model to make conversations more flexible while still grounding answers in your Fitbit, body-composition, and Zepbound data.

Recommended env vars:

- `OPENAI_API_KEY`
- `OPENAI_MODEL=gpt-5.4-mini`
- `OPENAI_BASE_URL=https://api.openai.com/v1`

The app keeps the current deterministic data layer and uses the LLM only as the conversation layer. If the OpenAI call fails, chat falls back to the built-in rule-based replies.
