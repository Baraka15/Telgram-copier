import os
import asyncio
import logging
import logging.handlers
import hashlib
import sqlite3
import json
import yaml
import aiohttp
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from telethon.errors import FloodWaitError, ChannelPrivateError, ChatAdminRequiredError, RPCError

import prometheus_client as prom
from prometheus_client import start_http_server

# =========================
# ENV
# =========================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

CONFIG_FILE = os.getenv("CONFIG_FILE", "advanced_config.yaml")
DB_FILE = os.getenv("DB_FILE", "advanced_copier.db")
METRICS_PORT = int(os.getenv("METRICS_PORT", 8000))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not API_ID or not API_HASH or not SESSION_STRING:
    raise Exception("❌ Missing API credentials")

# =========================
# LOAD CONFIG
# =========================
def load_config() -> Dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return yaml.safe_load(f)
    logging.warning("⚠ No config file found, using defaults")
    return {}

config = load_config()

SOURCE_CHATS: List[Dict] = config.get("sources", [])
TARGET_CHATS: List[Dict] = config.get("targets", [])

GLOBAL_SETTINGS: Dict = config.get(
    "global",
    {
        "rate_delay": 0.5,
        "dedup_window_minutes": 15,
        "retry_max": 3,
        "backoff_factor": 2,
        "log_level": "INFO",
        "cleanup_cron": "0 0 * * *",
        "metrics_enabled": True,
        "webhook_on_error": True,
    },
)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=GLOBAL_SETTINGS["log_level"],
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler("copier.log", maxBytes=1_000_000, backupCount=5),
    ],
)
logger = logging.getLogger(__name__)

# =========================
# METRICS
# =========================
metrics_enabled = GLOBAL_SETTINGS.get("metrics_enabled", False)

if metrics_enabled:
    forward_counter = prom.Counter("forwards_total", "Total forwarded", ["type", "source"])
    error_counter = prom.Counter("errors_total", "Errors", ["type"])
    dedup_counter = prom.Counter("dedups_total", "Deduplicated messages")
    latency_hist = prom.Histogram("forward_latency_seconds", "Forward latency")

    start_http_server(METRICS_PORT)
    logger.info(f"📊 Metrics running on {METRICS_PORT}")

