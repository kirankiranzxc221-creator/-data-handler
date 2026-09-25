import os
import re
import string
import random
import asyncio
import urllib.parse
import aiohttp
import logging
import traceback
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

# --- SPY MODE: LOGGING SETUP ---
# உள்ளே நடக்கும் ஒவ்வொரு அசைவையும் Render லாக்கில் பிரிண்ட் செய்ய
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [SPY] - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
# -------------------------------

# ---------------- CONFIG ----------------
API_ID = 9649038  # உங்கள் உண்மையான நம்பரை கொடுக்கவும்
API_HASH = "a5e111e536a6f95aec711676e43a0666"
BOT_TOKEN = "8296387630:AAHGr814iUrTBk_CgKWoGyp8IKQX3cAa1Ew"

WORKER_BASE_URL = os.environ.get("WORKER_BASE_URL", "https://v.trmteam1.workers.dev")
RENDER_APP_BASE_URL = os.environ.get("RENDER_APP_BASE_URL", "https://link-to-link.onrender.com")
PORT = int(os.environ.get("PORT", "8080"))

DL_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dl.html")
URL_REGEX = re.compile(r"^https?://\S+$", re.IGNORECASE)

pending_urls = {}

bot = Client(
    "bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

def gen_short_id(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))

@bot.on_message(filters.text & filters.private)
async def handle_text(client: Client, message: Message):
    text = message.text.strip()
    user_id = message.from_user.id
    
    # ஸ்பை சென்சார் 1: மெசேஜ் உள்ளே வருகிறதா?
    logger.info(f"புதிய மெசேஜ் வந்தது! User ID: {user_id} | Text: {text}")

    try:
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
                logger.error(f"Worker-ல் லிங்கை ரெஜிஸ்டர் செய்வதில் எரர்: {e}")
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

        if URL_REGEX.match(text):
            pending_urls[user_id] = text
            await message.reply_text("Please enter the custom filename (with extension).")
            return

        await message.reply_text("Please send a valid direct URL to begin.")
        
    except Exception as e:
        # ஸ்பை சென்சார் 2: மெசேஜ் ப்ராசஸ் ஆகும்போது ஏதாவது எரர் வருகிறதா?
        logger.error(f"மெசேஜை ப்ராசஸ் செய்யும்போது எதிர்பாராத எரர்: {e}")
        traceback.print_exc()


async def register_link(url: str, name: str) -> str:
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
    logger.info(f"Web server வெற்றிகரமாக போர்ட் {PORT}-ல் ஓடுகிறது.")

async def main():
    logger.info("சிஸ்டம் ஸ்டார்ட் ஆகிறது...")
    try:
        # 1. வெப் சர்வரை ஸ்டார்ட் செய்
        await run_web_server()
        
        # 2. பாட்டை ஸ்டார்ட் செய்
        logger.info("பாட்டை டெலிகிராம் சர்வருடன் இணைக்க முயற்சிக்கிறது...")
        await bot.start()
        logger.info("பாட் 100% சக்சஸ்ஃபுல்லா ஆன்லைனுக்கு வந்துடுச்சு! மெசேஜ்க்காக காத்திருக்கிறது...")
        
        # 3. பாட் ஆஃப் ஆகாமல் விழித்திருக்க
        await idle()
        
    except Exception as e:
        # ஸ்பை சென்சார் 3: சர்வர் ஸ்டார்ட் ஆகும்போது எரர் வந்தால் காட்ட
        logger.error(f"கிரிட்டிக்கல் எரர்! சிஸ்டம் ஸ்டார்ட் ஆகவில்லை: {e}")
        traceback.print_exc()
    finally:
        await bot.stop()
        logger.info("சிஸ்டம் நிறுத்தப்பட்டது.")

if __name__ == "__main__":
    asyncio.run(main())
