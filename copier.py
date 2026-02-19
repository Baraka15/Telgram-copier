import os
import asyncio
import logging
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict
import sqlite3

from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

# =========================
# ENV
# =========================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if not API_ID or not API_HASH or not SESSION_STRING:
    raise Exception("❌ Missing API credentials")

# =========================
# CONFIG
# =========================
SOURCE_CHATS = [
    -1001629856224,
    -1003735057293,
    -1003725482312
]

TARGET_CHATS = [
    -1003725482312
]

ALLOW_KEYWORDS = ["BUY", "SELL", "ENTRY", "SL", "TP", "XAUUSD"]

RATE_DELAY = 0.8   # seconds between sends (anti-flood)
DEDUP_WINDOW = 10  # minutes

DB_FILE = "copier.db"

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# =========================
# DB
# =========================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''
        CREATE TABLE IF NOT EXISTS forwarded (
            hash TEXT PRIMARY KEY,
            timestamp DATETIME
        )
        ''')

def is_duplicate(msg_hash: str) -> bool:
    cutoff = datetime.utcnow() - timedelta(minutes=DEDUP_WINDOW)
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT 1 FROM forwarded WHERE hash=? AND timestamp>?",
            (msg_hash, cutoff)
        ).fetchone()
        return row is not None

def record_hash(msg_hash: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO forwarded VALUES (?, ?)",
            (msg_hash, datetime.utcnow())
        )

# =========================
# FILTERS
# =========================
def should_forward(text: str) -> bool:
    if not text:
        return False
    t = text.upper()
    return any(k in t for k in ALLOW_KEYWORDS)

def build_hash(text: str, media) -> str:
    raw = (text or "") + (str(media) if media else "")
    return hashlib.sha256(raw.encode()).hexdigest()

def detect_media_type(message):
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"
    if isinstance(message.media, MessageMediaDocument):
        return "document"
    return None

# =========================
# RATE CONTROL
# =========================
last_send_time = 0

async def throttle():
    global last_send_time
    now = asyncio.get_event_loop().time()
    delta = now - last_send_time
    if delta < RATE_DELAY:
        await asyncio.sleep(RATE_DELAY - delta)
    last_send_time = asyncio.get_event_loop().time()

# =========================
# FORWARD LOGIC
# =========================
async def forward_message(client, event):

    message = event.message
    text = message.message

    if not should_forward(text):
        logger.debug(f"🚫 Filtered | {message.id}")
        return

    msg_hash = build_hash(text, message.media)

    if is_duplicate(msg_hash):
        logger.debug(f"🚫 Duplicate | {message.id}")
        return

    for target in TARGET_CHATS:

        try:
            await throttle()

            if message.media:
                await client.forward_messages(target, message)
                logger.info(f"🖼 Media forwarded → {target} | {message.id}")

            elif text:
                await client.send_message(target, text)
                logger.info(f"✅ Text forwarded → {target} | {message.id}")

        except errors.FloodWaitError as e:
            logger.warning(f"⏳ FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds)

        except Exception as e:
            logger.error(f"⚠ Forward error: {e}")

    record_hash(msg_hash)

# =========================
# MAIN
# =========================
async def main():

    init_db()

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        connection_retries=5
    )

    await client.start()

    me = await client.get_me()
    logger.info(f"✅ Connected as {me.username or me.first_name}")

    # Validate chats
    for chat_id in SOURCE_CHATS:
        try:
            await client.get_input_entity(chat_id)
        except Exception as e:
            logger.error(f"❌ Invalid source {chat_id}: {e}")

    @client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        logger.info(f"📩 New message in {event.chat_id}")
        await forward_message(client, event)

    logger.info("⚡ Copier running (REAL-TIME)")
    logger.info(f"👂 Sources: {SOURCE_CHATS}")
    logger.info(f"📤 Targets: {TARGET_CHATS}")

    await client.run_until_disconnected()

# =========================
# ENTRY
# =========================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Stopped")
