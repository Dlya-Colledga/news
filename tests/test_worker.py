from sqlalchemy import select
from app.db.models import Source, IngestionRun, TranslationJob
from app.workers import runner
from app.services.translation import TranslationService, enqueue_translations
from test_translation import FakeEngine, NoModels


def test_recovery_requeues_interrupted_import(db, sessions, monkeypatch):
    source = db.scalar(select(Source).where(Source.key == "anime_corner"))
    run = IngestionRun(source_id=source.id, status="running")
    db.add(run)
    db.commit()
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    runner.recover()
    db.expire_all()
    assert db.get(IngestionRun, run.id).status == "queued"


def test_worker_jobs_report_failures_and_retry(db, sessions, published, monkeypatch):
    published.status, published.reviewed = "TRANSLATING", False
    for t in published.translations:
        if t.language != "en":
            t.manual_edit = False
            t.status = "pending"
            t.title = t.summary = t.body = ""
    enqueue_translations(db, published)
    db.commit()
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    monkeypatch.setattr(runner, "TranslationService", lambda: TranslationService(NoModels()))
    runner.translate_pending()
    db.expire_all()
    jobs = db.scalars(select(TranslationJob)).all()
    assert all(j.status == "failed" and j.attempts == 1 for j in jobs)
    for job in jobs:
        job.status = "pending"
    db.commit()
    monkeypatch.setattr(runner, "TranslationService", lambda: TranslationService(FakeEngine()))
    runner.translate_pending()
    db.expire_all()
    assert all(j.status == "done" and j.attempts == 2 for j in jobs)
