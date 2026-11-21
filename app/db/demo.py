"""Opt-in synthetic newsroom data for local course demonstrations only.

Nothing here is scraped or derived from a real publication. This module is not
called by migrations, the normal seed command or the ingestion scheduler.
"""
from html import escape
from sqlalchemy import select
from app.core.config import get_settings
from app.db.models import Category, NewsEvent, NewsTranslation, Source, SourceArticle, utcnow
from app.services.article_html import content_text
from app.services.editorial import set_publication

EXAMPLES = (
    {
        "slug": "film-trailer", "category": "movies", "format": "film", "event_type": "trailer", "publish": True,
        "en": ("Demo: imaginary film trailer", "A fictional announcement for a classroom demonstration.", "A completely imaginary film has an imaginary trailer. This is classroom sample content, not real news."),
        "ru": ("Демо: трейлер вымышленного фильма", "Вымышленный анонс для демонстрации курсовой.", "У полностью вымышленного фильма появился вымышленный трейлер. Это учебный пример, а не настоящая новость."),
        "ky": ("Демо: ойдон чыгарылган тасманын трейлери", "Курстук ишти көрсөтүү үчүн ойдон чыгарылган билдирүү.", "Толугу менен ойдон чыгарылган тасманын трейлери жарыяланды. Бул чыныгы жаңылык эмес, окуу үлгүсү."),
    },
    {
        "slug": "series-announcement", "category": "series", "format": "series", "event_type": "announcement", "publish": True,
        "en": ("Demo: fictional television series announced", "Another original sample news article.", "An imaginary television series was announced for demonstration purposes only. No actual project or date is claimed."),
        "ru": ("Демо: анонс вымышленного сериала", "Ещё одна оригинальная учебная публикация.", "Вымышленный сериал анонсирован исключительно для демонстрации проекта. Реального проекта и даты выхода не существует."),
        "ky": ("Демо: ойдон чыгарылган сериал жарыяланды", "Дагы бир окуу жаңылыгынын мисалы.", "Ойдон чыгарылган сериал долбоорду көрсөтүү үчүн гана жарыяланды. Чыныгы долбоор же чыгуу күнү жок."),
    },
    {
        "slug": "anime-review", "category": "anime", "format": "series", "event_type": "announcement", "publish": False,
        "en": ("Demo: fictional anime awaits editorial review", "An unpublished sample to showcase the moderation queue.", "This imaginary anime article is intentionally unpublished. The administrator may approve it during a classroom demo."),
        "ru": ("Демо: вымышленное аниме ожидает модерации", "Черновик для показа редакционной очереди.", "Эта вымышленная новость об аниме пока не опубликована. Администратор может одобрить её во время защиты."),
        "ky": ("Демо: ойдон чыгарылган аниме текшерүүнү күтөт", "Редакциялык кезекти көрсөтүү үчүн даярдалган үлгү.", "Бул ойдон чыгарылган аниме жөнүндөгү материал азырынча жарыялана элек. Окуу көрсөтүүсүндө аны бекитсе болот."),
    },
)


def seed_demo(db, admin):
    """Insert three idempotent, clearly labeled examples; never run in production."""
    if get_settings().environment == "production":
        raise ValueError("Demo news cannot be seeded in production")
    if admin is None or not admin.active or admin.role != "admin":
        raise ValueError("An active administrator is required to seed demo news")
    source = db.scalar(select(Source).where(Source.key == "frame_demo"))
    if source is None:
        source = Source(
            key="frame_demo", name="FRAME — fictional classroom examples",
            site_url="https://example.com", feed_url="https://example.com/feed",
            enabled=False, access_status="local_fixture",
            publication_mode="full", allow_media=False, allow_embeds=False,
            policy_note="Original fictional classroom examples; not publisher content",
        )
        db.add(source)
        db.flush()
    created = 0
    for item in EXAMPLES:
        url = f"https://example.com/frame-news-demo/{item['slug']}"
        if db.scalar(select(SourceArticle.id).where(SourceArticle.canonical_url == url)) is not None:
            continue
        category = db.scalar(select(Category).where(Category.slug == item["category"]))
        if category is None:
            category = Category(slug=item["category"])
            db.add(category)
        event = NewsEvent(
            slug="demo-" + item["slug"], canonical_title=item["en"][0],
            event_type=item["event_type"], formats=[item["format"]],
            status="READY_FOR_REVIEW", categories=[category],
            editorial_note="Synthetic classroom fixture: fictional, not real news",
        )
        db.add(event)
        en_body = item["en"][2]
        en_html = "<p>" + escape(en_body) + "</p>"
        db.add(SourceArticle(
            event=event, source=source, guid="demo-" + item["slug"], canonical_url=url,
            title=item["en"][0], excerpt=item["en"][1], language="en",
            original_html=en_html, sanitized_html=en_html, plain_text=en_body,
            scrape_status="ready", scraped_at=utcnow(),
            rights_snapshot={"publication_mode": "full", "allow_media": False, "allow_embeds": False, "excerpt_limit": 0},
        ))
        for lang in ("en", "ru", "ky"):
            title, summary, body = item[lang]
            article_html = "<p>" + escape(body) + "</p>"
            db.add(NewsTranslation(
                event=event, language=lang, title=title, summary=summary,
                content_html=article_html, body=content_text(article_html),
                status="ready", engine="synthetic-demo", manual_edit=True,
            ))
        db.flush()
        if item["publish"]:
            set_publication(db, event.id, True, admin.id)
        created += 1
    return created
