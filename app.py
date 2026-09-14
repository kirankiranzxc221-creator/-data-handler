import os
import logging
import traceback
import asyncio
from aiohttp import web
import aiohttp
from multidict import CIMultiDict

UPSTREAM_BASE_URL = "https://trm-team-file-to-link.onrender.com"
PORT = int(os.environ.get("PORT", 5000))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("snapdeploy")

# True hop-by-hop headers மட்டும் (RFC 7230 §6.1) — இவை மட்டும் forward பண்ணக்கூடாது.
# Content-Length, Content-Range, Accept-Ranges ஆகியவை end-to-end headers,
# இவற்றை எப்போதும் preserve பண்ணனும்.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}

_session = None


async def get_session():
    global _session
    if _session is None or _session.closed:
        # --- Speed optimizations ---
        # limit=0: connection count-ஐ artificially restrict பண்ணாது.
        # limit_per_host: ஒரே upstream host-க்கு பல connections parallel-ஆ வைச்சு reuse
        #          பண்ண அனுமதிக்கும் — ஒவ்வொரு request-க்கும் புது TCP/TLS handshake
        #          பண்ணாம connection pool-ல் இருந்து reuse ஆகும்.
        # keepalive_timeout: idle connections-ஐ pool-ல் அதிக நேரம் வச்சிருக்கும்.
        # ttl_dns_cache: DNS lookup-ஐ ஒவ்வொரு request-க்கும் மறுபடி பண்ணாம cache பண்ணும்.
        connector = aiohttp.TCPConnector(
            limit=0,
            limit_per_host=32,
            keepalive_timeout=75,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        # auto_decompress=False: இல்லேன்னா aiohttp client தானாக gzip/br decompress
        # பண்ணிடும், ஆனா நாம upstream-ல் இருந்து வந்த Content-Encoding/Content-Length
        # headers-ஐ அப்படியே forward பண்றோம் — decompressed body + compressed headers
        # mismatch ஆகி client-side-ல் broken response-க்கு வழிவகுக்கும்.
        _session = aiohttp.ClientSession(
            auto_decompress=False,
            connector=connector,
            read_bufsize=2 ** 20,  # 1 MB
        )
    return _session


async def on_cleanup(app):
    if _session is not None and not _session.closed:
        await _session.close()


async def health(request):
    return web.json_response({"status": "ok"})


async def proxy_handler(request):
    """
    Catch-all reverse proxy.
    /watch/..., /<id>/<filename>?hash=..., /stream/{message_id} — எல்லாமே
    இதன் மூலமா Render backend-க்கு forward ஆகும், response chunk-by-chunk
    திரும்ப அனுப்பப்படும்.

    இந்த handler-ல் எந்த ஒரு exception-ஆ இருந்தாலும் வெளியே leak ஆகாது.
    """
    response = None
    upstream_resp = None
    try:
        upstream_url = f"{UPSTREAM_BASE_URL}{request.rel_url}"

        # Range header (எ.கா. "bytes=1048576-") இங்க தானா forward ஆகுது — இது
        # HOP_BY_HOP செட்-ல் இல்லாததால் கீழே உள்ள dict comprehension-ல் தானே
        # உள்ளடங்கும். Video player seek பண்ணும்போது இந்த header தான் அனுப்பப்படும்,
        # upstream அதுக்கு 206 Partial Content + Content-Range header-ஓட பதில்
        # கொடுக்கும், அதுவும் கீழே response_headers-ல் அப்படியே preserve ஆகும்.
        forward_headers = CIMultiDict(
            (k, v) for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP
        )

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
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120),
                allow_redirects=False,
            )
        except asyncio.TimeoutError:
            log.error("Upstream timed out for %s", request.rel_url)
            return web.json_response({"error": "upstream timeout"}, status=504)
        except aiohttp.ClientError as e:
            log.error("Failed to reach upstream for %s: %s", request.rel_url, e)
            return web.json_response({"error": "upstream unreachable"}, status=502)

        # CIMultiDict ஆல் duplicate headers (எ.கா. பல Set-Cookie) இழக்காம பாதுகாக்கப்படும்.
        # Content-Range, Accept-Ranges, Content-Length ஆகியவை HOP_BY_HOP-ல் இல்லாததால்
        # இங்கயும் அப்படியே client-க்கு போகும் — seeking/206 சரியா வேலை செய்யும்.
        response_headers = CIMultiDict(
            (k, v) for k, v in upstream_resp.headers.items()
            if k.lower() not in HOP_BY_HOP
        )

        response = web.StreamResponse(
            status=upstream_resp.status,
            headers=response_headers,
        )

        if "content-length" not in response_headers:
            response.enable_chunked_encoding()

        await response.prepare(request)

        # --- True pass-through streaming ---
        # iter_any() network-ல் இருந்து எந்த அளவு bytes கிடைக்குதோ அதையே உடனடியா
        # yield பண்ணும், எந்த buffering/re-chunking/fixed-size காத்திருப்பும் இல்லை.
        # Render எந்த chunk size-ல் அனுப்புதோ, அதே boundaries-ல் client-க்கு போகும்.
        try:
            async for chunk in upstream_resp.content.iter_any():
                await response.write(chunk)
        except (ConnectionResetError, aiohttp.ClientError):
            log.info("Client or upstream disconnected mid-stream for %s", request.rel_url)

        await response.write_eof()
        return response

    except Exception:
        log.error("Unhandled error in proxy_handler for %s:\n%s",
                   request.rel_url, traceback.format_exc())
        if response is not None and response.prepared:
            try:
                await response.write_eof()
            except Exception:
                pass
            return response
        return web.json_response({"error": "internal proxy error"}, status=500)
    finally:
        if upstream_resp is not None:
            upstream_resp.close()


def create_app():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_route("*", "/{path:.*}", proxy_handler)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
