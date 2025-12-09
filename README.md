# Slack-to-Jira Bot (Bulletproof Edition)

## What's Fixed

### Problem: "Session expired or bot restarted"
**Root cause**: The previous version stored tickets in memory (`pending_tickets = {}`). If the bot crashed, restarted, or you ran it twice, the data was gone.

**Fix**: Tickets are now stored in a JSON file (`./data/pending_tickets.json`). They survive restarts.

### Other Fixes

| Issue | Fix |
|-------|-----|
| Thread safety | Uses `slack_sdk.WebClient` directly in threads instead of Bolt's client |
| Storage verification | After storing, we read back to verify it worked |
| Detailed logging | Every step prints `[TIMESTAMP] [TAG] message` |
| Clean error handling | All errors are caught, logged, and reported to user |
| Daemon threads | Background threads won't block shutdown |

## Installation

```bash
pip install slack-bolt slack-sdk google-genai requests python-dotenv
cp .env.example .env
# Edit .env with your credentials
python bot.py
```

## How It Works

```
1. React with 🎫
2. Bot adds ⏳ reaction
3. Background thread:
   - Fetches conversation
   - Calls Gemini AI  
   - Saves to ./data/pending_tickets.json  ← PERSISTENT!
   - Verifies storage
   - Sends DM with buttons
   - Changes ⏳ → ✅

4. User clicks "Review & Create"
5. Bot reads from JSON file ← survives restarts!
6. Modal opens

7. User submits
8. Bot acks instantly
9. Background thread creates Jira + uploads files
10. Success DM sent
```

## Logging Output

```
[14:32:01] [REACTION] Received reaction=ticket user=U123ABC
[14:32:01] [WORKER] Starting ticket_id=C123_1234567890.123456
[14:32:02] [WORKER] Channel: #bugs
[14:32:02] [WORKER] Got conversation files=2 chars=450
[14:32:03] [AI] Calling Gemini...
[14:32:04] [AI] Generated ticket title=Fix login button not responding
[14:32:04] [STORAGE] Stored ticket id=C123_1234567890.123456 total=1
[14:32:04] [STORAGE] Found ticket id=C123_1234567890.123456
[14:32:04] [WORKER] Verified storage
[14:32:04] [WORKER] Sent DM
[14:32:04] [WORKER] Done! ticket_id=C123_1234567890.123456
```

## Debugging

### Check pending tickets
```bash
cat ./data/pending_tickets.json
```

### Clear all pending tickets
```bash
rm ./data/pending_tickets.json
```

### Watch logs
The bot prints everything to stdout. Watch for `[STORAGE]` lines to see what's being saved/loaded.

## File Structure

```
./
├── bot.py
├── .env
└── data/
    └── pending_tickets.json   ← auto-created
```

## Still Having Issues?

1. **Check the logs** - every operation is logged
2. **Check the JSON file** - `cat ./data/pending_tickets.json`
3. **Make sure only ONE bot instance is running** - `ps aux | grep bot.py`
4. **Check Slack app permissions** - needs `reactions:read`, `reactions:write`, `chat:write`, `im:write`, `files:read`, `channels:history`, `users:read`
