"""
Slack-to-Jira Bot (Auto-Create Edition)
React with 🎫 → Ticket created automatically. No confirmation needed.

- Type: Always "Story"
- Priority: Always "Needs Priority"
- Epic: Uses DEFAULT_EPIC from .env
"""

import os
import json
import requests
import tempfile
import threading
import time
from pathlib import Path
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai
from google.genai import types

load_dotenv()

# === CONFIGURATION ===
JIRA_URL = os.environ["JIRA_BASE_URL"]
JIRA_AUTH = HTTPBasicAuth(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
FIELD_EPIC_LINK = "customfield_10014"
PROJECT_KEY_DEFAULT = os.environ.get("DEFAULT_PROJECT", "GOD")
EPIC_KEY_DEFAULT = os.environ.get("DEFAULT_EPIC", "GOD-26345")

# === INITIALIZE CLIENTS ===
app = App(token=os.environ["SLACK_BOT_TOKEN"])
gemini_client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])


def log(tag: str, msg: str, **kwargs):
    """Simple logging with timestamp."""
    ts = time.strftime("%H:%M:%S")
    extras = " ".join(f"{k}={v}" for k, v in kwargs.items())
    print(f"[{ts}] [{tag}] {msg} {extras}".strip())


# === SLACK HELPERS ===
def get_conversation(token: str, channel_id: str, message_ts: str):
    """Fetch conversation and files."""
    from slack_sdk import WebClient
    client = WebClient(token=token)
    
    result = client.conversations_replies(channel=channel_id, ts=message_ts, limit=50)
    messages = result.get("messages", [])
    
    formatted = []
    files = []
    user_cache = {}
    
    for msg in messages:
        user_id = msg.get("user", "unknown")
        
        if user_id not in user_cache:
            try:
                user_info = client.users_info(user=user_id)
                user_cache[user_id] = user_info["user"]["real_name"] if user_info.get("user") else "Unknown"
            except:
                user_cache[user_id] = "Unknown"
        
        formatted.append(f"@{user_cache[user_id]}: {msg.get('text', '')}")
        
        if "files" in msg:
            for f in msg["files"]:
                files.append({
                    "name": f.get("name", "unknown"),
                    "url_private": f.get("url_private"),
                    "mode": f.get("mode"),
                    "mimetype": f.get("mimetype")
                })
    
    return "\n".join(formatted), files


def send_dm(token: str, user_id: str, text: str, blocks: list = None):
    """Send DM."""
    from slack_sdk import WebClient
    client = WebClient(token=token)
    return client.chat_postMessage(channel=user_id, text=text, blocks=blocks)


def update_dm(token: str, channel: str, ts: str, text: str, blocks: list = None):
    """Update a DM message."""
    from slack_sdk import WebClient
    client = WebClient(token=token)
    client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)


def add_reaction(token: str, channel: str, ts: str, name: str):
    """Add reaction."""
    from slack_sdk import WebClient
    client = WebClient(token=token)
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=name)
    except:
        pass


def remove_reaction(token: str, channel: str, ts: str, name: str):
    """Remove reaction."""
    from slack_sdk import WebClient
    client = WebClient(token=token)
    try:
        client.reactions_remove(channel=channel, timestamp=ts, name=name)
    except:
        pass


# === JIRA HELPERS ===
def download_slack_file(file_info: dict, token: str) -> str | None:
    """Download file from Slack."""
    url = file_info.get("url_private")
    if not url:
        return None
    
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            stream=True,
            timeout=30
        )
        if response.status_code == 200:
            fd, path = tempfile.mkstemp(suffix=f"_{file_info['name']}")
            with os.fdopen(fd, 'wb') as tmp:
                for chunk in response.iter_content(chunk_size=8192):
                    tmp.write(chunk)
            return path
    except Exception as e:
        log("FILE", f"Download failed: {e}", file=file_info.get("name"))
    return None


