import asyncio
import logging
import hashlib
import httpx
from slugify import slugify
from sqlalchemy import select
from app.collectors import ADAPTERS
from app.core.config import get_settings
from app.db.models import (
    Source,
    SourceArticle,
    NewsEvent,
    NewsTranslation,
    Category,
    IngestionRun,
    DuplicateReview,
    utcnow,
    ArticleAsset,
    ArticleEmbed,
    GlossaryEntry,
)
from app.db.session import editorial_lock
from app.services.classification import classify
from app.services.deduplication import DeduplicationService
from app.services.scraping import ArticleScraperService, ScrapedArticle
from app.services.article_preparation import publication_copy
from app.services.article_html import content_text, validate_html, sanitize_html, text_html
from app.services.moderation import PROTECTED, refresh_readiness
from app.services.translation import TranslationService, detect_language, MissingModel
from app.services.urls import normalize_url

log = logging.getLogger(__name__)


class IngestionService:
    def __init__(self, session_factory, settings=None, translator=None, scraper=None):
        self.sessions = session_factory
        self.settings = settings or get_settings()
        self.dedup = DeduplicationService(self.settings)
        self.translator = translator or TranslationService()
        self.scraper = scraper or ArticleScraperService()

    def import_article(self, db, source, article):
        editorial_lock(db)
        url = normalize_url(article.url)
        if self.dedup.exact(db, source.id, article.guid, url):
            return False
        language = detect_language(article.title + " " + article.excerpt, article.language)
        english_title = article.title if language == "en" else ""
        classification = classify(
            english_title, article.tags, source.key, article.excerpt if language == "en" else ""
        )
        event = NewsEvent(
            canonical_title=article.title,
            slug=slugify(article.title)[:220] or "news",
            event_type=classification.event_type,
            facts=classification.facts,
            formats=classification.formats,
            production_types=classification.production_types,
            countries=classification.countries,
            cultural_categories=classification.cultural_categories,
            created_at=utcnow(),
            categories=list(db.scalars(select(Category).where(Category.slug.in_(classification.categories)))),
        )
        db.add(event)
        db.flush()
        for lang in ("en", "ru", "ky"):
            db.add(NewsTranslation(event=event, language=lang, status="pending"))
        db.add(
            SourceArticle(
                source_id=source.id,
                event=event,
                guid=article.guid,
                canonical_url=url,
                title=article.title,
                excerpt=article.excerpt,
                language=language,
                english_title=english_title or None,
                published_at=article.published_at,
                raw_data={"tags": article.tags},
            )
        )
        db.flush()
        return True

    async def run_source(self, run_id, client):
        with self.sessions() as db:
            run = db.get(IngestionRun, run_id)
            source = db.get(Source, run.source_id)
            if not source.enabled:
                run.status, run.error, run.finished_at = "skipped", "Source disabled", utcnow()
                db.commit()
                return
            run.status, run.started_at = "running", utcnow()
            db.commit()
            try:
                from app.collectors.base import RSSAdapter
                from urllib.parse import urlsplit

                adapter = ADAPTERS.get(source.key, RSSAdapter)(client, self.settings)
                if source.key not in ADAPTERS:
                    adapter.allowed_hosts = {urlsplit(source.site_url).hostname}
                result = await adapter.fetch(source)
                # Translation/model work stays in the worker thread, away from the event loop.
                imported, skipped = await asyncio.to_thread(self.import_batch, source.id, result.articles)
                await self.process_pending(client, source.id)
                db.refresh(source)
                source.etag, source.last_modified = result.etag, result.last_modified
                source.last_success_at, source.last_error = utcnow(), None
                run.status, run.imported, run.skipped = (
                    "unchanged" if result.not_modified else "success",
                    imported,
                    skipped,
                )
            except Exception as exc:
                db.rollback()
                source, run = db.get(Source, source.id), db.get(IngestionRun, run_id)
                source.last_error = run.error = str(exc)[:2000]
                run.status = "failed"
                log.exception("Source %s failed", source.key)
            source.last_checked_at = run.finished_at = utcnow()
            db.commit()

    def import_batch(self, source_id, articles):
        imported = skipped = 0
        for article in articles:
            with self.sessions() as db:
                source = db.get(Source, source_id)
                if not source.enabled:
                    break
                if self.import_article(db, source, article):
                    imported += 1
                else:
                    skipped += 1
                db.commit()
        return imported, skipped

    async def process_pending(self, client, source_id=None):
        with self.sessions() as db:
            stmt = (
                select(SourceArticle.id)
                .join(Source)
                .where(SourceArticle.scrape_status == "pending", Source.enabled.is_(True))
            )
            if source_id:
                stmt = stmt.where(SourceArticle.source_id == source_id)
            ids = list(db.scalars(stmt.order_by(SourceArticle.id)))
        for article_id in ids:
            await self.process_article(article_id, client)

    async def process_article(self, article_id, client):
        with self.sessions() as db:
            editorial_lock(db)
            article = db.get(SourceArticle, article_id)
            if not article or article.scrape_status != "pending":
                return
            source, event = article.source, article.event
            if not source.enabled:
                return
            article.scrape_status = "scraping"
            if event.status not in PROTECTED and not event.reviewed:
                event.status = "SCRAPING"
            db.commit()
            try:
                if article.sanitized_html and article.scraped_at:
                    # Explicit reprocessing reuses the stored article and media inventory.
                    # Failed fetches have no stored fragment and will be downloaded again.
                    result = ScrapedArticle(
                        article.title,
                        article.subtitle,
                        article.original_html,
                        article.sanitized_html,
                        article.plain_text,
                        article.language,
                        article.featured_image,
                        [
                            {
                                key: getattr(a, key)
                                for key in (
                                    "position",
                                    "kind",
                                    "url",
                                    "srcset",
                                    "alt",
                                    "title",
                                    "caption",
                                    "credit",
                                )
                            }
                            for a in article.assets
                        ],
                        [
                            {
                                key: getattr(e, key)
                                for key in ("position", "provider", "original_url", "embed_url")
                            }
                            for e in article.embeds
                        ],
                        article.scraper_version,
                    )
                else:
                    result = await self.scraper.scrape(source, article.canonical_url, client, self.settings)
                await asyncio.to_thread(self.prepare_article, article.id, result)
            except Exception as exc:
                db.rollback()
                db.expire_all()
                article = db.get(SourceArticle, article_id)
                article.scrape_status, article.scrape_error = (
                    ("ready" if isinstance(exc, MissingModel) else "failed"),
                    str(exc)[:2000],
                )
                if isinstance(exc, MissingModel):
                    en = next(t for t in article.event.translations if t.language == "en")
                    en.status, en.error = "missing_model", str(exc)
                if article.event.status not in PROTECTED and not article.event.reviewed:
                    article.event.status, article.event.editorial_note = (
                        ("TRANSLATING" if isinstance(exc, MissingModel) else "FAILED"),
                        str(exc)[:2000],
                    )
                db.commit()

    def prepare_article(self, article_id, result):
        with self.sessions() as db:
            editorial_lock(db)
            article = db.get(SourceArticle, article_id)
            source, event = article.source, article.event
            if not source.enabled:
                raise ValueError("Source disabled")
            # A rejection/approval during network IO must never be overwritten.
            article.title, article.subtitle, article.language = result.title, result.subtitle, result.language
            article.original_html, article.sanitized_html = result.original_html, result.sanitized_html
            article.plain_text, article.featured_image = result.plain_text, result.featured_image
            article.scraped_at, article.scraper_version = utcnow(), result.version
            article.content_hash = hashlib.sha256(result.plain_text.encode()).hexdigest()
            article.rights_snapshot = {
                "publication_mode": source.publication_mode,
                "allow_media": source.allow_media,
                "allow_embeds": source.allow_embeds,
                "excerpt_limit": source.excerpt_limit,
                "policy_note": source.policy_note,
            }
            article.assets = [ArticleAsset(**a, permitted=source.allow_media) for a in result.assets]
            article.embeds = [ArticleEmbed(**a, permitted=source.allow_embeds) for a in result.embeds]
            if not event.image_url:
                event.image_url = result.featured_image
                event.image_credit = next(
                    (a.credit or a.caption for a in article.assets if a.url == event.image_url), None
                )
            article.scrape_status, article.scrape_error = "ready", None
            if event.status in PROTECTED or event.reviewed:
                db.commit()
                return
            manual_en = next((t for t in event.translations if t.language == "en" and t.manual_edit), None)
            if manual_en:
                event.image_url = result.featured_image
                for t in event.translations:
                    if t.manual_edit:
                        t.content_html = sanitize_html(t.content_html or text_html(t.body))
                        t.body = content_text(t.content_html)
                from app.services.translation import enqueue_translations

                enqueue_translations(db, event)
                refresh_readiness(event)
                db.commit()
                return
            event.status = "PROCESSING"
            db.commit()
            editorial_lock(db)
            # The transaction boundary preserves extracted evidence on translation
            # errors; re-read editorial decisions made while that lock was released.
            event = db.get(NewsEvent, event.id, populate_existing=True)
            source = db.get(Source, source.id, populate_existing=True)
            if event.status in PROTECTED or event.reviewed:
                return
            if not source.enabled:
                raise ValueError("Source disabled")
            article.english_title = (
                result.title
                if result.language == "en"
                else self.translator.translate_text(result.title, result.language, "en")
            )
            english_text = (
                result.plain_text
                if result.language == "en"
                else self.translator.translate_text(result.plain_text, result.language, "en")
            )
            c = classify(article.english_title, article.raw_data.get("tags", []), source.key, english_text)
            works = self.dedup.matching_works(db, article.english_title + " " + english_text)
            # Second deduplication uses complete content and event facts before body translation.
            duplicate = db.scalar(
                select(SourceArticle).where(
                    SourceArticle.content_hash == article.content_hash,
                    SourceArticle.id != article.id,
                    SourceArticle.event_id != event.id,
                )
            )
            from app.collectors.base import Article

            candidate = Article(
                article.guid,
                article.canonical_url,
                article.english_title,
                english_text,
                "en",
                article.published_at,
            )
            decision = self.dedup.decide(db, candidate, c, works, exclude_id=event.id, processed_only=True)
            if duplicate:
                comparison = self.dedup.compare(
                    article.english_title, c, article.published_at, duplicate.event, works
                )
                if comparison.reason.startswith(("Conflicting", "Different", "Outside")):
                    duplicate = None
            target = (
                duplicate.event
                if duplicate
                else (db.get(NewsEvent, decision.event_id) if decision.outcome == "merge" else None)
            )
            if decision.outcome == "review" and not duplicate:
                db.add(
                    DuplicateReview(
                        event_id=event.id,
                        candidate_id=decision.event_id,
                        score=round(decision.score),
                        reason=decision.reason,
                    )
                )
            if target:
                article.event = target
                db.flush()
                if not event.articles:
                    for review in db.scalars(
                        select(DuplicateReview).where(
                            (DuplicateReview.event_id == event.id)
                            | (DuplicateReview.candidate_id == event.id)
                        )
                    ):
                        db.delete(review)
                    db.delete(event)
                db.commit()
                return
            event.event_type, event.facts, event.works = c.event_type, c.facts, works
            for attr in ("formats", "production_types", "countries", "cultural_categories"):
                setattr(event, attr, getattr(c, attr))
            event.categories = list(db.scalars(select(Category).where(Category.slug.in_(c.categories))))
            english_html = None
            if source.publication_mode == "facts" and article.language != "en":
                english_html = self.translator.translate_html(article.sanitized_html, article.language, "en")
            title, summary, html, language = publication_copy(article, source, event, english_html)
            en = next(t for t in event.translations if t.language == "en")
            if language != "en":
                glossary = list(db.scalars(select(GlossaryEntry).where(GlossaryEntry.language == "en")))
                title, summary = [
                    self.translator.translate_text(v, language, "en", glossary) for v in (title, summary)
                ]
                html = self.translator.translate_html(html, language, "en", glossary)
            if not validate_html(html):
                raise ValueError("Article HTML failed validation")
            en.title, en.summary, en.content_html, en.body = title, summary, html, content_text(html)
            en.status, en.engine, en.error = "ready", "source:" + source.publication_mode, None
            event.canonical_title, event.slug = title, slugify(title)[:220] or "news"
            event.image_url = result.featured_image
            event.image_credit = next(
                (a.credit or a.caption for a in article.assets if a.url == event.image_url), None
            )
            event.status = "TRANSLATING"
            from app.services.translation import enqueue_translations

            enqueue_translations(db, event)
            refresh_readiness(event)
            db.commit()

    async def run_queued(self):
        with self.sessions() as db:
            ids = db.scalars(
                select(IngestionRun.id).where(IngestionRun.status == "queued").order_by(IngestionRun.id)
            ).all()
        async with httpx.AsyncClient(headers={"User-Agent": self.settings.user_agent}) as client:
            # At most one queued run per publisher; different publishers are independent.
            await asyncio.gather(*(self.run_source(i, client) for i in ids))
            await self.process_pending(client)


def queue_source(db, source_id, user_id=None):
    editorial_lock(db)
    active = db.scalar(
        select(IngestionRun).where(
            IngestionRun.source_id == source_id, IngestionRun.status.in_(["queued", "running"])
        )
    )
    if active:
        return active
    run = IngestionRun(source_id=source_id, requested_by=user_id)
    db.add(run)
    db.flush()
    return run
