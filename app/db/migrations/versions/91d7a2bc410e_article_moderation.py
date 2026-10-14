"""Rich article fragments, source rights and explicit administrator moderation.

Legacy public items return to processing: their old English-only approval does
not prove the new three-language and source-rights requirements.
"""

from alembic import op
import sqlalchemy as sa
from html import escape

revision = "91d7a2bc410e"
down_revision = "4d81c606d07b"
branch_labels = None
depends_on = None


def upgrade():
    for column in [
        sa.Column("publication_mode", sa.String(20), nullable=False, server_default="facts"),
        sa.Column("allow_media", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allow_embeds", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("excerpt_limit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("article_selectors", sa.JSON(), nullable=False, server_default="[]"),
    ]:
        op.add_column("sources", column)
    connection = op.get_bind()
    checks = sa.inspect(connection).get_check_constraints("news_events")
    with op.batch_alter_table("news_events", naming_convention={"ck": "ck_%(table_name)s_legacy"}) as batch:
        for check in checks:
            batch.drop_constraint(check["name"] or "ck_news_events_legacy", type_="check")
        batch.add_column(sa.Column("approved_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key("fk_event_approver", "users", ["approved_by"], ["id"])
    connection.execute(sa.text("UPDATE news_events SET status='DISCOVERED', reviewed=false"))
    with op.batch_alter_table("news_events") as batch:
        batch.create_check_constraint(
            "ck_news_moderation_status",
            "status IN ('DISCOVERED','SCRAPING','PROCESSING','TRANSLATING','READY_FOR_REVIEW','APPROVED','PUBLISHED','REJECTED','FAILED')",
        )
    for name in ("original_html", "sanitized_html", "plain_text", "subtitle"):
        op.add_column("source_articles", sa.Column(name, sa.Text(), nullable=False, server_default=""))
    for column in [
        sa.Column("featured_image", sa.Text()),
        sa.Column("scraped_at", sa.DateTime(timezone=True)),
        sa.Column("scraper_version", sa.String(60), nullable=False, server_default=""),
        sa.Column("scrape_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("scrape_error", sa.Text()),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("rights_snapshot", sa.JSON(), nullable=False, server_default="{}"),
    ]:
        op.add_column("source_articles", column)
    op.create_index("ix_source_articles_scrape_status", "source_articles", ["scrape_status"])
    op.create_index("ix_source_articles_content_hash", "source_articles", ["content_hash"])
    op.add_column(
        "news_translations", sa.Column("content_html", sa.Text(), nullable=False, server_default="")
    )
    rows = connection.execute(sa.text("SELECT id, body FROM news_translations")).all()
    for row in rows:
        html = "".join(f"<p>{escape(p)}</p>" for p in row.body.splitlines() if p.strip())
        connection.execute(
            sa.text("UPDATE news_translations SET content_html=:html WHERE id=:id"),
            {"html": html, "id": row.id},
        )
    op.create_table(
        "article_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("source_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        *[
            sa.Column(n, sa.Text(), nullable=False)
            for n in ("url", "srcset", "alt", "title", "caption", "credit")
        ],
        sa.Column("permitted", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_article_assets_article_id", "article_assets", ["article_id"])
    op.create_table(
        "article_embeds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("source_articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("embed_url", sa.Text()),
        sa.Column("permitted", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_article_embeds_article_id", "article_embeds", ["article_id"])


def downgrade():
    op.drop_table("article_embeds")
    op.drop_table("article_assets")
    op.drop_column("news_translations", "content_html")
    op.drop_index("ix_source_articles_scrape_status", "source_articles")
    op.drop_index("ix_source_articles_content_hash", "source_articles")
    for name in (
        "original_html",
        "sanitized_html",
        "plain_text",
        "subtitle",
        "featured_image",
        "scraped_at",
        "scraper_version",
        "scrape_status",
        "scrape_error",
        "content_hash",
        "rights_snapshot",
    ):
        op.drop_column("source_articles", name)
    with op.batch_alter_table("news_events") as batch:
        batch.drop_constraint("ck_news_moderation_status", type_="check")
        batch.drop_constraint("fk_event_approver", type_="foreignkey")
        batch.drop_column("approved_at")
        batch.drop_column("approved_by")
    op.execute("UPDATE news_events SET status='review', reviewed=false")
    with op.batch_alter_table("news_events") as batch:
        batch.create_check_constraint("news_events_status_check", "status IN ('review','published','draft')")
    for name in ("publication_mode", "allow_media", "allow_embeds", "excerpt_limit", "article_selectors"):
        op.drop_column("sources", name)
