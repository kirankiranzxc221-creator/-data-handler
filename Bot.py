import os
import logging
import asyncio
from pyrogram import Client, filters, idle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("diag")

API_ID = 9649038
API_HASH = "a5e111e536a6f95aec711676e43a0666"
BOT_TOKEN = "8296387630:AAHhzp_M0VahMZusJ8WswfBRPVAy8UJ8N-E"

app = Client(
    "diag_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)


@app.on_raw_update()
async def raw(client, update, users, chats):
    logger.info(f"[RAW] {type(update).__name__} -> {update}")


@app.on_message(filters.all)
async def any_message(client, message):
    logger.info(f"[MESSAGE] from={message.from_user} text={message.text}")


async def main():
    await app.start()
    logger.info("Diagnostic client running. Send /start now.")
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
