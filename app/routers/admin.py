from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from slugify import slugify
from app.core.security import csrf_token, verify_csrf, current_user, require_user, authenticate
from app.db.session import get_db, editorial_lock
from app.db.models import (
    Source,
    IngestionRun,
    NewsEvent,
    TranslationJob,
    DuplicateReview,
    GlossaryEntry,
    Work,
    WorkAlias,
    Category,
    AuditLog,
)
from app.db.seed import CATEGORIES
from app.services.classification import EVENT_TYPES
from app.services.ingestion import queue_source
from app.services.editorial import (
    edit_translation,
    set_publication,
    merge_events,
    split_event,
    audit,
    reject_event,
    retry_event,
)
from app.services.moderation import STATUSES, readiness_errors
from app.routers.public import render
from app.routers.public import templates, eager

router = APIRouter(prefix="/admin")


def page(request, name, **context):
    return templates.TemplateResponse(
        request=request,
        name="admin/" + name,
        context={
            "csrf": csrf_token(request),
            "event_types": EVENT_TYPES,
            "category_slugs": CATEGORIES,
            **context,
        },
    )


async def form_data(request: Request, db=Depends(get_db)):
    if request.url.path != "/admin/login":
        await run_in_threadpool(require_user, request, db)
    form = await request.form(max_fields=150, max_files=0)
    verify_csrf(request, form.get("csrf"))
    if any(len(str(v)) > 600_000 for v in form.values()):
        raise HTTPException(413, "Field exceeds size limit")
    return form


def redirect(path="/admin/", message="Saved"):
    from urllib.parse import urlencode

    return RedirectResponse(path + "?" + urlencode({"message": message}), status_code=303)


@router.get("/login")
def login_page(request: Request, db=Depends(get_db)):
    if current_user(request, db):
        return redirect()
    return page(request, "login.html", error=None)


@router.post("/login")
def login(request: Request, db=Depends(get_db), form=Depends(form_data)):
    user = authenticate(
        db, str(form.get("username", ""))[:120], str(form.get("password", "")), request.client.host
    )
    if not user:
        response = page(request, "login.html", error="Неверное имя пользователя или пароль")
        response.status_code = 401
        return response
    request.session.clear()
    request.session.update(user_id=user.id, version=user.session_version)
    csrf_token(request)
    return redirect(message="Вы вошли в редакцию")


@router.post("/logout")
def logout(request: Request, db=Depends(get_db), form=Depends(form_data)):
    require_user(request, db)
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/")
def dashboard(
    request: Request, status: str = "READY_FOR_REVIEW", page_number: int = Query(1, ge=1), db=Depends(get_db)
):
    user = require_user(request, db)
    if status not in (*STATUSES, "all"):
        raise HTTPException(422, "Unknown status")
    stmt = select(NewsEvent)
    if status != "all":
        stmt = stmt.where(NewsEvent.status == status)
    count = db.scalar(select(func.count()).select_from(stmt.subquery()))
    events = db.scalars(
        eager(stmt).order_by(NewsEvent.created_at.desc()).offset((page_number - 1) * 30).limit(30)
    ).all()
    stats = {
        s: db.scalar(select(func.count()).select_from(NewsEvent).where(NewsEvent.status == s))
        for s in STATUSES
    }
    runs = db.scalars(
        select(IngestionRun)
        .options(selectinload(IngestionRun.source))
        .order_by(IngestionRun.id.desc())
        .limit(15)
    ).all()
    reviews = db.scalars(
        select(DuplicateReview)
        .where(DuplicateReview.resolved.is_(False))
        .order_by(DuplicateReview.id.desc())
        .limit(40)
    ).all()
    event_ids = [event.id for event in events]
    duplicate_pairs = db.execute(
        select(DuplicateReview.event_id, DuplicateReview.candidate_id).where(
            DuplicateReview.resolved.is_(False),
            (DuplicateReview.event_id.in_(event_ids)) | (DuplicateReview.candidate_id.in_(event_ids)),
        )
    ).all()
    return page(
        request,
        "dashboard.html",
        user=user,
        events=events,
        stats=stats,
        runs=runs,
        reviews=reviews,
        status=status,
        page_number=page_number,
        total=count,
        statuses=STATUSES,
        duplicate_ids={identifier for pair in duplicate_pairs for identifier in pair},
    )


