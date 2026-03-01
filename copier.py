import os
import asyncio
import logging
import time
from collections import deque, OrderedDict
from threading import Thread
from typing import Optional

from telethon import TelegramClient
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
SOURCE_CHAT = int(require_env("SOURCE_CHATS"))  # only 1 source now

# ==========================================================
# CONFIG
# ==========================================================

BASE_DELAY = 0.6
MAX_DELAY = 25
POLL_INTERVAL = 3  # seconds between checks
DUP_LIMIT = 8000

# ==========================================================
# LOGGING
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("POLL_ENGINE")

# ==========================================================
# GLOBAL STATE
# ==========================================================

client: Optional[TelegramClient] = None
adaptive_delay = BASE_DELAY
cooldown_until = 0

send_times = deque(maxlen=300)
flood_memory = deque(maxlen=30)
duplicate_cache = OrderedDict()

last_seen_id = 0

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
# POLLING ENGINE
# ==========================================================

async def poll_source():
    global last_seen_id, cooldown_until

    while True:

        if time.time() < cooldown_until:
            await asyncio.sleep(1)
            continue

        try:
            messages = await client.get_messages(SOURCE_CHAT, limit=1)

            if not messages:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            msg = messages[0]

            if msg.id <= last_seen_id:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            last_seen_id = msg.id

            unique_key = f"{SOURCE_CHAT}:{msg.id}"
            if unique_key in duplicate_cache:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            logger.info(f"New message detected | ID={msg.id}")

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
                logger.info("Unsupported message type")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            duplicate_cache[unique_key] = True
            if len(duplicate_cache) > DUP_LIMIT:
                duplicate_cache.popitem(last=False)

            send_times.append(time.time())
            risk = adjust_delay()

            logger.info(
                f"Resent | Risk={round(risk,3)} | "
                f"Delay={round(adaptive_delay,2)}"
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

        await asyncio.sleep(POLL_INTERVAL)

# ==========================================================
# TELEGRAM START
# ==========================================================

async def start_bot():
    global client, last_seen_id

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        auto_reconnect=True
    )

    await client.start()

    me = await client.get_me()
    logger.info(f"Telegram connected as {me.username}")

    # Warm entities
    await client.get_entity(SOURCE_CHAT)
    await client.get_entity(TARGET_CHAT)

    # Initialize last_seen_id
    msgs = await client.get_messages(SOURCE_CHAT, limit=1)
    if msgs:
        last_seen_id = msgs[0].id
        logger.info(f"Starting from message ID {last_seen_id}")

    await poll_source()

# ==========================================================
# HEALTH SERVER
# ==========================================================

def start_web():
    app = Flask(__name__)

    @app.route("/")
    def health():
        return jsonify({
            "status": "running",
            "delay": round(adaptive_delay, 2),
            "last_seen_id": last_seen_id
        })

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==========================================================
# ENTRY
# ==========================================================

if __name__ == "__main__":
    Thread(target=start_web, daemon=True).start()
    asyncio.run(start_bot())
