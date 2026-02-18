import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# =========================
# ENV VALIDATION
# =========================
print("🚀 Copier booting...")

api_id_raw = os.getenv("API_ID")
api_hash = os.getenv("API_HASH")
session_string = os.getenv("SESSION_STRING")

print("API_ID:", api_id_raw)
print("API_HASH exists:", bool(api_hash))
print("SESSION exists:", bool(session_string))

if not api_id_raw or not api_hash or not session_string:
    raise Exception("❌ Missing required environment variables")

API_ID = int(api_id_raw)
API_HASH = api_hash
SESSION_STRING = session_string

SOURCE_CHATS = [-1001629856224]
TARGET_CHAT = -1003725482312

STATE_FILE = "last_seen.json"

# Uganda = UTC+3
UGANDA_TZ = timezone(timedelta(hours=3))

# =========================
# NIGHT FILTER (23 → 05)
# =========================
def is_night():
    now = datetime.now(UGANDA_TZ).hour
    return now >= 23 or now < 5

# =========================
# STATE
# =========================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# =========================
# OPTIONAL FILTER
# =========================
ALLOW = ["BUY", "SELL", "ENTRY", "SL", "TP", "XAUUSD"]

def should_forward(text: str) -> bool:
    if not text:
        return False
    t = text.upper()
    return any(p in t for p in ALLOW)

# =========================
# SAFE SEND
# =========================
async def safe_send(client, text, msg_id):
    try:
        await client.send_message(TARGET_CHAT, text)
        print(f"✅ Sent {msg_id}")

    except errors.FloodWaitError as e:
        print(f"⏳ Flood wait {e.seconds}s (send)")
        await asyncio.sleep(e.seconds)
        await client.send_message(TARGET_CHAT, text)
        print(f"✅ Sent after wait {msg_id}")

# =========================
# MAIN
# =========================
async def main():

    if is_night():
        print("🌙 Night mode — paused")
        return

    state = load_state()
    print("📦 Loaded state:", state)

    try:
        async with TelegramClient(
            StringSession(SESSION_STRING),
            API_ID,
            API_HASH
        ) as client:

            print("✅ Connected to Telegram")

            for chat_id in SOURCE_CHATS:

                last_id = state.get(str(chat_id), 0)
                print(f"🔎 Checking {chat_id} from ID {last_id}")

                try:
                    async for msg in client.iter_messages(chat_id, min_id=last_id):

                        if not msg.message:
                            continue

                        text = msg.message

                        if should_forward(text):
                            await safe_send(client, text, msg.id)

                        # ALWAYS update state even if filtered
                        state[str(chat_id)] = max(
                            state.get(str(chat_id), 0),
                            msg.id
                        )

                except errors.FloodWaitError as e:
                    print(f"⏳ Flood wait {e.seconds}s (iter)")
                    await asyncio.sleep(e.seconds)

                except Exception as e:
                    print(f"⚠ Error in {chat_id}: {e}")

            save_state(state)

    except Exception as e:
        print("❌ Fatal error:", e)

    print("🔌 Done")

# =========================
# ENTRY
# =========================
if __name__ == "__main__":
    asyncio.run(main())
