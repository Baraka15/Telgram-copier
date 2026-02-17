import os
import json
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

SOURCE_CHATS = [
    -1001629856224,
    -1003537546255
]

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

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:

        for chat_id in SOURCE_CHATS:
            last_id = state.get(str(chat_id), 0)

            messages = await client.get_messages(chat_id, min_id=last_id)

            for msg in reversed(messages):
                if msg.message:
                    await client.send_message(TARGET_CHAT, msg.message)
                    state[str(chat_id)] = msg.id

    save_state(state)


asyncio.run(main())
