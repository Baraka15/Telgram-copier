import os
import json
import asyncio
import hashlib
from datetime import datetime
from telethon import TelegramClient, errors
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

DEDUP_LIMIT = 500  # keep last 500 hashes
PROCESS_EDITS = False  # toggle if you want edited messages

# =========================
# TIME FILTER (UTC)
# =========================
def is_night():
    hour = datetime.utcnow().hour
    return hour >= 20 or hour < 2

# =========================
# FILTER
# =========================
def should_forward(text: str) -> bool:
    t = text.upper()
    allow = any(p in t for p in ALLOW_PATTERNS)
    block = any(p in t for p in BLOCK_PATTERNS)
    return allow and not block

# =========================
# HASH (GLOBAL DEDUP)
# =========================
def msg_hash(text: str) -> str:
    return hashlib.md5(text.strip().encode()).hexdigest()

# =========================
# STATE
# =========================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            print("⚠ State corrupted — resetting")
            return {}
    return {}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)  # atomic write

# =========================
# FORMATTER
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
# SAFE SEND (ANTI FLOOD)
# =========================
async def safe_send(client, message):
    while True:
        try:
            await client.send_message(TARGET_CHAT, message)
            return

        except errors.FloodWaitError as e:
            wait = e.seconds + 2
            print(f"⏳ FloodWait {wait}s")
            await asyncio.sleep(wait)

        except Exception as e:
            print("🚨 Send error:", e)
            await asyncio.sleep(3)

# =========================
# CHAT PROCESSOR
# =========================
async def process_chat(client, chat_id, state):
    print(f"🔎 Checking {chat_id}")

    last_id = state.get(str(chat_id), {}).get("last_id", 0)
    hashes = state.get(str(chat_id), {}).get("hashes", [])

    try:
        async for msg in client.iter_messages(chat_id, min_id=last_id):

            if not msg.message:
                continue

            text = msg.message
            h = msg_hash(text)

            if h in hashes:
                continue

            if should_forward(text):
                formatted = clean_message(text)
                await safe_send(client, formatted)

                print(f"✅ Forwarded {chat_id} | msg {msg.id}")

                hashes.append(h)
                if len(hashes) > DEDUP_LIMIT:
                    hashes.pop(0)

            last_id = max(last_id, msg.id)

    except errors.FloodWaitError as e:
        print(f"⏳ FloodWait(iter) {e.seconds}s")
        await asyncio.sleep(e.seconds + 2)

    except Exception as e:
        print(f"⚠ Chat {chat_id} error:", e)

    state[str(chat_id)] = {
        "last_id": last_id,
        "hashes": hashes
    }

# =========================
# MAIN LOOP
# =========================
async def run():
    if is_night():
        print("🌙 Night mode — skipping cycle")
        return

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        connection_retries=5,
        retry_delay=3,
        auto_reconnect=True
    )

    try:
        await client.start()
        print("✅ Connected to Telegram")

        state = load_state()

        tasks = [
            process_chat(client, chat_id, state)
            for chat_id in SOURCE_CHATS
        ]

        await asyncio.gather(*tasks)

        save_state(state)

    except Exception as e:
        print("🚨 Client failure:", e)

    finally:
        await client.disconnect()
        print("🔌 Disconnected cleanly")

# =========================
# ENTRY
# =========================
if __name__ == "__main__":
    asyncio.run(run())
