import os
import asyncio
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# =========================
# SAFE ENV LOADING
# =========================
def get_env(name, required=True):
    value = os.getenv(name)
    if required and not value:
        raise RuntimeError(f"❌ Missing environment variable: {name}")
    return value


try:
    API_ID = int(get_env("API_ID"))
    API_HASH = get_env("API_HASH")
    SESSION_STRING = get_env("SESSION_STRING")

except Exception as e:
    print(f"⚠ ENV ERROR → {e}")
    raise SystemExit(1)


# =========================
# CLIENT
# =========================
client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)


# =========================
# TEST LOGIC
# =========================
async def main():

    print("🔄 Connecting to Telegram...")

    try:
        await client.connect()

        if not await client.is_user_authorized():
            print("❌ Session not authorized")
            return

        me = await client.get_me()

        print("✅ CONNECTION SUCCESS")
        print(f"User ID: {me.id}")
        print(f"Username: @{me.username}" if me.username else "No username")
        print(f"Name: {me.first_name}")

    except errors.ApiIdInvalidError:
        print("❌ Invalid API_ID / API_HASH")

    except errors.AuthKeyError:
        print("❌ Invalid / corrupted SESSION_STRING")

    except errors.FloodWaitError as e:
        print(f"⏳ FloodWait → wait {e.seconds}s")

    except Exception as e:
        print(f"⚠ Unexpected error → {type(e).__name__}: {e}")

    finally:
        await client.disconnect()
        print("🔌 Disconnected")


# =========================
# ENTRY
# =========================
if __name__ == "__main__":
    asyncio.run(main())
