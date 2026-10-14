from pathlib import Path
import pytest
import httpx
from bs4 import BeautifulSoup
from app.services.scraping import ArticleScraperService, ScrapeError, SCRAPERS
from app.services.article_html import sanitize_html, validate_html, dom, frame_html, embed_url
from app.services.translation import TranslationService

RICH = (Path(__file__).parent / "fixtures/rich_article.html").read_text()
URL = "https://animecorner.me/news/fixture/"


def test_rich_article_and_order():
    result = ArticleScraperService().extract(RICH, URL)
    soup = dom(result.sanitized_html)
    assert result.title == "Fixture Movie trailer released"
    assert len(soup.find_all("img")) == 3
    assert soup.img["src"] == "https://animecorner.me/poster.jpg"
    assert "1200w" in soup.img["srcset"] and soup.img["loading"] == "lazy"
    assert soup.source["srcset"].startswith("https://animecorner.me/poster-small.webp")
    assert soup.select_one(".gallery img")["src"] == "https://animecorner.me/news/still1.jpg"
    assert soup.find("figcaption").text == "Official promotional poster · Studio press kit"
    assert result.assets[1]["credit"] == "Studio press kit"
    assert soup.find("h2") and soup.find("h3") and soup.find("strong") and soup.find("em")
    assert soup.find("table") and soup.find("audio") and soup.find("video")
    assert "Buy this product" not in result.plain_text and "Unrelated footer" not in result.plain_text
    assert "Site navigation" not in result.plain_text and "evil" not in result.sanitized_html
    assert (
        result.sanitized_html.index("Production details")
        < result.sanitized_html.index("poster.jpg")
        < result.sanitized_html.index("Cast")
    )
    assert validate_html(result.sanitized_html)


def test_embeds_are_canonical_and_no_source_scripts():
    result = ArticleScraperService().extract(RICH, URL)
    soup = dom(result.sanitized_html)
    assert len(soup.find_all("iframe")) == 4
    assert {e["provider"] for e in result.embeds} == {"youtube", "twitter", "instagram", "tiktok", "link"}
    assert soup.iframe["src"] == "https://www.youtube-nocookie.com/embed/VIDEO_ID123"
    assert all(
        i.get("sandbox") and i.get("referrerpolicy") == "strict-origin-when-cross-origin"
        for i in soup.find_all("iframe")
    )
    assert soup.select_one('a[href="https://untrusted.example/widget"]')
    assert not soup.find("script")


@pytest.mark.parametrize(
    "html",
    [
        '<p onclick="alert(1)">Text <img src="javascript:alert(1)" onerror="evil()"></p>',
        '<p>Text<a href="java&#x73;cript:alert(1)">click</a></p><svg onload="evil()"><script>evil()</script></svg>',
        '<p>Text</p><math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=evil()>">',
        '<p>Text</p><iframe srcdoc="<script>evil()</script>" src="https://www.youtube.com/redirect"></iframe>',
        '<p>Text</p><iframe src="https://www.youtube-nocookie.com.evil.example/embed/VIDEO_ID123"></iframe>',
        '<p>Text</p><img src="https://site.example/a" srcset="javascript:evil() 1x, data:image/svg+xml,evil 2x">',
        '<p><a href="file:///etc/passwd">Text</a></p><object data="https://evil.example"></object>',
    ],
)
def test_sanitization_xss(html):
    clean = sanitize_html(html)
    browser = BeautifulSoup(clean, "html5lib")
    assert not browser.find(["script", "svg", "math", "style", "object", "iframe"])
    for tag in browser.find_all(True):
        assert not any(key.startswith("on") or key in {"srcdoc", "style"} for key in tag.attrs)
        for key in ("href", "src", "srcset"):
            assert not any(bad in str(tag.get(key, "")) for bad in ("javascript:", "data:", "file:"))
    assert sanitize_html(clean) == clean


def test_iframe_path_allowlist():
    assert not dom(sanitize_html('<iframe src="https://www.youtube-nocookie.com/evil"></iframe>')).iframe
    assert dom(sanitize_html(frame_html(embed_url("https://youtu.be/VIDEO_ID123")[1]))).iframe


