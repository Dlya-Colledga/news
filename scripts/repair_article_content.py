"""Explicit repair of existing automatic translations and source covers.

Run after a backup: python scripts/repair_article_content.py [--refresh-covers]
Manual translations are preserved. This administrator maintenance operation also
repairs published versions atomically; failed translations return to review.
"""

import argparse
import asyncio

import httpx
from bs4 import NavigableString
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import NewsEvent, ArticleAsset
from app.db.session import SessionLocal, editorial_lock
from app.services.article_html import dom, sanitize_html, content_text, safe_url
from app.services.editorial import audit
from app.services.moderation import refresh_readiness
from app.services.scraping import ArticleScraperService
from app.services.text_normalization import readable_text
from app.services.translation import TranslationService


def clean_html(html):
    document = dom(html)
    for node in list(document.find_all(string=True)):
        node.replace_with(NavigableString(readable_text(str(node))))
    return sanitize_html(str(document))


async def repair(refresh_covers=False):
    settings = get_settings()
    service = TranslationService()
    with SessionLocal() as db:
        ids = list(db.scalars(select(NewsEvent.id).order_by(NewsEvent.id)))
    covers = translations = failures = 0
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}) as client:
        for event_id in ids:
            with SessionLocal() as db:
                editorial_lock(db)
                event = db.get(NewsEvent, event_id)
                canonical = next(
                    (t for t in event.translations if t.language == "en" and t.status == "ready"), None
                )
                for article in event.articles:
                    for attr in ("title", "subtitle", "excerpt", "english_title"):
                        value = getattr(article, attr)
                        if value:
                            setattr(article, attr, readable_text(value))
                    if refresh_covers and canonical and article.source.enabled:
                        try:
                            result = await ArticleScraperService().scrape(
                                article.source, article.canonical_url, client, settings
                            )
                            if result.featured_image:
                                article.featured_image = result.featured_image
                                if not any(a.url == result.featured_image for a in article.assets):
                                    cover = next(
                                        a for a in result.assets if a["url"] == result.featured_image
                                    )
                                    article.assets.append(
                                        ArticleAsset(**cover, permitted=article.source.allow_media)
                                    )
                        except Exception as exc:
                            print(f"Event {event_id}: cover refresh failed: {exc}", flush=True)
                    if not event.image_url and safe_url(article.featured_image):
                        asset = next(
                            (
                                a
                                for a in article.assets
                                if a.kind == "img" and a.url == article.featured_image
                            ),
                            None,
                        )
                        if asset:
                            event.image_url = asset.url
                            event.image_credit = asset.credit or asset.caption or None
                            covers += 1
                if canonical and event.status != "REJECTED":
                    if not canonical.manual_edit:
                        canonical.title = readable_text(canonical.title)
                        canonical.summary = readable_text(canonical.summary)
                        canonical.content_html = clean_html(canonical.content_html)
                        canonical.body = content_text(canonical.content_html)
                        event.canonical_title = canonical.title
                    failed = False
                    for language in ("ru", "ky"):
                        target = next((t for t in event.translations if t.language == language), None)
                        if target and target.manual_edit:
                            continue
                        if target:
                            target.status = "pending"
                        result = service.localize(db, event, language)
                        if result.status == "ready":
                            translations += 1
                        else:
                            failures += 1
                            failed = True
                            print(f"Event {event_id}/{language}: {result.error}", flush=True)
                    if failed and event.status in ("PUBLISHED", "APPROVED"):
                        event.status, event.reviewed = "TRANSLATING", False
                        event.approved_by = event.approved_at = None
                    refresh_readiness(event)
                    audit(db, None, "repair_automatic_translations", event_id=event_id)
                    print(f"Event {event_id}: {event.status}", flush=True)
                db.commit()
    print(f"Repaired covers: {covers}; translations: {translations}; failures: {failures}", flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-covers", action="store_true")
    asyncio.run(repair(parser.parse_args().refresh_covers))
