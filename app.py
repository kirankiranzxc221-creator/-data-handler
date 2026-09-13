import os
import logging
import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("snapdeploy")

UPSTREAM_BASE_URL = "https://trm-team-file-to-link.onrender.com"
PORT = int(os.environ.get("PORT", 8080))

# Headers we should not blindly forward in either direction.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def stream_handler(request: web.Request) -> web.StreamResponse:
    """
    GET /stream/{message_id}
    Forwards the request (with Range header intact) to the Render backend
    and relays the response back to the Cloudflare Worker chunk-for-chunk.
    """
    message_id = request.match_info["message_id"]
    upstream_url = f"{UPSTREAM_BASE_URL}/stream/{message_id}"

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }

    session = aiohttp.ClientSession()
    try:
        upstream_resp = await session.get(
            upstream_url, headers=forward_headers, timeout=aiohttp.ClientTimeout(total=None)
        )
    except aiohttp.ClientError as e:
        await session.close()
        log.error("Failed to reach upstream for message %s: %s", message_id, e)
        return web.json_response({"error": "upstream unreachable"}, status=502)

    response_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in HOP_BY_HOP
    }

    response = web.StreamResponse(
        status=upstream_resp.status,
        headers=response_headers,
    )
    await response.prepare(request)

    try:
        async for chunk in upstream_resp.content.iter_chunked(64 * 1024):
            await response.write(chunk)
    except (ConnectionResetError, aiohttp.ClientError):
        log.info("Client or upstream disconnected mid-stream for message %s", message_id)
    finally:
        upstream_resp.close()
        await session.close()

    await response.write_eof()
    return response


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/stream/{message_id}", stream_handler)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)

