import os
import logging
import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("snapdeploy")

UPSTREAM_BASE_URL = "https://trm-team-file-to-link.onrender.com"
PORT = int(os.environ.get("PORT", 8080))

# True hop-by-hop headers only (RFC 7230 §6.1) — these must NOT be forwarded
# in either direction. Content-Length, Content-Range, and Accept-Ranges are
# end-to-end headers and must always be preserved.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    """Reuse one ClientSession (with connection pooling) across all requests
    instead of opening a fresh TCP/TLS connection to upstream every time."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def on_cleanup(app: web.Application):
    if _session is not None and not _session.closed:
        await _session.close()


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

    # Only bodies for methods that actually carry one — avoids any chance
    # of blocking on a body read for GET/HEAD, which is what was making
    # HTML pages (served via GET) hang.
    body = None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        body = await request.read()

    session = await get_session()
    try:
        upstream_resp = await session.request(
            request.method,
            upstream_url,
            headers=forward_headers,
            data=body,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
            allow_redirects=False,
        )
    except aiohttp.ClientError as e:
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

    # If upstream didn't give us a Content-Length (e.g. it's itself streaming
    # chunked HTML), tell aiohttp explicitly to use chunked encoding so the
    # response has a defined end instead of hanging open.
    if "content-length" not in response_headers:
        response.enable_chunked_encoding()

    await response.prepare(request)

    try:
        async for chunk in upstream_resp.content.iter_chunked(64 * 1024):
            await response.write(chunk)
    except (ConnectionResetError, aiohttp.ClientError):
        log.info("Client or upstream disconnected mid-stream for %s", request.rel_url)
    finally:
        upstream_resp.close()

    await response.write_eof()
    return response


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    # Catch-all: any method, any path, forwarded verbatim to upstream.
    app.router.add_route("*", "/{path:.*}", proxy_handler)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
