from pathlib import Path
import httpx
import pytest
from sqlalchemy import select, func
from app.collectors.base import Article
from app.core.config import Settings
from app.db.models import Source, SourceArticle, NewsEvent, AuditLog
from app.services.ingestion import IngestionService, queue_source
from app.services.translation import TranslationService, MissingModel
from app.services.editorial import set_publication, reject_event, retry_event, edit_translation
from app.services.moderation import readiness_errors
from app.services.article_html import dom
from app.workers import runner

HTML = (Path(__file__).parent / "fixtures/rich_article.html").read_text()


class FixtureEngine:
    """Deterministic test translator; deliberately not used as a production fallback."""

    def path(self, source, target):
        return [source, target]

    def translate(self, text, source, target):
        return f"[{target}] {text}"


@pytest.fixture
def configured_source(db):
    source = db.scalar(select(Source).where(Source.key == "anime_corner"))
    source.allow_extraction = source.allow_media = source.allow_embeds = True
    source.publication_mode = "full"
    source.policy_note = "Local fixture with test-only reproduction permission"
    db.commit()
    return source


async def prepare(db, sessions, source, monkeypatch, engine=None):
    requested = []

    def response(request):
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/feed/":
            return httpx.Response(
                200,
                text='<rss version="2.0"><channel><title>Fixture</title><item><guid>rich-1</guid><title>Fixture Movie trailer released</title><link>https://animecorner.me/news/fixture/</link></item></channel></rss>',
            )
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    translator = TranslationService(engine or FixtureEngine())
    service = IngestionService(sessions, Settings(request_interval_seconds=0, _env_file=None), translator)
    run = queue_source(db, source.id)
    db.commit()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        await service.run_source(run.id, client)
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    monkeypatch.setattr(runner, "TranslationService", lambda: translator)
    runner.translate_pending()
    db.expire_all()
    return db.scalar(select(NewsEvent)), service, response, requested


