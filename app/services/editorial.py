from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
from slugify import slugify
from app.db.models import (
    NewsEvent,
    NewsTranslation,
    TranslationJob,
    DuplicateReview,
    AuditLog,
    Category,
    utcnow,
    User,
)
from app.db.session import editorial_lock
from app.services.classification import classify
from app.services.translation import enqueue_translations
from app.services.article_html import sanitize_html, content_text, text_html, validate_html
from app.services.moderation import readiness_errors, refresh_readiness


def audit(db, user_id, action, **details):
    db.add(AuditLog(user_id=user_id, action=action, details=details))


def set_publication(db, event_id, publish, user_id):
    editorial_lock(db)
    event = db.get(NewsEvent, event_id, populate_existing=True)
    if not event:
        raise ValueError("Event not found")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.active or user.role != "admin":
        raise ValueError("Administrator authorization required")
    if publish and event.status == "PUBLISHED":
        return event  # Retried commands do not duplicate timestamps or audit entries.
    if publish:
        errors = readiness_errors(event)
        if event.status != "READY_FOR_REVIEW" or errors:
            raise ValueError("Publication requires READY_FOR_REVIEW: " + "; ".join(errors))
        event.status, event.reviewed = "APPROVED", True
        event.approved_by, event.approved_at = user.id, utcnow()
        audit(db, user_id, "approve", event_id=event_id)
        db.flush()
        event.status, event.published_at = "PUBLISHED", utcnow()
    else:
        event.status, event.reviewed = "READY_FOR_REVIEW", False
        event.approved_by, event.approved_at = None, None
        refresh_readiness(event)
    audit(db, user_id, "publish" if publish else "unpublish", event_id=event_id)
    return event


def reject_event(db, event_id, user_id):
    editorial_lock(db)
    event = db.get(NewsEvent, event_id, populate_existing=True)
    if not event:
        raise ValueError("Event not found")
    event.status, event.reviewed = "REJECTED", False
    event.approved_by, event.approved_at = None, None
    audit(db, user_id, "reject", event_id=event_id)


def retry_event(db, event_id, user_id):
    editorial_lock(db)
    event = db.get(NewsEvent, event_id, populate_existing=True)
    if not event:
        raise ValueError("Event not found")
    if event.status in ("APPROVED", "PUBLISHED") or any(t.manual_edit for t in event.translations):
        raise ValueError("Unpublish first; manually edited versions are protected from automatic replacement")
    if any(a.scrape_status == "scraping" for a in event.articles):
        raise ValueError("Article processing is already running")
    event.status, event.reviewed = "DISCOVERED", False
    event.editorial_note = "Повторная обработка поставлена в очередь."
    for article in event.articles:
        article.scrape_status, article.scrape_error = "pending", None
    for translation in event.translations:
        translation.status, translation.error = "pending", None
    audit(db, user_id, "retry_article", event_id=event_id)


def edit_translation(db, event_id, language, title, summary, body, reviewed, user_id):
    editorial_lock(db)
    if language not in ("en", "ru", "ky"):
        raise ValueError("Unsupported language")
    event = db.get(NewsEvent, event_id, populate_existing=True)
    if not event:
        raise ValueError("Event not found")
    if not all(x.strip() for x in (title, summary, body)):
        raise ValueError("Title, summary and body are required")
    t = db.scalar(
        select(NewsTranslation).where(
            NewsTranslation.event_id == event_id, NewsTranslation.language == language
        )
    )
    if not t:
        t = NewsTranslation(event=event, language=language)
        db.add(t)
    html = sanitize_html(body if "<" in body else text_html(body))
    if not validate_html(html):
        raise ValueError("Article HTML must contain readable content")
    changed = (t.title, t.summary, t.content_html) != (title.strip(), summary.strip(), html)
    t.content_html = html
    body = content_text(html)
    t.title, t.summary, t.body = title.strip(), summary.strip(), body.strip()
    t.manual_edit, t.status, t.engine, t.error = True, "ready", "editor", None
    if language == "en":
        event.canonical_title = t.title
        event.slug = slugify(t.title)[:220] or "news"
        event.reviewed = False
        if changed:
            for other in event.translations:
                if other.language != "en":
                    # Preserve editorial work but flag it for re-verification after source edits.
                    other.status = "needs_review" if other.manual_edit else "pending"
                    other.error = "English version changed; verify this translation"
        db.flush()
        enqueue_translations(db, event)
    if changed:
        event.reviewed, event.status = False, "TRANSLATING"
        event.approved_by, event.approved_at = None, None
    refresh_readiness(event)
    audit(db, user_id, "edit_translation", event_id=event_id, language=language)
    return t


