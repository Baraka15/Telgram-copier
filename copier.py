import os
import json
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from datetime import datetime

LOG_FILE = "copier.log"

def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(f"{datetime.utcnow()} | {msg}\n")


API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

log("Starting copier")

if not API_ID or not API_HASH or not SESSION_STRING:
    log("Missing environment variables!")
    raise Exception("Secrets not loaded")

API_ID = int(API_ID)

SOURCE_CHATS = [-1001629856224]
TARGET_CHAT = -1003725482312
STATE_FILE = "last_seen.json"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


async def main():
    state = load_state()
    log("State loaded")

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        log("Client connected")

        for chat_id in SOURCE_CHATS:
            last_id = state.get(str(chat_id), 0)
            log(f"Reading chat {chat_id} from {last_id}")

            messages = await client.get_messages(chat_id, min_id=last_id)

            for msg in reversed(messages):
                if msg.message:
                    await client.send_message(TARGET_CHAT, msg.message)
                    state[str(chat_id)] = msg.id
                    log(f"Forwarded message {msg.id}")

    save_state(state)
    log("State saved")


try:
    asyncio.run(main())
    log("Finished successfully")

except Exception as e:
    log(f"CRASH: {str(e)}")
    raise
