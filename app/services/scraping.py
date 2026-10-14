"""Publisher adapters plus generic DOM, JSON-LD and trafilatura extraction."""

from dataclasses import dataclass, field
from html import escape
import json
import re
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import trafilatura
from app.collectors.base import RSSAdapter, CollectorError
from app.collectors import ADAPTERS
from app.services.text_normalization import readable_text
from app.services.article_html import (
    dom,
    sanitize_html,
    content_text,
    safe_url,
    normalize_srcset,
    embed_url,
    frame_html,
    fallback_link,
    text_html,
)


class ScrapeError(RuntimeError):
    pass


@dataclass
class ScrapedArticle:
    title: str
    subtitle: str
    original_html: str
    sanitized_html: str
    plain_text: str
    language: str
    featured_image: str | None = None
    assets: list[dict] = field(default_factory=list)
    embeds: list[dict] = field(default_factory=list)
    version: str = "dom-2"


class GenericArticleScraper:
    selectors = (
        '[itemprop="articleBody"]',
        "article",
        "main",
        '[role="main"]',
        ".entry-content",
        ".post-content",
    )

    def container(self, soup, selectors=()):
        for selector in (*selectors, *self.selectors):
            candidates = soup.select(selector)
            candidates = [c for c in candidates if len(c.get_text(" ", strip=True)) >= 40]
            if candidates:
                return max(candidates, key=lambda c: len(c.get_text(" ", strip=True)))
        return None


class VarietyScraper(GenericArticleScraper):
    selectors = (".c-content", ".article-body", *GenericArticleScraper.selectors)


class DeadlineScraper(GenericArticleScraper):
    selectors = (".a-content", ".entry-content", *GenericArticleScraper.selectors)


class AnimeCornerScraper(GenericArticleScraper):
    selectors = (".post-entry", ".entry-content", *GenericArticleScraper.selectors)


class DramabeansScraper(GenericArticleScraper):
    selectors = (".entry-content", ".post-content", *GenericArticleScraper.selectors)


SCRAPERS = {
    "variety": VarietyScraper,
    "deadline": DeadlineScraper,
    "anime_corner": AnimeCornerScraper,
    "dramabeans": DramabeansScraper,
}
NOISE = re.compile(
    r"(?:^|[-_\s])(ads?|advertisement|advertising|banner|avatar|icon|social-share|share-buttons|related-posts|newsletter|subscribe|comments?|navigation)(?:$|[-_\s])",
    re.I,
)


def json_metadata(soup):
    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            if any(
                k in (kind if isinstance(kind, list) else [kind])
                for k in ("NewsArticle", "Article", "BlogPosting")
            ):
                yield value
            yield from walk(value.get("@graph", []))

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            for result in walk(json.loads(script.string or script.get_text())):
                return result
        except (ValueError, TypeError):
            continue
    return {}


