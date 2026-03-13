# Claudette — Proactive Personal Assistant

## What this is
A daily automation system that acts as a proactive personal assistant, delivered via Telegram bot. Built for an academic behavioral science researcher but designed to be generalizable.

## Architecture
- **bot.py** — Telegram bot (long-polling). Handles commands, free-text messages, and file attachments. Uses Claude tool-use for Drive/Dropbox search.
- **email_monitor.py** — Scans Gmail for emails at risk of being dropped (unreplied, aging, needs follow-up). Uses batch API for performance.
- **calendar_digest.py** — Fetches today's and tomorrow's meetings. Flags non-recurring events and meetings needing prep. Also exposes `get_user_timezone()` for schedule detection.
- **analyzer.py** — Sends email + calendar + priorities data to Claude to generate a natural language digest.
- **scheduler.py** — Entry point for scheduled runs. Checks user's Google Calendar timezone to decide if it's digest time, then orchestrates the full pipeline. Supports `--force` to skip the time check.
- **priorities.py** — Fetches a published priorities list (URL configurable via PRIORITIES_URL env var).
- **drive_search.py** — Google Drive file search.
- **dropbox_search.py** — Dropbox file search.
- **preferences.py** — Learning system. Stores rules, sender preferences, and feedback log in preferences.json.
- **google_auth.py** — Shared Google OAuth2 (Gmail, Calendar, Drive).
- **config.py** — Loads all config from .env with `override=True`. Includes DIGEST_HOUR/DIGEST_MINUTE.

## Deployment
- Runs on a GCP e2-micro VM (free tier) in us-central1-a, instance name "claudette"
- Bot runs as systemd service: `claudette-bot.service` (always on, auto-restart)
- Digest runs via systemd timer: `claudette-digest.timer` (every 3 hours)
  - scheduler.py checks Google Calendar timezone and only sends if it's within 90 min of the target time (default 5:30 AM local)
  - This means the digest automatically adjusts when the user changes their calendar timezone
- Deploy changes: scp files to VM, then `sudo systemctl restart claudette-bot.service`

## Key design decisions
- **Telegram for delivery** — works over WiFi internationally, supports interactive replies
- **Claude Sonnet for analysis** — balances cost and quality for daily use. Model configurable via CLAUDE_MODEL env var.
- **Batch Gmail API** — threads fetched in batches of 20 (not one-by-one) for ~5x speedup
- **Preferences as JSON** — simple, human-readable, no database needed. Upgrade to SQLite if it grows.
- **OAuth token on VM** — token.json must be generated locally (browser required) then copied to VM
- **Timezone-aware scheduling** — timer fires every 3h, Python checks Google Calendar timezone to decide whether to send. No hardcoded timezone in the timer.

## Development workflow
1. Edit files locally in `/Users/erez/Documents/proactive-assistant/`
2. Test locally: `source venv/bin/activate && python3 scheduler.py --force`
3. Deploy to VM:
   ```
   export PATH="/opt/homebrew/share/google-cloud-sdk/bin:$PATH"
   gcloud compute scp --zone=us-central1-a <files> claudette:~/proactive-assistant/
   gcloud compute ssh claudette --zone=us-central1-a --command='sudo systemctl restart claudette-bot.service'
   ```
4. Check logs: `gcloud compute ssh claudette --zone=us-central1-a --command='sudo journalctl -u claudette-bot.service --since "10 minutes ago" --no-pager'`

## GitHub repo
- Public repo: https://github.com/kaxixi/proactive-assistant
- Push changes: `git push origin main`

## Adding new features
- New tools for Claude: add to `TOOLS` list in bot.py, implement in `_execute_tool()`
- New preference rules: learned automatically from user feedback via RULE: extraction
- New data sources: create a module, import in scheduler.py and/or bot.py

## Sensitive files (never commit)
- .env (API keys)
- credentials.json (Google OAuth client secrets)
- token.json (Google OAuth tokens)
- preferences.json (personal preference data)
