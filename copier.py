from telethon import TelegramClient, events
from telethon.sessions import StringSession
import os

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]
session = os.environ["SESSION_STRING"]

SOURCE_CHATS = [
    -1001629856224,  # Source 1
    -1003537546255   # Source 2
]

DEST_CHAT = -1003725482312  # Destination

client = TelegramClient(StringSession(session), api_id, api_hash)

@client.on(events.NewMessage(chats=SOURCE_CHATS))
async def handler(event):
    try:
        await client.forward_messages(DEST_CHAT, event.message)
        print(f"✅ Copied from {event.chat_id}")
    except Exception as e:
        print(f"❌ Error: {e}")

with client:
    print("🚀 Copier running (2 sources)...")
    client.run_until_disconnected()
