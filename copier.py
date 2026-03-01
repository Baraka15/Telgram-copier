import os
import asyncio
import logging
import time
from collections import deque, OrderedDict
from threading import Thread
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from flask import Flask, jsonify

# ==========================================================
# ENV VALIDATION
# ==========================================================

def require_env(name: str):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing ENV variable: {name}")
    return value

API_ID = int(require_env("API_ID"))
API_HASH = require_env("API_HASH")
SESSION_STRING = require_env("SESSION_1")
TARGET_CHAT = int(require_env("TARGET_CHAT"))

SOURCE_CHATS = [
    int(x.strip())
    for x in require_env("SOURCE_CHATS").split(",")
    if x.strip()
]

# ==========================================================
# CONFIG
# ==========================================================

BASE_DELAY = 0.6
MAX_DELAY = 25
QUEUE_LIMIT = 500
DUP_LIMIT = 8000

# ==========================================================
# LOGGING
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("RESEND_ENGINE")

logger.info(f"SOURCE_CHATS loaded: {SOURCE_CHATS}")
logger.info(f"TARGET_CHAT loaded: {TARGET_CHAT}")

# ==========================================================
# GLOBAL STATE
# ==========================================================

client: Optional[TelegramClient] = None
adaptive_delay = BASE_DELAY
cooldown_until = 0

send_times = deque(maxlen=300)
flood_memory = deque(maxlen=30)

duplicate_cache = OrderedDict()
message_queue = asyncio.Queue(maxsize=QUEUE_LIMIT)

# ==========================================================
# RISK MODEL
# ==========================================================

def send_rate():
    if len(send_times) < 2:
        return 0
    span = send_times[-1] - send_times[0]
    return len(send_times) / max(span, 1)

def burst_pressure():
    now = time.time()
    return len([t for t in send_times if now - t < 8]) / 8

def flood_history():
    now = time.time()
    return len([t for t in flood_memory if now - t < 300])

def risk_score():
    score = (
        2.2 * send_rate() +
        3.0 * burst_pressure() +
        3.5 * flood_history() - 4
    )
    return 1 / (1 + pow(2.718, -score))

def adjust_delay():
    global adaptive_delay
    risk = risk_score()

    if risk > 0.85:
        adaptive_delay = min(adaptive_delay * 1.7, MAX_DELAY)
    elif risk > 0.65:
        adaptive_delay = min(adaptive_delay * 1.3, MAX_DELAY)
    elif risk < 0.3:
        adaptive_delay = max(BASE_DELAY, adaptive_delay * 0.85)

    return risk

# ==========================================================
# RESEND WORKER
# ==========================================================

async def resend_worker():
    global cooldown_until

    while True:
        event = await message_queue.get()

        if time.time() < cooldown_until:
            await asyncio.sleep(1)
            await message_queue.put(event)
            continue

        unique_key = f"{event.chat_id}:{event.id}"

        if unique_key in duplicate_cache:
            continue

        msg = event.message

        try:
            if msg.media:
                await client.send_file(
                    TARGET_CHAT,
                    msg.media,
                    caption=msg.text or ""
                )

            elif msg.text:
                await client.send_message(
                    TARGET_CHAT,
                    msg.text
                )

            else:
                logger.info(f"Skipped unsupported type | ID={event.id}")
                continue

            duplicate_cache[unique_key] = True
            if len(duplicate_cache) > DUP_LIMIT:
                duplicate_cache.popitem(last=False)

            send_times.append(time.time())
            risk = adjust_delay()

            logger.info(
                f"Resent | Q={message_queue.qsize()} | "
                f"Risk={round(risk,3)} | Delay={round(adaptive_delay,2)}"
            )

            await asyncio.sleep(adaptive_delay)

        except FloodWaitError as e:
            cooldown_until = time.time() + e.seconds
            flood_memory.append(time.time())
            logger.warning(f"FloodWait {e.seconds}s")

        except RPCError as e:
            logger.error(f"RPC Error: {e}")

        except Exception as e:
            logger.error(f"Unexpected error: {e}")

# ==========================================================
# TELEGRAM START
# ==========================================================

async def start_bot():
    global client

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        auto_reconnect=True,
        connection_retries=None,
        retry_delay=5,
    )

    await client.start()

    me = await client.get_me()
    logger.info(f"Telegram connected as {me.id} | {me.username}")

    # 🔥 ENTITY WARM-UP (CRITICAL)
    for chat_id in SOURCE_CHATS + [TARGET_CHAT]:
        try:
            entity = await client.get_entity(chat_id)
            logger.info(
                f"Warmed entity: {getattr(entity, 'title', 'Unknown')} | {chat_id}"
            )
        except Exception as e:
            logger.error(f"Entity warm-up failed for {chat_id}: {e}")

    asyncio.create_task(resend_worker())

    # 🔥 STRICT CHANNEL LISTENER
    @client.on(events.NewMessage(incoming=True, chats=SOURCE_CHATS))
    async def handler(event):

        logger.info(
            f"New message detected | "
            f"Chat={event.chat_id} | ID={event.id}"
        )

        try:
            message_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Queue full. Dropping message.")

    await client.run_until_disconnected()

# ==========================================================
# HEALTH SERVER
# ==========================================================

def start_web():
    app = Flask(__name__)

    @app.route("/")
    def health():
        return jsonify({
            "status": "running",
            "queue": message_queue.qsize(),
            "delay": round(adaptive_delay, 2)
        })

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==========================================================
# AUTO RESTART LOOP
# ==========================================================

def run_forever():
    while True:
        try:
            asyncio.run(start_bot())
        except Exception as e:
            logger.error(f"System crash: {e}")
            time.sleep(5)

# ==========================================================
# ENTRY
# ==========================================================

if __name__ == "__main__":
    Thread(target=start_web, daemon=True).start()
    run_forever()
