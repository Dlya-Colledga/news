from datetime import timedelta
from dataclasses import replace
from sqlalchemy import select, func
from app.collectors.base import Article
from app.db.models import (
    Source,
    SourceArticle,
    NewsEvent,
    Work,
    WorkAlias,
    DuplicateReview,
    utcnow,
    IngestionRun,
)
from app.services.ingestion import IngestionService
from app.services.classification import classify
from app.services.deduplication import DeduplicationService
from app.services.scraping import ArticleScraperService
from html import escape


def source(db, key="anime_corner"):
    return db.scalar(select(Source).where(Source.key == key))


def article(title="Fixture Movie Season 3 Officially Confirmed", guid="one", path="one"):
    return Article(guid, f"https://animecorner.me/{path}/", title, published_at=utcnow())


def import_prepared(service, db, source, article):
    source.allow_extraction = source.enabled = True
    source.publication_mode = "full"
    imported = service.import_article(db, source, article)
    db.commit()
    if imported:
        row = db.scalar(select(SourceArticle).where(SourceArticle.canonical_url == article.url))
        html = f"<article><h1>{escape(article.title)}</h1><p>{escape(article.title)}. {escape(article.excerpt)} The studio provided the announcement in its public statement.</p></article>"
        result = ArticleScraperService().extract(html, article.url, source.key)
        service.prepare_article(row.id, result)
        db.expire_all()
    return imported


def test_idempotent_and_exact_identifiers(db, sessions):
    service = IngestionService(sessions)
    a = article()
    assert import_prepared(service, db, source(db), a)
    db.commit()
    assert not import_prepared(service, db, source(db), a)
    assert not service.import_article(
        db, source(db), replace(a, url=a.url + "?utm_source=other", guid="new-guid")
    )
    assert not service.import_article(db, source(db), replace(a, url="https://animecorner.me/changed/"))
    db.commit()
    assert db.scalar(select(func.count()).select_from(SourceArticle)) == 1
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 1


def test_merge_identical_event_from_two_sources(db, sessions):
    service = IngestionService(sessions)
    a = article()
    import_prepared(service, db, source(db), a)
    import_prepared(
        service, db, source(db, "deadline"), replace(a, guid="second", url="https://deadline.com/second/")
    )
    db.commit()
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 1
    assert db.scalar(select(func.count()).select_from(SourceArticle)) == 2


def test_distinct_events_same_work(db, sessions):
    service = IngestionService(sessions)
    titles = [
        "Fixture Movie Season 3 Confirmed",
        "Fixture Movie Season 3 Trailer Released",
        "Fixture Movie Season 3 Release Date Announced",
        "Fixture Movie Season 3 Delayed",
        "Fixture Movie Season 4 Confirmed",
    ]
    for i, title in enumerate(titles):
        import_prepared(service, db, source(db), article(title, str(i), str(i)))
    db.commit()
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 5
    assert all(e.status == "TRANSLATING" for e in db.scalars(select(NewsEvent)))


def test_aliases_and_fact_conflicts(db):
    work = Work(
        title="Wednesday",
        original_title="Wednesday",
        slug="wednesday",
        format="series",
        aliases=[WorkAlias(name="Уэнсдей")],
    )
    db.add(work)
    db.flush()
    svc = DeduplicationService()
    assert svc.matching_works(db, "Уэнсдей Season 3 Confirmed") == [work]
    event = NewsEvent(
        id=42,
        canonical_title="Wednesday Season 3 Confirmed",
        slug="fixture",
        event_type="announcement",
        facts={"season": "3"},
        created_at=utcnow(),
        works=[work],
    )
    result = svc.compare(
        "Wednesday Season 4 Confirmed", classify("Wednesday Season 4 Confirmed"), utcnow(), event, [work]
    )
    assert result.outcome == "separate" and "season" in result.reason
    event.facts = {"regions": ["japan"]}
    c = classify("Wednesday Season Confirmed in UK")
    assert svc.compare("Wednesday Season Confirmed in UK", c, utcnow(), event, [work]).outcome == "separate"
    event.created_at = utcnow() - timedelta(days=30)
    assert (
        svc.compare(event.canonical_title, classify(event.canonical_title), utcnow(), event, [work]).outcome
        == "separate"
    )


def test_uncertain_requires_review(db, sessions):
    svc = IngestionService(sessions)
    import_prepared(svc, db, source(db), article("Fixture Movie announces a brand new season", "1", "1"))
    import_prepared(svc, db, source(db), article("Fixture Movie confirms a brand new season", "2", "2"))
    db.commit()
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 2
    assert db.scalar(select(func.count()).select_from(DuplicateReview)) == 1


