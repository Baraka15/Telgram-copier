import os
import asyncio
import logging
import time
import signal
from collections import deque
from typing import Optional, Set
from threading import Thread

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from flask import Flask

# =========================
# SAFE ENV LOADER
# =========================

def require_env(name: str):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required ENV variable: {name}")
    return value

try:
    API_ID = int(require_env("API_ID"))
    API_HASH = require_env("API_HASH")
    SESSION_STRING = require_env("SESSION_1")
    SOURCE_RAW = require_env("SOURCE_CHATS")
    TARGET_CHAT = int(require_env("TARGET_CHAT"))

    SOURCE_CHATS = [int(x.strip()) for x in SOURCE_RAW.split(",") if x.strip()]

except Exception as e:
    raise RuntimeError(f"ENV CONFIG ERROR: {e}")

# =========================
# CONFIG
# =========================

BASE_DELAY = 0.8
MAX_DELAY = 20

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("COPIER")

# =========================
# GLOBAL STATE
# =========================

adaptive_delay = BASE_DELAY
cooldown_until = 0

send_timestamps = deque(maxlen=200)
recent_floods = deque(maxlen=20)
processed_ids: Set[str] = set()

client: Optional[TelegramClient] = None

# =========================
# KEEP RENDER ALIVE
# =========================

def start_web_server():
    app = Flask(__name__)

    @app.route("/")
    def home():
        return "Bot Running"

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# =========================
# FLOOD CONTROL
# =========================

def compute_send_rate():
    if len(send_timestamps) < 2:
        return 0
    interval = send_timestamps[-1] - send_timestamps[0]
    return len(send_timestamps) / max(interval, 1)

def compute_burst():
    now = time.time()
    return len([t for t in send_timestamps if now - t < 10]) / 10

def compute_flood_memory():
    now = time.time()
    return len([t for t in recent_floods if now - t < 300])

def predict_risk():
    score = (
        1.8 * compute_send_rate() +
        2.5 * compute_burst() +
        3.0 * compute_flood_memory() -
        3.0
    )
    return 1 / (1 + pow(2.718, -score))

def adjust_delay(risk):
    global adaptive_delay
    if risk > 0.8:
        adaptive_delay = min(adaptive_delay * 1.5, MAX_DELAY)
    elif risk > 0.6:
        adaptive_delay = min(adaptive_delay * 1.2, MAX_DELAY)
    elif risk < 0.3:
        adaptive_delay = max(BASE_DELAY, adaptive_delay * 0.9)

# =========================
# FORWARDING
# =========================

async def safe_forward(event):
    global cooldown_until

    if time.time() < cooldown_until:
        return

    key = f"{event.chat_id}:{event.id}"
    if key in processed_ids:
        return

    try:
        await client.forward_messages(
            TARGET_CHAT,
            event.message
        )

        processed_ids.add(key)
        if len(processed_ids) > 5000:
            processed_ids.clear()

        send_timestamps.append(time.time())

        risk = predict_risk()
        adjust_delay(risk)

        logger.info(f"Forwarded | Risk={round(risk,3)} | Delay={round(adaptive_delay,2)}")

        await asyncio.sleep(adaptive_delay)

    except FloodWaitError as e:
        cooldown_until = time.time() + e.seconds
        recent_floods.append(time.time())
        logger.warning(f"FloodWait {e.seconds}s")

    except RPCError as e:
        logger.error(f"RPC Error: {e}")

    except Exception as e:
        logger.error(f"Unexpected error: {e}")

# =========================
# TELEGRAM START
# =========================

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
    logger.info("Telegram connected")

    @client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        await safe_forward(event)

    await client.run_until_disconnected()

# =========================
# AUTO RESTART
# =========================

def run_forever():
    while True:
        try:
            asyncio.run(start_bot())
        except Exception as e:
            logger.error(f"System crashed: {e}")
            time.sleep(5)

# =========================
# ENTRY
# =========================

if __name__ == "__main__":
    Thread(target=start_web_server, daemon=True).start()
    run_forever()
