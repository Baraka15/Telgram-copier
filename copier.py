import os
import re
import asyncio
import logging
import hashlib
import sqlite3
from datetime import datetime, timedelta
from telethon import TelegramClient, events, errors
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
    -1001111111111,  # replace with real ID
]

TARGET_CHAT = -1003725482312

KEYWORDS = ["BUY", "SELL", "ENTRY", "SL", "TP", "XAUUSD"]
REGEX_FILTER = r"(buy|sell).*?(sl|tp)"

DEDUP_WINDOW_MINUTES = 30
RATE_DELAY = 0.2

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
logger = logging.getLogger(__name__)

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
        "INSERT OR REPLACE INTO forwarded (hash, timestamp) VALUES (?, ?)",
        (msg_hash, datetime.utcnow())
    )
    db.commit()

# =========================
# UTILITIES
# =========================
def build_hash(text, media):
    raw = f"{text}|{media}"
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
# RATE LIMITER
# =========================
class RateLimiter:
    def __init__(self, delay):
        self.delay = delay
        self.last_time = 0

    async def wait(self):
        now = asyncio.get_event_loop().time()
        delta = now - self.last_time
        if delta < self.delay:
            await asyncio.sleep(self.delay - delta)
        self.last_time = asyncio.get_event_loop().time()

rate_limiter = RateLimiter(RATE_DELAY)

# =========================
# SAFE FORWARD
# =========================
async def safe_forward(client, message):

    text = message.text or ""
    media_flag = bool(message.media)

    if not passes_filter(text):
        logger.debug("🚫 Filtered")
        return

    msg_hash = build_hash(text, media_flag)

    if is_duplicate(msg_hash):
        logger.debug("🔁 Duplicate blocked")
        return

    try:
        await rate_limiter.wait()

        if message.media:
            await client.forward_messages(TARGET_CHAT, message)
            logger.info(f"🖼 Media forwarded | ID {message.id}")

        else:
            await client.send_message(TARGET_CHAT, text)
            logger.info(f"✅ Text forwarded | ID {message.id}")

        save_hash(msg_hash)

    except FloodWaitError as e:
        logger.warning(f"⏳ FloodWait {e.seconds}s")
        await asyncio.sleep(e.seconds)
        await safe_forward(client, message)

    except RPCError as e:
        logger.error(f"⚠ RPC Error: {e}")

    except Exception as e:
        logger.exception(f"💥 Unexpected error: {e}")

# =========================
# MAIN ENGINE
# =========================
async def main():

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
            logger.info(f"👂 Listening: {SOURCE_CHATS}")
            logger.info(f"📤 Target: {TARGET_CHAT}")

            @client.on(events.NewMessage(chats=SOURCE_CHATS))
            async def handler(event):
                logger.info(
                    f"📩 New message | Chat {event.chat_id} | ID {event.message.id}"
                )
                await safe_forward(client, event.message)

            await client.run_until_disconnected()

        except Exception as fatal:
            logger.exception(f"💥 CRASH — restarting engine: {fatal}")
            await asyncio.sleep(5)

# =========================
# ENTRY POINT
# =========================
if __name__ == "__main__":
    asyncio.run(main())
