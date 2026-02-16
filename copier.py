import os
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# =========================
# CONFIG
# =========================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# Hardcoded IDs (stable)
SOURCE = -1001629856224     # TRADE WITH ZAIN
TARGET = -1003725482312     # BRAX FX VIP

# =========================
# FILTER RULES
# =========================

ALLOW_PATTERNS = [
    "BUY", "SELL",
    "ENTRY", "SL", "TP",
    "STOP LOSS", "TAKE PROFIT",
    "LIMIT", "STOP",
    "XAUUSD", "XAUUSDm"
]

BLOCK_PATTERNS = [
    "RISK", "%", "MANAGEMENT",
    "ACCOUNT", "LOT SIZE",
    "SIGNAL CLOSED", "RESULT"
]

# =========================
# CLIENT
# =========================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)

# =========================
# FILTER FUNCTION
# =========================

def should_forward(text: str) -> bool:
    text_upper = text.upper()

    allow = any(pat in text_upper for pat in ALLOW_PATTERNS)
    block = any(pat in text_upper for pat in BLOCK_PATTERNS)

    return allow and not block

# =========================
# EVENT HANDLER
# =========================

@client.on(events.NewMessage(chats=SOURCE))
async def handler(event):
    try:
        if not event.message.message:
            return

        text = event.message.message

        if should_forward(text):
            await client.send_message(TARGET, text)
            print(f"Forwarded: {text[:50]}")

    except Exception as e:
        print("ERROR:", e)

# =========================
# MAIN
# =========================

async def main():
    await client.start()
    print("✅ Copier running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