# =========================
# DATABASE
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forwarded (
            hash TEXT PRIMARY KEY,
            source_id INTEGER,
            message_id INTEGER,
            timestamp DATETIME,
            metadata TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON forwarded(timestamp)")
    conn.commit()
    return conn

db_conn = init_db()

def is_duplicate(msg_hash: str, source_id: int) -> bool:
    cutoff = datetime.utcnow() - timedelta(minutes=GLOBAL_SETTINGS["dedup_window_minutes"])
    row = db_conn.execute(
        "SELECT 1 FROM forwarded WHERE hash=? AND source_id=? AND timestamp>?",
        (msg_hash, source_id, cutoff),
    ).fetchone()
    return row is not None

def record_hash(msg_hash: str, source_id: int, message_id: int, metadata: Dict):
    db_conn.execute(
        "INSERT OR REPLACE INTO forwarded VALUES (?, ?, ?, ?, ?)",
        (msg_hash, source_id, message_id, datetime.utcnow(), json.dumps(metadata)),
    )
    db_conn.commit()

async def cleanup_db():
    cutoff = datetime.utcnow() - timedelta(days=30)
    db_conn.execute("DELETE FROM forwarded WHERE timestamp < ?", (cutoff,))
    db_conn.commit()
    logger.info("🧹 DB cleanup complete")

# =========================
# MEDIA DETECTION
# =========================
def detect_media_type(message) -> Optional[str]:
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"

    if isinstance(message.media, MessageMediaDocument):
        mime = message.media.document.mime_type or ""
        if mime.startswith("video"):
            return "video"
        if mime.startswith("audio"):
            return "audio"
        return "document"

    return None

# =========================
# HASHING
# =========================
def build_hash(message) -> str:
    parts = [
        message.text or "",
        str(message.id),
        detect_media_type(message) or "",
    ]
    if isinstance(message.media, MessageMediaDocument):
        parts.append(str(message.media.document.id))

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()

# =========================
# FILTERS
# =========================
def apply_filters(message, filters: Dict) -> bool:
    text = message.text or ""

    if filters.get("keywords"):
        if not any(k in text.upper() for k in filters["keywords"]):
            return False

    if filters.get("regex"):
        if not re.search(filters["regex"], text, re.IGNORECASE):
            return False

    if len(text) < filters.get("min_length", 0):
        return False

    media_types = filters.get("media_types", ["any"])
    if "any" not in media_types:
        media_type = detect_media_type(message)
        if media_type not in media_types:
            return False

    return True

# =========================
# RATE LIMITER
# =========================
class RateLimiter:
    def __init__(self, delay):
        self.delay = delay
        self.last_send = 0.0

    async def wait(self):
        now = asyncio.get_running_loop().time()
        delta = now - self.last_send
        if delta < self.delay:
            await asyncio.sleep(self.delay - delta)
        self.last_send = asyncio.get_running_loop().time()

rate_limiter = RateLimiter(GLOBAL_SETTINGS["rate_delay"])

# =========================
# WEBHOOK
# =========================
async def send_webhook(payload: Dict):
    if not WEBHOOK_URL or not GLOBAL_SETTINGS.get("webhook_on_error"):
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(WEBHOOK_URL, json=payload)
    except Exception as e:
        logger.error(f"Webhook error: {e}")

# =========================
# FORWARD LOGIC
# =========================
async def forward_message(client, event, source_config):

    message = event.message
    source_id = event.chat_id

    if not apply_filters(message, source_config.get("filters", {})):
        logger.debug(f"🚫 Filtered | {message.id}")
        return

    msg_hash = build_hash(message)

    if is_duplicate(msg_hash, source_id):
        if metrics_enabled:
            dedup_counter.inc()
        logger.debug(f"🚫 Duplicate | {message.id}")
        return

    metadata = {
        "media_type": detect_media_type(message),
        "length": len(message.text or "")
    }

    for target in TARGET_CHATS:
        target_id = target["chat_id"]
        rules = target.get("rules", {})

        if rules.get("anti_loop") and source_id == target_id:
            continue

        try:
            if metrics_enabled:
                with latency_hist.time():
                    await rate_limiter.wait()
                    await dispatch_forward(client, message, source_config, target_id, rules, source_id)
            else:
                await rate_limiter.wait()
                await dispatch_forward(client, message, source_config, target_id, rules, source_id)

        except FloodWaitError as e:
            if metrics_enabled:
                error_counter.labels(type="flood").inc()
            await asyncio.sleep(e.seconds)

        except (ChannelPrivateError, ChatAdminRequiredError) as e:
            if metrics_enabled:
                error_counter.labels(type="access").inc()
            logger.error(f"Access error: {e}")
            await send_webhook({"error": str(e)})

        except RPCError as e:
            if metrics_enabled:
                error_counter.labels(type="rpc").inc()
            logger.error(f"RPC error: {e}")

        except Exception as e:
            if metrics_enabled:
                error_counter.labels(type="unknown").inc()
            logger.error(f"Unknown error: {e}")

    record_hash(msg_hash, source_id, message.id, metadata)

async def dispatch_forward(client, message, source_config, target_id, rules, source_id):

    if message.media and rules.get("allow_media", True):
        await client.forward_messages(target_id, message)
        if metrics_enabled:
            forward_counter.labels(type="media", source=str(source_id)).inc()
        logger.info(f"🖼 Media → {target_id}")

    elif message.text and rules.get("allow_text", True):
        text = message.text
        transform = source_config.get("transform", {})
        if transform:
            text = transform.get("prefix", "") + text + transform.get("suffix", "")
        await client.send_message(target_id, text)
        if metrics_enabled:
            forward_counter.labels(type="text", source=str(source_id)).inc()
        logger.info(f"✅ Text → {target_id}")

# =========================
# MAIN
# =========================
async def main():

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        connection_retries=20,
        retry_delay=5,
        auto_reconnect=True,
    )

    await client.start()
    me = await client.get_me()
    logger.info(f"✅ Connected as {me.username or me.first_name}")

    source_ids = [s["chat_id"] for s in SOURCE_CHATS]

    @client.on(events.NewMessage(chats=source_ids))
    async def handler(event):
        logger.info(f"📩 New message | {event.chat_id}")
        source_config = next((s for s in SOURCE_CHATS if s["chat_id"] == event.chat_id), None)
        if source_config:
            await forward_message(client, event, source_config)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(cleanup_db, CronTrigger.from_crontab(GLOBAL_SETTINGS["cleanup_cron"]))
    scheduler.start()

    logger.info("⚡ Copier running in REAL-TIME")
    await client.run_until_disconnected()

# =========================
# ENTRY
# =========================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Stopped")
    finally:
        db_conn.close()
        logger.info("🔌 Shutdown complete")
