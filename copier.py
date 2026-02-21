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

DEDUP_WINDOW_MINUTES = 30
RATE_DELAY = 0.30
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
# HASHING
# =========================
def build_hash(message):
    raw = (message.raw_text or "") + str(message.id)
    return hashlib.sha256(raw.encode()).hexdigest()

# =========================
# FILTERS
# =========================
LINK_PATTERNS = [
    r"http[s]?://",
    r"www\.",
    r"t\.me/",
]

def contains_link(text):
    if not text:
        return False
    return any(re.search(p, text, re.IGNORECASE) for p in LINK_PATTERNS)

def contains_xauusd(text):
    if not text:
        return False
    t = text.upper()
    return "XAUUSD" in t  # covers XAUUSD & XAUUSDm

def passes_filter(text):
    if not text:
        return False

    if contains_link(text):
        return False

    if not contains_xauusd(text):
        return False

    return True

# =========================
# RATE LIMITER
# =========================
class RateLimiter:
    def __init__(self, delay):
        self.delay = delay
        self.lock = asyncio.Lock()
        self.last = 0

    async def wait(self):
        async with self.lock:
            now = asyncio.get_running_loop().time()

            if now - self.last < self.delay:
                await asyncio.sleep(self.delay - (now - self.last))

            await asyncio.sleep(random.uniform(0, 0.05))
            self.last = asyncio.get_running_loop().time()

rate_limiter = RateLimiter(RATE_DELAY)

# =========================
# MESSAGE ENGINE (UNIQUE STYLE)
# =========================
def enhance_message(text):
    headers = [
        "📡 Gold Market Feed",
        "⚡ XAUUSD Signal Intelligence",
        "🧠 Trade Flow Update",
        "📊 Precision Setup Detected",
        "🔥 Gold Execution Alert"
    ]

    footer = random.choice([
        "\n\n⚠ Manage risk responsibly",
        "\n\n📈 Follow your plan",
        "\n\n🧠 Discipline = Edge",
        "\n\n⏳ Timing matters"
    ])

    header = random.choice(headers)

    return f"{header}\n\n{text}{footer}"

# =========================
# QUEUE
# =========================
queue = asyncio.Queue(maxsize=QUEUE_SIZE)

# =========================
# WORKER
# =========================
async def worker(client):
    logger.info("🚀 Worker started")

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

            final_text = enhance_message(text)

            await rate_limiter.wait()

            if message.media:
                await client.send_file(
                    TARGET_CHAT,
                    message.media,
                    caption=final_text
                )
                logger.info(f"🖼 Sent MEDIA {message.id}")
            else:
                await client.send_message(TARGET_CHAT, final_text)
                logger.info(f"✅ Sent TEXT {message.id}")

            save_hash(msg_hash)

        except FloodWaitError as e:
            logger.warning(f"⏳ FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds)

        except RPCError as e:
            logger.error(f"⚠ RPC Error: {e}")

        except Exception as e:
            logger.exception(f"💥 Worker crash: {e}")

        finally:
            queue.task_done()

# =========================
# CLIENT ENGINE
# =========================
async def start():
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    logger.info(f"✅ Running as {me.first_name}")

    asyncio.create_task(worker(client))

    @client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        try:
            queue.put_nowait(event.message)
            logger.info(f"📩 Queued {event.message.id}")
        except asyncio.QueueFull:
            logger.warning("⚠ Queue FULL")

    logger.info("⚡ XAUUSD Realtime Engine ACTIVE")
    await client.run_until_disconnected()

# =========================
# ENTRY
# =========================
async def main():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: None)

    await start()

if __name__ == "__main__":
    asyncio.run(main())
