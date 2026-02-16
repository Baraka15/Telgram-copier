import os
import json
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

# =========================
# ENV VARIABLES
# =========================
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# =========================
# CHAT IDS
# =========================
SOURCE_CHATS = [
    -1001629856224,
    -1003537546255
]

TARGET_CHAT = -1003725482312

STATE_FILE = "last_seen.json"

# =========================
# FILTER RULES
# =========================
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

def should_forward(text: str) -> bool:
    text_upper = text.upper()
    allow = any(p in text_upper for p in ALLOW_PATTERNS)
    block = any(p in text_upper for p in BLOCK_PATTERNS)
    return allow and not block

# =========================
# LOAD / SAVE STATE
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
# MAIN LOGIC
# =========================
async def run():
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    state = load_state()

    for chat_id in SOURCE_CHATS:
        last_id = state.get(str(chat_id), 0)

        async for message in client.iter_messages(chat_id, min_id=last_id):
            if not message.message:
                continue

            text = message.message

            if should_forward(text):
                await client.send_message(TARGET_CHAT, text)
                print(f"Forwarded from {chat_id}: {text[:40]}")

            state[str(chat_id)] = max(state.get(str(chat_id), 0), message.id)

    save_state(state)
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(run())
