import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

# ================== ENV ==================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

SOURCE_CHATS = [int(x.strip()) for x in os.getenv("SOURCE_CHATS").split(",")]
TARGET_CHAT = int(os.getenv("TARGET_CHAT"))

RATE_DELAY = 0.5  # anti-flood micro delay


# ================== LOGGING ==================

logger = logging.getLogger("relay")
logger.setLevel(logging.INFO)

handler = RotatingFileHandler("relay.log", maxBytes=5_000_000, backupCount=3)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
handler.setFormatter(formatter)

logger.addHandler(handler)
logger.addHandler(logging.StreamHandler())


# ================== SAFE SEND ==================

async def safe_forward(client, message):
    try:
        if message.media:
            await client.send_file(
                TARGET_CHAT,
                message.media,
                caption=message.text or ""
            )
        else:
            if message.text:
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
        logger.error(f"Unexpected send error: {e}")


# ================== MAIN ==================

async def main():
    if not SESSION_STRING:
        raise ValueError("SESSION_STRING not set in environment")

    while True:
        try:
            async with TelegramClient(
                StringSession(SESSION_STRING),
                API_ID,
                API_HASH
            ) as client:

                logger.info("Telegram connected. Real-time listener active.")

                @client.on(events.NewMessage(chats=SOURCE_CHATS))
                async def handler(event):
                    try:
                        msg = event.message
                        logger.info(
                            f"New message | Chat={event.chat_id} | ID={msg.id}"
                        )

                        await safe_forward(client, msg)

                    except Exception as e:
                        logger.error(f"Handler failure: {e}")

                await client.run_until_disconnected()

        except Exception as e:
            logger.error(f"Connection crashed: {e}")
            logger.info("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
