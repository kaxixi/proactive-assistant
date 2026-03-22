# Claudette — Proactive Personal Assistant

## What this is
A daily automation system that acts as a proactive personal assistant, delivered via Telegram bot. Built for an academic behavioral science researcher but designed to be generalizable.

## Architecture
- **scheduler.py** — Entry point for scheduled runs. Orchestrates the full pipeline: scan emails → group into loops → generate digest. Picks digest type by day of week: weekday (Mon-Fri), weekend (Sat), week-ahead (Sun). Checks Google Calendar timezone to decide if it's digest time. Supports `--force` to skip the time check.
- **email_monitor.py** — Scans Gmail for emails at risk of being dropped (unreplied, aging, needs follow-up). Uses batch API for performance. Fetches body preview for context. Only active when ENABLE_EMAIL=true.
- **open_loops.py** — Data model and persistence for topic-level email grouping. See [Open loops system](#open-loops-system).
- **analyzer.py** — Generates natural language digests via Claude. Three modes: weekday, weekend, week-ahead.
- **bot.py** — Telegram bot (long-polling). Handles commands, free-text messages, file attachments. Uses Claude tool-use for search and loop dismissals.
- **memory.py** — Episodic memory with hierarchical compaction. See [Memory & learning system](#memory--learning-system).
- **calendar_digest.py** — Fetches meetings, flags non-recurring events and prep needs. Exposes `get_user_timezone()` and `get_meetings_for_range()`.
- **priorities.py** — Fetches a published priorities list (URL configurable via PRIORITIES_URL env var).
- **availability.py** — Computes free meeting slots. Supports `/availability` and `/morningavailability` commands with flexible date parsing.
- **preferences.py** — Legacy dismissed threads storage (preferences.json). Preference rules live in memory.json.
- **drive_search.py** / **dropbox_search.py** — File search for Google Drive and Dropbox.
- **google_auth.py** — Shared Google OAuth2. Scopes conditional: Calendar+Drive always, Gmail only when ENABLE_EMAIL=true.
- **config.py** — Loads config from .env. Includes DIGEST_HOUR/DIGEST_MINUTE/ENABLE_EMAIL/CLAUDE_MODEL.

## Memory & learning system

All learned knowledge lives in `memory.json`. The system learns from digest extraction, bot conversations, and explicit user feedback.

### Memory types
| Type | Expiry | Purpose |
|---|---|---|
| `preference` | Never | Lasting behavioral rules ("seminar emails are low priority") |
| `relationship` | Never | Who people are ("Arjun is an undergraduate advisee") |
| `follow_up` | Never (until dismissed) | Persistent reminders Erez explicitly asked for |
| `fact` | 60 days | Situational context ("Erez is moving offices") |
| `resolved` | 14 days | Record of dismissed/handled items |
| `conversation_summary` | 14 days | Condensed summaries of multi-turn bot chats |
| `pending` | 30 days | Open action items (no longer created from digests — tracked as open loops) |

### How memories are created
- **Digest pipeline** → extracts facts, relationships, preferences (not pending — those are open loops now)
- **Bot conversations** → extracts atomic memories + generates conversation summaries after multi-turn exchanges
- **Dismissals** → creates `resolved` memory, clears matching `follow_up` memories
- **User feedback** → `RULE:` lines become `preference` memories; can also be added manually

### How memories are consumed
- **Digest prompt** → tiered injection via `get_memories_for_prompt()`: follow-ups/preferences first, then recent facts, then historical summaries
- **Grouping prompt** → resolved, follow-up, and preference memories passed as `<learned_context>` to influence loop urgency
- **Bot prompt** → same tiered injection for conversational continuity

### Hygiene
- Per-type expiry (see table above)
- Tag-based dedup: same type + overlapping tags → replace old with new, don't duplicate
- Hard caps per type (preferences: 30, relationships: 40, facts: 50, resolved: 40, etc.)
- Compaction: facts/resolved/summaries roll up weekly → monthly → yearly
- Weekly review (Sunday): Claude flags contradictions, duplicates, stale preferences/facts

## Open loops system

Open loops are the unit of email tracking. A loop is a topic-level concern (e.g., "Arjun's HCRP application") that may group multiple Gmail threads from different senders.

### Pipeline
```
email_monitor.scan_inbox()          → list[FlaggedEmail]
scheduler._hard_filter_dismissed()  → removes threads from dismissed loops + legacy dismissed
scheduler._group_into_loops()       → Claude API call groups emails into loops (with learned context)
scheduler._priority_match_loops()   → tag loops matching priorities list
scheduler._cap_loops()              → cap at ~15 loops
scheduler._group_loops_by_priority()→ format as text
scheduler._apply_follow_up_to_loops()→ annotate with follow-up reminders
analyzer.generate_daily_digest()    → Claude generates natural language digest from loops
```

### Dismissal flow
1. User tells bot "dismiss X" → `find_loop_by_query()` searches loops by title/senders/tags
2. `dismiss_loop()` sets status=dismissed, clears matching follow-up memories
3. Creates `resolved` memory with loop's tags
4. Falls back to Gmail search if no loop matches (for items not in latest scan)

### Lifecycle
- Created during each digest pipeline by Claude grouping call
- Persisted in `open_loops.json` between digests (new emails join existing loops)
- Expire after 30 days without activity
- Dismissed loop thread IDs are hard-filtered from future scans

## Key design decisions
- **Telegram for delivery** — works over WiFi internationally, supports interactive replies
- **Claude Sonnet for analysis** — balances cost and quality for daily use. Model configurable via CLAUDE_MODEL env var.
- **Calendar-first, email-optional** — calendar features work independently (ENABLE_EMAIL=false). Email adds Gmail scanning, search, and loop dismissals.
- **Batch Gmail API** — threads fetched in batches of 20 for ~5x speedup
- **Loop-based dismissals** — dismissing a topic closes the loop (all member threads), clears follow-up memories, creates resolved memory. Legacy per-thread dismissals in preferences.json still work as fallback.
- **OAuth token on VM** — token.json must be generated locally (browser required) then copied to VM
- **Timezone-aware scheduling** — timer fires every 3h, Python checks Google Calendar timezone. No hardcoded timezone.

## Deployment
- GCP e2-micro VM (free tier) in us-central1-a, instance name "claudette"
- Bot: systemd service `claudette-bot.service` (always on, auto-restart)
- Digest: systemd timer `claudette-digest.timer` (every 3 hours, scheduler.py checks timezone)

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
- .env, credentials.json, token.json, preferences.json, memory.json, open_loops.json
