import os
import re
import string
import random
import asyncio
import logging
import secrets
import urllib.parse
import aiohttp
from aiohttp import web

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bot")

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

WORKER_BASE_URL = os.environ.get("WORKER_BASE_URL", "https://my-worker.dev")
RENDER_APP_BASE_URL = os.environ.get("RENDER_APP_BASE_URL", "https://my-render-app.onrender.com")
PORT = int(os.environ.get("PORT", "8080"))

# Secret used both to build an unguessable webhook path AND as Telegram's
# secret_token header, so random POSTs to /webhook/* can't spoof updates.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", secrets.token_urlsafe(24))
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

DL_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dl.html")

URL_REGEX = re.compile(r"^https?://\S+$", re.IGNORECASE)

# In-memory state: user_id -> pending URL waiting for a filename
pending_urls = {}

_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def tg_call(method: str, payload: dict) -> dict:
    session = await get_session()
    async with session.post(
        f"{TG_API}/{method}", json=payload, timeout=aiohttp.ClientTimeout(total=20)
    ) as resp:
        data = await resp.json()
        if not data.get("ok"):
            logger.warning(f"[TG API] {method} failed: {data}")
        return data


async def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_call("sendMessage", payload)


async def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_call("editMessageText", payload)


def gen_short_id(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


async def set_webhook():
    webhook_url = f"{RENDER_APP_BASE_URL}{WEBHOOK_PATH}"
    result = await tg_call(
        "setWebhook",
        {
            "url": webhook_url,
            "secret_token": WEBHOOK_SECRET,
            "drop_pending_updates": True,
            "allowed_updates": ["message"],
        },
    )
    logger.info(f"[SET WEBHOOK] url={webhook_url} result={result}")

    info = await tg_call("getWebhookInfo", {})
    logger.info(f"[WEBHOOK INFO] {info}")


# ---------------- UPDATE HANDLING ----------------

async def handle_message(message: dict):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    from_user = message.get("from", {})
    user_id = from_user.get("id")
    text = (message.get("text") or "").strip()

    if chat_id is None or user_id is None:
        return

    logger.info(f"[MESSAGE] user_id={user_id} text={text!r}")

    if text.startswith("/start"):
        pending_urls.pop(user_id, None)
        await send_message(chat_id, "Send me a direct URL to begin.")
        return

    # Case 1: user is replying with a filename for a previously sent URL
    if user_id in pending_urls:
        url = pending_urls.pop(user_id)
        filename = text.strip()

        if not filename:
            pending_urls[user_id] = url
            await send_message(chat_id, "Filename can't be empty. Please enter a valid filename (with extension).")
            return

        status = await send_message(chat_id, "Registering your link, please wait...")
        status_message_id = status.get("result", {}).get("message_id")

        try:
            short_id = await register_link(url, filename)
        except Exception as e:
            if status_message_id:
                await edit_message(chat_id, status_message_id, f"Failed to register link: {e}")
            else:
                await send_message(chat_id, f"Failed to register link: {e}")
            return

        encoded_name = urllib.parse.quote_plus(filename)
        watch_url = f"{RENDER_APP_BASE_URL}/watch/{short_id}?name={encoded_name}"

        reply_markup = {
            "inline_keyboard": [[{"text": "▶️ Watch / Download", "url": watch_url}]]
        }

        final_text = f"Your link is ready!\n\n**Filename:** `{filename}`\n**Link:** {watch_url}"
        if status_message_id:
            await edit_message(chat_id, status_message_id, final_text, reply_markup)
        else:
            await send_message(chat_id, final_text, reply_markup)
        return

    # Case 2: user is sending a fresh URL
    if URL_REGEX.match(text):
        pending_urls[user_id] = text
        await send_message(chat_id, "Please enter the custom filename (with extension).")
        return

    # Case 3: not a URL, and no pending state
    await send_message(chat_id, "Please send a valid direct URL to begin.")


async def register_link(url: str, name: str) -> str:
    endpoint = f"{WORKER_BASE_URL}/api/add"
    payload = {"url": url, "name": name}

    session = await get_session()
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

async def webhook_handler(request: web.Request) -> web.Response:
    # Verify the secret token Telegram sends back, so only real Telegram
    # requests (matching what we set in setWebhook) are processed.
    incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if incoming_secret != WEBHOOK_SECRET:
        logger.warning("[WEBHOOK] Rejected request with bad/missing secret token")
        return web.Response(status=403, text="Forbidden")

    try:
        update = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad Request")

    logger.info(f"[UPDATE RECEIVED] {update.get('update_id')}")

    message = update.get("message")
    if message:
        try:
            await handle_message(message)
        except Exception as e:
            logger.exception(f"[HANDLE_MESSAGE ERROR] {e}")

    # Always 200 quickly, or Telegram will retry/backoff this update.
    return web.Response(status=200, text="OK")


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
    app.router.add_post(WEBHOOK_PATH, webhook_handler)
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


async def keepalive_loop():
    await asyncio.sleep(15)
    url = f"{RENDER_APP_BASE_URL}/health"
    session = await get_session()
    while True:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                logger.info(f"[KEEPALIVE] pinged {url} -> {resp.status}")
        except Exception as e:
            logger.warning(f"[KEEPALIVE] ping failed: {e}")
        await asyncio.sleep(600)


async def main():
    await run_web_server()
    await set_webhook()
    logger.info("Webhook set. Waiting for updates...")
    asyncio.create_task(keepalive_loop())
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