@router.post("/approve-all")
def approve_all(request: Request, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db, admin=True)
    editorial_lock(db)
    events = db.scalars(eager(select(NewsEvent).where(NewsEvent.status == "READY_FOR_REVIEW"))).all()
    approved = 0
    for event in events:
        if readiness_errors(event):
            continue
        set_publication(db, event.id, True, user.id)
        approved += 1
    db.commit()
    return redirect("/admin/", f"Опубликовано: {approved}.")


@router.get("/sources")
def sources(request: Request, db=Depends(get_db)):
    user = require_user(request, db)
    sources = db.scalars(select(Source).order_by(Source.id)).all()
    return page(request, "sources.html", user=user, sources=sources)


@router.post("/sources/{source_id}/toggle")
def toggle_source(request: Request, source_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db, admin=True)
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404)
    source.enabled = not source.enabled
    if source.enabled:
        source.access_status = "public_access"
    audit(db, user.id, "toggle_source", source_id=source.id, enabled=source.enabled)
    db.commit()
    return redirect("/admin/sources")


@router.post("/sources/{source_id}/import")
def import_source(request: Request, source_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404)
    if not source.enabled:
        raise HTTPException(422, "Source is disabled")
    queue_source(db, source_id, user.id)
    db.commit()
    return redirect("/admin/sources", "Импорт в очереди. Его выполнит отдельный worker.")


@router.get("/events/{event_id}")
def event_page(request: Request, event_id: int, db=Depends(get_db)):
    user = require_user(request, db)
    event = db.scalar(eager(select(NewsEvent).where(NewsEvent.id == event_id)))
    if not event:
        raise HTTPException(404)
    jobs = db.scalars(select(TranslationJob).where(TranslationJob.event_id == event.id)).all()
    works = db.scalars(select(Work).order_by(Work.title)).all()
    return page(
        request,
        "event.html",
        user=user,
        event=event,
        jobs=jobs,
        works=works,
        readiness_errors=readiness_errors(event),
    )


