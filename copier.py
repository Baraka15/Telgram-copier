import os
import json
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
SOURCE_CHAT = os.getenv("SOURCE_CHAT")
DEST_CHAT = os.getenv("DEST_CHAT")

STATE_FILE = "last_seen.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_id": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

async def main():
    state = load_state()
    last_id = state["last_id"]

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        source = await client.get_entity(SOURCE_CHAT)
        dest = await client.get_entity(DEST_CHAT)

        messages = await client.get_messages(source, min_id=last_id)

        for msg in reversed(messages):
            if msg.message:
                await client.send_message(dest, msg.message)
                state["last_id"] = msg.id

        save_state(state)

asyncio.run(main())
