import os
import asyncio
import logging
import time
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

# ================= ENV =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

SOURCE_CHATS = [
    int(x.strip())
    for x in os.getenv("SOURCE_CHATS", "").split(",")
    if x.strip()
]

TARGET_CHAT = int(os.getenv("TARGET_CHAT"))

RATE_DELAY = 0.4
MAX_RUNTIME_SECONDS = 5 * 60 * 60  # 5 hours

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("relay")

# ================= SAFE FORWARD =================

async def safe_forward(client, message):
    try:
        if message.media:
            await client.send_file(
                TARGET_CHAT,
                message.media,
                caption=message.text or ""
            )
        elif message.text:
            await client.send_message(
                TARGET_CHAT,
                message.text
            )

        await asyncio.sleep(RATE_DELAY)

    except FloodWaitError as e:
        logger.warning(f"FloodWait {e.seconds}s")
        await asyncio.sleep(e.seconds)
        await safe_forward(client, message)

    except RPCError as e:
        logger.error(f"RPCError: {e}")

    except Exception as e:
        logger.error(f"Send error: {e}")

# ================= TELEGRAM CORE =================

async def run_bot():
    start_time = time.time()

    async with TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH
    ) as client:

        logger.info("Telegram connected. Listener active.")

        @client.on(events.NewMessage)
        async def handler(event):
            try:
                if event.chat_id not in SOURCE_CHATS:
                    return

                await safe_forward(client, event.message)

            except Exception as e:
                logger.error(f"Handler error: {e}")

        # Runtime monitor loop
        while True:
            elapsed = time.time() - start_time

            if elapsed >= MAX_RUNTIME_SECONDS:
                logger.info("5 hours reached. Restarting cleanly...")
                await client.disconnect()
                break

            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(run_bot())
