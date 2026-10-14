"""Explicit installed-model smoke check. Uses a temporary PostgreSQL schema; no public fixtures."""

import json
import uuid
from pathlib import Path
from sqlalchemy import text, select
from sqlalchemy.orm import sessionmaker
from app.db.models import Base, NewsEvent, NewsTranslation
from app.db.session import engine
from app.services.translation import ArgosEngine, TranslationService
from app.services.article_html import sanitize_html, content_text, dom


def main():
    schema = "translation_check_" + uuid.uuid4().hex
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Use PostgreSQL for this persistence check")
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(scoped)
    sessions = sessionmaker(scoped, expire_on_commit=False)
    result = {"installed_pairs": ArgosEngine().installed_pairs(), "translations": {}, "persistence": False}
    try:
        with sessions() as db:
            event = NewsEvent(canonical_title="The film festival opens on Friday", slug="private-fixture")
            db.add(event)
            db.flush()
            html = sanitize_html(
                '<h2>Festival programme</h2><p>The film festival opens on <strong>Friday</strong>.</p><figure><img src="https://example.com/poster.jpg" alt="Festival poster"><figcaption>Official poster</figcaption></figure><iframe src="https://www.youtube-nocookie.com/embed/VIDEO_ID123"></iframe>'
            )
            db.add(
                NewsTranslation(
                    event=event,
                    language="en",
                    title=event.canonical_title,
                    summary="The festival will show films from different countries.",
                    body=content_text(html),
                    content_html=html,
                    status="ready",
                    manual_edit=True,
                    engine="smoke_fixture",
                )
            )
            db.flush()
            service = TranslationService()
            for language in ("ru", "ky"):
                t = service.localize(db, event, language)
                assert t.status == "ready", t.error
                assert t.title != event.canonical_title
                assert [tag.name for tag in dom(t.content_html).find_all(True)] == [
                    tag.name for tag in dom(html).find_all(True)
                ]
                assert dom(t.content_html).iframe["src"] == dom(html).iframe["src"]
                assert dom(t.content_html).img["alt"] != "Festival poster"
                result["translations"][language] = {
                    "title": t.title,
                    "summary": t.summary,
                    "body": t.body,
                    "content_html": t.content_html,
                    "dom_preserved": True,
                    "engine": t.engine,
                }
                print(language, t.title, flush=True)
            db.commit()
            event_id = event.id
        engine.dispose()
        with sessions() as db:
            translations = db.scalars(
                select(NewsTranslation).where(NewsTranslation.event_id == event_id)
            ).all()
            assert len(translations) == 3 and all(t.status == "ready" for t in translations)
            result["persistence"] = True
        Path("docs/translation-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


if __name__ == "__main__":
    main()
