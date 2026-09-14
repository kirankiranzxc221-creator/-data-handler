import os, asyncio, logging
from aiohttp import web
from pyrogram import Client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("SnapDeploy-Stream")

# ============================================================
# 1. HARDCODED CREDENTIALS
# ============================================================
API_ID = 9649038
API_HASH = "a5e111e536a6f95aec711676e43a0666"
BOT_TOKEN = "8808144589:AAEJG4px0Eaya1icF7W7iV6mlwCLwxT2vqY"
CHANNEL_ID = -1003649271176

CHUNK_SIZE = 1024 * 1024  # 1MB Buffer for max speed

# SnapDeploy தானாகவே போர்ட்டை அசைன் செய்யும், இல்லையென்றால் 8080 எடுக்கும்
PORT = int(os.environ.get("PORT", 8080))

# ============================================================
# 2. DUMMY BOT INITIALIZATION
# ============================================================
bot = Client("dummy_stream_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

# ============================================================
# 3. DIRECT TELEGRAM STREAMING LOGIC
# ============================================================
async def stream_handler(request):
    message_id = int(request.match_info["message_id"])
    
    try:
        message = await bot.get_messages(CHANNEL_ID, message_id)
    except Exception as e:
        raise web.HTTPNotFound(text=f"Error fetching message: {str(e)}")

    if not message or message.empty:
        raise web.HTTPNotFound(text="Message not found in Telegram Channel")
        
    media = message.video or message.document or message.audio or message.animation
    if not media:
        raise web.HTTPNotFound(text="No media found in this message")
        
    file_size = getattr(media, "file_size", 0)
    file_name = getattr(media, "file_name", f"video_{message_id}.mp4")
    mime_type = getattr(media, "mime_type", "video/mp4")

    # Range logic for VLC/MX Player Seeking
    range_header = request.headers.get("Range")
    start, end = 0, file_size - 1
    if range_header:
        range_val = range_header.replace("bytes=", "").split("-")
        start = int(range_val[0]) if range_val[0] else 0
        end = int(range_val[1]) if len(range_val) > 1 and range_val[1] else file_size - 1

    content_length = (end - start) + 1
    status = 206 if range_header else 200
    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(content_length),
        "Content-Disposition": f'inline; filename="{file_name}"',
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)

    offset = start // CHUNK_SIZE
    first_chunk_cut = start % CHUNK_SIZE
    remaining = content_length
    first = True

    try:
        async for chunk in bot.stream_media(message, offset=offset):
            if not chunk: continue
            if first:
                chunk = chunk[first_chunk_cut:]
                first = False
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            if chunk:
                await response.write(chunk)
                remaining -= len(chunk)
            if remaining <= 0:
                break
    except (ConnectionResetError, asyncio.CancelledError):
        log.info(f"Client disconnected - message {message_id}")
    except Exception as e:
        log.error(f"Stream error: {e}")
    finally:
        try:
            await response.write_eof()
        except:
            pass
            
    return response

async def root_handler(request):
    return web.json_response({
        "server": "SnapDeploy Direct Streamer",
        "status": "Online",
        "port": PORT
    })

# ============================================================
# 4. APP & SERVER SETUP
# ============================================================
async def init_app():
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get(r"/stream/{message_id:\d+}", stream_handler)
    app.router.add_get(r"/stream/{message_id:\d+}/{tail:.*}", stream_handler)
    app.router.add_head(r"/stream/{message_id:\d+}", stream_handler)
    app.router.add_head(r"/stream/{message_id:\d+}/{tail:.*}", stream_handler)
    return app

async def main():
    print("🤖 Starting Pyrogram Dummy Bot...")
    await bot.start()
    
    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    
    # SnapDeploy-ல் 0.0.0.0-ல் தான் சர்வர் ரன் ஆக வேண்டும்
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"✅ aiohttp Streaming Server running on port {PORT}")
    
    # Keep the server running forever
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
