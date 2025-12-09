"""
Slack-to-Jira Bot (Bulletproof Edition)
- File-based persistence (survives restarts)
- Extensive logging
- Proper error handling
- Thread-safe operations
"""

import os
import json
import requests
import tempfile
import re
import threading
import time
import hashlib
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

# Persistence directory
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)
TICKETS_FILE = DATA_DIR / "pending_tickets.json"

# === INITIALIZE CLIENTS ===
app = App(token=os.environ["SLACK_BOT_TOKEN"])
gemini_client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

# Thread lock for file operations
file_lock = threading.Lock()


def log(tag: str, msg: str, **kwargs):
    """Simple logging with timestamp."""
    ts = time.strftime("%H:%M:%S")
    extras = " ".join(f"{k}={v}" for k, v in kwargs.items())
    print(f"[{ts}] [{tag}] {msg} {extras}".strip())


# === PERSISTENCE LAYER ===
def load_tickets() -> dict:
    """Load tickets from disk."""
    with file_lock:
        if TICKETS_FILE.exists():
            try:
                return json.loads(TICKETS_FILE.read_text())
            except Exception as e:
                log("STORAGE", f"Error loading tickets: {e}")
                return {}
        return {}


def save_tickets(tickets: dict):
    """Save tickets to disk."""
    with file_lock:
        TICKETS_FILE.write_text(json.dumps(tickets, indent=2))


def store_ticket(ticket_id: str, data: dict):
    """Store a single ticket (thread-safe, persistent)."""
    tickets = load_tickets()
    tickets[ticket_id] = data
    save_tickets(tickets)
    log("STORAGE", f"Stored ticket", id=ticket_id, total=len(tickets))


def get_ticket(ticket_id: str) -> dict | None:
    """Get a ticket by ID."""
    tickets = load_tickets()
    ticket = tickets.get(ticket_id)
    if ticket:
        log("STORAGE", f"Found ticket", id=ticket_id)
    else:
        log("STORAGE", f"Ticket NOT FOUND", id=ticket_id, available=list(tickets.keys()))
    return ticket


def pop_ticket(ticket_id: str) -> dict | None:
    """Get and remove a ticket."""
    tickets = load_tickets()
    ticket = tickets.pop(ticket_id, None)
    if ticket:
        save_tickets(tickets)
        log("STORAGE", f"Popped ticket", id=ticket_id)
    return ticket


def delete_ticket(ticket_id: str):
    """Delete a ticket."""
    tickets = load_tickets()
    if ticket_id in tickets:
        del tickets[ticket_id]
        save_tickets(tickets)
        log("STORAGE", f"Deleted ticket", id=ticket_id)


# === SLACK HELPERS ===
def get_conversation(token: str, channel_id: str, message_ts: str):
    """Fetch conversation using raw API (more reliable in threads)."""
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
            # Store file metadata (but not the actual file yet)
            for f in msg["files"]:
                files.append({
                    "name": f.get("name", "unknown"),
                    "url_private": f.get("url_private"),
                    "mode": f.get("mode"),
                    "mimetype": f.get("mimetype")
                })
    
    return "\n".join(formatted), files


def send_dm(token: str, user_id: str, text: str, blocks: list = None):
    """Send DM using raw API."""
    from slack_sdk import WebClient
    client = WebClient(token=token)
    client.chat_postMessage(channel=user_id, text=text, blocks=blocks)


def add_reaction(token: str, channel: str, ts: str, name: str):
    """Add reaction using raw API."""
    from slack_sdk import WebClient
    client = WebClient(token=token)
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception as e:
        log("REACTION", f"Failed to add {name}: {e}")


def remove_reaction(token: str, channel: str, ts: str, name: str):
    """Remove reaction using raw API."""
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
                    log("FILE", f"Uploaded to Jira", file=f["name"])
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


