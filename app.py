import os
import re
import time
import threading
from datetime import datetime, timedelta

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

load_dotenv()

# when testing in dev
TEST_CHANNEL_ID = "C09TCJH10JX"      # connor-dev (for local testing)
DEFAULT_SALES_CHANNEL_ID = "C09LD0UH38E"  # #sales-force-won

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
LEADERS_CHANNEL_ID = os.getenv("LEADERS_CHANNEL_ID")

# Use env var if set; otherwise default to the prod channel
SALES_CHANNEL_ID = os.getenv("SALES_CHANNEL_ID", DEFAULT_SALES_CHANNEL_ID)

print(f"[startup] SALES_CHANNEL_ID = {SALES_CHANNEL_ID}")


app = App(token=SLACK_BOT_TOKEN)

# ---- AUTH / DEBUG AT STARTUP ----
try:
    auth = app.client.auth_test()
    print("\n=== AUTH TEST ===")
    print(auth)
    print("=================\n")
except SlackApiError as e:
    print(f"auth_test failed: {e.response['error']}")

try:
    info = app.client.conversations_info(channel=SALES_CHANNEL_ID)
    print("\n=== CONVERSATIONS INFO ===")
    print(info)
    print("==========================\n")
except SlackApiError as e:
    print(f"conversations_info error: {e.response['error']}")

try:
    members = app.client.conversations_members(channel=SALES_CHANNEL_ID)
    print("\n=== CONVERSATIONS MEMBERS ===")
    print(members)
    print("=============================\n")
except SlackApiError as e:
    print(f"conversations_members error: {e.response['error']}")

# ---- REGEX HELPERS ----
EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_REGEX = re.compile(
    r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b"
)

def format_route_update(user_id: str, text: str) -> str:
    return (
        "🚐 *Route Update*\n"
        f"*From:* <@{user_id}>\n"
        f"*Message:* {text.strip()}\n"
    )

# ---- LEAD STATE + REMINDERS + WEEKLY STATS ----

# Tracks per-lead info (for reminders + posted_at)
lead_state_lock = threading.Lock()
# key: root_ts of the original lead message
# value: dict(channel, posted_at, claimed_by, claimed_at, completed)
lead_state = {}

# Weekly stats: how many leads each person closed in a given "Ransford week"
# week_key = period_start.isoformat() where period_start is Friday 3pm local
weekly_lock = threading.Lock()
# weekly_stats[week_key][user_id] = count
weekly_stats = {}

def current_period_start(dt: datetime) -> datetime:
    """
    Given a datetime, return the start of the "Ransford week" that dt belongs to.
    A week runs from Friday 3:00pm -> next Friday 3:00pm (exclusive).
    """
    # Friday is weekday() == 4
    days_since_friday = (dt.weekday() - 4) % 7
    # Candidate Friday 3pm in the same "week"
    candidate = datetime(dt.year, dt.month, dt.day, 15, 0) - timedelta(days=days_since_friday)
    if dt < candidate:
        # We're before this week's Friday 3pm -> go back one more week
        candidate -= timedelta(days=7)
    return candidate


def schedule_unclaimed_reminder(root_ts: str, delay_seconds: int = 20 * 60):
    """Ping the channel thread if lead is still unclaimed after delay."""
    def _check():
        with lead_state_lock:
            state = lead_state.get(root_ts)
            if not state:
                return
            # If claimed or completed, skip
            if state.get("completed") or state.get("claimed_by") is not None:
                return
            channel = state["channel"]

        try:
            app.client.chat_postMessage(
                channel=channel,
                thread_ts=root_ts,
                text="⏰ <!here> This lead hasn't been claimed in 20 minutes. "
                     "Can someone take it?",
            )
        except SlackApiError as e:
            print(f"unclaimed reminder error: {e.response['error']}")

    t = threading.Timer(delay_seconds, _check)
    t.daemon = True
    t.start()


def schedule_unclosed_reminder(root_ts: str, user_id: str, delay_seconds: int = 2 * 60 * 60):
    """DM the claimer if the lead is still not completed after delay."""
    def _check():
        with lead_state_lock:
            state = lead_state.get(root_ts)
            print(f"[unclosed_check] state for {root_ts} =", state)

            if not state:
                # Lead no longer tracked (bot restarted, etc.)
                return
            # If completed or no longer claimed by this user, skip
            if state.get("completed"):
                print(f"[unclosed_check] lead {root_ts} already completed")
                return
            if state.get("claimed_by") != user_id:
                print(f"[unclosed_check] lead {root_ts} claimed by someone else now")
                return

            channel = state["channel"]

        try:
            # Get a permalink to the original lead message/thread
            perm = app.client.chat_getPermalink(
                channel=channel,
                message_ts=root_ts,
            )
            permalink = perm.get("permalink")

            dm = app.client.conversations_open(users=user_id)
            dm_channel = dm["channel"]["id"]

            text = (
                "⏰ Reminder: You claimed a lead a while ago and it hasn't been "
                "marked done yet. Please follow up or close it out when you're finished."
            )
            if permalink:
                text += f"\n\n<{permalink}|Open the lead thread>"

            app.client.chat_postMessage(
                channel=dm_channel,
                text=text,
            )
            print(f"[unclosed_check] sent DM reminder to {user_id} for {root_ts}")

        except SlackApiError as e:
            print("unclosed reminder error:", e.response.get("error"))

    t = threading.Timer(delay_seconds, _check)
    t.daemon = True
    t.start()