def attach_files_to_jira(issue_key: str, file_list: list, token: str):
    """Upload files to Jira issue."""
    url = f"{JIRA_URL}/rest/api/2/issue/{issue_key}/attachments"
    headers = {"X-Atlassian-Token": "no-check"}
    
    uploaded, failed = [], []
    
    for f in file_list:
        if f.get("mode") == "external":
            continue
        
        path = download_slack_file(f, token)
        if not path:
            failed.append(f.get("name", "unknown"))
            continue
        
        try:
            with open(path, 'rb') as file_obj:
                r = requests.post(
                    url,
                    auth=JIRA_AUTH,
                    headers=headers,
                    files={'file': (f["name"], file_obj)},
                    timeout=60
                )
                if r.status_code == 200:
                    uploaded.append(f["name"])
                    log("FILE", f"Uploaded", file=f["name"])
                else:
                    failed.append(f["name"])
                    log("FILE", f"Upload failed: {r.status_code}", file=f["name"])
        except Exception as e:
            log("FILE", f"Upload error: {e}", file=f["name"])
            failed.append(f.get("name", "unknown"))
        finally:
            if os.path.exists(path):
                os.remove(path)
    
    return uploaded, failed


def create_jira_ticket(title: str, description: str, slack_link: str):
    """Create Jira ticket with fixed type and priority."""
    full_description = description + f"\n\n----\n[View Slack conversation|{slack_link}]"
    
    fields = {
        "project": {"key": PROJECT_KEY_DEFAULT},
        "summary": title,
        "description": full_description,
        "issuetype": {"name": "Story"},  # Always Story
        "priority": {"name": "Needs Priority"},  # Always Needs Priority
        "labels": [],
    }
    
    if EPIC_KEY_DEFAULT:
        fields[FIELD_EPIC_LINK] = EPIC_KEY_DEFAULT
    
    log("JIRA", f"Creating ticket", project=PROJECT_KEY_DEFAULT, epic=EPIC_KEY_DEFAULT)
    
    response = requests.post(
        f"{JIRA_URL}/rest/api/2/issue",
        json={"fields": fields},
        auth=JIRA_AUTH,
        headers={"Content-Type": "application/json"},
        timeout=30
    )
    
    if response.status_code != 201:
        log("JIRA", f"Error: {response.status_code} - {response.text}")
        raise Exception(f"Jira API error: {response.status_code} - {response.text[:200]}")
    
    data = response.json()
    key = data["key"]
    url = f"{JIRA_URL}/browse/{key}"
    log("JIRA", f"Created ticket", key=key)
    return key, url


# === AI ===
def generate_ticket(channel_name: str, conversation: str) -> dict:
    """Generate ticket title and description using AI."""
    system_prompt = """You convert Slack conversations into Jira tickets.
                
Return JSON with these fields:
{
  "title": "Clear, actionable title under 80 chars",
  "description": "Jira-formatted description using h2. for headers, * for bullets"
}

Guidelines:
- Title should be a clear summary of what needs to be done
- Description should include context, requirements, and any relevant details
- Use Jira markup: h2. for headers, * for bullets, {code} for code blocks
- Extract key information from the conversation"""

    log("AI", "Calling Gemini...")
    
    response = gemini_client.models.generate_content(
        model="gemini-3-pro-preview",
        contents=f"Channel: #{channel_name}\n\nConversation:\n{conversation}",
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            temperature=0.3
        )
    )
    
    ticket = json.loads(response.text)
    log("AI", f"Generated", title=ticket["title"][:50])
    return ticket


