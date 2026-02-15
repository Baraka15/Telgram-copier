import os
import asyncio
import re
import random
from telethon import TelegramClient, events

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]

SOURCE_CHAT = -1001629856224
DEST_CHAT = -1003725482312

client = TelegramClient("session", api_id, api_hash)

def is_valid_signal(text):
    t = text.upper()
    return (
        "XAUUSD" in t and
        re.search(r"\b(BUY|SELL)\b", t) and
        "TP" in t and
        "SL" in t
    )

def normalize_symbol(text):
    return re.sub(r"\bXAUUSD\b", "XAUUSDm", text, flags=re.IGNORECASE)

@client.on(events.NewMessage(chats=SOURCE_CHAT))
async def handler(event):
    text = event.raw_text
    if text and is_valid_signal(text):
        await asyncio.sleep(random.uniform(0.5, 1.3))
        await client.send_message(DEST_CHAT, normalize_symbol(text))
        print("Copied:", text[:50])

async def main():
    await client.start()
    print("Running copier...")
    
    await asyncio.sleep(350 * 60)  # ~5h 50m safety window
    
    print("Stopping...")
    await client.disconnect()

asyncio.run(main())