@router.post("/events/{event_id}/translation/{language}")
def save_translation(
    request: Request, event_id: int, language: str, db=Depends(get_db), form=Depends(form_data)
):
    user = require_user(request, db)
    try:
        edit_translation(
            db,
            event_id,
            language,
            str(form.get("title", "")),
            str(form.get("summary", "")),
            str(form.get("body", "")),
            form.get("reviewed") == "yes",
            user.id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    return redirect(f"/admin/events/{event_id}", "Текст сохранён; ручные правки защищены от перезаписи")


@router.post("/events/{event_id}/publication")
def publication(request: Request, event_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db, admin=True)
    if form.get("action") not in ("publish", "unpublish"):
        raise HTTPException(422)
    try:
        set_publication(db, event_id, form["action"] == "publish", user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return redirect(f"/admin/events/{event_id}", str(exc))
    return redirect(f"/admin/events/{event_id}")


@router.post("/events/{event_id}/classification")
def classification(request: Request, event_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    editorial_lock(db)
    event = db.get(NewsEvent, event_id)
    if not event:
        raise HTTPException(404)
    if form.get("event_type") not in EVENT_TYPES:
        raise HTTPException(422)
    event.event_type = form["event_type"]
    cats = form.getlist("categories")
    if not set(cats) <= set(CATEGORIES):
        raise HTTPException(422)
    event.categories = list(db.scalars(select(Category).where(Category.slug.in_(cats))))
    event.formats = [v for v in form.getlist("formats") if v in ("film", "series")]
    event.production_types = [
        v for v in form.getlist("production_types") if v in ("animation", "live_action")
    ]
    event.countries = [v.strip().upper() for v in str(form.get("countries", "")).split(",") if v.strip()]
    event.cultural_categories = [v.strip() for v in str(form.get("culture", "")).split(",") if v.strip()]
    try:
        ids = [int(v) for v in form.getlist("works")]
    except ValueError:
        raise HTTPException(422)
    event.works = list(db.scalars(select(Work).where(Work.id.in_(ids))))
    audit(db, user.id, "classify", event_id=event.id)
    db.commit()
    return redirect(f"/admin/events/{event_id}")


@router.post("/events/{event_id}/merge")
def merge(request: Request, event_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    try:
        merge_events(db, event_id, int(form.get("source_id", "0")), user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    return redirect(f"/admin/events/{event_id}", "События объединены и возвращены на проверку")


@router.post("/events/{event_id}/split")
def split(request: Request, event_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    try:
        new = split_event(db, event_id, [int(v) for v in form.getlist("article_ids")], user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    return redirect(f"/admin/events/{new.id}", "Создано отдельное событие для выбранных источников")


@router.post("/reviews/{review_id}/dismiss")
def dismiss_review(request: Request, review_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    review = db.get(DuplicateReview, review_id)
    if not review:
        raise HTTPException(404)
    review.resolved = True
    audit(db, user.id, "dismiss_duplicate", review_id=review_id)
    db.commit()
    return redirect()


@router.get("/translations")
def translations(request: Request, db=Depends(get_db)):
    user = require_user(request, db)
    jobs = db.scalars(select(TranslationJob).order_by(TranslationJob.id.desc()).limit(100)).all()
    glossary = db.scalars(select(GlossaryEntry).order_by(GlossaryEntry.term)).all()
    return page(request, "translations.html", user=user, jobs=jobs, glossary=glossary)


@router.post("/jobs/{job_id}/retry")
def retry(request: Request, job_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    editorial_lock(db)
    job = db.get(TranslationJob, job_id)
    if not job:
        raise HTTPException(404)
    if job.status != "failed":
        raise HTTPException(422, "Only failed jobs can be retried")
    job.status, job.error = "pending", None
    audit(db, user.id, "retry_translation", job_id=job.id)
    db.commit()
    return redirect("/admin/translations")


@router.post("/glossary")
def glossary_save(request: Request, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    term, lang, replacement = (
        str(form.get("term", "")).strip(),
        form.get("language"),
        str(form.get("replacement", "")).strip(),
    )
    if (
        not term
        or not replacement
        or len(term) > 200
        or len(replacement) > 240
        or lang not in ("en", "ru", "ky")
    ):
        raise HTTPException(422, "Invalid glossary entry")
    editorial_lock(db)
    entry = db.scalar(select(GlossaryEntry).where(GlossaryEntry.term == term, GlossaryEntry.language == lang))
    if not entry:
        entry = GlossaryEntry(term=term, language=lang)
        db.add(entry)
    entry.replacement = replacement
    audit(db, user.id, "glossary", term=term, language=lang)
    db.commit()
    return redirect("/admin/translations")


@router.post("/glossary/{entry_id}/delete")
def glossary_delete(request: Request, entry_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    entry = db.get(GlossaryEntry, entry_id)
    if not entry:
        raise HTTPException(404)
    db.delete(entry)
    audit(db, user.id, "delete_glossary", entry_id=entry_id)
    db.commit()
    return redirect("/admin/translations")


@router.get("/works")
def works_page(request: Request, db=Depends(get_db)):
    user = require_user(request, db)
    works = db.scalars(select(Work).options(selectinload(Work.aliases)).order_by(Work.id.desc())).all()
    return page(request, "works.html", user=user, works=works)


@router.post("/works")
def create_work(request: Request, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    title, fmt = str(form.get("title", "")).strip(), form.get("format")
    if not title or fmt not in ("film", "series"):
        raise HTTPException(422)
    work = Work(
        title=title,
        slug=slugify(title)[:220] or "title",
        format=fmt,
        original_title=str(form.get("original_title", "")).strip(),
    )
    names = {v.strip() for v in str(form.get("aliases", "")).splitlines() if v.strip()}
    work.aliases = [WorkAlias(name=name) for name in names]
    db.add(work)
    db.flush()
    audit(db, user.id, "create_work", work_id=work.id)
    db.commit()
    return redirect("/admin/works")


@router.get("/audit")
def audit_page(request: Request, db=Depends(get_db)):
    user = require_user(request, db, admin=True)
    logs = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)).all()
    return page(request, "audit.html", user=user, logs=logs)


@router.get("/events/{event_id}/preview/{language}")
def preview(request: Request, event_id: int, language: str, db=Depends(get_db)):
    require_user(request, db)
    event = db.scalar(eager(select(NewsEvent).where(NewsEvent.id == event_id)))
    if not event or language not in ("en", "ru", "ky"):
        raise HTTPException(404)
    translation = next((t for t in event.translations if t.language == language), None)
    if not translation:
        raise HTTPException(404)
    links = {lang: f"/admin/events/{event.id}/preview/{lang}" for lang in ("en", "ru", "ky")}
    return render(
        request,
        "news.html",
        language,
        event=event,
        translation=translation,
        related=[],
        title=translation.title or event.canonical_title,
        description=translation.summary,
        preview=True,
        alternates={},
        switch_links=links,
        preview_links=links,
        noindex=True,
    )


@router.post("/events/{event_id}/reject")
def reject(request: Request, event_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db, admin=True)
    try:
        reject_event(db, event_id, user.id)
        db.commit()
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return redirect(f"/admin/events/{event_id}")


@router.post("/events/{event_id}/retry")
def reprocess(request: Request, event_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db)
    try:
        retry_event(db, event_id, user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    return redirect(f"/admin/events/{event_id}", "Повторная обработка поставлена в очередь")


@router.post("/sources/{source_id}/policy")
def source_policy(request: Request, source_id: int, db=Depends(get_db), form=Depends(form_data)):
    user = require_user(request, db, admin=True)
    editorial_lock(db)
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404)
    mode = form.get("publication_mode")
    note = str(form.get("policy_note", "")).strip()
    try:
        limit = int(form.get("excerpt_limit", "0"))
        selectors = [x.strip() for x in str(form.get("article_selectors", "")).splitlines() if x.strip()]
        import soupsieve

        for selector in selectors:
            soupsieve.compile(selector)
    except Exception:
        raise HTTPException(422, "Invalid excerpt limit or CSS selector")
    if mode not in ("full", "facts") or not 0 <= limit <= 2000 or not note:
        raise HTTPException(422, "Record the rights basis, a valid mode and permitted excerpt length")
    source.publication_mode, source.policy_note, source.excerpt_limit = mode, note, limit
    source.allow_media = form.get("allow_media") == "yes"
    source.allow_embeds = form.get("allow_embeds") == "yes"
    source.article_selectors = selectors
    for event in db.scalars(eager(select(NewsEvent).where(NewsEvent.articles.any(source_id=source.id)))):
        if event.status in ("READY_FOR_REVIEW", "APPROVED", "PUBLISHED") and readiness_errors(event):
            event.status, event.reviewed = "PROCESSING", False
            event.approved_by, event.approved_at = None, None
            event.editorial_note = "Source rights changed; reprocess this article."
    audit(
        db,
        user.id,
        "source_policy",
        source_id=source.id,
        mode=mode,
        note=note,
        allow_media=source.allow_media,
        allow_embeds=source.allow_embeds,
        excerpt_limit=limit,
    )
    db.commit()
    return redirect("/admin/sources")


@router.post("/sources")
def create_source(request: Request, db=Depends(get_db), form=Depends(form_data)):
    from app.services.article_html import safe_url
    from urllib.parse import urlsplit

    user = require_user(request, db, admin=True)
    name = str(form.get("name", "")).strip()
    site = safe_url(form.get("site_url"))
    feed = safe_url(form.get("feed_url"))
    key = slugify(name)[:60]
    if not key or not site or not feed or urlsplit(site).hostname != urlsplit(feed).hostname:
        raise HTTPException(
            422, "Specify a name and public HTTP(S) site/feed URLs on the same publisher host"
        )
    editorial_lock(db)
    if db.scalar(select(Source).where(Source.key == key)):
        raise HTTPException(422, "Source already exists")
    source = Source(
        key=key,
        name=name[:160],
        site_url=site,
        feed_url=feed,
        language=str(form.get("language", "en"))[:12],
        enabled=False,
        access_status="review_required",
        policy_note="Record rights before enabling collection.",
    )
    db.add(source)
    db.flush()
    audit(db, user.id, "create_source", source_id=source.id)
    db.commit()
    return redirect("/admin/sources")
