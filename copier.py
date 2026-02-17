import os
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

SOURCE_CHATS = [-1001629856224]
TARGET_CHAT = -1003725482312

async def main():
    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        for chat_id in SOURCE_CHATS:
            messages = await client.get_messages(chat_id, limit=5)

            for msg in reversed(messages):
                if msg.message:
                    await client.send_message(TARGET_CHAT, msg.message)

asyncio.run(main())
