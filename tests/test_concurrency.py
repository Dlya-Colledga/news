from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select, func
from app.db.models import Source, SourceArticle, NewsEvent
from app.collectors.base import Article
from app.services.ingestion import IngestionService


def test_parallel_imports_are_serialized(sessions):
    with sessions() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("PostgreSQL advisory lock integration test")
        source_id = db.scalar(select(Source.id).where(Source.key == "anime_corner"))

    def ingest(_):
        with sessions() as db:
            svc = IngestionService(sessions)
            result = svc.import_article(
                db,
                db.get(Source, source_id),
                Article("parallel-guid", "https://animecorner.me/parallel/", "Fixture film trailer"),
            )
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(ingest, range(4)))
    assert results.count(True) == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(SourceArticle)) == 1
        assert db.scalar(select(func.count()).select_from(NewsEvent)) == 1


def test_postgresql_session_utc(sessions):
    from sqlalchemy import text

    with sessions() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("PostgreSQL session timezone")
        assert db.scalar(text("SHOW TIME ZONE")) == "UTC"


def test_only_one_worker_owns_scheduler(sessions, monkeypatch):
    from app.workers import runner

    with sessions() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("PostgreSQL scheduler lock")
        monkeypatch.setattr(runner, "engine", db.bind)
    with runner.worker_lock():
        with pytest.raises(RuntimeError, match="Another Frame worker"):
            with runner.worker_lock():
                pass
    with runner.worker_lock():
        pass


def test_concurrent_approval_is_idempotent(sessions, published, admin, db):
    from threading import Barrier
    from app.db.models import AuditLog
    from app.services.editorial import set_publication

    if db.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL atomic publication integration test")
    published.status, published.reviewed = "READY_FOR_REVIEW", False
    db.commit()
    barrier = Barrier(2)

    def approve(_):
        with sessions() as session:
            # Both sessions deliberately cache the old state before acquiring the lock.
            event = session.get(NewsEvent, published.id)
            barrier.wait(timeout=10)
            set_publication(session, event.id, True, admin.id)
            session.commit()
            return event.published_at

    with ThreadPoolExecutor(max_workers=2) as pool:
        timestamps = list(pool.map(approve, range(2)))
    assert timestamps[0] == timestamps[1]
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "publish")) == 1
