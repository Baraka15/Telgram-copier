import os
import json
import asyncio
from datetime import datetime
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

SOURCE_CHATS = [
    -1001629856224,
    -1003537546255
]

TARGET_CHAT = -1003725482312

STATE_FILE = "last_seen.json"

ALLOW_PATTERNS = [
    "BUY", "SELL",
    "ENTRY", "SL", "TP",
    "STOP LOSS", "TAKE PROFIT",
    "LIMIT", "STOP",
    "XAUUSD", "XAUUSDM"
]

BLOCK_PATTERNS = [
    "RISK", "%", "MANAGEMENT",
    "ACCOUNT", "LOT SIZE",
    "SIGNAL CLOSED", "RESULT"
]

# =========================
# TIME FILTER (UTC → Uganda)
# =========================
def is_night():
    utc_hour = datetime.utcnow().hour

    # Block 20:00 → 02:00 UTC
    # Equals 23:00 → 05:00 Uganda (UTC+3)
    return utc_hour >= 20 or utc_hour < 2

# =========================
# FILTER LOGIC
# =========================
def should_forward(text: str) -> bool:
    t = text.upper()
    allow = any(p in t for p in ALLOW_PATTERNS)
    block = any(p in t for p in BLOCK_PATTERNS)
    return allow and not block

# =========================
# STATE MGMT
# =========================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# =========================
# CLEAN FORMATTER
# =========================
def clean_message(text: str) -> str:
    lines = text.splitlines()
    cleaned = []

    for line in lines:
        if any(b in line.upper() for b in BLOCK_PATTERNS):
            continue
        cleaned.append(line.strip())

    final = "\n".join(cleaned)
    final += "\n\n📡 Signal Relay"
    return final

# =========================
# SAFE MESSAGE FETCH
# =========================
async def process_chat(client, chat_id, state):

    last_id = state.get(str(chat_id), 0)
    max_seen = last_id

    print(f"🔎 Checking {chat_id} from ID {last_id}")

    async for msg in client.iter_messages(
        chat_id,
        min_id=last_id,
        limit=30   # ✅ CRITICAL LIMIT
    ):
        if not msg.message:
            continue

        max_seen = max(max_seen, msg.id)

        text = msg.message

        if should_forward(text):
            formatted = clean_message(text)

            try:
                await asyncio.wait_for(
                    client.send_message(TARGET_CHAT, formatted),
                    timeout=15  # ✅ prevents hang
                )
                print(f"✅ Forwarded from {chat_id}")

            except asyncio.TimeoutError:
                print("⚠️ Send timeout — skipped")

            except Exception as e:
                print("⚠️ Send error:", e)

    state[str(chat_id)] = max_seen

# =========================
# MAIN
# =========================
async def run():

    if is_night():
        print("🌙 Night mode — skipping run")
        return

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH
    )

    try:
        await asyncio.wait_for(client.start(), timeout=20)

    except asyncio.TimeoutError:
        print("❌ Telegram connection timeout")
        return

    state = load_state()

    for chat_id in SOURCE_CHATS:
        await process_chat(client, chat_id, state)

    save_state(state)
    await client.disconnect()

    print("✅ Run complete — exiting cleanly")

if __name__ == "__main__":
    asyncio.run(run())