@pytest.mark.parametrize("key", list(SCRAPERS) + ["unknown"])
def test_adapters_and_custom_selectors(key):
    result = ArticleScraperService().extract(RICH, URL, key)
    assert "official trailer" in result.plain_text
    custom = (
        '<h1>Custom publisher</h1><div class="custom"><p>'
        + "A detailed article with useful facts. " * 4
        + "</p></div>"
    )
    assert (
        "detailed article" in ArticleScraperService().extract(custom, URL, selectors=[".custom"]).plain_text
    )


def test_jsonld_and_malformed_fallback():
    html = '<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","headline":"Schema news","articleBody":"A complete article available in structured metadata. With sufficient facts to process."}]}</script>'
    assert ArticleScraperService().extract(html, URL).title == "Schema news"
    broken = "<article><h1>Broken source<p>Available article with a long paragraph and unclosed tags. The content must still be recoverable."
    # The structural fallback recovers a malformed document rather than approving an empty body.
    result = ArticleScraperService().extract(broken, URL)
    assert result.title == "Broken source"
    assert "unclosed tags" in result.plain_text and validate_html(result.sanitized_html)


def test_trafilatura_fallback(monkeypatch):
    monkeypatch.setattr(
        "app.services.scraping.trafilatura.extract",
        lambda *a, **kw: "<p>Recovered a complete article with enough available text to process.</p>",
    )
    assert (
        "Recovered"
        in ArticleScraperService()
        .extract("<title>Fallback title</title><div>Unusual markup</div>", URL)
        .plain_text
    )


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<h1>Not an article</h1>",
        '<script type="application/ld+json">{"@type":"NewsArticle","isAccessibleForFree":false}</script>',
    ],
)
def test_unavailable_content(html):
    with pytest.raises(ScrapeError):
        ArticleScraperService().extract(html, URL)


def test_dom_translation_preserves_structure_and_technical_attributes():
    class Engine:
        def translate(self, text, source, target):
            assert "<" not in text and "https://" not in text
            return "Перевод " + text

    original = ArticleScraperService().extract(RICH, URL).sanitized_html
    translated = TranslationService(Engine()).translate_html(original, "en", "ru")
    before, after = dom(original), dom(translated)
    assert [t.name for t in before.find_all(True)] == [t.name for t in after.find_all(True)]
    for a, b in zip(before.find_all(True), after.find_all(True)):
        assert {k: v for k, v in a.attrs.items() if k not in ("alt", "title")} == {
            k: v for k, v in b.attrs.items() if k not in ("alt", "title")
        }
    assert after.img["alt"].startswith("Перевод")
    assert validate_html(translated)


