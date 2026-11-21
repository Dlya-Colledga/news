"""The optional classroom fixture must not contaminate normal seeds."""
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.db.models import NewsEvent, Source
from app.db.demo import seed_demo


def test_demo_examples_are_clearly_labeled_and_idempotent(db, admin):
    assert seed_demo(db, admin) == 3
    db.commit()
    examples = db.scalars(select(NewsEvent).where(NewsEvent.slug.like("demo-%"))).all()
    assert len(examples) == 3
    assert sorted(event.status for event in examples) == ["PUBLISHED", "PUBLISHED", "READY_FOR_REVIEW"]
    assert all(len(event.translations) == 3 and not event.image_url for event in examples)
    source = db.scalar(select(Source).where(Source.key == "frame_demo"))
    assert source is not None and not source.enabled and not source.allow_media
    assert seed_demo(db, admin) == 0


def test_demo_seed_refuses_production(db, admin, monkeypatch):
    monkeypatch.setattr("app.db.demo.get_settings", lambda: SimpleNamespace(environment="production"))
    with pytest.raises(ValueError, match="production"):
        seed_demo(db, admin)