def create_jira_ticket(ticket: dict, slack_link: str, project_key: str, epic_link: str | None):
    """Create Jira ticket."""
    description = ticket["description"] + f"\n\n----\n[View Slack conversation|{slack_link}]"
    
    PRIORITY_MAP = {
        "Highest": "P0 - Critical / Blocker",
        "High": "P1 - High Priority",
        "Medium": "P2 - Medium Priority",
        "Low": "P3 - Low Priority",
        "Needs Priority": "Needs Priority"
    }
    
    fields = {
        "project": {"key": project_key},
        "summary": ticket["title"],
        "description": description,
        "issuetype": {"name": ticket["issue_type"]},
        "priority": {"name": PRIORITY_MAP.get(ticket["priority"], ticket["priority"])},
        "labels": ticket.get("labels", []),
    }
    
    if epic_link and epic_link.strip():
        fields[FIELD_EPIC_LINK] = epic_link.strip()
    
    log("JIRA", f"Creating ticket", project=project_key, epic=epic_link)
    
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
    """Generate ticket using AI."""
    system_prompt = """You convert Slack conversations into Jira tickets.
                
Return JSON with these fields:
{
  "title": "Clear, actionable title under 80 chars",
  "description": "Jira-formatted description using h2. for headers, * for bullets",
  "issue_type": "Story" | "Bug" | "Task",
  "priority": "Needs Priority",
  "labels": ["relevant", "labels"]
}

Guidelines:
- Default to "Story" if unsure
- Bug: Something is broken or not working as expected
- Story: New feature or enhancement request  
- Task: General work item, maintenance, or documentation
- Use Jira markup: h2. for headers, * for bullets, {code} for code blocks"""

    log("AI", "Calling Gemini...")
    
    response = gemini_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=f"Channel: #{channel_name}\n\nConversation:\n{conversation}",
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            temperature=0.3
        )
    )
    
    ticket = json.loads(response.text)
    ticket["priority"] = "Needs Priority"  # Force default
    log("AI", f"Generated ticket", title=ticket["title"][:50])
    return ticket


# === MODAL ===
def build_modal(ticket: dict, ticket_id: str, file_count: int) -> dict:
    """Build the review modal."""
    return {
        "type": "modal",
        "callback_id": f"approve_ticket_{ticket_id}",
        "title": {"type": "plain_text", "text": "Review Ticket"},
        "submit": {"type": "plain_text", "text": "Create Ticket"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "title",
                "label": {"type": "plain_text", "text": "Title"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "title_input",
                    "initial_value": ticket["title"]
                }
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"📎 *{file_count} file(s)* will be attached after creation"}]
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "project",
                "label": {"type": "plain_text", "text": "Project Key"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "project_input",
                    "initial_value": PROJECT_KEY_DEFAULT
                }
            },
            {
                "type": "input",
                "block_id": "epic",
                "label": {"type": "plain_text", "text": "Epic Link (Key)"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "epic_input",
                    "initial_value": EPIC_KEY_DEFAULT,
                    "placeholder": {"type": "plain_text", "text": "e.g., GOD-12345 (leave empty for none)"}
                }
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "type",
                "label": {"type": "plain_text", "text": "Type"},
                "element": {
                    "type": "static_select",
                    "action_id": "type_select",
                    "initial_option": {
                        "text": {"type": "plain_text", "text": ticket["issue_type"]},
                        "value": ticket["issue_type"]
                    },
                    "options": [
                        {"text": {"type": "plain_text", "text": t}, "value": t}
                        for t in ["Story", "Bug", "Task"]
                    ]
                }
            },
            {
                "type": "input",
                "block_id": "priority",
                "label": {"type": "plain_text", "text": "Priority"},
                "element": {
                    "type": "static_select",
                    "action_id": "priority_select",
                    "initial_option": {
                        "text": {"type": "plain_text", "text": ticket["priority"]},
                        "value": ticket["priority"]
                    },
                    "options": [
                        {"text": {"type": "plain_text", "text": p}, "value": p}
                        for p in ["Needs Priority", "Highest", "High", "Medium", "Low"]
                    ]
                }
            },
            {
                "type": "input",
                "block_id": "description",
                "label": {"type": "plain_text", "text": "Description"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "desc_input",
                    "multiline": True,
                    "initial_value": ticket["description"][:3000]  # Slack limit
                }
            }
        ]
    }


