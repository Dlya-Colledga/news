import pytest
from sqlalchemy import select, func
from app.db.models import SourceArticle, NewsEvent, AuditLog
from app.services.editorial import merge_events, split_event


def test_merge_split_preserves_originals(db, published, admin):
    original = published.articles[0]
    second = NewsEvent(canonical_title="Another fixture", slug="another", event_type="trailer")
    db.add(second)
    db.flush()
    second_id = second.id
    a = SourceArticle(
        source_id=original.source_id,
        event=second,
        guid="second",
        canonical_url="https://animecorner.me/fixture-2/",
        title="Another fixture source",
        language="en",
        english_title="Another fixture trailer",
    )
    db.add(a)
    db.commit()
    merged = merge_events(db, published.id, second.id, admin.id)
    db.commit()
    assert merged.status == "READY_FOR_REVIEW"  # preserves complete versions, requires fresh approval
    assert db.get(NewsEvent, second_id) is None
    assert db.scalar(select(func.count()).select_from(SourceArticle)) == 2
    new = split_event(db, merged.id, [a.id], admin.id)
    db.commit()
    assert new.id != merged.id and new.status == "DISCOVERED"
    assert a.event_id == new.id
    assert db.scalar(select(func.count()).select_from(SourceArticle)) == 2
    assert len(db.scalars(select(AuditLog)).all()) == 2


def test_invalid_merge_split_and_atomic_rollback(db, published, admin):
    with pytest.raises(ValueError):
        merge_events(db, published.id, published.id, admin.id)
    with pytest.raises(ValueError):
        split_event(db, published.id, [published.articles[0].id], admin.id)
    db.rollback()
    assert db.get(NewsEvent, published.id).status == "PUBLISHED"
