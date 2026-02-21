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
RATE_DELAY = 0.35
WORKERS = 2
QUEUE_SIZE = 1000
MIN_RR = 1.2  # Quality threshold

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
logger = logging.getLogger("Engine")

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
    raw = (message.raw_text or "") + str(message.id)
    return hashlib.sha256(raw.encode()).hexdigest()

# =========================
# BLOCK FILTER
# =========================
BLOCK_PATTERNS = [
    r"REGISTER",
    r"SIGN\s?UP",
    r"CREATE\s?ACCOUNT",
    r"ACCOUNT\s?MANAGEMENT",
    r"BONUS",
    r"PROMO",
    r"REFERRAL",
    r"http[s]?://",
]

def is_blocked(text):
    if not text:
        return False

    t = text.upper()
    return any(re.search(p, t) for p in BLOCK_PATTERNS)

# =========================
# SIGNAL PARSER
# =========================
def parse_trade(text):
    if not text:
        return None

    t = text.upper()

    side = None
    if "BUY" in t:
        side = "BUY"
    elif "SELL" in t:
        side = "SELL"

    prices = re.findall(r"\d{3,5}(?:\.\d+)?", t)

    if side and len(prices) >= 3:
        return {
            "side": side,
            "entry": float(prices[0]),
            "sl": float(prices[1]),
            "tp": float(prices[2])
        }

    return None

# =========================
# RISK / REWARD
# =========================
def calculate_rr(trade):
    entry, sl, tp, side = trade.values()

    if side == "BUY":
        risk = entry - sl
        reward = tp - entry
    else:
        risk = sl - entry
        reward = entry - tp

    if risk <= 0:
        return None

    rr = reward / risk

    return {
        "risk": abs(risk),
        "reward": abs(reward),
        "rr": rr
    }

# =========================
# HUMAN MESSAGE ENGINE
# =========================
def humanize_signal(trade, rr_data):
    emoji = "🟢" if trade["side"] == "BUY" else "🔴"

    confidence = (
        "High Probability Setup"
        if rr_data["rr"] >= 2
        else "Moderate Probability Setup"
    )

    return (
        f"{emoji} {trade['side']} Opportunity\n\n"
        f"Entry : {trade['entry']}\n"
        f"Stop  : {trade['sl']}\n"
        f"Target: {trade['tp']}\n\n"
        f"📊 R:R Profile → 1:{rr_data['rr']:.2f}\n"
        f"⚠ Risk Distance → {rr_data['risk']:.2f}\n"
        f"🎯 Reward Range → {rr_data['reward']:.2f}\n\n"
        f"🧠 Trade Assessment:\n{confidence}\n\n"
        f"💡 Risk Guidance:\n"
        f"• Conservative: 0.5–1%\n"
        f"• Standard: 1–2%"
    )

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
# QUEUE
# =========================
queue = asyncio.Queue(maxsize=QUEUE_SIZE)

# =========================
# WORKER
# =========================
async def worker(client):
    while True:
        message = await queue.get()

        try:
            text = message.raw_text or ""

            if is_blocked(text):
                logger.info(f"🚫 Blocked {message.id}")
                continue

            trade = parse_trade(text)

            if not trade:
                logger.info(f"ℹ Non-signal {message.id}")
                continue

            rr_data = calculate_rr(trade)

            if not rr_data:
                logger.info(f"⚠ Invalid RR {message.id}")
                continue

            if rr_data["rr"] < MIN_RR:
                logger.info(f"🚫 Low RR filtered {message.id}")
                continue

            msg_hash = build_hash(message)

            if is_duplicate(msg_hash):
                logger.info(f"🔁 Duplicate {message.id}")
                continue

            final_text = humanize_signal(trade, rr_data)

            await rate_limiter.wait()

            await client.send_message(TARGET_CHAT, final_text)

            save_hash(msg_hash)

            logger.info(f"✅ Sent refined signal {message.id}")

        except FloodWaitError as e:
            logger.warning(f"⏳ FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds)

        except RPCError as e:
            logger.error(f"RPC Error: {e}")

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

    logger.info("⚡ Signal Intelligence Engine ACTIVE")
    await client.run_until_disconnected()

# =========================
# ENTRY
# =========================
async def main():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(asyncio.sleep(0)))

    await start()

if __name__ == "__main__":
    asyncio.run(main())
