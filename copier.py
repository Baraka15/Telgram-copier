import os
import re
import asyncio
import logging
import hashlib
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
SOURCE_CHATS = [-1001629856224, -1003735057293]
TARGET_CHAT = -1003725482312
ADMIN_CHAT = TARGET_CHAT

RATE_DELAY = 0.35
QUEUE_SIZE = 1000
SIMILARITY_THRESHOLD = 0.90
WORKERS = 2

MAX_RUNTIME = random.uniform(5.6, 5.8) * 60 * 60  # Randomized GitHub-safe exit
STARTUP_GRACE = 15
STATE_FILE = "forwarded_hashes.txt"
MAX_HASHES = 5000  # rotation cap

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("copier.log")]
)
logger = logging.getLogger("EliteCopier")

BOOT_TIME = datetime.utcnow()

# =========================
# STATE MANAGEMENT
# =========================
def load_hashes():
    if not os.path.exists(STATE_FILE):
        return []
    with open(STATE_FILE, "r") as f:
        return [line.strip() for line in f.readlines()]

def save_hash(msg_hash):
    hashes.append(msg_hash)
    if len(hashes) > MAX_HASHES:
        rotate_hashes()

    with open(STATE_FILE, "a") as f:
        f.write(msg_hash + "\n")

def rotate_hashes():
    logger.warning("♻ Rotating hash file")
    trimmed = hashes[-MAX_HASHES:]
    with open(STATE_FILE, "w") as f:
        for h in trimmed:
            f.write(h + "\n")
    hashes[:] = trimmed

hashes = load_hashes()
KNOWN_HASHES = set(hashes)

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
# HASHING
# =========================
def build_hash(message):
    text = strip_links(message.raw_text or "")
    normalized = re.sub(r"\s+", " ", text).strip().upper()
    return hashlib.sha256(normalized.encode()).hexdigest()

# =========================
# SIGNAL DETECTION
# =========================
def detect_signal(text):
    t = text.upper()
    return any(x in t for x in ["BUY", "SELL"])

# =========================
# MESSAGE ENHANCEMENT
# =========================
def enhance_message(text, is_signal):
    header = random.choice(
        ["🔥 GOLD TRADE SETUP", "📊 SIGNAL DETECTED", "⚡ EXECUTION INTEL"]
        if is_signal else
        ["📡 Market Feed", "🧠 Trade Intelligence", "⚡ Live Update"]
    )

    footer = random.choice([
        "\n\n⚠ Risk control advised",
        "\n\n📈 Plan your trade",
        "\n\n🧠 Stay disciplined"
    ])

    return f"{header}\n\n{text}{footer}"

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
        self.delay = min(self.delay * 1.2, 5)
        logger.warning(f"🐢 Rate slowed → {self.delay:.2f}s")

    def normalize(self):
        self.delay = max(self.base_delay, self.delay * 0.9)

rate_limiter = AdaptiveRateLimiter(RATE_DELAY)

# =========================
# QUEUE
# =========================
queue = asyncio.PriorityQueue(maxsize=QUEUE_SIZE)

def priority_score(text):
    return 0 if detect_signal(text) else 1

# =========================
# RUNTIME GUARD
# =========================
async def runtime_guard(client):
    start = asyncio.get_running_loop().time()

    while True:
        await asyncio.sleep(30)
        elapsed = asyncio.get_running_loop().time() - start

        if elapsed > MAX_RUNTIME:
            logger.warning("⏳ Scheduled graceful restart")

            try:
                await client.send_message(
                    ADMIN_CHAT,
                    "⏳ Restarting before GitHub timeout…"
                )
            except:
                pass

            await client.disconnect()
            break

# =========================
# HEARTBEAT
# =========================
async def heartbeat():
    while True:
        await asyncio.sleep(300)
        logger.info("💓 Heartbeat OK")

# =========================
# FILTER OLD
# =========================
def ignore_old_messages(message):
    if not message.date:
        return False
    return message.date < BOOT_TIME - timedelta(seconds=10)

# =========================
# WORKER
# =========================
async def worker(client, wid):
    logger.info(f"🚀 Worker-{wid} started")

    while True:
        _, message = await queue.get()

        try:
            if ignore_old_messages(message):
                continue

            raw_text = message.raw_text or ""
            clean_text = strip_links(raw_text)

            if len(clean_text) < 3:
                continue

            msg_hash = build_hash(message)

            if msg_hash in KNOWN_HASHES:
                continue

            final_text = enhance_message(clean_text, detect_signal(clean_text))

            await rate_limiter.wait()

            if message.media:
                await client.send_file(TARGET_CHAT, message.media, caption=final_text)
                logger.info(f"🖼 Worker-{wid} → MEDIA {message.id}")
            else:
                await client.send_message(TARGET_CHAT, final_text)
                logger.info(f"✅ Worker-{wid} → TEXT {message.id}")

            save_hash(msg_hash)
            KNOWN_HASHES.add(msg_hash)
            rate_limiter.normalize()

        except FloodWaitError as e:
            logger.warning(f"⏳ FloodWait {e.seconds}s")
            rate_limiter.slow_down()
            await asyncio.sleep(e.seconds + 5)

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
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    logger.info(f"✅ Running as {me.first_name}")

    asyncio.create_task(runtime_guard(client))
    asyncio.create_task(heartbeat())

    await asyncio.sleep(STARTUP_GRACE)

    for i in range(WORKERS):
        asyncio.create_task(worker(client, i + 1))

    @client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        try:
            score = priority_score(event.message.raw_text or "")
            queue.put_nowait((score, event.message))
        except asyncio.QueueFull:
            logger.warning("⚠ Queue FULL")

    logger.info("⚡ Elite Copier ACTIVE")
    await client.run_until_disconnected()

# =========================
# RESILIENT LOOP
# =========================
async def resilient_runner():
    while True:
        try:
            await start_client()
        except Exception as e:
            logger.exception(f"💥 Client crash → restarting: {e}")
            await asyncio.sleep(10)

# =========================
# ENTRY
# =========================
def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: None)
        except NotImplementedError:
            pass

    loop.run_until_complete(resilient_runner())

if __name__ == "__main__":
    main()
