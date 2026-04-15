# Unified state — plan

## Context

Claudette currently keeps her state across ~7 JSON files (`memory.json`, `open_loops.json`, `preferences.json`, `scan_state.json`, `digest_loops.json`, `last_scheduler_messages.json`, `interactions.json`). Each surface (digest, bot, `/loops`, `/memoryreview`) reads a partially-overlapping subset. The result is fragility: one surface can fail silently while another carries on (e.g. `/loops` crashed without the bot knowing), and user feedback lands in one silo but doesn't propagate to the layer that actually makes the decision (e.g. a narrative preference saying "Chase statements aren't loops" doesn't update the ingestion filter).

There are two orthogonal problems:

1. **Coherence** — fragmented state means no single source of truth about "what's going on." This is the user's explicit top priority.
2. **Generalization** — narrative memory nudges LLM reasoning but doesn't compile into deterministic behavior, so the same lesson has to be re-learned each scan.

The unified state addresses (1) directly and lays groundwork for (2) via an embedded `rules` section.

## Design principles (load-bearing)

1. **One `state.json`, namespaced.** Every surface reads and writes the same object. Sections are separated by concern, not by historical file.
2. **Narrative is ground truth for rules.** Every structured rule carries a `source_memory_id` pointing to the memory that spawned it. Delete or expire the memory → rule auto-vanishes. No orphans, no duplicated meaning.
3. **One store for loops.** All loops (open + dismissed + expired) live in one list with a `status` field. Retire `preferences.json`'s `dismissed_threads`.
4. **Separate durable from ephemeral.** `narrative`, `rules`, `loops`, `audit` are the durable state. `session` and `pipeline` are housekeeping; they can reset without loss. This guards against fragmentation creeping back.
5. **Hygiene is a first-class property.** Every section declares its expiry/compaction/cap behavior. A single `prune()` pass runs on every save. No ad-hoc growth.

## Schema

```json
{
  "version": 1,
  "narrative": {
    "memories": [...],          // typed: preference, relationship, follow_up, fact, resolved, conversation_summary
    "summaries": { "weekly": [], "monthly": [], "yearly": [] },
    "last_review": "..."
  },
  "rules": {
    "ingestion": [...],         // skip / always-flag at scan time
    "closure":   [...],         // when to auto-close a loop
    "priority":  [...]          // sender/topic weighting
  },
  "loops": [...],               // status=open|dismissed|expired; one store for active + history
  "session": {
    "last_scheduler_messages": [...],
    "digest_loop_numbers": {...},
    "conversation_history": [...]
  },
  "pipeline": {
    "last_scan_at": "...",
    "scanned_thread_ids": [...]
  },
  "audit": [...]                // append-only log of dismissals, corrections, auto-closes, rule compiles
}
```

### Rule entry shape

```json
{
  "id": "r_abc123",
  "source_memory_id": "m_xyz789",
  "kind": "ingestion" | "closure" | "priority",
  "match": { ... },             // structured predicate: sender_regex, subject_contains, labels, etc.
  "action": "skip" | "always_flag" | "auto_close" | "demote" | ...,
  "dry_run_count": 0,           // first N firings surfaced to user; 0 after confirmation
  "confirmed": false,           // flips true after user approves on first compile
  "last_fired_at": "...",
  "fire_count": 0,
  "created_at": "..."
}
```

## Confirmed decisions

| Question | Decision |
|---|---|
| Audit-log visibility | Internal only (no `/audit` command for now) |
| Rule confirmation UX | Confirm first time; auto-reuse once confirmed |
| Write safety | Journaled writes (`.tmp` → fsync → rename) + keep last 3 backups |
| Migration cutover | Delete old files immediately once migrated — rely on git history for recovery |

## Expiry & hygiene per section

| Section | Rule |
|---|---|
| `narrative.memories` | Existing: per-type TTL (`fact` 60d, `resolved` 14d, `conversation_summary` 14d, `pending` 30d; `preference`/`relationship`/`follow_up` never) + per-type hard caps (preferences 30, relationships 40, facts 50, resolved 40) + tag-based dedup on write |
| `narrative.summaries` | Existing: weekly → monthly → yearly compaction |
| `rules.*` | (a) auto-retire when `source_memory_id` is deleted/expired; (b) retire if `last_fired_at` > 6 months; hard cap 50 per kind |
| `loops` (open) | 30d inactivity expiry (existing) |
| `loops` (dismissed) | Keep 90d for pattern detection / never-reopen filtering; then collapse into a `dismissed_tally` (per-sender / per-tag counts) and drop individual entries |
| `session.*` | Already self-limiting (e.g. `last_scheduler_messages` capped at 3, `conversation_history` at 5 exchanges, 30-min staleness reset) |
| `pipeline.scanned_thread_ids` | Cap at last 30 days of scans (currently unbounded — fix as part of migration) |
| `audit` | Keep last 90 days in-state, hard cap 2000 entries; older rolled out to `state.archive.json` or dropped |

