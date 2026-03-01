import os
import asyncio
import logging
import threading
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from dotenv import load_dotenv
from flask import Flask

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

logger = logging.getLogger("relay")
logger.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s"
)

file_handler = RotatingFileHandler(
    "relay.log", maxBytes=5_000_000, backupCount=3
)
file_handler.setFormatter(formatter)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)

# ================== FLASK (Render Port Fix) ==================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================== TELEGRAM ==================

async def safe_forward(client, message):
    try:
        logger.info(f"Forwarding message ID {message.id}")

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

        logger.info("Forward success")

    except FloodWaitError as e:
        logger.warning(f"FloodWait {e.seconds}s")
        await asyncio.sleep(e.seconds)
        await safe_forward(client, message)

    except RPCError as e:
        logger.error(f"RPCError: {e}")

    except Exception as e:
        logger.error(f"Send error: {e}")


async def dump_dialogs(client):
    logger.info("==== ACCOUNT DIALOG LIST ====")
    async for dialog in client.iter_dialogs():
        logger.info(f"{dialog.name} | ID = {dialog.id}")
    logger.info("==== END DIALOG LIST ====")


async def telegram_main():
    async with TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH
    ) as client:

        logger.info("Telegram connected. Real-time listener active.")

        # Dump dialogs at startup (CRITICAL for debugging IDs)
        await dump_dialogs(client)

        @client.on(events.NewMessage)
        async def handler(event):
            try:
                logger.info(
                    f"Incoming | chat_id={event.chat_id} | sender={event.sender_id} | text={event.text}"
                )

                if event.chat_id not in SOURCE_CHATS:
                    logger.info("Ignored (not in SOURCE_CHATS)")
                    return

                logger.info("Matched SOURCE. Forwarding...")
                await safe_forward(client, event.message)

            except Exception as e:
                logger.error(f"Handler error: {e}")

        await client.run_until_disconnected()

# ================== START BOTH ==================

if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    asyncio.run(telegram_main())
