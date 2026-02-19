import os
import asyncio
import logging
import hashlib
import sqlite3
import json
import yaml
import aiohttp
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import (
    Message, PeerChannel, InputPeerChannel,
    MessageMediaPhoto, MessageMediaDocument, MessageMediaVideo,
    MessageMediaAudio, MessageMediaGeo, MessageMediaPoll,
    MessageMediaDice, MessageMediaContact, MessageMediaGame,
    MessageMediaInvoice, MessageMediaWebPage
)
from telethon.errors import (
    FloodWaitError, ChannelPrivateError, ChatAdminRequiredError,
    RPCError
)
import re
import functools
import prometheus_client as prom
from prometheus_client import start_http_server

# =========================
# GLOBAL CONSTANTS AND ENV VALIDATION
# =========================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
CONFIG_FILE = os.getenv("CONFIG_FILE", "advanced_config.yaml")
DB_FILE = os.getenv("DB_FILE", "advanced_copier.db")
METRICS_PORT = int(os.getenv("METRICS_PORT", 8000))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Optional for error notifications

if not API_ID or not API_HASH or not SESSION_STRING:
    raise Exception("❌ Missing API credentials")

# =========================
# LOAD ADVANCED CONFIGURATION (YAML)
# =========================
def load_config() -> Dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = yaml.safe_load(f)
    else:
        config = {}
        logging.warning("⚠ No config file found, using defaults")
    return config

config = load_config()

# Advanced Config Defaults with Overrides
SOURCE_CHATS: List[Dict] = config.get('sources', [
    {
        'chat_id': -1001629856224,
        'filters': {
            'keywords': ["BUY", "SELL", "ENTRY", "SL", "TP", "XAUUSD"],
            'regex': r'(buy|sell)\s+(entry|sl|tp)',  # Optional regex pattern
            'senders': None,  # List of usernames or None
            'time_window': {'start': '00:00', 'end': '23:59'},  # 24h format
            'media_types': ['photo', 'video', 'document', 'any'],  # 'any' for all
            'min_length': 10,
            'strict_mode': False
        },
        'transform': {
            'prefix': '[SOURCE1] ',
            'suffix': ' | Copied at {timestamp}',
            'replacements': {'XAUUSD': 'Gold/USD'}
        }
    },
    # Add more sources similarly
])

TARGET_CHATS: List[Dict] = config.get('targets', [
    {
        'chat_id': -1003725482312,
        'rules': {
            'allow_media': True,
            'allow_text': True,
            'batch_size': 5,  # Batch forwarding for efficiency
            'anti_loop': True
        }
    }
    # Support multiple targets
])

GLOBAL_SETTINGS: Dict = config.get('global', {
    'rate_delay': 0.5,  # Seconds between sends
    'dedup_window_minutes': 15,
    'retry_max': 3,
    'backoff_factor': 2,
    'log_level': 'INFO',
    'cleanup_cron': '0 0 * * *',  # Daily at midnight
    'metrics_enabled': True,
    'webhook_on_error': True
})

# =========================
# LOGGING SETUP (With Rotation)
# =========================
logging.basicConfig(
    level=GLOBAL_SETTINGS['log_level'],
    format="%(asctime)s | %(levelname)s | %(message)s | %(module)s:%(lineno)d",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler('copier.log', maxBytes=10**6, backupCount=5)
    ]
)
logger = logging.getLogger(__name__)

# =========================
# METRICS (Prometheus Integration)
# =========================
if GLOBAL_SETTINGS['metrics_enabled']:
    forward_counter = prom.Counter('forwards_total', 'Total forwarded messages', ['type', 'source'])
    error_counter = prom.Counter('errors_total', 'Total errors', ['type'])
    dedup_counter = prom.Counter('dedups_total', 'Deduplicated messages')
    latency_hist = prom.Histogram('forward_latency_seconds', 'Forward latency')

    start_http_server(METRICS_PORT)
    logger.info(f"📊 Metrics server started on port {METRICS_PORT}")

# =========================
# DATABASE (Async SQLite with WAL and Indexing)
# =========================
async def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
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

db_conn = None  # Will be initialized in main

def is_duplicate(msg_hash: str, source_id: int) -> bool:
    cutoff = datetime.utcnow() - timedelta(minutes=GLOBAL_SETTINGS['dedup_window_minutes'])
    cursor = db_conn.cursor()
    cursor.execute(
        "SELECT 1 FROM forwarded WHERE hash=? AND source_id=? AND timestamp>?",
        (msg_hash, source_id, cutoff)
    )
    return cursor.fetchone() is not None

