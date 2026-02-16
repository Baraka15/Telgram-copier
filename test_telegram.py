import os
from telethon import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")
session = os.getenv("SESSION_STRING")

client = TelegramClient(StringSession(session), api_id, api_hash)

async def main():
    me = await client.get_me()
    print(f"Connected as: {me.username or me.first_name}")

with client:
    client.loop.run_until_complete(main())
