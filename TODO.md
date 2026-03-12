# Claudette TODO

## High Priority
- [ ] Create GitHub repo with sanitized code (no API keys, no personal data)
- [ ] Write detailed setup guide for new users (Claude Code can walk them through it)
- [ ] Newsletter audit — analyze which email subscriptions are actually opened/read, help unsubscribe from the rest
- [ ] Handle Dropbox token refresh (short-lived tokens expire; switch to OAuth refresh flow)

## Medium Priority
- [ ] Smarter follow-up detection — lower urgency if a meeting is already scheduled with that person
- [ ] Google Drive integration into digest — find relevant docs before meetings that need prep
- [ ] Conversation memory — maintain context across multiple messages in a session (not just last digest)
- [ ] Better error messages to user when services fail (e.g., "Gmail auth expired, re-authorize")
- [ ] Add /mute and /snooze commands (mute a thread, snooze a reminder)

## Low Priority / Future
- [ ] Dropbox file content reading (not just search — actually read doc contents for meeting prep)
- [ ] Email drafting — "want me to draft a reply?" with approval flow
- [ ] Weekly summary — trends, response times, balls dropped vs caught
- [ ] Multi-calendar support (not just primary)
- [ ] SQLite for preferences if JSON gets unwieldy
- [ ] Simplenote API integration (instead of scraping published page)
- [ ] Cost monitoring — track Claude API usage per day/month

## Done
- [x] Telegram bot setup and interactive replies
- [x] Gmail scanning with importance heuristics
- [x] Calendar digest with non-recurring meeting flags
- [x] Claude-powered natural language digest
- [x] Learning system (preferences.json)
- [x] GCP VM deployment (always-on)
- [x] Daily cron at 5:30 AM EST
- [x] Google Drive search
- [x] Dropbox search
- [x] File attachment reading
- [x] Priorities integration (Simplenote)
- [x] Batch Gmail API for performance
- [x] Centralized model config
