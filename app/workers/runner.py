import asyncio
import logging
from contextlib import contextmanager
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.db.session import SessionLocal, engine, editorial_lock
from app.db.models import Source, SourceArticle, IngestionRun, TranslationJob, NewsEvent, utcnow
from app.services.ingestion import IngestionService, queue_source
from app.services.translation import TranslationService
from app.core.config import get_settings

log = logging.getLogger(__name__)


@contextmanager
def worker_lock():
    """Dedicated connection owns the process lock; released automatically on process death."""
    with engine.connect() as connection:
        locked = engine.dialect.name != "postgresql" or connection.scalar(
            text("SELECT pg_try_advisory_lock(73419202)")
        )
        if not locked:
            raise RuntimeError("Another Frame worker already holds the scheduler lock")
        try:
            yield
        finally:
            if engine.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_unlock(73419202)"))


def recover():
    # Called only after acquiring the exclusive worker lock: no live owner can be interrupted.
    with SessionLocal() as db:
        for run in db.scalars(select(IngestionRun).where(IngestionRun.status == "running")):
            run.status, run.error = "queued", "Recovered after worker interruption"
        for article in db.scalars(select(SourceArticle).where(SourceArticle.scrape_status == "scraping")):
            article.scrape_status = "pending"
        for event in db.scalars(select(NewsEvent).where(NewsEvent.status.in_(["SCRAPING", "PROCESSING"]))):
            if not event.reviewed:
                for article in event.articles:
                    article.scrape_status = "pending"
        db.commit()


def schedule_imports():
    with SessionLocal() as db:
        for source in db.scalars(select(Source).where(Source.enabled.is_(True))):
            queue_source(db, source.id)
        db.commit()


def translate_pending():
    translator = TranslationService()
    while True:
        with SessionLocal() as db:
            editorial_lock(db)
            job = db.scalar(
                select(TranslationJob)
                .where(TranslationJob.status == "pending")
                .order_by(TranslationJob.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not job:
                return
            event = db.scalar(
                select(NewsEvent)
                .where(NewsEvent.id == job.event_id)
                .options(selectinload(NewsEvent.translations))
            )
            if event.status in ("REJECTED", "APPROVED", "PUBLISHED"):
                job.status, job.error = "done", "Event is protected from automatic changes"
                db.commit()
                continue
            job.attempts += 1
            result = translator.localize(db, event, job.language)
            job.status = "done" if result.status == "ready" else "failed"
            job.error, job.updated_at = result.error, utcnow()
            db.commit()


async def tick():
    try:
        await IngestionService(SessionLocal).run_queued()
        await asyncio.to_thread(translate_pending)
    except Exception:
        log.exception("Worker tick failed; next scheduled tick will retry")


async def run(once=False):
    with worker_lock():
        recover()
        schedule_imports()
        await tick()
        if once:
            return
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            schedule_imports,
            "interval",
            minutes=get_settings().ingest_interval_minutes,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(tick, "interval", seconds=10, max_instances=1, coalesce=True)
        scheduler.start()
        try:
            await asyncio.Event().wait()
        finally:
            scheduler.shutdown(wait=False)


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