def post_weekly_leaderboard():
    """Compute and post the leaderboard for the week that just ended."""
    now = datetime.now()
    # Just-ended week: look at dt one second before the cutoff
    period_end = now
    period_start = current_period_start(now - timedelta(seconds=1))
    key = period_start.isoformat()

    with weekly_lock:
        stats = weekly_stats.get(key, {}).copy()

    # Build nice label
    period_end_display = period_start + timedelta(days=7)
    header = (
        f":trophy: *Weekly Lead Leaderboard*\n"
        f"Period: {period_start.strftime('%b %d, %I:%M %p')} – "
        f"{period_end_display.strftime('%b %d, %I:%M %p')}"
    )

    if not stats:
        text = header + "\n\n_No leads were completed in this period._"
    else:
        lines = [header, ""]
        # Sort by count descending
        sorted_stats = sorted(stats.items(), key=lambda kv: kv[1], reverse=True)

        for rank, (user_id, count) in enumerate(sorted_stats, start=1):
            if rank == 1:
                lines.append(
                    f"*1) :crown: Lead Legend:* <@{user_id}> – *{count}* lead"
                    f"{'' if count == 1 else 's'}"
                )
            else:
                lines.append(
                    f"{rank}) <@{user_id}> – {count} lead"
                    f"{'' if count == 1 else 's'}"
                )

        text = "\n".join(lines)

    try:
        app.client.chat_postMessage(
            channel=SALES_CHANNEL_ID,
            text=text,
        )
        print(f"[leaderboard] posted leaderboard for period starting {period_start}")
    except SlackApiError as e:
        print(f"[leaderboard] error posting leaderboard: {e.response['error']}")

# ---- SINGLE MESSAGE HANDLER ----
@app.event("message")
def handle_all_messages(event, say, client, logger):
    try:
        # DEBUG: dump every message event we get
        print("\n=== DEBUG EVENT ===")
        print(event)
        print("===================\n")

        subtype = event.get("subtype")
        channel_id = event.get("channel")
        channel_type = event.get("channel_type")

        # Ignore most bot messages (to avoid loops)
        if subtype == "bot_message":
            return

        # ------- 1) DM ROUTING LOGIC -------
        if channel_type == "im":
            user = event.get("user")
            text = (event.get("text") or "").strip()
            if not text:
                return

            # Forward message to leadership channel
            client.chat_postMessage(
                channel=LEADERS_CHANNEL_ID,
                text=format_route_update(user, text),
            )

            # Confirm privately
            say("Got it 👍 I’ve sent this to the scheduling / office team.")
            return  # done for DM

        # ------- 2) SALES / LEADS CHANNEL LOGIC -------
        if channel_id == SALES_CHANNEL_ID:
            # Only act on ROOT messages (not replies to our own threads)
            if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
                return

            text = (event.get("text") or "").strip()
            full_text = text

            # Top-level blocks (for normal text messages)
            for block in event.get("blocks") or []:
                if block.get("type") == "section" and "text" in block:
                    full_text += "\n" + (block["text"].get("text") or "")

            # Pull text out of attachments (tables from the form paste)
            for att in event.get("attachments") or []:
                if att.get("text"):
                    full_text += "\n" + att["text"]

                for block in att.get("blocks") or []:
                    if block.get("type") == "table":
                        for row in block.get("rows", []):
                            for cell in row:
                                cell_type = cell.get("type")
                                if cell_type == "raw_text":
                                    full_text += "\n" + cell.get("text", "")
                                elif cell_type == "rich_text":
                                    for elt in cell.get("elements", []):
                                        if elt.get("type") == "rich_text_section":
                                            for part in elt.get("elements", []):
                                                if part.get("type") == "text":
                                                    full_text += "\n" + part.get("text", "")
                    elif block.get("type") == "section" and "text" in block:
                        full_text += "\n" + (block["text"].get("text") or "")

            if not full_text.strip():
                return

            looks_like_lead = (
                EMAIL_REGEX.search(full_text)
                or PHONE_REGEX.search(full_text)
                or ("Email:" in full_text and "Phone:" in full_text)
            )

            print(f"looks_like_lead={bool(looks_like_lead)} for message: {full_text!r}")

            if not looks_like_lead:
                return

            root_ts = event["ts"]

            # Track the new lead
            with lead_state_lock:
                lead_state[root_ts] = {
                    "channel": channel_id,
                    "posted_at": time.time(),
                    "claimed_by": None,
                    "claimed_at": None,
                    "completed": False,
                }

            # Schedule 20-min unclaimed reminder
            schedule_unclaimed_reminder(root_ts)

            # Post status + buttons in a thread
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=root_ts,
                text="Lead status",
                blocks=[
                    {
                        "type": "section",
                        "block_id": "lead_status",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Lead status:* Unassigned\n",
                        },
                    },
                    {
                        "type": "actions",
                        "block_id": "lead_actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "claim_lead",
                                "text": {"type": "plain_text", "text": "I'll take it"},
                                "style": "primary",
                                "value": "claim",
                            },
                            {
                                "type": "button",
                                "action_id": "mark_lead_done",
                                "text": {"type": "plain_text", "text": "Mark done"},
                                "style": "danger",
                                "value": "done",
                            },
                        ],
                    },
                ],
            )

    except Exception as e:
        logger.error(f"handle_all_messages error: {e}")
        print(f"handle_all_messages error: {e}")

