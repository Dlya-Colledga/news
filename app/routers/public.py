from datetime import date, datetime, time, timedelta, timezone
from math import ceil
from markupsafe import Markup
from app.services.article_html import sanitize_html, text_html
from pathlib import Path
from urllib.parse import urlencode
from xml.sax.saxutils import escape
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, or_, func
from sqlalchemy.orm import selectinload
from app.core.config import get_settings
from app.core.i18n import MESSAGES, LANGUAGES
from app.db.models import NewsEvent, NewsTranslation, SourceArticle, Work, WorkAlias, Category
from app.db.seed import CATEGORIES
from app.db.session import get_db
from app.services.classification import EVENT_TYPES

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
templates.env.filters["article_html"] = lambda value: Markup(sanitize_html(value))
templates.env.filters["text_html"] = text_html


def local(event, lang):
    return next((t for t in event.translations if t.language == lang and t.status == "ready"), None)


def event_url(event, lang):
    return f"/{lang}/news/{event.id}-{event.slug}"


def work_url(work, lang):
    return f"/{lang}/title/{work.id}-{work.slug}"


def public_query(lang):
    return select(NewsEvent).where(
        NewsEvent.status == "PUBLISHED",
        NewsEvent.translations.any((NewsTranslation.language == lang) & (NewsTranslation.status == "ready")),
    )


def eager(stmt):
    return stmt.options(
        selectinload(NewsEvent.translations),
        selectinload(NewsEvent.categories),
        selectinload(NewsEvent.articles).selectinload(SourceArticle.source),
        selectinload(NewsEvent.works),
    )


def render(request, template, lang="en", status_code=200, **context):
    if lang not in LANGUAGES:
        raise HTTPException(404)
    base = get_settings().base_url.rstrip("/")
    path = request.url.path
    parts = path.split("/")
    links = {}
    for code in LANGUAGES:
        alternate = list(parts)
        if len(alternate) > 1 and alternate[1] in LANGUAGES:
            alternate[1] = code
        else:
            alternate = ["", code, ""]
        query = dict(request.query_params)
        query.pop("content_language", None)
        links[code] = "/".join(alternate) + ("?" + urlencode(query) if query else "")
    context.setdefault("alternates", links)
    context.setdefault("switch_links", links)
    context.setdefault(
        "canonical",
        base
        + path
        + (
            "?page=" + str(request.query_params["page"])
            if request.query_params.get("page", "1") != "1"
            else ""
        ),
    )
    context.setdefault("noindex", False)
    context.setdefault("description", MESSAGES[lang]["about_text"])
    context.setdefault("title", MESSAGES[lang]["tagline"])
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "lang": lang,
            "ui": MESSAGES[lang],
            "languages": LANGUAGES,
            "category_slugs": CATEGORIES,
            "base_url": base,
            "local": local,
            "event_url": event_url,
            "work_url": work_url,
            "event_types": EVENT_TYPES,
            **context,
        },
        status_code=status_code,
    )


@router.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ru/", status_code=307)


