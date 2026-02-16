import os
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# =========================
# MULTI SOURCE CONFIG
# =========================

SOURCES = [
    -1001629856224,   # SOURCE 1
    -1003537546255    # SOURCE 2
]

TARGET = -1003725482312

# =========================
# FILTER RULES
# =========================

BLOCK_PATTERNS = [
    "RISK", "%", "MANAGEMENT",
    "ACCOUNT", "LOT SIZE",
    "SIGNAL CLOSED", "RESULT"
]

def should_forward(text: str) -> bool:
    t = text.upper()

    has_direction = "BUY" in t or "SELL" in t
    has_sl = "SL" in t or "STOP LOSS" in t
    has_tp = "TP" in t or "TAKE PROFIT" in t

    block = any(pat in t for pat in BLOCK_PATTERNS)

    return has_direction and has_sl and has_tp and not block

# =========================
# CLIENT
# =========================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)

recent_messages = set()

# =========================
# EVENT HANDLER
# =========================

@client.on(events.NewMessage(chats=SOURCES))
async def handler(event):
    try:
        msg_id = (event.chat_id, event.message.id)

        if msg_id in recent_messages:
            return

        recent_messages.add(msg_id)

        if len(recent_messages) > 200:
            recent_messages.clear()

        text = event.message.message or event.message.text

        if not text:
            return

        if should_forward(text):
            await client.send_message(TARGET, text)
            print(f"Forwarded from {event.chat_id}")

    except Exception as e:
        print("ERROR:", e)

# =========================
# MAIN
# =========================

async def main():
    await client.start()
    print("✅ Copier running (multi-source)...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