# === BACKGROUND WORKERS ===
def process_reaction_worker(channel_id: str, message_ts: str, user_id: str):
    """Background worker for reaction processing."""
    token = os.environ["SLACK_BOT_TOKEN"]
    ticket_id = f"{channel_id}_{message_ts}"
    
    log("WORKER", "Starting", ticket_id=ticket_id)
    
    try:
        # 1. Get channel info
        from slack_sdk import WebClient
        client = WebClient(token=token)
        
        channel_info = client.conversations_info(channel=channel_id)
        channel_name = channel_info["channel"]["name"]
        log("WORKER", f"Channel: #{channel_name}")
        
        # 2. Get conversation and files
        conversation, files = get_conversation(token, channel_id, message_ts)
        log("WORKER", f"Got conversation", files=len(files), chars=len(conversation))
        
        # 3. Get permalink
        permalink = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        slack_link = permalink["permalink"]
        
        # 4. Generate ticket with AI
        ticket = generate_ticket(channel_name, conversation)
        
        # 5. Store ticket (PERSISTENT - survives restart!)
        store_ticket(ticket_id, {
            "ticket": ticket,
            "slack_link": slack_link,
            "channel_id": channel_id,
            "message_ts": message_ts,
            "files": files,
            "user_id": user_id,
            "created_at": time.time()
        })
        
        # 6. VERIFY it was stored
        verify = get_ticket(ticket_id)
        if not verify:
            raise Exception("Failed to store ticket!")
        log("WORKER", "Verified storage")
        
        # 7. NOW send DM (ticket is definitely stored)
        dm_response = client.chat_postMessage(
            channel=user_id,
            text=f"New ticket draft: {ticket['title']}",
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*🎫 New Ticket Draft*\n\n*{ticket['title']}*"}
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Type:* {ticket['issue_type']}"},
                        {"type": "mrkdwn", "text": f"*Priority:* {ticket['priority']}"},
                        {"type": "mrkdwn", "text": f"*Files:* {len(files)}"}
                    ]
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ Review & Create"},
                            "style": "primary",
                            "action_id": "edit_ticket",
                            "value": ticket_id
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "❌ Cancel"},
                            "style": "danger",
                            "action_id": "cancel_ticket",
                            "value": ticket_id
                        }
                    ]
                }
            ]
        )
        
        # Store DM info so we can update it later
        dm_channel = dm_response["channel"]
        dm_ts = dm_response["ts"]
        
        # Update stored ticket with DM info
        store_ticket(ticket_id, {
            "ticket": ticket,
            "slack_link": slack_link,
            "channel_id": channel_id,
            "message_ts": message_ts,
            "files": files,
            "user_id": user_id,
            "created_at": time.time(),
            "dm_channel": dm_channel,
            "dm_ts": dm_ts
        })
        
        log("WORKER", "Sent DM", dm_ts=dm_ts)
        
        # 8. Update reactions
        remove_reaction(token, channel_id, message_ts, "hourglass_flowing_sand")
        add_reaction(token, channel_id, message_ts, "white_check_mark")
        
        log("WORKER", "Done!", ticket_id=ticket_id)
        
    except Exception as e:
        log("WORKER", f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        
        # Clean up reaction and notify user
        remove_reaction(token, channel_id, message_ts, "hourglass_flowing_sand")
        add_reaction(token, channel_id, message_ts, "x")
        
        try:
            send_dm(token, user_id, f"❌ Error generating ticket: {str(e)[:200]}")
        except:
            pass


def create_ticket_worker(user_id: str, ticket_data: dict, ticket: dict, project_key: str, epic_link: str | None):
    """Background worker for ticket creation."""
    token = os.environ["SLACK_BOT_TOKEN"]
    
    log("CREATE", "Starting ticket creation")
    
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        
        # 1. Create Jira ticket
        key, url = create_jira_ticket(ticket, ticket_data["slack_link"], project_key, epic_link)
        
        # 2. Upload files
        files = ticket_data.get("files", [])
        uploaded, failed = [], []
        
        if files:
            # Update draft to show uploading status
            dm_channel = ticket_data.get("dm_channel")
            dm_ts = ticket_data.get("dm_ts")
            if dm_channel and dm_ts:
                try:
                    client.chat_update(
                        channel=dm_channel,
                        ts=dm_ts,
                        text=f"Uploading files to {key}...",
                        blocks=[
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": f"*✅ {key} Created!*\n\n*{ticket['title']}*"}
                            },
                            {
                                "type": "context",
                                "elements": [{"type": "mrkdwn", "text": f"📎 Uploading {len(files)} file(s)..."}]
                            }
                        ]
                    )
                except:
                    pass
            uploaded, failed = attach_files_to_jira(key, files, token)
        
        # 3. Build completion message
        attachment_text = ""
        if uploaded:
            attachment_text = f"\n📎 *Attached:* {', '.join(uploaded)}"
        if failed:
            attachment_text += f"\n⚠️ *Failed to attach:* {', '.join(failed)}"
        
        # 4. Update the original draft message to show completed state
        dm_channel = ticket_data.get("dm_channel")
        dm_ts = ticket_data.get("dm_ts")
        
        if dm_channel and dm_ts:
            try:
                client.chat_update(
                    channel=dm_channel,
                    ts=dm_ts,
                    text=f"Ticket Created: {key}",
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"*✅ Ticket Created*\n\n*<{url}|{key}>*: {ticket['title']}"}
                        },
                        {
                            "type": "section",
                            "fields": [
                                {"type": "mrkdwn", "text": f"*Type:* {ticket['issue_type']}"},
                                {"type": "mrkdwn", "text": f"*Priority:* {ticket['priority']}"},
                                {"type": "mrkdwn", "text": f"*Project:* {project_key}"},
                                {"type": "mrkdwn", "text": f"*Epic:* {epic_link or 'None'}"}
                            ]
                        },
                        {
                            "type": "context",
                            "elements": [{"type": "mrkdwn", "text": f"📎 {len(uploaded)} file(s) attached" if uploaded else "No attachments"}]
                        }
                    ]
                )
                log("CREATE", "Updated draft message to completed state")
            except Exception as e:
                log("CREATE", f"Failed to update draft message: {e}")
                # Still send success message as fallback
                send_dm(token, user_id, f"✅ *Ticket Created:* <{url}|{key}>{attachment_text}")
        else:
            # Fallback: send new message if we don't have DM info
            send_dm(token, user_id, f"✅ *Ticket Created:* <{url}|{key}>{attachment_text}")
        
        log("CREATE", "Done!", key=key, uploaded=len(uploaded), failed=len(failed))
        
    except Exception as e:
        log("CREATE", f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_dm(token, user_id, f"❌ Failed to create ticket: {str(e)[:200]}")


# === EVENT HANDLERS ===
@app.event("reaction_added")
def handle_reaction(event, client, logger):
    """Handle reaction trigger."""
    if event["reaction"] not in ["ticket", "jira", "memo"]:
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
        target=process_reaction_worker,
        args=(channel_id, message_ts, user_id),
        daemon=True
    )
    thread.start()