def merge_events(db, target_id, source_id, user_id):
    if target_id == source_id:
        raise ValueError("Choose two different events")
    editorial_lock(db)
    events = list(
        db.scalars(
            select(NewsEvent)
            .where(NewsEvent.id.in_([target_id, source_id]))
            .options(selectinload(NewsEvent.articles), selectinload(NewsEvent.translations))
        )
    )
    by_id = {e.id: e for e in events}
    if len(by_id) != 2:
        raise ValueError("Event not found")
    target, source = by_id[target_id], by_id[source_id]
    for article in list(source.articles):
        article.event = target
    target.works = list({w.id: w for w in [*target.works, *source.works]}.values())
    target.categories = list({c.id: c for c in [*target.categories, *source.categories]}.values())
    # Save every losing editorial version in the audit before removal.
    audit(
        db,
        user_id,
        "merge",
        target_id=target_id,
        removed_id=source_id,
        archived_translations=[
            {
                "language": t.language,
                "title": t.title,
                "summary": t.summary,
                "body": t.body,
                "content_html": t.content_html,
            }
            for t in source.translations
        ],
    )
    for review in db.scalars(
        select(DuplicateReview).where(
            or_(
                DuplicateReview.event_id.in_([source_id, target_id]),
                DuplicateReview.candidate_id.in_([source_id, target_id]),
            )
        )
    ):
        db.delete(review)
    for job in db.scalars(select(TranslationJob).where(TranslationJob.event_id == source_id)):
        db.delete(job)
    db.flush()
    db.delete(source)
    target.reviewed, target.status = False, "TRANSLATING"
    target.approved_by, target.approved_at = None, None
    refresh_readiness(target)
    target.editorial_note = "Sources merged by editor. Recheck the factual brief before republishing."
    db.flush()
    return target


def split_event(db, event_id, article_ids, user_id):
    editorial_lock(db)
    event = db.get(NewsEvent, event_id, populate_existing=True)
    if not event:
        raise ValueError("Event not found")
    selected = [a for a in event.articles if a.id in set(article_ids)]
    if not selected or len(selected) == len(event.articles) or len(selected) != len(set(article_ids)):
        raise ValueError("Select some, but not all, sources belonging to this event")
    first = selected[0]
    title = first.english_title or first.title
    c = classify(first.english_title or "", first.raw_data.get("tags", []), first.source.key)
    new = NewsEvent(
        canonical_title=title,
        slug=slugify(title)[:220] or "news",
        event_type=c.event_type,
        formats=c.formats,
        production_types=c.production_types,
        countries=c.countries,
        cultural_categories=c.cultural_categories,
        facts=c.facts,
        created_at=first.published_at,
        categories=list(db.scalars(select(Category).where(Category.slug.in_(c.categories)))),
    )
    db.add(new)
    db.flush()
    for article in selected:
        article.event = new
        article.scrape_status = "pending"
    for lang in ("en", "ru", "ky"):
        db.add(
            NewsTranslation(
                event=new,
                language=lang,
                title=title if lang == "en" else "",
                status="needs_review" if lang == "en" else "pending",
            )
        )
    event.reviewed, event.status = False, "TRANSLATING"
    event.editorial_note = "Sources split by editor. Recheck the remaining brief."
    audit(db, user_id, "split", event_id=event_id, new_event_id=new.id, article_ids=article_ids)
    return new
