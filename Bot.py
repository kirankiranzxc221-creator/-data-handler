import os
import re
import string
import random
import asyncio
import logging
import urllib.parse
import aiohttp
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.raw.base import Update

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bot")

# ---------------- CONFIG ----------------
API_ID = 9649038
API_HASH = "a5e111e536a6f95aec711676e43a0666"
BOT_TOKEN = "8296387630:AAHhzp_M0VahMZusJ8WswfBRPVAy8UJ8N-E"

WORKER_BASE_URL = os.environ.get("WORKER_BASE_URL", "https://my-worker.dev")
RENDER_APP_BASE_URL = os.environ.get("RENDER_APP_BASE_URL", "https://my-render-app.onrender.com")
PORT = int(os.environ.get("PORT", "8080"))

DL_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dl.html")

URL_REGEX = re.compile(r"^https?://\S+$", re.IGNORECASE)

# In-memory state: user_id -> pending URL waiting for a filename
pending_urls = {}

bot = Client(
    "bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    workers=4,
    sleep_threshold=60,
)


def gen_short_id(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


# ---------------- DIAGNOSTIC: RAW UPDATE LOGGER ----------------
# This fires on EVERY update Pyrogram receives at the transport layer,
# before any filters are applied. If this never logs anything when you
# send /start, the problem is network/transport (Render), not your
# handler filters. If it DOES log but handle_text below never fires,
# the problem is in your filters.
@bot.on_raw_update()
async def raw_update_logger(client: Client, update, users, chats):
    logger.info(f"[RAW UPDATE RECEIVED] type={type(update).__name__} raw={update}")


@bot.on_message(filters.private & (filters.text | filters.command(["start"])))
async def handle_text(client: Client, message: Message):
    logger.info(f"[HANDLE_TEXT TRIGGERED] from_user={message.from_user.id if message.from_user else 'unknown'} text={message.text!r}")

    text = (message.text or "").strip()
    user_id = message.from_user.id

    if text.startswith("/start"):
        pending_urls.pop(user_id, None)
        await message.reply_text("Send me a direct URL to begin.")
        return

    # Case 1: user is replying with a filename for a previously sent URL
    if user_id in pending_urls:
        url = pending_urls.pop(user_id)
        filename = text.strip()

        if not filename:
            pending_urls[user_id] = url
            await message.reply_text("Filename can't be empty. Please enter a valid filename (with extension).")
            return

        status_msg = await message.reply_text("Registering your link, please wait...")

        try:
            short_id = await register_link(url, filename)
        except Exception as e:
            await status_msg.edit_text(f"Failed to register link: {e}")
            return

        encoded_name = urllib.parse.quote_plus(filename)
        watch_url = f"{RENDER_APP_BASE_URL}/watch/{short_id}?name={encoded_name}"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("▶️ Watch / Download", url=watch_url)]]
        )

        await status_msg.edit_text(
            f"Your link is ready!\n\n**Filename:** `{filename}`\n**Link:** {watch_url}",
            reply_markup=keyboard,
        )
        return

    # Case 2: user is sending a fresh URL
    if URL_REGEX.match(text):
        pending_urls[user_id] = text
        await message.reply_text("Please enter the custom filename (with extension).")
        return

    # Case 3: not a URL, and no pending state
    await message.reply_text("Please send a valid direct URL to begin.")


async def register_link(url: str, name: str) -> str:
    """POST to the Cloudflare Worker /api/add endpoint and return the short ID."""
    endpoint = f"{WORKER_BASE_URL}/api/add"
    payload = {"url": url, "name": name}

    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Worker returned {resp.status}: {body}")
            data = await resp.json()
            short_id = data.get("id")
            if not short_id:
                raise RuntimeError(f"Worker response missing 'id': {data}")
            return short_id


# ---------------- WEB SERVER ----------------

async def watch_handler(request: web.Request) -> web.Response:
    short_id = request.match_info.get("id", "")

    if not short_id:
        return web.Response(status=400, text="Missing ID")

    raw_name = request.query.get("name", "Video.mp4")
    filename = urllib.parse.unquote_plus(raw_name)

    try:
        with open(DL_HTML_PATH, "r", encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        return web.Response(status=500, text="dl.html template not found on server")

    stream_url = f"{WORKER_BASE_URL}/stream/{short_id}"

    try:
        rendered = template % (filename, filename, stream_url, stream_url, "Download")
    except TypeError as e:
        return web.Response(status=500, text=f"Template formatting error: {e}")

    return web.Response(text=rendered, content_type="text/html")


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK")


def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/watch/{id}", watch_handler)
    app.router.add_get("/health", health_handler)
    return app


async def run_web_server():
    app = build_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server running on port {PORT}")


# ---------------- KEEPALIVE (defeats Render free-tier spin-down) ----------------
# Render's free Web Services suspend the whole process, including this
# background Pyrogram socket, after ~15 minutes with no inbound HTTP
# request. If Telegram sends a message while suspended, it is lost —
# there is no delivery queue to replay it on wake. This pings our own
# /health endpoint every 10 minutes to keep the dyno awake. If you're
# on a paid/always-on plan this is harmless but unnecessary.
async def keepalive_loop():
    await asyncio.sleep(15)  # let the web server bind first
    url = f"{RENDER_APP_BASE_URL}/health"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    logger.info(f"[KEEPALIVE] pinged {url} -> {resp.status}")
            except Exception as e:
                logger.warning(f"[KEEPALIVE] ping failed: {e}")
            await asyncio.sleep(600)  # every 10 minutes


async def main():
    await run_web_server()
    await bot.start()
    logger.info("Bot started. Waiting for updates...")
    asyncio.create_task(keepalive_loop())
    await idle()
    await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
```