@app.action("edit_ticket")
def handle_edit(ack, body, client):
    """Open edit modal."""
    ack()
    
    ticket_id = body["actions"][0]["value"]
    log("EDIT", f"Button clicked", ticket_id=ticket_id)
    
    # Get ticket from persistent storage
    data = get_ticket(ticket_id)
    
    if not data:
        log("EDIT", "TICKET NOT FOUND!")
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=body["user"]["id"],
            text="⚠️ Ticket not found. Please react with 🎫 again to generate a new draft."
        )
        return
    
    try:
        file_count = len(data.get("files", []))
        modal = build_modal(data["ticket"], ticket_id, file_count)
        client.views_open(trigger_id=body["trigger_id"], view=modal)
        log("EDIT", "Modal opened")
    except Exception as e:
        log("EDIT", f"Error opening modal: {e}")
        import traceback
        traceback.print_exc()


@app.action("cancel_ticket")
def handle_cancel(ack, body, client):
    """Cancel ticket."""
    ack()
    
    ticket_id = body["actions"][0]["value"]
    data = get_ticket(ticket_id)
    delete_ticket(ticket_id)
    
    # Update the message to show cancelled state (no buttons)
    client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text="🚫 Ticket Cancelled",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*🚫 Ticket Cancelled*\n\n~{data['ticket']['title'] if data else 'Draft'}~"}
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "React with 🎫 again to create a new draft"}]
            }
        ]
    )
    log("CANCEL", "Ticket cancelled", ticket_id=ticket_id)


