import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# =========================
# ENV
# =========================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

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
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# =========================
# OPTIONAL FILTER (signals)
# =========================
ALLOW = ["BUY", "SELL", "ENTRY", "SL", "TP", "XAUUSD"]


def should_forward(text: str) -> bool:
    t = text.upper()
    return any(p in t for p in ALLOW)


# =========================
# SAFE SEND
# =========================
async def safe_send(client, text):
    try:
        await client.send_message(TARGET_CHAT, text)

    except errors.FloodWaitError as e:
        print(f"⏳ Flood wait {e.seconds}s")
        await asyncio.sleep(e.seconds)
        await client.send_message(TARGET_CHAT, text)


# =========================
# MAIN
# =========================
async def main():

    if is_night():
        print("🌙 Night mode — paused")
        return

    state = load_state()

    async with TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH
    ) as client:

        print("✅ Connected")

        for chat_id in SOURCE_CHATS:

            last_id = state.get(str(chat_id), 0)
            print(f"Checking {chat_id} from ID {last_id}")

            try:
                async for msg in client.iter_messages(chat_id, min_id=last_id):

                    if not msg.message:
                        continue

                    text = msg.message

                    if should_forward(text):
                        await safe_send(client, text)
                        print(f"✅ Sent {msg.id}")

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

    print("🔌 Done")


if __name__ == "__main__":
    asyncio.run(main())
