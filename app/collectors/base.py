import asyncio
import calendar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
import re
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
import feedparser
import httpx
from app.core.config import get_settings
from app.services.urls import normalize_url


class CollectorError(Exception):
    pass


@dataclass
class Article:
    guid: str
    url: str
    title: str
    excerpt: str = ""
    language: str = "en"
    published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tags: list[str] = field(default_factory=list)


@dataclass
class FeedResult:
    articles: list[Article]
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


def plain_text(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


class RSSAdapter:
    key = ""
    allowed_hosts: set[str] = set()
    default_language = "en"

    def __init__(self, client: httpx.AsyncClient, settings=None):
        self.client = client
        self.settings = settings or get_settings()
        self._last_request = 0.0

    async def request(self, url, headers=None, robots_policy=None):
        for attempt in range(3):
            delay = self.settings.request_interval_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()
            try:
                # Follow redirects explicitly and only to the same publisher.
                target = url
                for _ in range(5):
                    from app.services.article_html import safe_url

                    if not safe_url(target):
                        raise CollectorError("Unsafe publisher URL")
                    if robots_policy and not robots_policy.can_fetch(self.settings.user_agent, target):
                        raise CollectorError("robots.txt disallows redirect destination")
                    if urlsplit(target).hostname not in self.allowed_hosts:
                        raise CollectorError("Redirect outside publisher allowlist")
                    async with self.client.stream(
                        "GET",
                        target,
                        headers=headers,
                        timeout=self.settings.http_timeout,
                        follow_redirects=False,
                    ) as response:
                        if response.has_redirect_location:
                            target = str(response.url.join(response.headers["location"]))
                            continue
                        if response.status_code in (429, 500, 502, 503, 504):
                            response.raise_for_status()
                        if response.status_code != 304:
                            response.raise_for_status()
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 4_000_000:
                                raise CollectorError("Document exceeds 4 MB limit")
                            chunks.append(chunk)
                        return httpx.Response(
                            response.status_code,
                            headers={
                                k: v
                                for k, v in response.headers.items()
                                if k.lower() not in ("content-encoding", "content-length")
                            },
                            content=b"".join(chunks),
                            request=response.request,
                        )
                raise CollectorError("Too many redirects")
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (
                    429,
                    500,
                    502,
                    503,
                    504,
                ):
                    raise CollectorError(f"HTTP {exc.response.status_code}; access was not bypassed") from exc
                if attempt == 2:
                    raise CollectorError(str(exc)) from exc
                retry_after = (
                    exc.response.headers.get("Retry-After", "")
                    if isinstance(exc, httpx.HTTPStatusError)
                    else ""
                )
                await asyncio.sleep(min(60, int(retry_after)) if retry_after.isdigit() else 2**attempt)
        raise CollectorError("Request failed")

    async def fetch(self, source):
        parts = urlsplit(source.feed_url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        policy = None
        try:
            robots = await self.request(robots_url)
        except CollectorError as exc:
            # 404 means no robots policy; other failures are not permission.
            if "HTTP 404;" not in str(exc):
                raise
        else:
            policy = RobotFileParser()
            policy.parse(robots.text.splitlines())
            if not policy.can_fetch(self.settings.user_agent, source.feed_url):
                raise CollectorError("robots.txt disallows this RSS endpoint")
        headers = {}
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified
        r = await self.request(source.feed_url, headers, robots_policy=policy)
        if r.status_code == 304:
            return FeedResult([], source.etag, source.last_modified, True)
        parsed = feedparser.parse(r.content)
        if not parsed.version or (parsed.bozo and not parsed.entries):
            raise CollectorError("Invalid RSS/Atom feed")
        entries = []
        for item in parsed.entries[:100]:
            title = plain_text(item.get("title", ""))
            try:
                url = normalize_url(item.get("link", ""))
            except ValueError:
                continue
            if not title or urlsplit(url).hostname not in self.allowed_hosts:
                continue
            date = item.get("published_parsed") or item.get("updated_parsed")
            published = (
                datetime.fromtimestamp(calendar.timegm(date), timezone.utc)
                if date
                else datetime.now(timezone.utc)
            )
            entries.append(
                Article(
                    str(item.get("id") or url)[:2048],
                    url,
                    title,
                    plain_text(item.get("summary", ""))[:1600],
                    parsed.feed.get("language", self.default_language).split("-")[0].lower(),
                    published,
                    [plain_text(t.get("term", "")) for t in item.get("tags", [])],
                )
            )
        return FeedResult(entries, r.headers.get("etag"), r.headers.get("last-modified"))