`prune()` runs on every save. A `/state` internal command (added later) prints per-section counts so growth anomalies are spottable.

Expected steady-state size after years of use: low single-digit MB.

## Migration — incremental

### Step 1 — Plumbing (no behavior change)
- Add `state.py` that loads/saves a single `state.json` via journaled writes with 3 rolling backups.
- Internally still exposes today's API (`load_memories()`, `load_loops()`, `load_preferences()`, etc.) — just reading/writing slices of the unified object.
- On first boot, migrate from the 7 existing files into `state.json`; delete the originals.
- Add `prune()` stub that delegates to existing per-type expiry logic.
- **Verify:** `python3 scheduler.py --force` produces an identical digest to the current behavior. `/loops`, `/memoryreview`, `/loopcleanup` all still work. No user-visible change.

### Step 2 — Retire `preferences.json` as separate concept
- Fold `senders_never_flag` / `senders_always_flag` into `rules.ingestion` entries (kind=priority/ingestion) with hand-authored `source_memory_id = "migrated"` placeholders.
- Remove `dismissed_threads` from preferences — the loops list already handles this.
- Delete `preferences.py` or reduce it to a thin shim.
- **Verify:** Erez's existing dismissals still filter correctly; always-flag senders still get surfaced.

### Step 3 — Introduce rules compile-confirm-apply loop
- Narrow surface first: compile ingestion rules from `preference` memories that look like filter statements (e.g. "skip X", "always flag Y").
- New bot command `/rules` to list / delete active rules.
- When a new rule is compiled, send a one-shot Telegram confirmation message; user replies "yes" / "no" / "narrower: ...". Store `confirmed=true` in state on approval.
- Dry-run: first 3 firings of a new rule are logged and surfaced once in the next digest ("skipped 3 Chase statements by rule #7"), then silent.
- **Verify:** at least one real correction Erez makes in conversation produces a structured rule; rule fires on next scan; dry-run note appears in next digest.

### Step 4 — Fold ephemeral state
- Move `digest_loops.json`, `last_scheduler_messages.json`, `scan_state.json` into the `session` and `pipeline` sections.
- Delete the originating files.
- Migrate `interactions.json` into `audit`; `detect_patterns` reads from the audit section.
- **Verify:** bot context (scheduler messages, numbered loops) still works; pattern detection still fires.

## Files touched

- New: `state.py` (core), `docs/unified-state-plan.md` (this file, already present)
- Modified: every module that today reads one of the 7 JSONs — `memory.py`, `open_loops.py`, `preferences.py`, `scan_state.py`, `bot.py`, `scheduler.py`, `interaction_tracker.py`
- Deleted over the course of migration: `memory.json`, `open_loops.json`, `preferences.json`, `scan_state.json`, `digest_loops.json`, `last_scheduler_messages.json`, `interactions.json` (contents folded into `state.json`)
- Docs: `CLAUDE.md` updated after each step

## Verification

At each step, the acceptance criterion is **"Erez doesn't notice anything changed"** — until Step 3, which is the first step with user-visible behavior (rule confirmation messages, dry-run notes). Steps 1–2 and 4 are pure refactors; their verification is that the existing surfaces still behave identically.

Specific end-to-end checks:
- `python3 scheduler.py --force` produces a working digest after each step.
- `/loops`, `/memoryreview`, `/loopcleanup`, `/digest`, `/status`, `/search`, `/availability` all work after each step.
- After Step 1: only `state.json` (+ 3 backups) in the project root; all 7 originals gone.
- After Step 3: `/rules` returns at least one rule compiled from a real preference memory; fires on a subsequent scan; dry-run note appears in the next digest footer.
- File size sanity: after migration `ls -la state.json` should be well under 1 MB for current data; verify `prune()` keeps it bounded.

## Out of scope (explicitly deferred)

- `/audit` user-facing command (internal only for now).
- Auto-learning rules from dismissal patterns without explicit user utterance. (`interaction_tracker.detect_patterns` already suggests these; keep as suggestions, don't auto-apply.)
- Cross-device sync or multi-user support.
- Structured rules beyond `ingestion` / `closure` / `priority` (e.g. scheduling, memory management rules). Start with the three with clearest payoff.