async def test_robots_disabled_sources_and_http_errors(db):
    from sqlalchemy import select
    from app.db.models import Source
    from app.core.config import Settings

    source = db.scalar(select(Source).where(Source.key == "anime_corner"))
    settings = Settings(request_interval_seconds=0, _env_file=None)
    scraper = ArticleScraperService()
    requested = []

    def response(request):
        requested.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /news/")

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        source.enabled = False
        with pytest.raises(ScrapeError, match="Source disabled"):
            await scraper.scrape(source, URL, client, settings)
        assert not requested
        source.enabled = True
        assert not source.allow_extraction
        with pytest.raises(ScrapeError, match="robots"):
            await scraper.scrape(source, URL, client, settings)
        assert requested == ["/robots.txt"]

    def unavailable(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(Exception, match="403"):
            await scraper.scrape(source, URL, client, settings)


def test_metadata_cover_and_placeholder_srcset():
    html = '<title>Article with cover</title><meta property="og:image" content="/cover.jpg"><article><p>A complete article with sufficient original content and no inline picture.</p></article>'
    result = ArticleScraperService().extract(html, URL)
    assert result.featured_image == "https://animecorner.me/cover.jpg"
    assert dom(result.sanitized_html).img["loading"] == "lazy"
    html = '<h1>Lazy image example</h1><article><p>A complete article containing a placeholder and a lazy responsive image.</p><img src="/placeholder.gif" data-srcset="/real.jpg 1200w"></article>'
    assert (
        dom(ArticleScraperService().extract(html, URL).sanitized_html).img["src"]
        == "https://animecorner.me/real.jpg"
    )


def test_factual_brief_uses_grounded_relations_not_copied_paragraphs():
    from app.services.factual_brief import extract_facts

    facts = extract_facts(
        "<p>The film was shot in Japan. Its release is scheduled for November 12, 2026.</p><p>Studio reportedly has renewed Film for season 2.</p>"
    )
    assert len(facts) == 2
    assert any("Filming" in f.text and "Japan" in f.text for f in facts)
    assert all(f.text != f.evidence for f in facts)
    assert not any("renewal" in f.text for f in facts)


@pytest.mark.parametrize(
    "key,container",
    [
        ("variety", "c-content"),
        ("deadline", "a-content"),
        ("anime_corner", "post-entry"),
        ("dramabeans", "entry-content"),
    ],
)
def test_specialized_container_wins_over_main(key, container):
    html = f'<h1>Publisher specific article</h1><main><p>Unrelated sidebar information.</p><div class="{container}"><p>This is the full article extracted by the specialized publisher adapter.</p></div></main>'
    result = ArticleScraperService().extract(html, URL, key)
    assert "full article" in result.plain_text and "Unrelated sidebar" not in result.plain_text


async def test_redirect_cannot_bypass_article_robots(db):
    from sqlalchemy import select
    from app.db.models import Source
    from app.core.config import Settings

    source = db.scalar(select(Source).where(Source.key == "anime_corner"))
    source.allow_extraction = True
    seen = []

    def response(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /restricted/")
        return httpx.Response(302, headers={"location": "/restricted/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(Exception, match="robots"):
            await ArticleScraperService().scrape(
                source, URL, client, Settings(request_interval_seconds=0, _env_file=None)
            )
    assert "/restricted/private" not in seen


def test_registry_accepts_new_constrained_widget(monkeypatch):
    from app.services.article_html import PROVIDERS, EmbedProvider

    monkeypatch.setitem(
        PROVIDERS,
        "fixture",
        EmbedProvider("fixture", frozenset({"widgets.example"}), "widgets.example", r"/embed/\d+"),
    )
    assert embed_url("https://widgets.example/embed/123") == ("fixture", "https://widgets.example/embed/123")
    assert dom(sanitize_html(frame_html("https://widgets.example/embed/123"))).iframe
    assert not dom(sanitize_html('<iframe src="https://widgets.example/account"></iframe>')).iframe


def test_translator_output_is_inserted_as_text_not_html():
    class Hostile:
        def translate(self, *args):
            return "<img src=x onerror=alert(1)>"

    html = TranslationService(Hostile()).translate_html("<p>Hello world</p>", "en", "ru")
    assert not dom(html).img and "&lt;img" in html


def test_auto_glossary_protects_work_names(db, published):
    from app.db.models import Work, WorkAlias
    from test_translation import FakeEngine

    published.works = [
        Work(
            title="Wednesday",
            original_title="Wednesday",
            slug="wednesday",
            aliases=[WorkAlias(name="Уэнсдей", language="ru")],
        )
    ]
    translator = TranslationService(FakeEngine())
    result = translator.translate_html(
        "<p>Wednesday has a trailer from Netflix.</p>", "en", "ru", translator.glossary(db, published, "ru")
    )
    assert "Уэнсдей" in result and "Wednesday" not in result and "Netflix" in result


def test_encoded_jsonld_title_and_og_cover_precede_inline_image():
    html = """<html><head><meta property="og:image" content="/social.jpg">
    <script type="application/ld+json">{"@type":"NewsArticle", "headline":"Mark &amp;amp; Michael &#8216;News&#8217;", "description":"A &amp; B"}</script>
    </head><body><article><p>A complete article with sufficient original content and a photograph.</p>
    <img src="/inline.jpg"></article></body></html>"""
    result = ArticleScraperService().extract(html, URL)
    assert result.title == "Mark & Michael ‘News’"
    assert result.subtitle == "A & B"
    assert result.featured_image == "https://animecorner.me/social.jpg"
    assert any(a["url"] == result.featured_image for a in result.assets)
    assert dom(result.sanitized_html).img["src"] == "https://animecorner.me/inline.jpg"


@pytest.mark.parametrize("cover", ["", "javascript:alert(1)", "http://127.0.0.1/image.jpg"])
def test_missing_or_unsafe_og_cover_uses_first_real_article_photo(cover):
    html = f'''<meta property="og:image" content="{cover}"><h1>Public article</h1><article>
    <p>A complete article with sufficient original content and a photograph.</p>
    <img src="/tracking.gif" width="1"><img data-src="/first.jpg"><img src="/second.jpg"></article>'''
    result = ArticleScraperService().extract(html, URL)
    assert result.featured_image == "https://animecorner.me/first.jpg"