@router.get("/healthz")
def health(db=Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


@router.get("/robots.txt")
def robots():
    return Response(
        f"User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /*/search/\nSitemap: {get_settings().base_url.rstrip('/')}/sitemap.xml\n",
        media_type="text/plain",
    )


@router.get("/sitemap.xml")
def sitemap(db=Depends(get_db)):
    base = get_settings().base_url.rstrip("/")
    urls = [f"{base}/{lang}/" for lang in LANGUAGES]
    urls += [f"{base}/{lang}/{cat}/" for lang in LANGUAGES for cat in CATEGORIES]
    events = db.scalars(
        select(NewsEvent).where(NewsEvent.status == "PUBLISHED").options(selectinload(NewsEvent.translations))
    ).all()
    urls += [base + event_url(e, t.language) for e in events for t in e.translations if t.status == "ready"]
    works = db.scalars(
        select(Work).where(
            Work.id.in_(select(Work.id).join(NewsEvent.works).where(NewsEvent.status == "PUBLISHED"))
        )
    ).all()
    urls += [base + work_url(w, lang) for w in works for lang in LANGUAGES]
    body = (
        '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    )
    body += "".join(f"<url><loc>{escape(url)}</loc></url>" for url in urls)
    return Response(body + "</urlset>", media_type="application/xml")


@router.get("/{lang}/")
def home(request: Request, lang: str, db=Depends(get_db)):
    if lang not in LANGUAGES:
        raise HTTPException(404)
    events = list(
        db.scalars(
            eager(public_query(lang)).order_by(NewsEvent.published_at.desc(), NewsEvent.id.desc()).limit(13)
        )
    )
    sections = {}
    for cat in CATEGORIES:
        sections[cat] = list(
            db.scalars(
                eager(public_query(lang).where(NewsEvent.categories.any(Category.slug == cat)))
                .order_by(NewsEvent.published_at.desc())
                .limit(3)
            )
        )
    return render(request, "home.html", lang, events=events, sections=sections)


def listing(
    request,
    db,
    lang,
    category=None,
    q="",
    page=1,
    event_type="",
    start=None,
    end=None,
    sort="newest",
    content_language="",
):
    if lang not in LANGUAGES or (category and category not in CATEGORIES):
        raise HTTPException(404)
    if event_type and event_type not in EVENT_TYPES or sort not in ("newest", "oldest"):
        raise HTTPException(422, "Invalid filter")
    if start and end and start > end:
        raise HTTPException(422, "Start date must precede end date")
    content_lang = content_language or lang
    if content_lang not in LANGUAGES:
        raise HTTPException(422, "Invalid content language")
    stmt = public_query(content_lang)
    if category:
        stmt = stmt.where(NewsEvent.categories.any(Category.slug == category))
    if event_type:
        stmt = stmt.where(NewsEvent.event_type == event_type)
    if start:
        stmt = stmt.where(NewsEvent.published_at >= datetime.combine(start, time.min, timezone.utc))
    if end:
        stmt = stmt.where(
            NewsEvent.published_at < datetime.combine(end + timedelta(days=1), time.min, timezone.utc)
        )
    if q.strip():
        pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            or_(
                NewsEvent.translations.any(
                    (NewsTranslation.language == content_lang)
                    & (NewsTranslation.status == "ready")
                    & or_(
                        NewsTranslation.title.ilike(pattern, escape="\\"),
                        NewsTranslation.summary.ilike(pattern, escape="\\"),
                    )
                ),
                NewsEvent.works.any(
                    or_(
                        Work.title.ilike(pattern, escape="\\"),
                        Work.original_title.ilike(pattern, escape="\\"),
                        Work.aliases.any(WorkAlias.name.ilike(pattern, escape="\\")),
                    )
                ),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, ceil(total / get_settings().page_size))
    if page > pages:
        raise HTTPException(404)
    stmt = stmt.order_by(
        NewsEvent.published_at.asc() if sort == "oldest" else NewsEvent.published_at.desc(), NewsEvent.id
    )
    events = list(
        db.scalars(eager(stmt).offset((page - 1) * get_settings().page_size).limit(get_settings().page_size))
    )

    def page_url(n):
        args = dict(request.query_params)
        args["page"] = n
        return request.url.path + "?" + urlencode(args)

    return render(
        request,
        "listing.html",
        lang,
        title=MESSAGES[lang][category or "search_title"],
        events=events,
        content_lang=content_lang,
        category=category,
        q=q,
        total=total,
        page=page,
        pages=pages,
        page_url=page_url,
        filters={"event_type": event_type, "start": start, "end": end, "sort": sort},
        searching="/search/" in request.url.path,
        noindex="/search/" in request.url.path or bool(request.query_params),
    )


@router.get("/{lang}/search/")
def search(
    request: Request,
    lang: str,
    q: str = Query("", max_length=200),
    category: str = "",
    content_language: str = "",
    page: int = Query(1, ge=1),
    db=Depends(get_db),
):
    return listing(request, db, lang, category or None, q=q, page=page, content_language=content_language)


@router.get("/{lang}/news/{event_id:int}-{slug}")
def news(request: Request, lang: str, event_id: int, slug: str, db=Depends(get_db)):
    if lang not in LANGUAGES:
        raise HTTPException(404)
    event = db.scalar(
        eager(select(NewsEvent).where(NewsEvent.id == event_id, NewsEvent.status == "PUBLISHED"))
    )
    if not event:
        raise HTTPException(404)
    alternates = {t.language: event_url(event, t.language) for t in event.translations if t.status == "ready"}
    t = local(event, lang)
    if not t:
        return render(
            request,
            "error.html",
            lang,
            status_code=404,
            title=MESSAGES[lang]["not_found"],
            error_code=404,
            alternates={},
            available=alternates,
            noindex=True,
        )
    if slug != event.slug:
        return RedirectResponse(event_url(event, lang), status_code=308)
    related = list(
        db.scalars(
            eager(
                public_query(lang).where(
                    NewsEvent.id != event.id,
                    or_(
                        NewsEvent.works.any(Work.id.in_([w.id for w in event.works])),
                        NewsEvent.categories.any(Category.id.in_([c.id for c in event.categories])),
                    ),
                )
            )
            .order_by(NewsEvent.published_at.desc())
            .limit(3)
        )
    )
    return render(
        request,
        "news.html",
        lang,
        event=event,
        translation=t,
        related=related,
        title=t.title,
        description=t.summary,
        alternates=alternates,
        og_type="article",
    )


@router.get("/{lang}/title/{work_id:int}-{slug}")
def title_page(
    request: Request, lang: str, work_id: int, slug: str, page: int = Query(1, ge=1), db=Depends(get_db)
):
    if lang not in LANGUAGES:
        raise HTTPException(404)
    work = db.scalar(select(Work).where(Work.id == work_id).options(selectinload(Work.aliases)))
    if not work:
        raise HTTPException(404)
    if work.slug != slug:
        return RedirectResponse(work_url(work, lang), status_code=308)
    stmt = public_query(lang).where(NewsEvent.works.any(Work.id == work_id))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, ceil(total / get_settings().page_size))
    if page > pages:
        raise HTTPException(404)
    events = list(
        db.scalars(
            eager(stmt)
            .order_by(NewsEvent.published_at.desc())
            .offset((page - 1) * get_settings().page_size)
            .limit(get_settings().page_size)
        )
    )
    return render(
        request,
        "work.html",
        lang,
        work=work,
        events=events,
        title=work.title,
        page=page,
        pages=pages,
        page_url=lambda n: work_url(work, lang) + f"?page={n}",
    )


@router.get("/{lang}/{category}/")
def category_page(
    request: Request,
    lang: str,
    category: str,
    page: int = Query(1, ge=1),
    event_type: str = "",
    start: str = "",
    end: str = "",
    sort: str = "newest",
    db=Depends(get_db),
):
    try:
        start_date, end_date = (
            date.fromisoformat(start) if start else None,
            date.fromisoformat(end) if end else None,
        )
    except ValueError:
        raise HTTPException(422, "Dates must use YYYY-MM-DD")
    return listing(
        request,
        db,
        lang,
        category,
        page=page,
        event_type=event_type,
        start=start_date,
        end=end_date,
        sort=sort,
    )