def record_hash(msg_hash: str, source_id: int, message_id: int, metadata: Dict):
    cursor = db_conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO forwarded (hash, source_id, message_id, timestamp, metadata) VALUES (?, ?, ?, ?, ?)",
        (msg_hash, source_id, message_id, datetime.utcnow(), json.dumps(metadata))
    )
    db_conn.commit()

async def cleanup_db():
    cutoff = datetime.utcnow() - timedelta(days=30)  # Retain 30 days
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM forwarded WHERE timestamp < ?", (cutoff,))
    db_conn.commit()
    logger.info(f"🧹 DB cleanup: Removed old records")

# =========================
# ADVANCED FILTERS AND TRANSFORMS
# =========================
def apply_filters(message: Message, filters: Dict) -> bool:
    if not message.text and not filters.get('strict_mode', False):
        return True  # Allow media without text if not strict

    text = message.text or ''
    upper_text = text.upper()

    # Keyword check
    keywords = filters.get('keywords', [])
    if keywords and not any(k in upper_text for k in keywords):
        return False

    # Regex check
    regex_pattern = filters.get('regex')
    if regex_pattern and not re.search(regex_pattern, text, re.IGNORECASE):
        return False

    # Sender check
    senders = filters.get('senders')
    if senders and (not message.sender or message.sender.username not in senders):
        return False

    # Time window check
    time_window = filters.get('time_window')
    if time_window:
        msg_time = message.date.time()
        start = datetime.strptime(time_window['start'], '%H:%M').time()
        end = datetime.strptime(time_window['end'], '%H:%M').time()
        if not (start <= msg_time <= end):
            return False

    # Media type check
    media_types = filters.get('media_types', ['any'])
    if 'any' not in media_types:
        media_type = detect_media_type(message)
        if media_type not in media_types:
            return False

    # Min length
    if len(text) < filters.get('min_length', 0):
        return False

    return True

def transform_message(text: str, transform: Dict) -> str:
    prefix = transform.get('prefix', '')
    suffix = transform.get('suffix', '').format(timestamp=datetime.utcnow().isoformat())
    replacements = transform.get('replacements', {})
    for old, new in replacements.items():
        text = text.replace(old, new)
    return prefix + text + suffix

def detect_media_type(message: Message) -> Optional[str]:
    media = message.media
    if isinstance(media, MessageMediaPhoto): return 'photo'
    if isinstance(media, MessageMediaDocument): return 'document'
    if isinstance(media, MessageMediaVideo): return 'video'
    if isinstance(media, MessageMediaAudio): return 'audio'
    if isinstance(media, MessageMediaGeo): return 'geo'
    if isinstance(media, MessageMediaPoll): return 'poll'
    if isinstance(media, MessageMediaDice): return 'dice'
    if isinstance(media, MessageMediaContact): return 'contact'
    if isinstance(media, MessageMediaGame): return 'game'
    if isinstance(media, MessageMediaInvoice): return 'invoice'
    if isinstance(media, MessageMediaWebPage): return 'webpage'
    return None

def build_hash(message: Message) -> str:
    parts = [message.text or '', str(message.id), detect_media_type(message) or '']
    if message.media and hasattr(message.media, 'document'):
        parts.append(str(message.media.document.id))  # Include media ID for better dedup
    raw = '|'.join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()

# =========================
# RATE LIMITING WITH EXPONENTIAL BACKOFF
# =========================
class RateLimiter:
    def __init__(self, delay: float, max_retries: int, backoff_factor: int):
        self.delay = delay
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.last_send = 0.0
        self.sem = asyncio.Semaphore(1)  # For thread-safety

    async def acquire(self, retry_count: int = 0):
        async with self.sem:
            now = asyncio.get_running_loop().time()
            delta = now - self.last_send
            if delta < self.delay:
                await asyncio.sleep(self.delay - delta)
            self.last_send = asyncio.get_running_loop().time()

    async def handle_flood(self, e: FloodWaitError, func, *args, **kwargs):
        for attempt in range(self.max_retries):
            try:
                await asyncio.sleep(e.seconds + (self.backoff_factor ** attempt))
                return await func(*args, **kwargs)
            except FloodWaitError as inner_e:
                e = inner_e
        raise e

rate_limiter = RateLimiter(
    GLOBAL_SETTINGS['rate_delay'],
    GLOBAL_SETTINGS['retry_max'],
    GLOBAL_SETTINGS['backoff_factor']
)

# =========================
# WEBHOOK NOTIFICATION
# =========================
async def send_webhook(payload: Dict):
    if not WEBHOOK_URL or not GLOBAL_SETTINGS['webhook_on_error']:
        return
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    logger.warning(f"⚠ Webhook failed: {resp.status}")
        except Exception as e:
            logger.error(f"⚠ Webhook error: {e}")

