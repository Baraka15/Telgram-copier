import os
import re
import asyncio
import logging
import hashlib
import signal
import random
import requests
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
GH_TOKEN = os.getenv("GH_TOKEN")

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

MAX_RUNTIME = random.uniform(5.5, 5.7) * 60 * 60
STARTUP_GRACE = 15

STATE_FILE = "forwarded_hashes.txt"
MAX_HASHES = 5000

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
shutdown_event = asyncio.Event()

# =========================
# GITHUB RESTART
# =========================
def trigger_github_restart():
    repo = os.getenv("GITHUB_REPOSITORY")

    if not repo or not GH_TOKEN:
        logger.error("❌ Restart aborted: Missing repo or GH_TOKEN")
        return False

    owner, name = repo.split("/")
    url = f"https://api.github.com/repos/{owner}/{name}/dispatches"

    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    payload = {"event_type": "restart_bot"}

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)

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
    if not os.path.exists(STATE_FILE):
        return []
    with open(STATE_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]

hashes = load_hashes()
KNOWN_HASHES = set(hashes)

def persist_hashes():
    with open(STATE_FILE, "w") as f:
        for h in hashes[-MAX_HASHES:]:
            f.write(h + "\n")

def save_hash(msg_hash):
    hashes.append(msg_hash)

    if len(hashes) > MAX_HASHES:
        logger.warning("♻ Rotating hash file")
        persist_hashes()
        del hashes[:-MAX_HASHES]
    else:
        with open(STATE_FILE, "a") as f:
            f.write(msg_hash + "\n")

# =========================
# UTILITIES
# =========================
LINK_PATTERNS = [r"http[s]?://\S+", r"www\.\S+", r"t\.me/\S+"]

def strip_links(text):
    for pattern in LINK_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip()

def build_hash(message):
    text = strip_links(message.raw_text or "")
    normalized = re.sub(r"\s+", " ", text).strip().upper()
    return hashlib.sha256(normalized.encode()).hexdigest()

def detect_signal(text):
    t = (text or "").upper()
    return "BUY" in t or "SELL" in t

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
        logger.warning(f"🐢 Slowdown → {self.delay:.2f}s")

    def normalize(self):
        self.delay = max(self.base_delay, self.delay * 0.9)

rate_limiter = AdaptiveRateLimiter(RATE_DELAY)
queue = asyncio.PriorityQueue(maxsize=QUEUE_SIZE)

# =========================
# RUNTIME GUARD
# =========================
async def runtime_guard(client):
    start = asyncio.get_running_loop().time()

    while not shutdown_event.is_set():
        await asyncio.sleep(60)
        elapsed = asyncio.get_running_loop().time() - start

        if elapsed > MAX_RUNTIME:
            logger.warning("⏳ Restart cycle triggered")

            restart_ok = trigger_github_restart()

            try:
                await client.send_message(
                    ADMIN_CHAT,
                    "🔄 Restarting before GitHub timeout…"
                )
            except:
                pass

            shutdown_event.set()
            await client.disconnect()

            await asyncio.sleep(2)

            os._exit(0)

# =========================
# WORKER
# =========================
async def worker(client, wid):
    logger.info(f"🚀 Worker-{wid} started")

    while not shutdown_event.is_set():
        try:
            priority, message = await queue.get()

            if message.date and message.date < BOOT_TIME - timedelta(seconds=10):
                queue.task_done()
                continue

            clean_text = strip_links(message.raw_text or "")
            if len(clean_text) < 3:
                queue.task_done()
                continue

            msg_hash = build_hash(message)

            if msg_hash in KNOWN_HASHES:
                queue.task_done()
                continue

            final_text = enhance_message(clean_text, detect_signal(clean_text))

            await rate_limiter.wait()

            if message.media:
                await client.send_file(TARGET_CHAT, message.media, caption=final_text)
            else:
                await client.send_message(TARGET_CHAT, final_text)

            save_hash(msg_hash)
            KNOWN_HASHES.add(msg_hash)
            rate_limiter.normalize()

            queue.task_done()

        except FloodWaitError as e:
            rate_limiter.slow_down()
            await asyncio.sleep(e.seconds + 5)

        except Exception as e:
            logger.error(f"Worker-{wid} error: {e}")

# =========================
# CLIENT
# =========================
async def start_client():
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    asyncio.create_task(runtime_guard(client))

    await asyncio.sleep(STARTUP_GRACE)

    for i in range(WORKERS):
        asyncio.create_task(worker(client, i + 1))

    @client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        try:
            score = 0 if detect_signal(event.message.raw_text) else 1
            queue.put_nowait((score, event.message))
        except asyncio.QueueFull:
            rate_limiter.slow_down()

    logger.info("⚡ Elite Copier ACTIVE")
    await client.run_until_disconnected()

# =========================
# ENTRY
# =========================
if __name__ == "__main__":
    asyncio.run(start_client())
