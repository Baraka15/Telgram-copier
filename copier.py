import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = os.getenv("SESSION_1")

SOURCE_CHATS = [int(x.strip()) for x in os.getenv("SOURCE_CHATS").split(",")]
TARGET_CHAT = int(os.getenv("TARGET_CHAT"))

STATE_FILE = "last_ids.txt"
POLL_INTERVAL = 2
MAX_BATCH = 20
RATE_DELAY = 0.6


# -------------------- LOGGING --------------------

logger = logging.getLogger("relay")
logger.setLevel(logging.INFO)

handler = RotatingFileHandler("relay.log", maxBytes=5_000_000, backupCount=3)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
handler.setFormatter(formatter)

logger.addHandler(handler)
logger.addHandler(logging.StreamHandler())


# -------------------- STATE --------------------

def load_last_ids():
    if not os.path.exists(STATE_FILE):
        return {}
    data = {}
    with open(STATE_FILE, "r") as f:
        for line in f:
            chat, msg_id = line.strip().split(":")
            data[int(chat)] = int(msg_id)
    return data


def save_last_ids(data):
    with open(STATE_FILE, "w") as f:
        for chat, msg_id in data.items():
            f.write(f"{chat}:{msg_id}\n")


# -------------------- RELAY CORE --------------------

async def safe_send(client, message):
    try:
        if message.media:
            await client.send_file(
                TARGET_CHAT,
                message.media,
                caption=message.text or ""
            )
        else:
            await client.send_message(
                TARGET_CHAT,
                message.text or ""
            )
        await asyncio.sleep(RATE_DELAY)

    except FloodWaitError as e:
        logger.warning(f"FloodWait {e.seconds}s")
        await asyncio.sleep(e.seconds)
        await safe_send(client, message)

    except RPCError as e:
        logger.error(f"RPCError: {e}")

    except Exception as e:
        logger.error(f"Send failed: {e}")


async def initialize_baseline(client, last_ids):
    for chat in SOURCE_CHATS:
        if chat not in last_ids:
            entity = await client.get_entity(chat)
            messages = await client.get_messages(entity, limit=1)
            last_ids[chat] = messages[0].id if messages else 0
    save_last_ids(last_ids)
    logger.info(f"Initialized baseline: {last_ids}")


async def poll_loop(client):
    last_ids = load_last_ids()
    await initialize_baseline(client, last_ids)

    while True:
        for chat in SOURCE_CHATS:
            try:
                entity = await client.get_entity(chat)

                messages = await client.get_messages(
                    entity,
                    min_id=last_ids.get(chat, 0),
                    limit=MAX_BATCH
                )

                for msg in reversed(messages):
                    if msg.id > last_ids.get(chat, 0):
                        logger.info(f"New message | Chat={chat} | ID={msg.id}")

                        await safe_send(client, msg)

                        last_ids[chat] = msg.id
                        save_last_ids(last_ids)

            except FloodWaitError as e:
                logger.warning(f"FloodWait during poll {e.seconds}s")
                await asyncio.sleep(e.seconds)

            except Exception as e:
                logger.error(f"Polling error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


# -------------------- AUTO RECONNECT --------------------

async def main():
    while True:
        try:
            async with TelegramClient(SESSION, API_ID, API_HASH) as client:
                logger.info("Telegram connected.")
                await poll_loop(client)

        except Exception as e:
            logger.error(f"Connection crashed: {e}")
            logger.info("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