def test_multidimensional_classification():
    result = classify("New anime film trailer announced", ["Movie"], "anime_corner")
    assert set(result.categories) >= {"movies", "anime", "animation"}
    assert result.formats == ["film"] and result.event_type == "trailer"
    assert result.production_types == ["animation"] and result.countries == ["JP"]


async def test_source_failure_does_not_stop_others(db, sessions):
    import httpx
    from pathlib import Path
    from app.core.config import Settings
    from app.services.ingestion import queue_source

    anime = source(db)
    deadline = source(db, "deadline")
    deadline.enabled = True
    runs = [queue_source(db, s.id) for s in [anime, deadline]]
    db.commit()

    def response(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if req.url.host == "deadline.com":
            return httpx.Response(403)
        return httpx.Response(200, content=(Path(__file__).parent / "fixtures/feed.xml").read_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        svc = IngestionService(sessions, Settings(request_interval_seconds=0, _env_file=None))
        import asyncio

        await asyncio.gather(*(svc.run_source(r.id, client) for r in runs))
    db.expire_all()
    assert db.get(IngestionRun, runs[0].id).status == "success"
    assert db.get(IngestionRun, runs[1].id).status == "failed"
    assert db.get(Source, deadline.id).last_checked_at is not None


def test_paraphrased_season_announcement_and_fact_brief(db, sessions):
    from app.db.models import NewsTranslation

    db.add(Work(title="Wednesday", original_title="Wednesday", slug="wednesday", format="series"))
    db.flush()
    svc = IngestionService(sessions)
    import_prepared(svc, db, source(db), article("Netflix Announces Wednesday Season 3", "a", "a"))
    import_prepared(
        svc,
        db,
        source(db, "deadline"),
        replace(
            article("Wednesday Season 3 Officially Confirmed", "b", "b"),
            url="https://deadline.com/wednesday/",
        ),
    )
    db.commit()
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 1
    event = db.scalar(select(NewsEvent))
    en = db.scalar(
        select(NewsTranslation).where(NewsTranslation.event_id == event.id, NewsTranslation.language == "en")
    )
    assert en.engine == "source:full" and en.status == "ready"
    assert "season 3" in en.body.lower()
    assert {a.source.name for a in event.articles} == {"Anime Corner", "Deadline"}
    assert event.status == "TRANSLATING"


def test_rumour_and_cancellation_are_not_renewals(db):
    work = Work(id=42, title="Wednesday", slug="wednesday", format="series")
    event = NewsEvent(
        id=42,
        canonical_title="Wednesday Season 3 Confirmed",
        slug="wednesday",
        event_type="announcement",
        facts={"season": "3"},
        created_at=utcnow(),
        works=[work],
    )
    svc = DeduplicationService()
    for title in ["Wednesday Season 3 Not Renewed", "Wednesday Season 3 Reportedly Confirmed"]:
        assert svc.compare(title, classify(title), utcnow(), event, [work]).outcome == "separate"


def test_same_season_soundtrack_is_not_renewal(db, sessions):
    db.add(Work(title="Wednesday", slug="wednesday", format="series"))
    db.flush()
    svc = IngestionService(sessions)
    import_prepared(svc, db, source(db), article("Netflix Announces Wednesday Season 3", "a", "a"))
    import_prepared(svc, db, source(db), article("Wednesday Season 3 Soundtrack Announced", "b", "b"))
    db.commit()
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 2


def test_identical_headline_different_dates_in_rss_excerpt(db, sessions):
    svc = IngestionService(sessions)
    first = replace(
        article("Fixture Movie Release Date Announced", "1", "date-1"),
        excerpt="The film opens on 2026-11-01 in Japan.",
    )
    second = replace(article(first.title, "2", "date-2"), excerpt="The film opens on 2026-12-01 in Japan.")
    import_prepared(svc, db, source(db), first)
    import_prepared(svc, db, source(db), second)
    db.commit()
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 2


def test_generic_same_headline_different_works_in_excerpt(db, sessions):
    db.add_all(
        [
            Work(title="First Fixture", slug="first-fixture", format="film"),
            Work(title="Second Fixture", slug="second-fixture", format="film"),
        ]
    )
    db.flush()
    svc = IngestionService(sessions)
    for i, name in enumerate(["First Fixture", "Second Fixture"]):
        import_prepared(
            svc,
            db,
            source(db),
            replace(
                article("A New Trailer Released", str(i), f"work-{i}"),
                excerpt=f"{name} reveals its first trailer.",
            ),
        )
    db.commit()
    assert db.scalar(select(func.count()).select_from(NewsEvent)) == 2
