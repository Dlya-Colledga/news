from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from app.collectors.anime_corner import AnimeCornerAdapter
from app.collectors.base import CollectorError
from app.core.config import Settings

FEED = (Path(__file__).parent / "fixtures/feed.xml").read_bytes()
SOURCE = SimpleNamespace(feed_url="https://animecorner.me/feed/", etag=None, last_modified=None)
SETTINGS = Settings(request_interval_seconds=0, _env_file=None)


async def test_valid_feed_and_conditionals():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(
            200, content=FEED, headers={"ETag": '"v1"', "Last-Modified": "Tue, 13 Oct 2026 10:00:00 GMT"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AnimeCornerAdapter(client, SETTINGS).fetch(SOURCE)
    assert len(result.articles) == 2
    assert result.etag == '"v1"'
    assert result.articles[0].url == "https://animecorner.me/fixture-film/"
    assert "<script>" not in result.articles[0].excerpt

    def unchanged(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        assert req.headers["If-None-Match"] == '"v1"'
        assert "If-Modified-Since" in req.headers
        return httpx.Response(304)

    source = SimpleNamespace(feed_url=SOURCE.feed_url, etag=result.etag, last_modified=result.last_modified)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unchanged)) as client:
        assert (await AnimeCornerAdapter(client, SETTINGS).fetch(source)).not_modified


@pytest.mark.parametrize(
    "status,body", [(200, b"<html>captcha</html>"), (403, b"Forbidden"), (200, b"<rss>broken")]
)
async def test_invalid_and_forbidden(status, body):
    def handler(req):
        return (
            httpx.Response(200, text="User-agent: *\nAllow: /")
            if req.url.path == "/robots.txt"
            else httpx.Response(status, content=body)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CollectorError):
            await AnimeCornerAdapter(client, SETTINGS).fetch(SOURCE)


async def test_timeout_retry(monkeypatch):
    import app.collectors.base as module

    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    calls = []

    def fail(req):
        calls.append(req.url)
        raise httpx.ReadTimeout("timed out", request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(CollectorError):
            await AnimeCornerAdapter(client, SETTINGS).fetch(SOURCE)
    assert len(calls) == 3 and delays == [1, 2]


async def test_robots_and_external_redirect():
    for response in [
        httpx.Response(200, text="User-agent: *\nDisallow: /"),
        httpx.Response(302, headers={"location": "http://127.0.0.1/private"}),
    ]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response)) as client:
            with pytest.raises(CollectorError):
                await AnimeCornerAdapter(client, SETTINGS).fetch(SOURCE)


async def test_compressed_response_is_decoded_once():
    import gzip

    def handler(req):
        body = b"User-agent: *\nAllow: /" if req.url.path == "/robots.txt" else FEED
        return httpx.Response(200, content=gzip.compress(body), headers={"Content-Encoding": "gzip"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AnimeCornerAdapter(client, SETTINGS).fetch(SOURCE)
    assert len(result.articles) == 2
