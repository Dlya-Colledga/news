"""Retry drafts blocked by the obsolete extraction permission flag."""

import sqlalchemy as sa
from alembic import op

revision = "b63a47e821d0"
down_revision = "91d7a2bc410e"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    errors = (
        "Source has not granted extraction permission",
        "Source disabled or extraction permission revoked",
    )
    # Only untouched failed drafts are retried. Manual and editorial decisions
    # remain intact, and disabled sources still wait until an administrator enables them.
    statement = sa.text("""
        SELECT DISTINCT e.id FROM news_events e
        JOIN source_articles a ON a.event_id = e.id
        WHERE e.status = 'FAILED' AND e.reviewed = false
          AND a.scrape_status = 'failed' AND a.scrape_error IN :errors
          AND NOT EXISTS (
              SELECT 1 FROM news_translations t
              WHERE t.event_id = e.id AND t.manual_edit = true
          )
    """).bindparams(sa.bindparam("errors", expanding=True))
    for event_id in connection.execute(statement, {"errors": errors}).scalars().all():
        connection.execute(
            sa.text("""
                UPDATE source_articles SET scrape_status = 'pending', scrape_error = NULL
                WHERE event_id = :event_id AND scrape_status = 'failed' AND scrape_error IN :errors
            """).bindparams(sa.bindparam("errors", expanding=True)),
            {"event_id": event_id, "errors": errors},
        )
        connection.execute(
            sa.text("""
                UPDATE news_events SET status = 'DISCOVERED', editorial_note = :note WHERE id = :event_id
            """),
            {"event_id": event_id, "note": "Повторная обработка поставлена в очередь."},
        )
    connection.execute(
        sa.text("""
        UPDATE sources SET access_status = 'public_access'
        WHERE enabled = true AND access_status IN ('permission_required', 'permission_confirmed', 'rss_only')
    """)
    )


def downgrade():
    # Processing may have finished since upgrade; never discard its results.
    pass
