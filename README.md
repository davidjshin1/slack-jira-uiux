# Slack-to-Jira Bot (Auto-Create)

React with :uiux: → Ticket created immediately. No confirmation modal.

## Fixed Values

| Field | Value |
|-------|-------|
| Type | Always **Story** |
| Priority | Always **Needs Priority** |
| Project | From `DEFAULT_PROJECT` env var |
| Epic | From `DEFAULT_EPIC` env var |

## Flow

```
1. Someone reacts with 🎫 to any message
2. Bot adds ⏳ reaction
3. You get a DM showing progress:
   - "Analyzing conversation..."
   - "Generating title..."
   - "Creating in Jira..."
   - "Uploading files..."
4. DM updates to show completed ticket
5. Original message gets ✅ reaction
```

## Setup

```bash
pip install slack-bolt slack-sdk google-genai requests python-dotenv
cp .env.example .env
# Edit .env
python bot.py
```

## Deploy to Railway

1. Push to GitHub
2. railway.app → New Project → Deploy from GitHub
3. Add environment variables
4. Done

## Differences from Confirmation Version

| Feature | Confirmation Version | Auto-Create Version |
|---------|---------------------|---------------------|
| Modal | Yes | No |
| Edit before create | Yes | No |
| Change type/priority | Yes | No |
| Change epic | Yes | No |
| Speed | ~10 sec + user time | ~5 sec |
| User control | High | Low |

Use this version when you want maximum speed and consistency.
Use the confirmation version when users need to review/edit tickets.