@app.view(re.compile(r"approve_ticket_.+"))
def handle_modal_submit(ack, body, client, logger):
    """Handle modal submission."""
    ack()  # MUST be first!
    
    callback_id = body["view"]["callback_id"]
    ticket_id = callback_id.replace("approve_ticket_", "")
    user_id = body["user"]["id"]
    
    log("SUBMIT", f"Modal submitted", ticket_id=ticket_id)
    
    # Get and remove from storage
    data = pop_ticket(ticket_id)
    
    if not data:
        log("SUBMIT", "TICKET NOT FOUND!")
        client.chat_postMessage(channel=user_id, text="❌ Ticket data not found. Please try again.")
        return
    
    values = body["view"]["state"]["values"]
    
    # Build ticket from form values
    ticket = {
        "title": values["title"]["title_input"]["value"],
        "issue_type": values["type"]["type_select"]["selected_option"]["value"],
        "priority": values["priority"]["priority_select"]["selected_option"]["value"],
        "description": values["description"]["desc_input"]["value"],
        "labels": data["ticket"].get("labels", [])
    }
    
    project_key = values["project"]["project_input"]["value"].strip()
    epic_input = values["epic"]["epic_input"]
    epic_link = epic_input.get("value", "").strip() if epic_input else None
    
    # Notify user we're working by updating the draft message temporarily
    dm_channel = data.get("dm_channel")
    dm_ts = data.get("dm_ts")
    
    if dm_channel and dm_ts:
        try:
            client.chat_update(
                channel=dm_channel,
                ts=dm_ts,
                text=f"Creating ticket: {ticket['title']}",
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*⏳ Creating Ticket...*\n\n*{ticket['title']}*"}
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Type:* {ticket['issue_type']}"},
                            {"type": "mrkdwn", "text": f"*Priority:* {ticket['priority']}"}
                        ]
                    },
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": "Please wait..."}]
                    }
                ]
            )
        except:
            pass
    
    # Create in background
    thread = threading.Thread(
        target=create_ticket_worker,
        args=(user_id, data, ticket, project_key, epic_link),
        daemon=True
    )
    thread.start()


# === STARTUP ===
if __name__ == "__main__":
    # Show pending tickets on startup
    tickets = load_tickets()
    log("STARTUP", f"Bot starting", pending_tickets=len(tickets))
    log("STARTUP", f"Project: {PROJECT_KEY_DEFAULT}, Epic: {EPIC_KEY_DEFAULT}")
    log("STARTUP", f"Data dir: {DATA_DIR.absolute()}")
    
    if tickets:
        log("STARTUP", f"Pending ticket IDs: {list(tickets.keys())}")
    
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
