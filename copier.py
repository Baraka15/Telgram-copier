import os
import re
import asyncio
import logging
import hashlib
import sqlite3
import signal
import random
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError

# =========================
# ENV CONFIG
# =========================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError("❌ Missing Telegram credentials")

# =========================
# SETTINGS
# =========================
DB_FILE = "copier.db"
LOG_FILE = "copier.log"

SOURCE_CHATS = [
    -1001629856224,
    -1003735057293,
]

TARGET_CHAT = -1003725482312

KEYWORDS = ["BUY", "SELL", "ENTRY", "SL", "TP", "XAUUSD"]
REGEX_FILTER = r"(buy|sell).*?(sl|tp)"

DEDUP_WINDOW_MINUTES = 30
RATE_DELAY = 0.25
WORKERS = 3
QUEUE_SIZE = 1000

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger("Copier")

# =========================
# DATABASE
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forwarded (
            hash TEXT PRIMARY KEY,
            timestamp DATETIME
        )
    """)
    conn.commit()
    return conn

db = init_db()

def is_duplicate(msg_hash):
    cutoff = datetime.utcnow() - timedelta(minutes=DEDUP_WINDOW_MINUTES)
    cur = db.cursor()
    cur.execute(
        "SELECT 1 FROM forwarded WHERE hash=? AND timestamp>?",
        (msg_hash, cutoff)
    )
    return cur.fetchone() is not None

def save_hash(msg_hash):
    db.execute(
        "INSERT OR REPLACE INTO forwarded VALUES (?, ?)",
        (msg_hash, datetime.utcnow())
    )
    db.commit()

# =========================
# FILTERS
# =========================
def build_hash(message):
    text = message.text or ""
    media_id = ""

    if message.media:
        try:
            if hasattr(message.media, "photo"):
                media_id = str(message.media.photo.id)
            elif hasattr(message.media, "document"):
                media_id = str(message.media.document.id)
        except Exception:
            media_id = "media"

    raw = f"{text}|{media_id}"
    return hashlib.sha256(raw.encode()).hexdigest()

def passes_filter(text):
    if not text:
        return False

    t = text.upper()

    if not any(k in t for k in KEYWORDS):
        return False

    if REGEX_FILTER and not re.search(REGEX_FILTER, text, re.IGNORECASE):
        return False

    return True

# =========================
# RATE LIMITER (JITTERED)
# =========================
class RateLimiter:
    def __init__(self, delay):
        self.delay = delay
        self.last_time = 0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = asyncio.get_running_loop().time()
            delta = now - self.last_time

            if delta < self.delay:
                await asyncio.sleep(self.delay - delta)

            jitter = random.uniform(0, 0.05)
            await asyncio.sleep(jitter)

            self.last_time = asyncio.get_running_loop().time()

rate_limiter = RateLimiter(RATE_DELAY)

# =========================
# MESSAGE QUEUE
# =========================
queue = asyncio.Queue(maxsize=QUEUE_SIZE)

# =========================
# WORKER LOGIC
# =========================
async def worker(client, wid):
    logger.info(f"🚀 Worker-{wid} started")

    while True:
        message = await queue.get()

        try:
            text = message.text or ""

            if not passes_filter(text):
                logger.debug("🚫 Filtered")
                continue

            msg_hash = build_hash(message)

            if is_duplicate(msg_hash):
                logger.debug("🔁 Duplicate blocked")
                continue

            await rate_limiter.wait()

            if message.media:
                await client.forward_messages(TARGET_CHAT, message)
                logger.info(f"🖼 Worker-{wid} → Media {message.id}")

            else:
                await client.send_message(TARGET_CHAT, text)
                logger.info(f"✅ Worker-{wid} → Text {message.id}")

            save_hash(msg_hash)

        except FloodWaitError as e:
            logger.warning(f"⏳ FloodWait {e.seconds}s (Worker-{wid})")
            await asyncio.sleep(e.seconds + random.uniform(0.5, 1.5))

        except RPCError as e:
            logger.error(f"⚠ RPC Error (Worker-{wid}): {e}")

        except Exception as e:
            logger.exception(f"💥 Worker-{wid} crash: {e}")

        finally:
            queue.task_done()

# =========================
# MAIN ENGINE
# =========================
async def start_client():

    while True:
        try:
            client = TelegramClient(
                StringSession(SESSION_STRING),
                API_ID,
                API_HASH,
                connection_retries=999,
                retry_delay=3,
                auto_reconnect=True
            )

            await client.start()
            me = await client.get_me()

            logger.info(f"✅ Connected as {me.username or me.first_name}")

            # Validate chats
            for cid in SOURCE_CHATS + [TARGET_CHAT]:
                try:
                    await client.get_input_entity(cid)
                except Exception as e:
                    logger.error(f"❌ Invalid chat {cid}: {e}")

            # Start workers
            workers = [
                asyncio.create_task(worker(client, i))
                for i in range(1, WORKERS + 1)
            ]

            @client.on(events.NewMessage(chats=SOURCE_CHATS))
            async def handler(event):
                try:
                    queue.put_nowait(event.message)
                    logger.info(f"📩 Queued | {event.chat_id} | {event.message.id}")
                except asyncio.QueueFull:
                    logger.warning("⚠ Queue full — dropping message")

            logger.info("⚡ Copier running in REAL-TIME")

            await client.run_until_disconnected()

        except Exception as fatal:
            logger.exception(f"💥 CLIENT CRASH — restarting: {fatal}")
            await asyncio.sleep(5)

# =========================
# GRACEFUL SHUTDOWN
# =========================
def shutdown():
    logger.warning("🛑 Shutdown signal received")
    for task in asyncio.all_tasks():
        task.cancel()

# =========================
# ENTRY POINT
# =========================
async def main():
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    await start_client()

if __name__ == "__main__":
    asyncio.run(main())
