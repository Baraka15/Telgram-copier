import os
import re
import asyncio
import logging
import hashlib
import random
import threading
import time
import signal
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError

# =========================
# ENV CONFIG (STRICT)
# =========================
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError("Missing Telegram credentials")

API_ID = int(API_ID)

# =========================
# SETTINGS
# =========================
SOURCE_CHATS = [-1001629856224, -1003735057293]
TARGET_CHAT = -1003725482312

RATE_DELAY = 0.45
QUEUE_SIZE = 500
WORKERS = 2
SEND_TIMEOUT = 25

STATE_DIR = "state_data"
STATE_FILE = os.path.join(STATE_DIR, "forwarded_hashes.txt")
MAX_HASHES = 3000

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("EliteCopier")

# =========================
# STATE MANAGEMENT
# =========================
os.makedirs(STATE_DIR, exist_ok=True)

def load_hashes():
    if not os.path.exists(STATE_FILE):
        return []
    with open(STATE_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]

hashes = load_hashes()
KNOWN_HASHES = set(hashes)

def rewrite_state_file():
    with open(STATE_FILE, "w") as f:
        for h in hashes[-MAX_HASHES:]:
            f.write(h + "\n")

def save_hash(msg_hash):
    if msg_hash in KNOWN_HASHES:
        return

    KNOWN_HASHES.add(msg_hash)
    hashes.append(msg_hash)

    if len(hashes) > MAX_HASHES:
        hashes.pop(0)

    rewrite_state_file()

# =========================
# TEXT + HASH
# =========================
LINK_PATTERNS = [r"http[s]?://\S+", r"www\.\S+", r"t\.me/\S+"]

def strip_links(text):
    for pattern in LINK_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip()

def build_hash(message):
    text = strip_links(message.raw_text or "")
    normalized = re.sub(r"\s+", " ", text).strip().upper()

    media_id = ""
    if message.media:
        try:
            media_id = str(message.media.document.id)
        except:
            media_id = str(message.id)

    base = normalized + "|" + media_id
    return hashlib.sha256(base.encode()).hexdigest()

def detect_signal(text):
    t = (text or "").upper()
    return "BUY" in t or "SELL" in t

def enhance_message(text, is_signal):
    if not text:
        return None
    header = random.choice(
        ["🔥 GOLD SETUP", "📊 SIGNAL", "⚡ INTEL"]
    ) if is_signal else "📡 Market Update"
    return f"{header}\n\n{text}\n\n🧠 Trade Smart"

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
            diff = now - self.last
            if diff < self.delay:
                await asyncio.sleep(self.delay - diff)
            self.last = asyncio.get_running_loop().time()

    def slow_down(self):
        self.delay = min(self.delay * 1.4, 8)

    def normalize(self):
        self.delay = max(self.base_delay, self.delay * 0.97)

rate_limiter = AdaptiveRateLimiter(RATE_DELAY)
queue = asyncio.PriorityQueue(maxsize=QUEUE_SIZE)

# =========================
# WORKER
# =========================
async def worker(client, wid):
    logger.info(f"Worker-{wid} started")

    while True:
        priority, message = await queue.get()
        try:
            msg_hash = build_hash(message)
            if msg_hash in KNOWN_HASHES:
                continue

            clean_text = strip_links(message.raw_text or "")
            final_text = enhance_message(clean_text, detect_signal(clean_text))

            await rate_limiter.wait()

            if message.media:
                await asyncio.wait_for(
                    client.send_file(TARGET_CHAT, message.media, caption=final_text),
                    timeout=SEND_TIMEOUT
                )
            elif final_text:
                await asyncio.wait_for(
                    client.send_message(TARGET_CHAT, final_text),
                    timeout=SEND_TIMEOUT
                )

            save_hash(msg_hash)
            rate_limiter.normalize()

        except FloodWaitError as e:
            logger.warning(f"FloodWait: {e.seconds}s")
            rate_limiter.slow_down()
            await asyncio.sleep(e.seconds)

        except Exception as e:
            logger.error(f"Worker-{wid} error: {e}")

        finally:
            queue.task_done()

# =========================
# TELEGRAM CLIENT
# =========================
async def start_client():
    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        connection_retries=10,
        retry_delay=5
    )

    await client.start()
    logger.info("Telegram connected")

    for i in range(WORKERS):
        asyncio.create_task(worker(client, i + 1))

    @client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        text = event.message.raw_text or ""
        priority = 0 if detect_signal(text) else 1

        if queue.full():
            logger.warning("Queue full — dropping message")
            return

        await queue.put((priority, event.message))

    logger.info("⚡ Elite Copier Live")

    # Heartbeat
    async def heartbeat():
        while True:
            logger.info("Heartbeat: bot alive")
            await asyncio.sleep(300)

    asyncio.create_task(heartbeat())

    await client.run_until_disconnected()

# =========================
# CRASH LOOP
# =========================
def run_bot():
    backoff = 5
    while True:
        try:
            asyncio.run(start_client())
        except Exception as e:
            logger.error(f"Fatal crash: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 60)

# =========================
# FLASK KEEP ALIVE
# =========================
app = Flask(__name__)

@app.route("/")
def health():
    return "OK"

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
