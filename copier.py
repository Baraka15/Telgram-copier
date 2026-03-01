import os
import asyncio
import logging
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

# ================== ENV ==================

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

# ================== LOGGING ==================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("relay")

# ================== SAFE FORWARD ==================

async def safe_forward(client, message):
    try:
        logger.info(f"Forwarding ID {message.id}")

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

# ================== TELEGRAM CORE ==================

async def run_bot():
    while True:
        try:
            async with TelegramClient(
                StringSession(SESSION_STRING),
                API_ID,
                API_HASH
            ) as client:

                logger.info("Telegram connected. Listener active.")

                @client.on(events.NewMessage)
                async def handler(event):
                    try:
                        logger.info(
                            f"Incoming | chat_id={event.chat_id}"
                        )

                        if event.chat_id not in SOURCE_CHATS:
                            return

                        await safe_forward(client, event.message)

                    except Exception as e:
                        logger.error(f"Handler error: {e}")

                await client.run_until_disconnected()

        except Exception as e:
            logger.error(f"Connection crash: {e}")
            logger.info("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

# ================== START ==================

if __name__ == "__main__":
    asyncio.run(run_bot())
