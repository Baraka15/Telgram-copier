import os
import re
import asyncio
import logging
import hashlib
import random
import requests
import threading
from datetime import datetime, timezone
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# =========================
# ENV CONFIG
# =========================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
GH_TOKEN = os.getenv("GH_TOKEN")
REPO = os.getenv("GITHUB_REPOSITORY")

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
WORKERS = 2

MAX_RUNTIME = random.uniform(330, 345) * 60

STATE_DIR = "state_data"
STATE_FILE = os.path.join(STATE_DIR, "forwarded_hashes.txt")
MAX_HASHES = 5000

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("EliteCopier")

BOOT_TIME = datetime.now(timezone.utc)
shutdown_event = asyncio.Event()

# =========================
# GITHUB RESTART
# =========================
def trigger_github_restart():
    if not REPO or not GH_TOKEN:
        logger.error("❌ Restart aborted: Missing repo/token")
        return False

    url = f"https://api.github.com/repos/{REPO}/actions/workflows/copier.yml/dispatches"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    payload = {"ref": "main"}

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code == 204:
            logger.info("🚀 GitHub restart triggered")
            return True
        else:
            logger.error(f"❌ Restart failed: {res.status_code} {res.text}")
            return False
    except Exception as e:
        logger.error(f"💥 Restart exception: {e}")
        return False

# =========================
# STATE MANAGEMENT
# =========================
def load_hashes():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(STATE_FILE):
        return []
    with open(STATE_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]

hashes = load_hashes()
KNOWN_HASHES = set(hashes)

def save_hash(msg_hash):
    if msg_hash in KNOWN_HASHES:
        return

    hashes.append(msg_hash)
    KNOWN_HASHES.add(msg_hash)

    if len(hashes) > MAX_HASHES:
        hashes[:] = hashes[-MAX_HASHES:]
        KNOWN_HASHES.clear()
        KNOWN_HASHES.update(hashes)

    with open(STATE_FILE, "w") as f:
        for h in hashes:
            f.write(h + "\n")

# =========================
# UTILITIES
# =========================
LINK_PATTERNS = [r"http[s]?://\S+", r"www\.\S+", r"t\.me/\S+"]

def strip_links(text):
    for pattern in LINK_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip()

def build_hash(message):
    text = message.raw_text or message.text or ""
    text = strip_links(text)
    normalized = re.sub(r"\s+", " ", text).strip().upper()
    media_id = str(message.media) if message.media else ""
    base = normalized + "|" + media_id
    return hashlib.sha256(base.encode()).hexdigest()

def detect_signal(text):
    t = (text or "").upper()
    return "BUY" in t or "SELL" in t

def enhance_message(text, is_signal):
    header = random.choice(["🔥 GOLD SETUP", "📊 SIGNAL", "⚡ INTEL"]) if is_signal else "📡 Market Update"
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
            if now - self.last < self.delay:
                await asyncio.sleep(self.delay - (now - self.last))
            self.last = asyncio.get_running_loop().time()

    def slow_down(self):
        self.delay = min(self.delay * 1.5, 10)

    def normalize(self):
        self.delay = max(self.base_delay, self.delay * 0.95)

rate_limiter = AdaptiveRateLimiter(RATE_DELAY)
queue = asyncio.PriorityQueue(maxsize=QUEUE_SIZE)

# =========================
# RUNTIME GUARD
# =========================
async def runtime_guard(client):
    await asyncio.sleep(MAX_RUNTIME)
    logger.warning("⏳ Restart cycle initiated")
    trigger_github_restart()

    try:
        await client.send_message(ADMIN_CHAT, "🔄 Cycle Complete. Restarting...")
    except:
        pass

    shutdown_event.set()
    await client.disconnect()
    os._exit(0)

# =========================
# WORKER
# =========================
async def worker(client, wid):
    while not shutdown_event.is_set():
        try:
            priority, message = await queue.get()

            msg_hash = build_hash(message)
            if msg_hash in KNOWN_HASHES:
                queue.task_done()
                continue

            clean_text = strip_links(message.raw_text or message.text or "")
            if not clean_text and not message.media:
                queue.task_done()
                continue

            final_text = enhance_message(clean_text, detect_signal(clean_text))
            await rate_limiter.wait()

            if message.media:
                await client.send_file(TARGET_CHAT, message.media, caption=final_text)
            else:
                await client.send_message(TARGET_CHAT, final_text)

            save_hash(msg_hash)
            rate_limiter.normalize()
            queue.task_done()

        except FloodWaitError as e:
            rate_limiter.slow_down()
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Worker-{wid} error: {e}")
            queue.task_done()

# =========================
# TELEGRAM CLIENT
# =========================
async def start_client():
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    asyncio.create_task(runtime_guard(client))

    for i in range(WORKERS):
        asyncio.create_task(worker(client, i + 1))

    @client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        if event.message.date and event.message.date < BOOT_TIME:
            return

        text = event.message.raw_text or event.message.text or ""
        priority = 0 if detect_signal(text) else 1

        await queue.put((priority, event.message))

    logger.info("⚡ Elite Copier Running 24/7")
    await client.run_until_disconnected()

# =========================
# RENDER WEB WRAPPER
# =========================
app = Flask(__name__)

@app.route("/")
def health():
    return "Elite Copier Running"

if __name__ == "__main__":
    threading.Thread(
        target=lambda: asyncio.run(start_client()),
        daemon=True
    ).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
