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


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """
    Catch-all reverse proxy.
    Forwards ANY path + query string (e.g. /watch/..., /<id>/<filename>?hash=...,
    /stream/{message_id}) to the Render backend, preserving method, headers
    (including Range), and query params, then relays the response back
    chunk-by-chunk exactly as received.
    """
    # request.rel_url includes the path plus the original query string.
    upstream_url = f"{UPSTREAM_BASE_URL}{request.rel_url}"

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }

    body = await request.read() if request.can_read_body else None

    session = aiohttp.ClientSession()
    try:
        upstream_resp = await session.request(
            request.method,
            upstream_url,
            headers=forward_headers,
            data=body,
            timeout=aiohttp.ClientTimeout(total=None),
            allow_redirects=False,
        )
    except aiohttp.ClientError as e:
        await session.close()
        log.error("Failed to reach upstream for %s: %s", request.rel_url, e)
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
        log.info("Client or upstream disconnected mid-stream for %s", request.rel_url)
    finally:
        upstream_resp.close()
        await session.close()

    await response.write_eof()
    return response


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    # Catch-all: any method, any path, forwarded verbatim to upstream.
    app.router.add_route("*", "/{path:.*}", proxy_handler)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
