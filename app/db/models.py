from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    CheckConstraint,
    Column,
    Table,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


news_categories = Table(
    "news_categories",
    Base.metadata,
    Column("event_id", ForeignKey("news_events.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
)
news_works = Table(
    "news_works",
    Base.metadata,
    Column("event_id", ForeignKey("news_events.id", ondelete="CASCADE"), primary_key=True),
    Column("work_id", ForeignKey("works.id", ondelete="CASCADE"), primary_key=True),
)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    site_url: Mapped[str] = mapped_column(Text)
    feed_url: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(12), default="en")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    access_status: Mapped[str] = mapped_column(String(30), default="review_required")
    policy_note: Mapped[str] = mapped_column(Text, default="")
    terms_url: Mapped[str] = mapped_column(Text, default="")
    # Legacy column retained for database compatibility; public fetching uses enabled.
    allow_extraction: Mapped[bool] = mapped_column(Boolean, default=False)
    publication_mode: Mapped[str] = mapped_column(String(20), default="facts")
    allow_media: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_embeds: Mapped[bool] = mapped_column(Boolean, default=False)
    excerpt_limit: Mapped[int] = mapped_column(Integer, default=0)
    article_selectors: Mapped[list] = mapped_column(JSON, default=list)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class NewsEvent(Base):
    __tablename__ = "news_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(240))
    canonical_title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="DISCOVERED", index=True)
    event_type: Mapped[str] = mapped_column(String(40), default="other", index=True)
    formats: Mapped[list] = mapped_column(JSON, default=list)
    production_types: Mapped[list] = mapped_column(JSON, default=list)
    countries: Mapped[list] = mapped_column(JSON, default=list)
    cultural_categories: Mapped[list] = mapped_column(JSON, default=list)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    editorial_note: Mapped[str] = mapped_column(Text, default="Awaiting automatic article processing.")
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    image_url: Mapped[str | None] = mapped_column(Text)
    image_credit: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    translations: Mapped[list["NewsTranslation"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    articles: Mapped[list["SourceArticle"]] = relationship(back_populates="event")
    categories: Mapped[list["Category"]] = relationship(secondary=news_categories)
    works: Mapped[list["Work"]] = relationship(secondary=news_works)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('DISCOVERED','SCRAPING','PROCESSING','TRANSLATING','READY_FOR_REVIEW',"
            "'APPROVED','PUBLISHED','REJECTED','FAILED')",
            name="ck_news_moderation_status",
        ),
    )


class SourceArticle(Base):
    __tablename__ = "source_articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("news_events.id"), index=True)
    guid: Mapped[str] = mapped_column(String(2048))
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(12))
    english_title: Mapped[str | None] = mapped_column(Text)
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    original_html: Mapped[str] = mapped_column(Text, default="")
    sanitized_html: Mapped[str] = mapped_column(Text, default="")
    plain_text: Mapped[str] = mapped_column(Text, default="")
    subtitle: Mapped[str] = mapped_column(Text, default="")
    featured_image: Mapped[str | None] = mapped_column(Text)
    scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scraper_version: Mapped[str] = mapped_column(String(60), default="")
    scrape_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    scrape_error: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    rights_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    assets: Mapped[list["ArticleAsset"]] = relationship(cascade="all, delete-orphan")
    embeds: Mapped[list["ArticleEmbed"]] = relationship(cascade="all, delete-orphan")
    source: Mapped[Source] = relationship()
    event: Mapped[NewsEvent] = relationship(back_populates="articles")
    __table_args__ = (UniqueConstraint("source_id", "guid"),)


class NewsTranslation(Base):
    __tablename__ = "news_translations"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("news_events.id", ondelete="CASCADE"), index=True)
    language: Mapped[str] = mapped_column(String(2))
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    content_html: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    engine: Mapped[str] = mapped_column(String(120), default="none")
    error: Mapped[str | None] = mapped_column(Text)
    manual_edit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    event: Mapped[NewsEvent] = relationship(back_populates="translations")
    __table_args__ = (
        UniqueConstraint("event_id", "language"),
        CheckConstraint("language IN ('en','ru','ky')"),
        CheckConstraint("status IN ('pending','ready','missing_model','failed','needs_review')"),
    )


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(30), unique=True)


class Work(Base):
    __tablename__ = "works"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    original_title: Mapped[str] = mapped_column(Text, default="")
    slug: Mapped[str] = mapped_column(String(240))
    format: Mapped[str] = mapped_column(String(20), default="film")
    production_type: Mapped[str] = mapped_column(String(30), default="unknown")
    countries: Mapped[list] = mapped_column(JSON, default=list)
    genres: Mapped[list] = mapped_column(JSON, default=list)
    cultural_category: Mapped[str | None] = mapped_column(String(30))
    year: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str] = mapped_column(Text, default="")
    poster_url: Mapped[str | None] = mapped_column(Text)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    metadata_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    aliases: Mapped[list["WorkAlias"]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("tmdb_id", "format"),)


class WorkAlias(Base):
    __tablename__ = "work_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Text, index=True)
    language: Mapped[str | None] = mapped_column(String(12))
    __table_args__ = (UniqueConstraint("work_id", "name"),)


class TranslationJob(Base):
    __tablename__ = "translation_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("news_events.id", ondelete="CASCADE"), index=True)
    language: Mapped[str] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("event_id", "language"),)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    source: Mapped[Source] = relationship()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="editor")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)


class GlossaryEntry(Base):
    __tablename__ = "glossary_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    term: Mapped[str] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(2))
    replacement: Mapped[str] = mapped_column(String(240))
    __table_args__ = (UniqueConstraint("term", "language"),)


class DuplicateReview(Base):
    __tablename__ = "duplicate_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("news_events.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("news_events.id", ondelete="CASCADE"))
    score: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_events_status_publication", NewsEvent.status, NewsEvent.published_at)
Index("ix_translations_language_status", NewsTranslation.language, NewsTranslation.status)


class ArticleAsset(Base):
    __tablename__ = "article_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("source_articles.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text)
    srcset: Mapped[str] = mapped_column(Text, default="")
    alt: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    credit: Mapped[str] = mapped_column(Text, default="")
    permitted: Mapped[bool] = mapped_column(Boolean, default=False)


class ArticleEmbed(Base):
    __tablename__ = "article_embeds"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("source_articles.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(40))
    original_url: Mapped[str] = mapped_column(Text)
    embed_url: Mapped[str | None] = mapped_column(Text)
    permitted: Mapped[bool] = mapped_column(Boolean, default=False)
