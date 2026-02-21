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
    -1003537546255,   # Source 3
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
        return False   # IMPORTANT FIX

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

    return True   # TEMPORARY FULL PASS MODE
