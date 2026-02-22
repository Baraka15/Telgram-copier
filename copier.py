import os
import re
import asyncio
import logging
import hashlib
import sqlite3
import signal
import random
from datetime import datetime, timedelta
from difflib import SequenceMatcher
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

SOURCE_CHATS = [-1001629856224, -1003735057293]
TARGET_CHAT = -1003725482312
ADMIN_CHAT = TARGET_CHAT  # send system alerts here

DEDUP_WINDOW_MINUTES = 30
RATE_DELAY = 0.35
QUEUE_SIZE = 1000
SIMILARITY_THRESHOLD = 0.90

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)]
)
logger = logging.getLogger("EliteCopier")

# =========================
# DATABASE
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forwarded (
            hash TEXT PRIMARY KEY,
            text TEXT,
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
        "SELECT text FROM forwarded WHERE hash=? AND timestamp>?",
        (msg_hash, cutoff)
    )
    return cur.fetchone()

def save_message(msg_hash, text):
    db.execute(
        "INSERT OR REPLACE INTO forwarded VALUES (?, ?, ?)",
        (msg_hash, text, datetime.utcnow())
    )
    db.commit()

# =========================
# HASHING
# =========================
def build_hash(message):
    raw = (message.raw_text or "") + str(message.id)
    return hashlib.sha256(raw.encode()).hexdigest()

# =========================
# TEXT UTILITIES
# =========================
LINK_PATTERNS = [r"http[s]?://\S+", r"www\.\S+", r"t\.me/\S+"]

def strip_links(text):
    for pattern in LINK_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip()

def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

# =========================
# SIGNAL DETECTION
# =========================
def detect_signal(text):
    t = text.upper()
    if any(x in t for x in ["BUY", "SELL"]):
        return True
    return False

def extract_trade_elements(text):
    entry = re.findall(r"\d{3,5}(?:\.\d+)?", text)
    sl = re.findall(r"SL[:\s]*\d+(?:\.\d+)?", text.upper())
    tp = re.findall(r"TP[:\s]*\d+(?:\.\d+)?", text.upper())
    return entry, sl, tp

# =========================
# RATE LIMITER
# =========================
class AdaptiveRateLimiter:
    def __init__(self, base_delay):
        self.base_delay = base_delay
        self.delay = base_delay
        self.lock = asyncio.Lock()
        self.last = 0

    async def wait(self):
        async with self.lock:
            now = asyncio.get_running_loop().time()

            if now - self.last < self.delay:
                await asyncio.sleep(self.delay - (now - self.last))

            await asyncio.sleep(random.uniform(0, 0.05))
            self.last = asyncio.get_running_loop().time()

    def slow_down(self):
        self.delay = min(self.delay * 1.2, 3)

    def normalize(self):
        self.delay = max(self.base_delay, self.delay * 0.9)

rate_limiter = AdaptiveRateLimiter(RATE_DELAY)

# =========================
# MESSAGE ENGINE
# =========================
def enhance_message(text, is_signal):
    if is_signal:
        header = random.choice([
            "🔥 GOLD TRADE SETUP",
            "📊 SIGNAL DETECTED",
            "⚡ EXECUTION INTEL"
        ])
    else:
        header = random.choice([
            "📡 Market Feed",
            "🧠 Trade Intelligence",
            "⚡ Live Update"
        ])

    footer = random.choice([
        "\n\n⚠ Risk control advised",
        "\n\n📈 Plan your trade",
        "\n\n🧠 Stay disciplined"
    ])

    return f"{header}\n\n{text}{footer}"

# =========================
# QUEUE
# =========================
queue = asyncio.PriorityQueue(maxsize=QUEUE_SIZE)

def priority_score(text):
    if detect_signal(text):
        return 0  # highest priority
    return 1

# =========================
# WORKER
# =========================
async def worker(client):
    logger.info("🚀 Elite Worker started")

    while True:
        _, message = await queue.get()

        try:
            raw_text = message.raw_text or ""
            clean_text = strip_links(raw_text)

            if len(clean_text) < 3:
                continue

            msg_hash = build_hash(message)
            existing = is_duplicate(msg_hash)

            if existing:
                if similarity(existing[0], clean_text) > SIMILARITY_THRESHOLD:
                    logger.info(f"🔁 Similar duplicate {message.id}")
                    continue

            is_signal = detect_signal(clean_text)
            final_text = enhance_message(clean_text, is_signal)

            await rate_limiter.wait()

            if message.media:
                await client.send_file(TARGET_CHAT, message.media, caption=final_text)
                logger.info(f"🖼 Sent MEDIA {message.id}")
            else:
                await client.send_message(TARGET_CHAT, final_text)
                logger.info(f"✅ Sent TEXT {message.id}")

            save_message(msg_hash, clean_text)
            rate_limiter.normalize()

        except FloodWaitError as e:
            logger.warning(f"⏳ FloodWait {e.seconds}s")
            rate_limiter.slow_down()
            await asyncio.sleep(e.seconds)

        except RPCError as e:
            logger.error(f"⚠ RPC Error: {e}")
            await client.send_message(ADMIN_CHAT, f"⚠ Copier RPC Error:\n{e}")

        except Exception as e:
            logger.exception(f"💥 Worker crash: {e}")
            await client.send_message(ADMIN_CHAT, f"💥 Copier Crash:\n{e}")

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
            text = event.message.raw_text or ""
            score = priority_score(text)
            queue.put_nowait((score, event.message))
            logger.info(f"📩 Queued {event.message.id} | priority={score}")
        except asyncio.QueueFull:
            logger.warning("⚠ Queue FULL")

    logger.info("⚡ Elite Copier ACTIVE")
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
