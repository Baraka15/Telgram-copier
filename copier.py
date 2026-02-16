import os
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
SOURCE = int(os.getenv("SOURCE"))
TARGET = int(os.getenv("TARGET"))

# ========================
# FILTER RULES
# ========================

ALLOW_PATTERNS = [
    "BUY", "SELL",
    "ENTRY", "SL", "TP",
    "STOP LOSS", "TAKE PROFIT",
    "LIMIT", "STOP",
    "XAUUSD", "XAUUSDm"
]

BLOCK_PATTERNS = [
    "RISK", "%",
    "LOT", "LOT SIZE",
    "ACCOUNT", "BALANCE",
    "COMPOUND", "GROWTH",
    "MANAGEMENT",
    "PSYCHOLOGY",
    "DISCIPLINE",
    "EMOTION"
]

# ========================
# VALIDATION ENGINE
# ========================

def is_signal(text: str) -> bool:
    if not text:
        return False

    t = text.upper()

    if any(block in t for block in BLOCK_PATTERNS):
        return False

    if any(allow in t for allow in ALLOW_PATTERNS):
        return True

    return False

# ========================
# FORMATTER
# ========================

def format_message(text: str) -> str:
    return (
        f"📡 **Signal Extracted**\n\n"
        f"{text}\n\n"
        f"━━━━━━━━━━━━━━"
    )

# ========================
# CLIENT
# ========================

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)

# ========================
# HANDLER
# ========================

@client.on(events.NewMessage(chats=SOURCE))
async def handler(event):
    try:
        msg_text = event.raw_text

        if not is_signal(msg_text):
            print("⛔ Skipped")
            return

        formatted = format_message(msg_text)

        await client.send_message(TARGET, formatted)

        print("✅ Forwarded")

    except Exception as e:
        print("❌ Error:", str(e))

# ========================
# MAIN
# ========================

async def main():
    await client.start()
    print("🚀 Copier running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
