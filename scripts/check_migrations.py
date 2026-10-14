"""Alembic lifecycle and legacy-data preservation in an isolated PostgreSQL database."""

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, text, MetaData, Table, select
from app.db.session import engine


def main():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("PostgreSQL required")
    name = "frame_migration_" + uuid.uuid4().hex
    admin = create_engine(engine.url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = engine.url.set(database=name).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": url}
    alembic = str(
        Path(__file__).resolve().parents[1]
        / ".venv"
        / ("Scripts/alembic.exe" if os.name == "nt" else "bin/alembic")
    )
    isolated = create_engine(url)
    body = "Legacy paragraph with <script>literal</script>.\nSecond paragraph."

    def run(*args):
        subprocess.run([alembic, *args], env=env, check=True)

    try:
        run("upgrade", "4d81c606d07b")
        metadata = MetaData()
        events = Table("news_events", metadata, autoload_with=isolated)
        translations = Table("news_translations", metadata, autoload_with=isolated)
        now = datetime.now(timezone.utc)
        with isolated.begin() as conn:
            event_id = conn.execute(
                events.insert()
                .values(
                    slug="legacy-fixture",
                    canonical_title="Legacy fixture",
                    status="published",
                    event_type="other",
                    formats=[],
                    production_types=[],
                    countries=[],
                    cultural_categories=[],
                    facts={},
                    editorial_note="Legacy approved fixture",
                    reviewed=True,
                    created_at=now,
                    published_at=now,
                )
                .returning(events.c.id)
            ).scalar_one()
            for language in ("en", "ru", "ky"):
                conn.execute(
                    translations.insert().values(
                        event_id=event_id,
                        language=language,
                        title="Legacy title",
                        summary="Legacy summary",
                        body=body,
                        status="ready",
                        engine="editor",
                        manual_edit=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
        run("upgrade", "head")
        run("check")
        with isolated.connect() as conn:
            state = conn.execute(
                text("SELECT status, reviewed FROM news_events WHERE id=:id"), {"id": event_id}
            ).one()
            assert state.status == "DISCOVERED" and not state.reviewed
            versions = conn.execute(
                text("SELECT body, content_html, manual_edit FROM news_translations")
            ).all()
            assert len(versions) == 3
            assert all(
                t.body == body
                and t.manual_edit
                and "&lt;script&gt;" in t.content_html
                and "<script>" not in t.content_html
                for t in versions
            )
        run("downgrade", "4d81c606d07b")
        with isolated.connect() as conn:
            assert conn.execute(select(translations.c.body)).scalars().all() == [body] * 3
        run("downgrade", "base")
        run("upgrade", "head")
        run("check")
        report = {
            "postgresql": True,
            "legacy_text_preserved": True,
            "legacy_html_escaped": True,
            "legacy_approval_reset": True,
            "upgrade_check_downgrade_upgrade_check": "passed",
        }
        Path("docs/migration-check.json").write_text(json.dumps(report, indent=2))
        print("PostgreSQL migration lifecycle and legacy-data preservation: PASS")
    finally:
        isolated.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()
