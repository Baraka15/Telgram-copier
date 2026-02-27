import os
import re
import asyncio
import logging
import hashlib
import random
import signal
import time
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# =========================
# ENV VALIDATION
# =========================
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError("Missing Telegram credentials")

API_ID = int(API_ID)

# =========================
# CONFIG
# =========================
SOURCE_CHATS = [-1001629856224, -1003735057293]
TARGET_CHAT = -1003725482312

BASE_DELAY = 0.5
WORKERS = 2
QUEUE_LIMIT = 800
SEND_TIMEOUT = 30
MAX_HASHES = 5000

STATE_FILE = "state_data/forwarded_hashes.txt"
os.makedirs("state_data", exist_ok=True)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("Copier")

# =========================
# STATE SYSTEM
# =========================
def load_hashes():
    if not os.path.exists(STATE_FILE):
        return []
    with open(STATE_FILE, "r") as f:
        return [x.strip() for x in f if x.strip()]

hash_list = load_hashes()
hash_set = set(hash_list)

def persist_hashes():
    with open(STATE_FILE, "w") as f:
        for h in hash_list[-MAX_HASHES:]:
            f.write(h + "\n")

def register_hash(h):
    if h in hash_set:
        return False
    hash_set.add(h)
    hash_list.append(h)
    if len(hash_list) > MAX_HASHES:
        removed = hash_list.pop(0)
        hash_set.discard(removed)
    persist_hashes()
    return True

# =========================
# TEXT ENGINE
# =========================
LINK_PATTERNS = [r"http\S+", r"www\.\S+", r"t\.me/\S+"]

def strip_links(text):
    for p in LINK_PATTERNS:
        text = re.sub(p, "", text, flags=re.IGNORECASE)
    return text.strip()

def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip().upper()

def build_hash(msg):
    base = normalize_text(strip_links(msg.raw_text or ""))
    media_id = ""
    if msg.media and hasattr(msg.media, "document"):
        media_id = str(msg.media.document.id)
    combined = f"{base}|{media_id}"
    return hashlib.sha256(combined.encode()).hexdigest()

def detect_signal(text):
    t = (text or "").upper()
    return "BUY" in t or "SELL" in t

def enhance(text):
    if not text:
        return None
    header = random.choice(["🔥 SIGNAL", "📊 MARKET INTEL", "⚡ SETUP"])
    return f"{header}\n\n{text}\n\n🧠 Trade Smart"

# =========================
# ADAPTIVE RATE LIMITER
# =========================
class RateLimiter:
    def __init__(self, base):
        self.base = base
        self.delay = base
        self.lock = asyncio.Lock()
        self.last = 0

    async def wait(self):
        async with self.lock:
            now = asyncio.get_running_loop().time()
            diff = now - self.last
            if diff < self.delay:
                await asyncio.sleep(self.delay - diff)
            self.last = asyncio.get_running_loop().time()

    def penalize(self):
        self.delay = min(self.delay * 1.3, 10)

    def recover(self):
        self.delay = max(self.base, self.delay * 0.95)

rate = RateLimiter(BASE_DELAY)
queue = asyncio.PriorityQueue(maxsize=QUEUE_LIMIT)

# =========================
# WORKER LOOP
# =========================
async def worker(client, wid):
    log.info(f"Worker-{wid} online")

    while True:
        priority, msg = await queue.get()
        try:
            msg_hash = build_hash(msg)
            if not register_hash(msg_hash):
                continue

            clean = strip_links(msg.raw_text or "")
            final = enhance(clean)

            await rate.wait()

            if msg.media:
                await asyncio.wait_for(
                    client.send_file(TARGET_CHAT, msg.media, caption=final),
                    timeout=SEND_TIMEOUT
                )
            elif final:
                await asyncio.wait_for(
                    client.send_message(TARGET_CHAT, final),
                    timeout=SEND_TIMEOUT
                )

            rate.recover()

        except FloodWaitError as e:
            log.warning(f"FloodWait {e.seconds}s")
            rate.penalize()
            await asyncio.sleep(e.seconds)

        except Exception as e:
            log.error(f"Worker-{wid} error: {e}")

        finally:
            queue.task_done()

# =========================
# TELEGRAM CORE
# =========================
async def telegram_main():
    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
        auto_reconnect=True,
        connection_retries=15,
        retry_delay=5
    )

    await client.start()
    log.info("Telegram connected")

    # Validate access to chats
    for chat in SOURCE_CHATS:
        try:
            await client.get_entity(chat)
        except Exception as e:
            log.error(f"Cannot access source chat {chat}: {e}")

    for i in range(WORKERS):
        asyncio.create_task(worker(client, i + 1))

    @client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        text = event.message.raw_text or ""
        priority = 0 if detect_signal(text) else 1

        if queue.full():
            log.warning("Queue full — dropped")
            return

        await queue.put((priority, event.message))

    async def heartbeat():
        while True:
            log.info("Heartbeat OK")
            await asyncio.sleep(300)

    asyncio.create_task(heartbeat())

    log.info("⚡ Elite Copier Live")
    await client.run_until_disconnected()

# =========================
# RESILIENT BOOT LOOP
# =========================
def main_loop():
    backoff = 5
    while True:
        try:
            asyncio.run(telegram_main())
        except Exception as e:
            log.error(f"Crash: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 90)

# =========================
# FLASK KEEP ALIVE
# =========================
app = Flask(__name__)

@app.route("/")
def health():
    return "OK"

if __name__ == "__main__":
    import threading
    threading.Thread(target=main_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