class ArticleScraperService:
    def extract(self, html, url, source_key="", selectors=(), language="en", _fallback=False):
        if not html.strip() or len(html) > 4_000_000:
            raise ScrapeError("Empty or oversized document")
        soup = dom(html)
        metadata = json_metadata(soup)
        if metadata.get("isAccessibleForFree") in (False, "false", "False") or soup.select_one(
            ".paywall, [data-paywall], .subscription-required"
        ):
            raise ScrapeError("Restricted article; access was not bypassed")
        title_tag = soup.find("h1")
        title_fragment = dom(str(title_tag)) if title_tag else dom("")
        for nested_block in title_fragment.find_all(["p", "div", "section", "article", "ul", "ol"]):
            nested_block.decompose()
        og = soup.select_one('meta[property="og:title"]')
        title = readable_text(
            metadata.get("headline")
            or title_fragment.get_text(" ", strip=True)
            or (og.get("content") if og else "")
            or (soup.title.get_text() if soup.title else "")
        ).strip()
        subtitle_tag = soup.select_one('.article-subtitle, .subtitle, .dek, [itemprop="description"]')
        subtitle = (
            subtitle_tag.get_text(" ", strip=True) if subtitle_tag else str(metadata.get("description") or "")
        )
        subtitle = readable_text(subtitle)
        lang = (soup.html.get("lang", language) if soup.html else language).split("-")[0]
        adapter = SCRAPERS.get(source_key, GenericArticleScraper)()
        container = adapter.container(soup, selectors)
        if container is None and metadata.get("articleBody"):
            container = dom(text_html(str(metadata["articleBody"])))
        if container is None:
            extracted = trafilatura.extract(
                html,
                output_format="html",
                include_images=True,
                include_tables=True,
                include_links=True,
                include_comments=False,
            )
            container = dom(extracted or "")
        container = dom(str(container))
        for tag in list(container.find_all(True)):
            if not tag.name:
                continue
            marker = " ".join(tag.get("class", [])) + " " + str(tag.get("id", ""))
            if tag.name in (
                "nav",
                "header",
                "footer",
                "aside",
                "form",
                "button",
                "script",
                "style",
                "noscript",
            ) or NOISE.search(marker):
                tag.decompose()
        for tag in container.find_all("h1"):
            tag.decompose()
        # Persist only this selected article fragment, never the source document/scripts.
        assets, embeds = [], []
        for position, tag in enumerate(list(container.find_all(True))):
            if not tag.name:
                continue
            if tag.name in ("img", "source", "video", "audio"):
                raw_src = next(
                    (
                        tag.get(k)
                        for k in ("data-src", "data-lazy-src", "data-original", "data-url", "src")
                        if tag.get(k)
                    ),
                    "",
                )
                srcset = normalize_srcset(
                    tag.get("data-srcset") or tag.get("data-lazy-srcset") or tag.get("srcset", ""), url
                )
                src = safe_url(raw_src, url)
                if src and re.search(
                    r"(?:^|/)(?:spacer|blank|transparent|placeholder|loading)[._/-]", urlsplit(src).path, re.I
                ):
                    src = None
                if not src and srcset:
                    src = srcset.split(",")[-1].strip().split()[0]
                if tag.name == "img" and not src:
                    picture = tag.find_parent("picture")
                    if picture:
                        source = picture.find("source")
                        choices = normalize_srcset(
                            (source.get("data-srcset") or source.get("srcset", "")) if source else "", url
                        )
                        if choices:
                            src = choices.split(",")[-1].strip().split()[0]
                if tag.name == "img" and (
                    (str(tag.get("width", "")).isdigit() and int(tag["width"]) <= 32) or not src
                ):
                    tag.decompose()
                    continue
                if src:
                    tag["src"] = src
                if srcset:
                    tag["srcset"] = srcset
                if tag.get("poster"):
                    tag["poster"] = safe_url(tag["poster"], url) or ""
                    if tag["poster"]:
                        assets.append(
                            dict(
                                position=position,
                                kind="poster",
                                url=tag["poster"],
                                srcset="",
                                alt="",
                                title="",
                                caption="",
                                credit="",
                            )
                        )
                figure = tag.find_parent("figure")
                caption = figure.find("figcaption") if figure else None
                credit = tag.get("data-credit") or tag.get("data-copyright") or ""
                if figure and not credit:
                    credit_tag = figure.select_one('.credit, .copyright, [itemprop="creditText"]')
                    credit = credit_tag.get_text(" ", strip=True) if credit_tag else ""
                if credit and tag.name == "img":
                    if not figure:
                        figure = container.new_tag("figure")
                        tag.wrap(figure)
                    if not caption:
                        caption = container.new_tag("figcaption")
                        figure.append(caption)
                    if credit not in caption.get_text(" ", strip=True):
                        caption.append((" · " if caption.get_text(strip=True) else "") + credit)
                if src or srcset:
                    assets.append(
                        dict(
                            position=position,
                            kind=tag.name,
                            url=src or srcset.split()[0],
                            srcset=srcset,
                            alt=tag.get("alt", ""),
                            title=tag.get("title", ""),
                            caption=caption.get_text(" ", strip=True) if caption else "",
                            credit=credit,
                        )
                    )
            if tag.name == "iframe" or (
                tag.name == "blockquote"
                and set(tag.get("class", [])) & {"twitter-tweet", "instagram-media", "tiktok-embed"}
            ):
                link = tag.find("a", href=True)
                raw = (
                    tag.get("data-instgrm-permalink")
                    or tag.get("cite")
                    or tag.get("data-src")
                    or tag.get("src")
                    or (link["href"] if link else "")
                )
                original = safe_url(raw, url)
                if tag.name == "blockquote" and "twitter-tweet" in tag.get("class", []):
                    original = next(
                        (
                            safe_url(a["href"], url)
                            for a in tag.find_all("a", href=True)
                            if "/status/" in a["href"]
                        ),
                        original,
                    )
                provider, frame = embed_url(original)
                if original:
                    embeds.append(
                        dict(
                            position=position,
                            provider=provider or "link",
                            original_url=original,
                            embed_url=frame,
                        )
                    )
                tag.replace_with(dom((frame_html(frame) if frame else "") + fallback_link(original)))
            elif tag.name == "a" and tag.get("href"):
                tag["href"] = safe_url(tag["href"], url) or ""
            if tag.name == "div" and any("gallery" in c for c in tag.get("class", [])):
                tag["class"] = ["gallery"]
        # Prefer the publisher's social cover, then the first article photograph.
        # Cover inventory is independent of whether inline media will be published.
        og_cover = next(
            (
                candidate
                for tag in soup.select(
                    'meta[property="og:image"], meta[name="og:image"], meta[property="og:image:secure_url"]'
                )
                if (candidate := safe_url(tag.get("content"), url))
            ),
            None,
        )
        first_image = next((a["url"] for a in assets if a["kind"] == "img"), None)
        metadata_image = metadata.get("image")
        if isinstance(metadata_image, list):
            metadata_image = metadata_image[0] if metadata_image else None
        cover_credit = metadata_image.get("creditText", "") if isinstance(metadata_image, dict) else ""
        if isinstance(metadata_image, dict):
            metadata_image = metadata_image.get("url") or metadata_image.get("contentUrl")
        metadata_image = safe_url(metadata_image, url)
        cover = og_cover or first_image or metadata_image
        if cover != metadata_image:
            cover_credit = ""
        if cover and not any(a["kind"] == "img" and a["url"] == cover for a in assets):
            assets.insert(
                0,
                dict(
                    position=-1,
                    kind="img",
                    url=cover,
                    srcset="",
                    alt=title,
                    title="",
                    caption="",
                    credit=cover_credit,
                ),
            )
        if cover and not first_image:
            image = container.new_tag("img", src=cover, alt=title)
            if cover_credit:
                figure = container.new_tag("figure")
                figure.append(image)
                caption = container.new_tag("figcaption")
                caption.string = cover_credit
                figure.append(caption)
                container.insert(0, figure)
            else:
                container.insert(0, image)
        for node in list(container.find_all(string=True)):
            node.replace_with(readable_text(str(node)))
        # "original" is the untranslated, normalized article fragment, already safe for storage.
        original = sanitize_html(str(container), url)
        text = content_text(original)
        if not title or len(text) < 40 or not dom(original).find(["p", "li", "blockquote"]):
            if not _fallback:
                fallback = (
                    text_html(str(metadata["articleBody"]))
                    if metadata.get("articleBody")
                    else trafilatura.extract(
                        html,
                        output_format="html",
                        include_images=True,
                        include_tables=True,
                        include_links=True,
                        include_comments=False,
                    )
                )
                if fallback and title:
                    return self.extract(
                        f'<meta property="og:image" content="{escape(cover or "", quote=True)}"><h1>{escape(title)}</h1>{fallback}',
                        url,
                        language=lang,
                        _fallback=True,
                    )
            raise ScrapeError("No complete article content found")
        featured = cover
        return ScrapedArticle(title, subtitle, original, original, text, lang, featured, assets, embeds)

    async def scrape(self, source, url, client, settings):
        if not source.enabled:
            raise ScrapeError("Source disabled")
        if not safe_url(url):
            raise ScrapeError("Unsafe article URL")
        if urlsplit(url).hostname != urlsplit(source.site_url).hostname:
            # Known publisher adapters explicitly permit their www variants.
            known = ADAPTERS.get(source.key)
            if not known or urlsplit(url).hostname not in known.allowed_hosts:
                raise ScrapeError("Article URL outside publisher allowlist")
        adapter_class = ADAPTERS.get(source.key, RSSAdapter)
        adapter = adapter_class(client, settings)
        if adapter_class is RSSAdapter:
            adapter.allowed_hosts = {urlsplit(source.site_url).hostname}
        parts = urlsplit(url)
        policy = None
        try:
            robots = await adapter.request(f"{parts.scheme}://{parts.netloc}/robots.txt")
        except CollectorError as exc:
            if "HTTP 404;" not in str(exc):
                raise
        else:
            policy = RobotFileParser()
            policy.parse(robots.text.splitlines())
            if not policy.can_fetch(settings.user_agent, url):
                raise ScrapeError("robots.txt disallows this article")
        response = await adapter.request(url, robots_policy=policy)
        if "html" not in response.headers.get("content-type", "text/html").lower():
            raise ScrapeError("Article is not HTML")
        return self.extract(
            response.text, str(response.url), source.key, source.article_selectors, source.language
        )