async def test_rss_to_preview_approval_publication_and_exact_reimport(
    db, sessions, configured_source, admin, client, monkeypatch
):
    from test_web import login

    event, service, response, requested = await prepare(db, sessions, configured_source, monkeypatch)
    assert event.status == "READY_FOR_REVIEW" and not event.reviewed
    assert not readiness_errors(event)
    article = event.articles[0]
    assert article.original_html and article.scraped_at and article.content_hash
    assert len(article.assets) >= 6 and len(article.embeds) == 5
    assert all(t.content_html and t.status == "ready" for t in event.translations)
    assert client.get(f"/en/news/{event.id}-{event.slug}").status_code == 404
    assert client.get(f"/admin/events/{event.id}/preview/en", follow_redirects=False).status_code == 303
    token = login(client)
    for language in ("en", "ru", "ky"):
        preview = client.get(f"/admin/events/{event.id}/preview/{language}")
        assert preview.status_code == 200
        assert "youtube-nocookie.com/embed/VIDEO_ID123" in preview.text
        assert 'class="prose article-content"' in preview.text
        assert "noindex" in preview.text and preview.headers["Cache-Control"] == "no-store"
    assert "Одобрить и опубликовать" in client.get(f"/admin/events/{event.id}").text
    assert (
        client.post(
            f"/admin/events/{event.id}/publication",
            data={"csrf": token, "action": "publish"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    db.expire_all()
    assert event.status == "PUBLISHED" and event.approved_by == admin.id
    timestamp = event.published_at
    for lang in ("en", "ru", "ky"):
        page = client.get(f"/{lang}/news/{event.id}-{event.slug}")
        assert page.status_code == 200 and "poster.jpg" in page.text
        assert "frame-src https://www.youtube-nocookie.com" in page.headers["Content-Security-Policy"]
    set_publication(db, event.id, True, admin.id)
    db.commit()
    assert event.published_at == timestamp
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "publish")) == 1
    before = requested.count("https://animecorner.me/news/fixture/")
    run = queue_source(db, configured_source.id)
    db.commit()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        await service.run_source(run.id, http)
    assert requested.count("https://animecorner.me/news/fixture/") == before
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 1


async def test_missing_models_stay_processing_and_retry(db, sessions, configured_source, monkeypatch, admin):
    class Missing(FixtureEngine):
        def path(self, source, target):
            if target == "ky":
                raise MissingModel("No Kyrgyz model")
            return super().path(source, target)

    event, _, _, _ = await prepare(db, sessions, configured_source, monkeypatch, Missing())
    assert event.status == "TRANSLATING"
    ky = next(t for t in event.translations if t.language == "ky")
    assert ky.status == "missing_model" and ky.content_html == "" and ky.title == ""
    with pytest.raises(ValueError, match="READY_FOR_REVIEW"):
        set_publication(db, event.id, True, admin.id)
    TranslationService(FixtureEngine()).localize(db, event, "ky")
    db.commit()
    assert event.status == "READY_FOR_REVIEW"


async def test_admin_required_and_reject_retry(db, sessions, configured_source, monkeypatch, admin, client):
    from test_web import login

    event, service, response, _ = await prepare(db, sessions, configured_source, monkeypatch)
    with pytest.raises(ValueError, match="Administrator"):
        set_publication(db, event.id, True, None)
    token = login(client)
    admin.role = "editor"
    db.commit()
    assert (
        client.post(
            f"/admin/events/{event.id}/publication", data={"csrf": token, "action": "publish"}
        ).status_code
        == 403
    )
    assert client.post("/admin/approve-all", data={"csrf": token}).status_code == 403
    with pytest.raises(ValueError, match="Administrator"):
        set_publication(db, event.id, True, admin.id)
    admin.role = "admin"
    reject_event(db, event.id, admin.id)
    db.commit()
    assert event.status == "REJECTED"
    with pytest.raises(ValueError):
        set_publication(db, event.id, True, admin.id)
    retry_event(db, event.id, admin.id)
    db.commit()
    assert event.status == "DISCOVERED"
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        await service.process_pending(http)
    runner.translate_pending()
    db.expire_all()
    assert event.status == "READY_FOR_REVIEW"


async def test_content_dedup_preserves_published_copy(db, sessions, configured_source, monkeypatch, admin):
    event, service, response, _ = await prepare(db, sessions, configured_source, monkeypatch)
    set_publication(db, event.id, True, admin.id)
    db.commit()
    original = [(t.title, t.content_html) for t in event.translations]
    second = db.scalar(select(Source).where(Source.key == "deadline"))
    second.enabled = second.allow_extraction = True
    second.publication_mode = "full"
    service.import_article(
        db,
        second,
        Article("other", "https://deadline.com/other/", "A different headline for identical article"),
    )
    db.commit()
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        await service.process_pending(http)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 1
    assert len(event.articles) == 2 and event.status == "PUBLISHED"
    assert [(t.title, t.content_html) for t in event.translations] == original


async def test_facts_mode_and_media_rights(db, sessions, configured_source, monkeypatch, admin):
    configured_source.publication_mode = "facts"
    configured_source.allow_media = configured_source.allow_embeds = False
    db.commit()
    event, _, _, _ = await prepare(db, sessions, configured_source, monkeypatch)
    en = next(t for t in event.translations if t.language == "en")
    assert event.status == "READY_FOR_REVIEW" and "Anime Corner reports" in en.body
    assert "The studio has released" not in en.body
    assert not dom(en.content_html).find(["img", "iframe", "video", "audio"])
    assert event.image_url == "https://animecorner.me/poster.jpg"
    assert not readiness_errors(event)
    assert all(not asset.permitted for asset in event.articles[0].assets)
    set_publication(db, event.id, True, admin.id)


async def test_edit_sanitizes_preserves_media_and_requires_fresh_approval(
    db, sessions, configured_source, monkeypatch, admin
):
    event, _, _, _ = await prepare(db, sessions, configured_source, monkeypatch)
    set_publication(db, event.id, True, admin.id)
    en = next(t for t in event.translations if t.language == "en")
    edit_translation(
        db,
        event.id,
        "en",
        en.title,
        en.summary,
        en.content_html + '<p onmouseover="evil()">Additional fact.</p><script>evil()</script>',
        True,
        admin.id,
    )
    db.commit()
    assert event.status == "TRANSLATING" and not event.reviewed
    assert "evil" not in en.content_html and dom(en.content_html).iframe
    assert all(t.status == "pending" for t in event.translations if t.language != "en")
    with pytest.raises(ValueError):
        set_publication(db, event.id, True, admin.id)


async def test_bad_html_empty_translation_and_rights_revocation_block_approval(
    db, sessions, configured_source, monkeypatch, admin
):
    event, _, _, _ = await prepare(db, sessions, configured_source, monkeypatch)
    en = next(t for t in event.translations if t.language == "en")
    html = en.content_html
    en.content_html = "<p>Untrusted</p><script>alert(1)</script>"
    with pytest.raises(ValueError, match="HTML"):
        set_publication(db, event.id, True, admin.id)
    en.content_html = html
    configured_source.allow_media = False
    with pytest.raises(ValueError, match="permission"):
        set_publication(db, event.id, True, admin.id)


async def test_unlicensed_media_cannot_be_added_by_editor(
    db, sessions, configured_source, monkeypatch, admin
):
    event, _, _, _ = await prepare(db, sessions, configured_source, monkeypatch)
    en = next(t for t in event.translations if t.language == "en")
    edit_translation(
        db,
        event.id,
        "en",
        en.title,
        en.summary,
        en.content_html + '<p>Photo</p><img src="https://unlicensed.example/stolen.jpg">',
        True,
        admin.id,
    )
    assert any("without recorded source permission" in e for e in readiness_errors(event))


async def test_retry_uses_stored_html_without_redownload(db, sessions, configured_source, monkeypatch, admin):
    event, service, _, _ = await prepare(db, sessions, configured_source, monkeypatch)
    retry_event(db, event.id, admin.id)
    db.commit()

    def forbidden(request):
        raise AssertionError("No network request is needed to reprocess saved HTML")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        await service.process_pending(client)
    runner.translate_pending()
    db.expire_all()
    assert event.status == "READY_FOR_REVIEW"


async def test_non_english_source_missing_english_model_keeps_scraped_document(
    db, sessions, configured_source, monkeypatch
):
    class MissingEnglish(FixtureEngine):
        def translate(self, text, source, target):
            raise MissingModel("No route to English")

    translator = TranslationService(MissingEnglish())
    service = IngestionService(sessions, Settings(request_interval_seconds=0, _env_file=None), translator)
    service.import_article(
        db,
        configured_source,
        Article("russian", "https://animecorner.me/russian/", "Русская новость", language="ru"),
    )
    db.commit()
    article = db.scalar(select(SourceArticle))

    def response(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(
            200, text=HTML.replace('lang="en"', 'lang="ru"'), headers={"content-type": "text/html"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        await service.process_article(article.id, client)
    db.expire_all()
    assert article.sanitized_html and article.scraped_at
    assert article.event.status == "TRANSLATING"
    assert next(t for t in article.event.translations if t.language == "en").status == "missing_model"


def test_generic_source_can_be_configured_without_code(client, admin, db):
    from test_web import login

    token = login(client)
    result = client.post(
        "/admin/sources",
        data={
            "csrf": token,
            "name": "Fixture Publisher",
            "site_url": "https://publisher.example",
            "feed_url": "https://publisher.example/feed/",
            "language": "en",
        },
    )
    assert result.status_code == 200
    source = db.scalar(select(Source).where(Source.key == "fixture-publisher"))
    assert source and not source.enabled
    response = client.post(
        f"/admin/sources/{source.id}/policy",
        data={
            "csrf": token,
            "publication_mode": "full",
            "policy_note": "Permission from fixture owner",
            "allow_media": "yes",
            "article_selectors": ".news-body",
            "excerpt_limit": "0",
        },
    )
    assert response.status_code == 200
    db.refresh(source)
    assert source.article_selectors == [".news-body"]
    assert (
        client.post(
            "/admin/sources",
            data={
                "csrf": token,
                "name": "Bad",
                "site_url": "http://127.0.0.1",
                "feed_url": "http://127.0.0.1/feed",
            },
        ).status_code
        == 422
    )


def test_facts_mode_does_not_mark_headline_link_card_as_ready(db, configured_source):
    from app.services.article_preparation import publication_copy

    configured_source.publication_mode = "facts"
    event = NewsEvent(canonical_title="An editorial opinion", slug="opinion", facts={})
    article = SourceArticle(
        source=configured_source,
        event=event,
        title="An editorial opinion",
        english_title="An editorial opinion",
        language="en",
        canonical_url="https://animecorner.me/opinion/",
        guid="opinion",
        plain_text="An opinion without supported factual relations.",
        sanitized_html="<p>An opinion without supported factual relations.</p>",
        raw_data={},
    )
    with pytest.raises(ValueError, match="Insufficient supported facts"):
        publication_copy(article, configured_source, event)


async def test_media_rights_are_independent_of_text_republication(
    db, sessions, configured_source, monkeypatch
):
    configured_source.publication_mode = "facts"
    db.commit()
    event, _, _, _ = await prepare(db, sessions, configured_source, monkeypatch)
    en = next(t for t in event.translations if t.language == "en")
    assert event.status == "READY_FOR_REVIEW"
    assert len(dom(en.content_html).find_all("img")) == 3
    assert len(dom(en.content_html).find_all("iframe")) == 4
    assert "The studio has released" not in en.body
    assert "The release is planned for November 12, 2026." in en.body


def test_rejection_between_scrape_and_translation_is_not_overwritten(db, sessions, configured_source, admin):
    from sqlalchemy import event as sa_event
    from app.services.scraping import ArticleScraperService

    service = IngestionService(sessions, translator=TranslationService(FixtureEngine()))
    service.import_article(
        db,
        configured_source,
        Article("reject-race", "https://animecorner.me/reject-race/", "Fixture Movie trailer released"),
    )
    db.commit()
    article = db.scalar(select(SourceArticle))
    result = ArticleScraperService().extract(HTML, article.canonical_url)

    def reject_after_extraction(session):
        with sessions() as editor:
            reject_event(editor, article.event_id, admin.id)
            editor.commit()

    sa_event.listen(sessions.class_, "after_commit", reject_after_extraction, once=True)
    try:
        service.prepare_article(article.id, result)
    finally:
        sa_event.remove(sessions.class_, "after_commit", reject_after_extraction)
    db.expire_all()
    assert article.event.status == "REJECTED"
    assert all(t.status == "pending" for t in article.event.translations)
    assert article.sanitized_html


async def test_public_articles_need_no_extraction_confirmation(
    db, sessions, configured_source, monkeypatch, admin, client
):
    from test_web import login

    configured_source.allow_extraction = False
    configured_source.publication_mode = "facts"
    configured_source.allow_media = configured_source.allow_embeds = False
    configured_source.enabled = False
    configured_source.access_status = "permission_required"
    db.commit()
    token = login(client)
    result = client.post(f"/admin/sources/{configured_source.id}/toggle", data={"csrf": token})
    assert result.status_code == 200
    assert 'name="permission_confirmed"' not in result.text
    assert 'name="allow_extraction"' not in result.text
    db.refresh(configured_source)
    assert configured_source.enabled and not configured_source.allow_extraction
    event, _, _, requested = await prepare(db, sessions, configured_source, monkeypatch)
    assert "https://animecorner.me/news/fixture/" in requested
    assert event.status == "READY_FOR_REVIEW"
    assert not readiness_errors(event)
    assert all(t.status == "ready" for t in event.translations)
    en = next(t for t in event.translations if t.language == "en")
    assert not dom(en.content_html).find(["img", "iframe", "audio", "video"])
    assert "The studio has released" not in en.body
    set_publication(db, event.id, True, admin.id)
    assert event.status == "PUBLISHED"


@pytest.mark.parametrize(
    "status,manual,reviewed,error,retried",
    [
        ("FAILED", False, False, "Source has not granted extraction permission", True),
        ("FAILED", False, False, "Source disabled or extraction permission revoked", True),
        ("FAILED", True, False, "Source has not granted extraction permission", False),
        ("FAILED", False, True, "Source has not granted extraction permission", False),
        ("REJECTED", False, False, "Source has not granted extraction permission", False),
        ("PUBLISHED", False, True, "Source has not granted extraction permission", False),
        ("FAILED", False, False, "HTTP 403; access denied", False),
    ],
)
def test_extraction_permission_migration_recovers_only_untouched_failures(
    db, sessions, configured_source, status, manual, reviewed, error, retried
):
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    service = IngestionService(sessions)
    service.import_article(
        db, configured_source, Article("migration", "https://animecorner.me/migration/", "Fixture")
    )
    db.flush()
    event = db.scalar(select(NewsEvent))
    event.status, event.reviewed, event.editorial_note = status, reviewed, error
    article = event.articles[0]
    article.scrape_status, article.scrape_error = "failed", error
    event.translations[0].manual_edit = manual
    db.flush()
    migration = import_module("app.db.migrations.versions.b63a47e821d0_public_article_fetching")
    connection = db.connection()
    if connection.dialect.name == "postgresql":
        # Raw migration SQL needs the same isolated schema as ORM statements.
        schema = connection.get_execution_options()["schema_translate_map"][None]
        assert schema.startswith("test_")
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
        migration.upgrade()  # A second run must not reset unrelated work.
    db.expire_all()
    assert event.status == ("DISCOVERED" if retried else status)
    assert article.scrape_status == ("pending" if retried else "failed")
    assert article.scrape_error == (None if retried else error)
    assert event.editorial_note == ("Повторная обработка поставлена в очередь." if retried else error)
    assert event.translations[0].manual_edit == manual


async def test_cover_is_visible_without_inline_media_permission(
    db, sessions, configured_source, monkeypatch, admin, client
):
    from test_web import login

    configured_source.allow_media = configured_source.allow_embeds = False
    configured_source.publication_mode = "facts"
    db.commit()
    event, _, _, _ = await prepare(db, sessions, configured_source, monkeypatch)
    login(client)
    page = client.get(f"/admin/events/{event.id}/preview/ru")
    cover = dom(page.text).select_one(".article-cover img")
    assert cover and cover["src"] == event.articles[0].featured_image
    assert not dom(page.text).select_one(".article-content img")
    event.image_url = "https://other.example/unverified.jpg"
    assert "Featured image is not a verified source image" in readiness_errors(event)