# === MAIN WORKER ===
def process_and_create_ticket(channel_id: str, message_ts: str, user_id: str):
    """Process reaction and create ticket immediately."""
    token = os.environ["SLACK_BOT_TOKEN"]
    
    log("WORKER", "Starting", channel=channel_id, ts=message_ts)
    
    dm_channel = None
    dm_ts = None
    
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        
        # 1. Send initial DM
        dm_response = send_dm(token, user_id, "🎫 Creating ticket...", blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*⏳ Creating Jira Ticket...*\n\nAnalyzing conversation..."}
            }
        ])
        dm_channel = dm_response["channel"]
        dm_ts = dm_response["ts"]
        
        # 2. Get channel info
        channel_info = client.conversations_info(channel=channel_id)
        channel_name = channel_info["channel"]["name"]
        log("WORKER", f"Channel: #{channel_name}")
        
        # 3. Get conversation and files
        conversation, files = get_conversation(token, channel_id, message_ts)
        log("WORKER", f"Got conversation", files=len(files), chars=len(conversation))
        
        # 4. Get permalink
        permalink = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        slack_link = permalink["permalink"]
        
        # 5. Generate ticket with AI
        update_dm(token, dm_channel, dm_ts, "Generating ticket...", blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*⏳ Creating Jira Ticket...*\n\nGenerating title and description..."}
            }
        ])
        
        ticket = generate_ticket(channel_name, conversation)
        
        # 6. Create Jira ticket
        update_dm(token, dm_channel, dm_ts, "Creating in Jira...", blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*⏳ Creating Jira Ticket...*\n\n*{ticket['title']}*\n\nCreating in Jira..."}
            }
        ])
        
        key, url = create_jira_ticket(ticket["title"], ticket["description"], slack_link)
        
        # 7. Upload files if any
        uploaded, failed = [], []
        if files:
            update_dm(token, dm_channel, dm_ts, f"Uploading {len(files)} file(s)...", blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*✅ {key} Created!*\n\n*{ticket['title']}*"}
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"📎 Uploading {len(files)} file(s)..."}]
                }
            ])
            uploaded, failed = attach_files_to_jira(key, files, token)
        
        # 8. Final success message
        attachment_info = ""
        if uploaded:
            attachment_info = f"📎 {len(uploaded)} file(s) attached"
        if failed:
            attachment_info += f" | ⚠️ {len(failed)} failed"
        if not attachment_info:
            attachment_info = "No attachments"
        
        update_dm(token, dm_channel, dm_ts, f"Ticket Created: {key}", blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*✅ Ticket Created*\n\n*<{url}|{key}>*: {ticket['title']}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Type:* Story"},
                    {"type": "mrkdwn", "text": f"*Priority:* Needs Priority"},
                    {"type": "mrkdwn", "text": f"*Project:* {PROJECT_KEY_DEFAULT}"},
                    {"type": "mrkdwn", "text": f"*Epic:* {EPIC_KEY_DEFAULT or 'None'}"}
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": attachment_info}]
            }
        ])
        
        # 9. Update reactions on original message
        remove_reaction(token, channel_id, message_ts, "hourglass_flowing_sand")
        add_reaction(token, channel_id, message_ts, "white_check_mark")
        
        log("WORKER", "Done!", key=key)
        
    except Exception as e:
        log("WORKER", f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        
        # Update DM with error
        if dm_channel and dm_ts:
            update_dm(token, dm_channel, dm_ts, f"❌ Failed: {e}", blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*❌ Failed to Create Ticket*\n\n{str(e)[:200]}"}
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "Please try again or create manually"}]
                }
            ])
        else:
            send_dm(token, user_id, f"❌ Failed to create ticket: {str(e)[:200]}")
        
        # Update reactions
        remove_reaction(token, channel_id, message_ts, "hourglass_flowing_sand")
        add_reaction(token, channel_id, message_ts, "x")


# === EVENT HANDLER ===
@app.event("reaction_added")
def handle_reaction(event, client, logger):
    """Handle reaction - immediately create ticket."""
    if event["reaction"] not in ["uiux"]:
        return
    
    channel_id = event["item"]["channel"]
    message_ts = event["item"]["ts"]
    user_id = event["user"]
    
    log("REACTION", f"Received", reaction=event["reaction"], user=user_id)
    
    # Add loading reaction
    try:
        client.reactions_add(channel=channel_id, timestamp=message_ts, name="hourglass_flowing_sand")
    except:
        pass
    
    # Process in background
    thread = threading.Thread(
        target=process_and_create_ticket,
        args=(channel_id, message_ts, user_id),
        daemon=True
    )
    thread.start()


# === STARTUP ===
if __name__ == "__main__":
    log("STARTUP", "Bot starting (Auto-Create Mode)")
    log("STARTUP", f"Project: {PROJECT_KEY_DEFAULT}")
    log("STARTUP", f"Epic: {EPIC_KEY_DEFAULT}")
    log("STARTUP", "Type: Story (fixed)")
    log("STARTUP", "Priority: Needs Priority (fixed)")
    
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
