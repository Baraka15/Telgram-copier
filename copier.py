import os
import asyncio
import hashlib
import logging
import time
import math
import signal
from collections import deque
from typing import Optional

import redis.asyncio as redis
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

# =========================
# CONFIG
# =========================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_1")

SOURCE_CHATS = list(map(int, os.getenv("SOURCE_CHATS").split(",")))
TARGET_CHAT = int(os.getenv("TARGET_CHAT"))

REDIS_URL = os.getenv("REDIS_URL")

QUEUE_KEY = "ml_priority_queue"
DEDUP_PREFIX = "ml_hash:"
HEARTBEAT_KEY = "ml_heartbeat"

BASE_DELAY = 0.8
MAX_DELAY = 25
DEDUP_TTL = 86400

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("ML_COPIER")

# =========================
# GLOBAL STATE
# =========================

shutdown_flag = False
adaptive_delay = BASE_DELAY
cooldown_until = 0

send_timestamps = deque(maxlen=200)
recent_floods = deque(maxlen=20)

r: Optional[redis.Redis] = None
client: Optional[TelegramClient] = None

# =========================
# ML FLOOD MODEL
# =========================

def sigmoid(x):
    return 1 / (1 + math.exp(-x))

def compute_send_rate():
    if len(send_timestamps) < 2:
        return 0
    interval = send_timestamps[-1] - send_timestamps[0]
    return len(send_timestamps) / max(interval, 1)

def compute_burst_pressure():
    now = time.time()
    return len([t for t in send_timestamps if now - t < 10]) / 10

def compute_flood_memory():
    now = time.time()
    return len([t for t in recent_floods if now - t < 300])

def predict_flood_risk():
    score = (
        1.8 * compute_send_rate() +
        2.5 * compute_burst_pressure() +
        3.2 * compute_flood_memory() -
        3.0
    )
    return sigmoid(score)

def adjust_delay(risk):
    global adaptive_delay
    if risk > 0.85:
        adaptive_delay = min(adaptive_delay * 1.6, MAX_DELAY)
    elif risk > 0.65:
        adaptive_delay = min(adaptive_delay * 1.3, MAX_DELAY)
    elif risk < 0.3:
        adaptive_delay = max(BASE_DELAY, adaptive_delay * 0.9)

# =========================
# REDIS SAFE CONNECT
# =========================

async def get_redis():
    global r
    while True:
        try:
            r = redis.from_url(REDIS_URL)
            await r.ping()
            logger.info("Redis connected")
            return
        except Exception as e:
            logger.error(f"Redis connect fail: {e}")
            await asyncio.sleep(5)

# =========================
# DEDUP
# =========================

def hash_message(signature: str) -> str:
    return hashlib.sha256(signature.encode()).hexdigest()

async def deduplicate(signature: str) -> bool:
    h = hash_message(signature)
    key = DEDUP_PREFIX + h
    try:
        if await r.setnx(key, 1):
            await r.expire(key, DEDUP_TTL)
            return True
        return False
    except Exception:
        return False

# =========================
# TELEGRAM CLIENT
# =========================

async def init_client():
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

# =========================
# INGESTION
# =========================

async def start_ingestion():
    @client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        try:
            signature = f"{event.chat_id}:{event.id}"

            if await deduplicate(signature):
                payload = f"{event.chat_id}|{event.id}"
                await r.lpush(QUEUE_KEY, payload)
                logger.info("Queued")

        except Exception as e:
            logger.error(f"Ingestion error: {e}")

    await client.run_until_disconnected()

# =========================
# DELIVERY
# =========================

async def deliver(source_id, message_id):
    global cooldown_until

    if time.time() < cooldown_until:
        return False

    try:
        msg = await client.get_messages(int(source_id), ids=int(message_id))
        if not msg:
            return True

        content = msg.text if msg.text else ""
        await client.send_message(
            TARGET_CHAT,
            f"📥 {source_id}\n\n{content}"
        )

        send_timestamps.append(time.time())

        risk = predict_flood_risk()
        adjust_delay(risk)

        logger.info(f"Delivered | Risk={round(risk,3)} | Delay={round(adaptive_delay,2)}")
        return True

    except FloodWaitError as e:
        cooldown_until = time.time() + e.seconds
        recent_floods.append(time.time())
        logger.warning(f"FloodWait {e.seconds}s")
        return False

    except RPCError as e:
        logger.error(f"RPC Error: {e}")
        return False

    except Exception as e:
        logger.error(f"Delivery error: {e}")
        return False

# =========================
# WORKER
# =========================

async def delivery_worker():
    while not shutdown_flag:
        try:
            item = await r.brpop(QUEUE_KEY, timeout=5)
            if item:
                source_id, message_id = item[1].decode().split("|")
                success = await deliver(source_id, message_id)

                if not success:
                    await r.lpush(QUEUE_KEY, item[1])

                await asyncio.sleep(adaptive_delay)

        except Exception as e:
            logger.error(f"Worker failure: {e}")
            await asyncio.sleep(5)

# =========================
# HEARTBEAT
# =========================

async def heartbeat():
    while not shutdown_flag:
        try:
            await r.set(HEARTBEAT_KEY, int(time.time()))
        except:
            pass
        await asyncio.sleep(30)

# =========================
# SHUTDOWN
# =========================

def stop_signal(*args):
    global shutdown_flag
    shutdown_flag = True
    logger.info("Shutdown signal received")

signal.signal(signal.SIGINT, stop_signal)
signal.signal(signal.SIGTERM, stop_signal)

# =========================
# MAIN LOOP WITH AUTO RESTART
# =========================

async def system():
    await get_redis()
    await init_client()

    await asyncio.gather(
        start_ingestion(),
        delivery_worker(),
        heartbeat()
    )

def run_forever():
    while True:
        try:
            asyncio.run(system())
        except Exception as e:
            logger.error(f"System crash: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_forever()