# =========================
# FORWARD LOGIC WITH RETRIES AND BATCHING
# =========================
async def forward_message(client: TelegramClient, event: events.NewMessage.Event, source_config: Dict):
    message = event.message
    source_id = event.chat_id

    if not apply_filters(message, source_config['filters']):
        logger.debug(f"🚫 Filtered out | ID: {message.id} | Source: {source_id}")
        return

    msg_hash = build_hash(message)
    if is_duplicate(msg_hash, source_id):
        dedup_counter.inc()
        logger.debug(f"🚫 Duplicate | ID: {message.id} | Hash: {msg_hash}")
        return

    metadata = {'text_length': len(message.text or ''), 'media_type': detect_media_type(message)}

    for target in TARGET_CHATS:
        target_id = target['chat_id']
        rules = target['rules']

        if rules['anti_loop'] and source_id == target_id:
            logger.debug(f"🔁 Loop prevented | ID: {message.id}")
            continue

        @latency_hist.time()
        async def perform_forward():
            if message.media and rules['allow_media']:
                await client.forward_messages(target_id, message)
                forward_counter.inc(labels={'type': 'media', 'source': str(source_id)})
                logger.info(f"🖼 Media forwarded to {target_id} | ID: {message.id}")

            elif message.text and rules['allow_text']:
                transformed_text = transform_message(message.text, source_config.get('transform', {}))
                await client.send_message(target_id, transformed_text)
                forward_counter.inc(labels={'type': 'text', 'source': str(source_id)})
                logger.info(f"✅ Text forwarded to {target_id} | ID: {message.id}")

        try:
            await rate_limiter.acquire()
            await perform_forward()
        except FloodWaitError as e:
            error_counter.inc(labels={'type': 'flood'})
            await rate_limiter.handle_flood(e, perform_forward)
        except (ChannelPrivateError, ChatAdminRequiredError) as e:
            error_counter.inc(labels={'type': 'access'})
            logger.error(f"🚫 Access error: {e}")
            await send_webhook({'error': str(e), 'type': 'access', 'source': source_id})
        except RPCError as e:
            error_counter.inc(labels={'type': 'rpc'})
            logger.error(f"⚠ RPC error: {e}")
            await send_webhook({'error': str(e), 'type': 'rpc', 'source': source_id})
        except Exception as e:
            error_counter.inc(labels={'type': 'unknown'})
            logger.error(f"⚠ Unknown error: {e}")
            await send_webhook({'error': str(e), 'type': 'unknown', 'source': source_id})

    record_hash(msg_hash, source_id, message.id, metadata)

# =========================
# MAIN CLIENT AND SCHEDULER
# =========================
async def main():
    global db_conn
    db_conn = await init_db()

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        connection_retries=20,
        retry_delay=5,
        auto_reconnect=True,
        flood_sleep_threshold=120
    )

    await client.start()

    me = await client.get_me()
    logger.info(f"✅ Connected as {me.username or me.first_name}")

    # Pre-resolve entities for efficiency
    for source in SOURCE_CHATS:
        try:
            await client.get_input_entity(source['chat_id'])
        except Exception as e:
            logger.error(f"❌ Invalid source {source['chat_id']}: {e}")

    source_ids = [s['chat_id'] for s in SOURCE_CHATS]

    @client.on(events.NewMessage(chats=source_ids))
    async def handler(event):
        logger.info(f"📩 New message detected | Chat: {event.chat_id} | ID: {event.message.id}")
        source_config = next((s for s in SOURCE_CHATS if s['chat_id'] == event.chat_id), None)
        if source_config:
            await forward_message(client, event, source_config)

    # Scheduler for advanced tasks
    scheduler = AsyncIOScheduler()
    scheduler.add_job(cleanup_db, CronTrigger.from_crontab(GLOBAL_SETTINGS['cleanup_cron']))
    scheduler.start()
    logger.info(f"⏰ Scheduler started with cron: {GLOBAL_SETTINGS['cleanup_cron']}")

    logger.info("⚡ Ultra Advanced Copier running in REAL-TIME")
    logger.info(f"👂 Listening to sources: {source_ids}")
    logger.info(f"📤 Forwarding to targets: {[t['chat_id'] for t in TARGET_CHATS]}")

    await client.run_until_disconnected()

# =========================
# ENTRY POINT WITH GRACEFUL SHUTDOWN
# =========================
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("🛑 Stopped by user")
    except Exception as e:
        logger.critical(f"💥 Fatal error: {e}")
    finally:
        if db_conn:
            db_conn.close()
        logger.info("🔌 Shutdown complete")
