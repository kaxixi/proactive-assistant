# Claudette — Proactive Personal Assistant

A daily automation system that monitors your Gmail inbox and Google Calendar, generates a natural-language digest using Claude, and delivers it via Telegram. It learns your preferences over time and adapts to your timezone automatically.

## What it does

- **Morning digest** — Scans your inbox for emails you might be dropping (unreplied, aging, needs follow-up), lists upcoming meetings, cross-references against your current priorities, and sends you a prioritized summary each morning via Telegram. Three modes:
  - **Weekday (Mon–Fri)**: Today + tomorrow, urgent actions
  - **Saturday**: Weekend overview + "if you have time" items from the week
  - **Sunday**: Full week-ahead planning — highlights non-recurring meetings, upcoming deadlines, and sets the tone for the week. Includes weekly memory review
- **Interactive bot** — Reply to any message with questions or feedback. Search Gmail, Google Drive, and Dropbox by chatting naturally
- **Persistent memory** — Remembers key facts across conversations: pending tasks, resolved items, who people are, and what you've told it. Say "Capital One is handled" and it won't come up again
- **Learning system** — Give feedback ("don't flag newsletters from X") and it learns lasting preferences
- **Timezone-aware** — Reads your Google Calendar timezone setting. Travel to a new timezone, update your calendar, and the digest follows you

## Architecture

```
scheduler.py          — Entry point for cron runs. Checks timezone, orchestrates pipeline
bot.py                — Telegram bot (long-polling). Commands, free-text, file attachments
email_monitor.py      — Gmail scanning with importance heuristics and batch API
calendar_digest.py    — Google Calendar: meetings, prep flags, timezone detection
analyzer.py           — Claude API: generates the natural language digest
priorities.py         — Fetches a published priorities list (e.g., Simplenote URL)
memory.py             — Episodic memory: extracts and stores key facts from every interaction
preferences.py        — Learning system: rules, sender prefs, dismissed threads, feedback log
drive_search.py       — Google Drive file search
dropbox_search.py     — Dropbox file search
google_auth.py        — Shared Google OAuth2 (Gmail, Calendar, Drive)
config.py             — Loads all config from .env
```

## Setup Guide

This guide is designed so that you can hand it to Claude Code (`claude` CLI) and it will walk you through each step interactively. Or follow it manually.

### Prerequisites

- Python 3.11+
- A Google account (Gmail, Calendar, Drive)
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- A Telegram account

### Step 1: Clone and install dependencies

```bash
git clone https://github.com/YOUR_USERNAME/proactive-assistant.git
cd proactive-assistant
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 2: Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts to name your bot
3. Copy the **bot token** BotFather gives you
4. To get your **chat ID**: message your new bot, then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser. Look for `"chat":{"id":XXXXXXXX}` — that number is your chat ID

### Step 3: Set up Google Cloud credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable these APIs:
   - Gmail API
   - Google Calendar API
   - Google Drive API
4. Go to **APIs & Services → Credentials**
5. Click **Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON file and save it as `credentials.json` in the project directory

### Step 4: Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From BotFather (Step 2) |
| `TELEGRAM_CHAT_ID` | Yes | Your Telegram chat ID (Step 2) |
| `ANTHROPIC_API_KEY` | Yes | From [console.anthropic.com](https://console.anthropic.com) |
| `DROPBOX_ACCESS_TOKEN` | No | For Dropbox search. Get from [Dropbox App Console](https://www.dropbox.com/developers/apps) |
| `PRIORITIES_URL` | No | URL to a published priorities/to-do list (plain text or HTML) |
| `CLAUDE_MODEL` | No | Defaults to `claude-sonnet-4-20250514` |
| `DIGEST_HOUR` | No | Hour to send digest (default: 5, meaning 5 AM) |
| `DIGEST_MINUTE` | No | Minute to send digest (default: 30) |

### Step 5: Authorize Google APIs

Run this once locally (requires a browser for the OAuth consent flow):

```bash
source venv/bin/activate
python3 -c "from google_auth import get_credentials; get_credentials()"
```

A browser window will open asking you to authorize Gmail, Calendar, and Drive access. After approving, a `token.json` file is created automatically.

### Step 6: Test locally

```bash
# Run a one-time digest
python3 scheduler.py --force

# Start the interactive bot
python3 bot.py
```

If the digest works, you should receive a Telegram message with your morning briefing.

### Step 7: Deploy to a cloud VM

The bot needs to run 24/7 for interactive replies, and the digest needs a cron-like scheduler. A free-tier GCP e2-micro VM works well.

#### 7a. Create the VM

```bash
gcloud compute instances create claudette \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --image-family=debian-12 \
  --image-project=debian-cloud
```

#### 7b. Set up the VM

```bash
# SSH in
gcloud compute ssh claudette --zone=us-central1-a

# On the VM:
sudo apt update && sudo apt install -y python3-venv python3-pip
mkdir -p ~/proactive-assistant
```

#### 7c. Copy files to the VM

From your local machine:

```bash
# Copy all project files (excluding secrets initially)
gcloud compute scp --zone=us-central1-a \
  *.py requirements.txt run_bot.sh run_digest.sh .env.example \
  claudette:~/proactive-assistant/