# ---- BUTTON HANDLERS (WITH REMINDERS + WEEKLY COUNTS) ----
@app.action("claim_lead")
def handle_claim_lead(ack, body, client, logger):
    ack()
    try:
        user_id = body["user"]["id"]
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        # Get root thread timestamp (original lead message)
        root_ts = (
            body.get("container", {}).get("thread_ts")
            or body["message"].get("thread_ts")
            or body["message"]["ts"]
        )

        # Update lead state
        with lead_state_lock:
            state = lead_state.get(root_ts)
            if state:
                state["claimed_by"] = user_id
                state["claimed_at"] = time.time()

        # Kick off 2-hour "not closed" reminder for this user
        schedule_unclosed_reminder(root_ts, user_id)

        # New status text with who + when
        status_text = (
            f"*Lead status:* Assigned to <@{user_id}>\n"
            f"_Claimed at <!date^{int(time.time())}^{{time}} {{date_short}}|now>_"
        )

        # Rebuild blocks: status + Mark done button
        new_blocks = [
            {
                "type": "section",
                "block_id": "lead_status",
                "text": {"type": "mrkdwn", "text": status_text},
            },
            {
                "type": "actions",
                "block_id": "lead_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "mark_lead_done",
                        "text": {"type": "plain_text", "text": "Mark done"},
                        "style": "danger",
                        "value": "done",
                    },
                ],
            },
        ]

        client.chat_update(
            channel=channel,
            ts=ts,
            text=f"Lead assigned to <@{user_id}>",
            blocks=new_blocks,
        )

    except Exception as e:
        logger.error(f"Claim handler error: {e}")
        print(f"Claim handler error: {e}")


@app.action("mark_lead_done")
def handle_mark_lead_done(ack, body, client, logger):
    ack()
    try:
        user_id = body["user"]["id"]
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        # Get root thread timestamp (original lead message)
        root_ts = (
            body.get("container", {}).get("thread_ts")
            or body["message"].get("thread_ts")
            or body["message"]["ts"]
        )

        # Mark as completed and grab posted_at for weekly bucket
        with lead_state_lock:
            state = lead_state.get(root_ts)
            if state:
                state["completed"] = True
                posted_at_ts = state.get("posted_at", time.time())
            else:
                posted_at_ts = time.time()

        # Figure out which "Ransford week" this lead belongs to
        posted_dt = datetime.fromtimestamp(posted_at_ts)
        period_start = current_period_start(posted_dt)
        week_key = period_start.isoformat()

        with weekly_lock:
            bucket = weekly_stats.setdefault(week_key, {})
            bucket[user_id] = bucket.get(user_id, 0) + 1
            print(f"[weekly] counted lead for {user_id} in week starting {period_start}")

        status_text = (
            f":white_check_mark: *Lead handled by <@{user_id}>*\n"
            f"_Completed at <!date^{int(time.time())}^{{time}} {{date_short}}|now>_"
        )

        # Final status: no buttons
        new_blocks = [
            {
                "type": "section",
                "block_id": "lead_status",
                "text": {"type": "mrkdwn", "text": status_text},
            }
        ]

        client.chat_update(
            channel=channel,
            ts=ts,
            text=f"Lead completed by <@{user_id}>",
            blocks=new_blocks,
        )

    except Exception as e:
        logger.error(f"Mark done handler error: {e}")
        print(f"Mark done handler error: {e}")

# ---- MAIN ----
if __name__ == "__main__":

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
