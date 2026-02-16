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
    "XAUUSD", "XAUUSDm"
]

BLOCK_PATTERNS = [
    "RISK", "%", "MANAGEMENT",
    "ACCOUNT", "LOT SIZE",
    "SIGNAL CLOSED", "RESULT"
]

# =========================
# TIME FILTER (LOCAL SAFETY)
# =========================
def is_night():
    hour = datetime.now().hour  # GitHub runs in UTC
    return hour >= 20 or hour < 2  # Block 20:00 → 02:00 UTC

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

    # Optional branding / footer
    final += "\n\n📡 Signal Relay"
    return final

# =========================
# MAIN
# =========================
async def run():
    if is_night():
        print("🌙 Night mode — skipping run")
        return

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    state = load_state()

    for chat_id in SOURCE_CHATS:
        last_id = state.get(str(chat_id), 0)

        async for msg in client.iter_messages(chat_id, min_id=last_id):
            if not msg.message:
                continue

            text = msg.message

            if should_forward(text):
                formatted = clean_message(text)
                await client.send_message(TARGET_CHAT, formatted)
                print(f"✅ Forwarded from {chat_id}")

            state[str(chat_id)] = max(
                state.get(str(chat_id), 0),
                msg.id
            )

    save_state(state)
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(run())
