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

print("✅ Session length:", len(SESSION_STRING))

# =========================
# SETTINGS
# =========================
DB_FILE = "copier.db"
LOG_FILE = "copier.log"

SOURCE_CHATS = [
    -1001629856224,
    -1003735057293,
    -1003537546255,  # Will auto-skip if invalid
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
    try:
        db.execute(
            "INSERT OR REPLACE INTO forwarded VALUES (?, ?)",
            (msg_hash, datetime.utcnow())
        )
        db.commit()
    except Exception as e:
        logger.error(f"DB write failed: {e}")

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
# FILTERS
# =========================
BLOCK_PATTERNS = [
    r"REGISTER",
    r"SIGN\s?UP",
    r"CREATE\s?ACCOUNT",
    r"ACCOUNT\s?MANAGEMENT",
    r"BONUS",
    r"PROMO",
    r"REFERRAL",
]

def is_blocked(text):
    if not text:
        return False

    t = text.upper()
    return any(re.search(p, t) for p in BLOCK_PATTERNS)

def passes_filter(text):
    if not text:
        return False

    if is_blocked(text):
        return False

    return True  # FULL PASS MODE (safe test mode)

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

            await asyncio.sleep(random.uniform(0, 0.05))
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

            if not passes_filter(text):
                logger.info(f"🚫 Filtered {message.id}")
                continue

            msg_hash = build_hash(message)

            if is_duplicate(msg_hash):
                logger.info(f"🔁 Duplicate {message.id}")
                continue

            await rate_limiter.wait()

            if message.media:
                await client.forward_messages(TARGET_CHAT, message)
                logger.info(f"🖼 Forwarded MEDIA {message.id}")
            else:
                await client.send_message(TARGET_CHAT, text)
                logger.info(f"✅ Forwarded TEXT {message.id}")

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

            valid_sources = []

            for cid in SOURCE_CHATS:
                try:
                    await client.get_input_entity(cid)
                    logger.info(f"✔ Source OK: {cid}")
                    valid_sources.append(cid)
                except Exception as e:
                    logger.error(f"❌ Invalid source {cid}: {e}")

            try:
                await client.get_input_entity(TARGET_CHAT)
                logger.info(f"✔ Target OK: {TARGET_CHAT}")
            except Exception as e:
                logger.error(f"❌ Invalid TARGET: {e}")
                await asyncio.sleep(10)
                continue

            if not valid_sources:
                logger.error("❌ No valid source chats. Sleeping...")
                await asyncio.sleep(10)
                continue

            for i in range(1, WORKERS + 1):
                asyncio.create_task(worker(client, i))

            @client.on(events.NewMessage(chats=valid_sources))
            async def handler(event):
                try:
                    queue.put_nowait(event.message)
                    logger.info(f"📩 Queued {event.message.id} from {event.chat_id}")
                except asyncio.QueueFull:
                    logger.warning("⚠ Queue FULL — dropping message")

            logger.info("⚡ Copier running REAL-TIME")

            await client.run_until_disconnected()

        except Exception as fatal:
            logger.exception(f"💥 CLIENT CRASH: {fatal}")
            await asyncio.sleep(5)

# =========================
# SHUTDOWN
# =========================
def shutdown():
    logger.warning("🛑 Shutdown signal received")
    for task in asyncio.all_tasks():
        task.cancel()

# =========================
# ENTRY
# =========================
async def main():
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass  # GitHub Actions safe

    await start_client()

if __name__ == "__main__":
    asyncio.run(main())
