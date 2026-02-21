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
    -1001629856224,     # Source 1
    -1003735057293,     # Source 2 (your ID)
]

TARGET_CHAT = -1003725482312

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
# MESSAGE HASH
# =========================
def build_hash(message):
    text = message.raw_text or ""
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

# =========================
# SMART FILTERS
# =========================
BLOCK_PATTERNS = [
    r"REGISTER",
    r"SIGN\s?UP",
    r"CREATE\s?ACCOUNT",
    r"ACCOUNT\s?MANAGEMENT",
    r"BONUS",
    r"PROMO",
    r"REFERRAL",
    r"http[s]?://.*",     # links
    r"www\..*"
]

def is_blocked(text):
    t = text.upper()
    for pattern in BLOCK_PATTERNS:
        if re.search(pattern, t):
            return True
    return False

def passes_filter(text):
    if not text:
        return False

    if is_blocked(text):
        return False

    t = text.upper()

    has_direction = "BUY" in t or "SELL" in t
    has_levels = any(x in t for x in ["SL", "TP", "ENTRY"])

    return has_direction and has_levels

# =========================
# RATE LIMITER
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
# QUEUE
# =========================
queue = asyncio.Queue(maxsize=QUEUE_SIZE)

# =========================
# WORKER
# =========================
async def worker(client, wid):
    logger.info(f"🚀 Worker-{wid} started")

    while True:
        message = await queue.get()

        try:
            text = message.raw_text or ""
            logger.info(f"🧪 Worker-{wid} processing {message.id}")

            if not passes_filter(text):
                logger.info(f"🚫 Worker-{wid} filtered {message.id}")
                continue

            msg_hash = build_hash(message)

            if is_duplicate(msg_hash):
                logger.info(f"🔁 Worker-{wid} duplicate {message.id}")
                continue

            await rate_limiter.wait()

            if message.media:
                await client.forward_messages(TARGET_CHAT, message)
                logger.info(f"🖼 Worker-{wid} forwarded MEDIA {message.id}")

            else:
                await client.send_message(TARGET_CHAT, text)
                logger.info(f"✅ Worker-{wid} forwarded TEXT {message.id}")

            save_hash(msg_hash)

        except FloodWaitError as e:
            logger.warning(f"⏳ FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)

        except RPCError as e:
            logger.error(f"⚠ RPC Error: {e}")

        except Exception as e:
            logger.exception(f"💥 Worker-{wid} crash: {e}")

        finally:
            queue.task_done()

# =========================
# CLIENT ENGINE
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

            for cid in SOURCE_CHATS + [TARGET_CHAT]:
                try:
                    await client.get_input_entity(cid)
                    logger.info(f"✔ Chat OK: {cid}")
                except Exception as e:
                    logger.error(f"❌ Invalid chat {cid}: {e}")

            for i in range(1, WORKERS + 1):
                asyncio.create_task(worker(client, i))

            @client.on(events.NewMessage(chats=SOURCE_CHATS))
            async def handler(event):
                try:
                    queue.put_nowait(event.message)
                    logger.info(f"📩 Queued | {event.chat_id} | {event.message.id}")
                except asyncio.QueueFull:
                    logger.warning("⚠ Queue FULL — dropping")

            logger.info("⚡ Copier running REAL-TIME")
            await client.run_until_disconnected()

        except Exception as fatal:
            logger.exception(f"💥 CLIENT CRASH: {fatal}")
            await asyncio.sleep(5)

# =========================
# SHUTDOWN
# =========================
def shutdown():
    logger.warning("🛑 Shutdown signal")
    for task in asyncio.all_tasks():
        task.cancel()

# =========================
# ENTRY
# =========================
async def main():
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    await start_client()

if __name__ == "__main__":
    asyncio.run(main())