# Copy your secrets separately
gcloud compute scp --zone=us-central1-a \
  .env credentials.json token.json \
  claudette:~/proactive-assistant/
```

#### 7d. Install dependencies on VM

```bash
gcloud compute ssh claudette --zone=us-central1-a --command='
  cd ~/proactive-assistant &&
  python3 -m venv venv &&
  source venv/bin/activate &&
  pip install -r requirements.txt
'
```

#### 7e. Create systemd services

SSH into the VM and create these files:

**Bot service** (`/etc/systemd/system/claudette-bot.service`):
```ini
[Unit]
Description=Claudette Telegram Bot
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/proactive-assistant
ExecStart=/home/YOUR_USERNAME/proactive-assistant/venv/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Digest timer** (`/etc/systemd/system/claudette-digest.timer`):
```ini
[Unit]
Description=Run Claudette digest check every 3 hours

[Timer]
OnCalendar=*-*-* 00/3:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

**Digest service** (`/etc/systemd/system/claudette-digest.service`):
```ini
[Unit]
Description=Claudette Daily Digest
After=network.target

[Service]
Type=oneshot
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/proactive-assistant
ExecStart=/home/YOUR_USERNAME/proactive-assistant/venv/bin/python3 scheduler.py
```

Then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now claudette-bot.service
sudo systemctl enable --now claudette-digest.timer
```

#### 7f. Verify

```bash
# Check bot is running
sudo systemctl status claudette-bot.service

# Check timer is scheduled
systemctl list-timers claudette-digest.timer

# Check logs
sudo journalctl -u claudette-bot.service --since "10 minutes ago" --no-pager
```

### How the digest schedule works

The timer fires every 3 hours. Each time, `scheduler.py` checks your **Google Calendar timezone** and compares it to your configured digest time (default 5:30 AM). If the current local time in your timezone is within 90 minutes of the target, the digest runs. Otherwise it exits immediately.

This means: if you travel and update your Google Calendar timezone, the digest automatically adjusts to arrive at 5:30 AM in your new timezone. No reconfiguration needed.

### Updating code

```bash
# Copy changed files to VM
gcloud compute scp --zone=us-central1-a <files> claudette:~/proactive-assistant/

# Restart the bot (picks up code changes)
gcloud compute ssh claudette --zone=us-central1-a --command='sudo systemctl restart claudette-bot.service'
```

## Bot commands

| Command | Description |
|---|---|
| `/start` or `/help` | Show available commands |
| `/status` | Check which services are connected |
| `/digest` | Trigger a digest immediately |
| `/search <query>` | Search Google Drive and Dropbox |
| *(ask about an email)* | Searches Gmail automatically via Claude tool use |
| *(free text)* | Chat naturally — ask questions, give feedback, request file searches |
| *(file attachment)* | Send a text file and Claudette will read and discuss it |

## How it learns

Claudette has two layers of memory:

### Episodic memory (`memory.json`)

After every conversation and every digest, a second Claude call extracts key facts and stores them with typed auto-expiry:

| Type | Expires | Example |
|---|---|---|
| `pending` | 30 days | "Chase Visa funding review still needs to be completed" |
| `resolved` | 14 days | "Capital One payment issue handled" |
| `fact` | 60 days | "Gassiraro appointment rescheduled to late March" |
| `relationship` | Never | "Desiree Plata is a collaborator on the Universal Climate course" |
| `preference` | Never | Graduates to preferences.json rules |

These memories are loaded into both bot and digest prompts with tiered priority: pending items first, then recent facts, then relationships, then historical summaries.

#### Hierarchical compaction

To stay bounded over decades of use, memories automatically roll up into summaries:

- **After 7 days**: Individual resolved/fact memories compact into weekly summaries
- **After 30 days**: Weekly summaries compact into monthly summaries
- **After 1 year**: Monthly summaries compact into yearly summaries

Relationships and preferences are never compacted. After 10 years, the memory store stays at ~130 entries (~30-50 KB) instead of growing unboundedly.

### Preferences and thread dismissals (`preferences.json`)

When you tell Claudette an email is handled ("Cap One is dealt with"), the bot dismisses the Gmail thread by ID. Dismissed threads aren't hard-filtered — instead, the dismissed context (subject + reason) is passed into the digest prompt so Claude can use judgment. If a new Capital One email arrives, Claude decides: "is this the same payment issue he already handled, or a genuinely new problem?" Same issue gets skipped; new issue gets flagged.

Dismissed threads auto-expire after 30 days. Lasting preference rules are extracted from feedback and applied to all future digests:

```json
{
  "rules": ["Always flag emails from Desiree Plata as high priority"],
  "senders_always_flag": ["important-person@example.com"],
  "senders_never_flag": ["noreply@vercel.com"],
  "dismissed_threads": [],
  "feedback_log": []
}
```

## License

MIT
