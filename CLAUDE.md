# Claudette — Proactive Personal Assistant

## What this is
A daily automation system that acts as a proactive personal assistant, delivered via Telegram bot. Built for an academic behavioral science researcher but designed to be generalizable.

## Common commands
- Run digest now (skips time check): `source venv/bin/activate && python3 scheduler.py --force`
- Start bot locally: `python3 bot.py`
- Re-auth Google APIs: `python3 -c "from google_auth import get_credentials; get_credentials()"`
- Deploy: `git push origin main`, then SSH to VM + `git pull` + restart bot (see [Development workflow](#development-workflow))
- Tail VM logs: `gcloud compute ssh claudette --zone=us-central1-a --command='sudo journalctl -u claudette-bot.service -f'`
- Wrapper scripts: `run_bot.sh` and `run_digest.sh` activate the venv and exec the corresponding Python entry point — used by systemd on the VM.

There is no test suite in this repo; verify changes by running `scheduler.py --force` against your own Telegram chat.

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
- **rules.py** — Structured ingestion/closure/priority rules stored under `state.rules.*`. Used by the ingestion pipeline to deterministically skip or always-flag senders (replacing the old `senders_never_flag` / `senders_always_flag` lists). Each rule tracks a `source_memory_id` so rules disappear automatically when their source memory is forgotten. When a new `preference` memory looks like a filter statement ("Skip all emails from X", "Always flag Y"), `compile_preference_to_rule` turns it into an unconfirmed rule operating in dry-run for 3 matches. The bot surfaces unconfirmed rules via `<unconfirmed_rules>` in the system prompt and offers `confirm_rule` / `delete_rule` tools; dry-run fires are aggregated into a single digest footer so Erez sees what the new rule actually caught.
- **scan_state.py** — Tracks incremental email scanning progress (last scan timestamp, seen thread IDs).
- **interaction_tracker.py** — Records button presses and loop interactions, detects behavioral patterns for auto-deprioritization suggestions.
- **drive_search.py** / **dropbox_search.py** — File search for Google Drive and Dropbox.
- **google_auth.py** — Shared Google OAuth2. Scopes conditional: Calendar+Drive always, Gmail only when ENABLE_EMAIL=true.
- **config.py** — Loads config from .env. Includes DIGEST_HOUR/DIGEST_MINUTE/ENABLE_EMAIL/CLAUDE_MODEL.
- **state.py** — Unified state store. All persistent state (memories, loops, preferences, scan state, digest loop numbers, scheduler-message context, audit log) lives in a single `state.json`, written atomically (`.tmp` → fsync → rename) with 3 rolling backups. The per-domain modules (memory.py, open_loops.py, etc.) read/write slices of this object via `state.get_section(name)` / `state.set_section(name, value)`. On first boot, `state.py` migrates from the legacy per-file JSONs and deletes them. Plan: see `docs/unified-state-plan.md`.

## Memory & learning system

All learned knowledge lives in the `narrative` section of `state.json`. The system learns from digest extraction, bot conversations, and explicit user feedback.

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
- Date-aware expiry: fact memories with specific dates (e.g., "March 20", "today") auto-expire 24h after the event
- Tag-based dedup: same type + overlapping tags → replace old with new, don't duplicate
- Hard caps per type (preferences: 30, relationships: 40, facts: 50, resolved: 40, etc.)
- Compaction: facts/resolved/summaries roll up weekly → monthly → yearly
- Weekly review (Sunday): Claude flags contradictions, duplicates, stale preferences/facts

## Open loops system

Open loops are the unit of email tracking. A loop is a topic-level concern (e.g., "Arjun's HCRP application") that may group multiple Gmail threads from different senders.

### Incremental scanning
- `scan_state.json` tracks `last_scan_at` timestamp and `scanned_thread_ids` (threads seen but not in any loop)
- First run: full 14-day backfill. Subsequent runs: only fetch emails newer than `last_scan_at`
- Threads already in any loop (open or dismissed) are never re-processed
- Previously filtered threads are re-evaluated if they have new activity

### Pipeline
```
scheduler._auto_close_handled_loops() → re-check each open loop's Gmail state; dismiss ones where Erez has participated and nothing is still awaiting his reply (runs before scan each digest, also via /loopcleanup)
email_monitor.scan_inbox()          → list[FlaggedEmail] (incremental since last scan)
scheduler: subtract accounted-for thread IDs (loops + previously scanned)
scheduler._hard_filter_dismissed()  → safety net for dismissed threads
scheduler._group_into_loops()       → Claude groups ONLY new emails into loops (with existing loop context)
scheduler._priority_match_loops()   → tag loops matching priorities list
scheduler._cap_loops()              → cap at ~10 loops
scheduler._group_loops_by_priority()→ format as numbered text (#1, #2, ...)
scheduler._apply_follow_up_to_loops()→ annotate with follow-up reminders
analyzer.generate_daily_digest()    → Claude generates natural language digest from loops
```

### Numbered loop references
- Loops are numbered sequentially (#1, #2...) in both the digest and `/loops` command
- Number→loop_id mapping persisted in `digest_loops.json` (written by scheduler and /loops, read by bot)
- Users reference loops by number: "1 handled", "dismiss 3 and 5", "tell me more about 2"
- Bot system prompt includes `<digest_loop_numbers>` section mapping numbers to titles

### Dismissal flow
1. User tells bot "dismiss X" or "3 handled" → `find_loop_by_query()` searches loops by title/senders/tags
2. `dismiss_loop()` sets status=dismissed, clears matching follow-up memories
3. Creates `resolved` memory with loop's tags
4. Falls back to Gmail search if no loop matches (for items not in latest scan)

### Snooze
- `snooze_loop(loop_id, days=2)` hides a loop for N days
- Snoozed loops filtered out by `get_open_loops()`, reappear automatically
- Repeated snoozes tracked (`snooze_count`) for pattern detection

### Lifecycle
- Created during digest pipeline by Claude grouping call (only for new emails)
- Persisted in `open_loops.json` between digests (new emails join existing loops)
- Expire after 30 days without activity
- Dismissed loop thread IDs are permanently filtered from future scans

## Bot commands
- `/start` — welcome message + command list
- `/commands`, `/help` — show command list only (source of truth: `_COMMANDS_TEXT` in bot.py)
- `/status` — check service connections
- `/digest` — trigger a digest right now
- `/memoryreview` — trigger a memory review on demand (normally runs Sundays)
- `/rules` — list structured rules (ingestion filters etc.)
- `/loops` — show open loops dashboard with numbered list
- `/loopcleanup` — re-check every open loop against Gmail and auto-close the ones Erez has engaged with. A loop is closed only when every one of its threads is "settled": Erez sent at least one message in the thread AND the thread is either out of the inbox or he sent the latest message. Archive alone is NOT enough — a thread where Erez never replied isn't handled just because it left the inbox. Runs automatically at the start of every digest; use `/loopcleanup` on demand when the backlog looks stale.
- `/search <query>` — search Drive and Dropbox
- `/availability [this/next week]` — show free meeting slots
- `/morningavailability [this/next week]` — morning slots only

## Multi-turn conversations
- Bot keeps last 5 exchanges in `_conversation_history` (30-minute staleness timeout)
- Enables drill-down: "tell me more about 2" → detailed analysis → "dismiss it"
- Full thread fetch via `fetch_full_thread()` for deep dives, cached in `_thread_cache`
- Conversation summaries generated after multi-turn exchanges

## Pattern detection
- `interaction_tracker.py` records every dismissal and snooze with loop metadata
- `detect_patterns()` identifies repeated actions (3+ similar over 7+ days)
- Suggests auto-deprioritization as preference memories after each digest
- Declined suggestions recorded as facts to prevent re-suggestion for 30 days

## Key design decisions
- **Telegram for delivery** — works over WiFi internationally, supports interactive replies
- **Claude Sonnet for analysis** — balances cost and quality for daily use. Model configurable via CLAUDE_MODEL env var.
- **Calendar-first, email-optional** — calendar features work independently (ENABLE_EMAIL=false). Email adds Gmail scanning, search, and loop dismissals.
- **Batch Gmail API** — threads fetched in batches of 20 for ~5x speedup
- **Incremental scanning** — only process new emails since last scan. Open loops are the persistent source of truth. Dismissed threads never re-processed.
- **Loop-based dismissals** — dismissing a topic closes the loop (all member threads), clears follow-up memories, creates resolved memory. The Gmail-fallback path (when no loop matches a user query) creates a single-thread dismissed loop via `open_loops.dismiss_thread_as_loop()` so every dismissal lives in the unified loops list.
- **Scheduler→bot context bridge** — scheduler and bot are separate processes. `last_scheduler_messages.json` persists the last 3 scheduler messages (digests, memory reviews) so the bot has context when the user replies. `digest_loops.json` maps loop numbers to IDs.
- **OAuth token on VM** — token.json must be generated locally (browser required) then copied to VM. Tokens expire every 7 days (unverified app). `google_auth.py` catches RefreshError and deletes stale token instead of crashing; also detects headless systemd environment and raises a clear error instead of trying to open a browser. Scheduler sends a Telegram warning starting on day 6 with the exact refresh commands.
- **Timezone-aware scheduling** — timer fires every 3h, Python checks Google Calendar timezone. No hardcoded timezone.

## Deployment
- GCP e2-micro VM (free tier) in us-central1-a, instance name "claudette"
- Bot: systemd service `claudette-bot.service` (always on, auto-restart)
- Digest: systemd timer `claudette-digest.timer` (every 3 hours, scheduler.py checks timezone)

## Development workflow
1. Edit files locally in `/Users/erez/Documents/proactive-assistant/`
2. Test locally: `source venv/bin/activate && python3 scheduler.py --force`
3. Commit and push: `git push origin main`
4. Deploy to VM:
   ```
   export PATH="/opt/homebrew/share/google-cloud-sdk/bin:$PATH"
   gcloud compute ssh claudette --zone=us-central1-a --command='cd ~/proactive-assistant && git pull origin main && sudo systemctl restart claudette-bot.service'
   ```
5. Check logs: `gcloud compute ssh claudette --zone=us-central1-a --command='sudo journalctl -u claudette-bot.service --since "10 minutes ago" --no-pager'`

Note: The VM repo is connected to GitHub. Sensitive files (.env, token.json, etc.) are gitignored and live only on the VM. To update those, use `gcloud compute scp`.

## GitHub repo
- Public repo: https://github.com/kaxixi/proactive-assistant
- Push changes: `git push origin main`

## Adding new features
- New tools for Claude: add to `TOOLS` list in bot.py, implement in `_execute_tool()`
- New preference rules: learned automatically from user feedback via RULE: extraction
- New data sources: create a module, import in scheduler.py and/or bot.py

## Sensitive files (never commit)
- .env, credentials.json, token.json
- state.json (+ state.json.bak.*, state.json.tmp, state.json.lock) — unified state file
- Legacy files (now consolidated but still gitignored in case an old checkout is migrated):
  preferences.json, memory.json, open_loops.json, scan_state.json, interactions.json,
  digest_loops.json, last_scheduler_messages.json
