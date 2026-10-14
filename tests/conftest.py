import os
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
from app.db.models import Base, Source, NewsEvent, NewsTranslation, SourceArticle, User, Category, utcnow
from app.db.session import make_engine, get_db
from app.db.seed import seed
from app.core.security import hasher
from app.main import create_app


@pytest.fixture
def sessions(tmp_path):
    url = os.getenv("TEST_DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    engine = make_engine(url)
    schema = "test_" + uuid.uuid4().hex
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        seed(db)
    yield factory
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


@pytest.fixture
def db(sessions):
    with sessions() as db:
        yield db


@pytest.fixture
def client(sessions):
    app = create_app()

    def override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def admin(db):
    user = User(username="editor", password_hash=hasher.hash("correct-horse-battery"), role="admin")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def published(db):
    from sqlalchemy import select

    source = db.scalar(select(Source).where(Source.key == "anime_corner"))
    source.allow_extraction = True
    source.publication_mode = "full"
    event = NewsEvent(
        canonical_title="Fixture film trailer",
        slug="fixture-film-trailer",
        status="PUBLISHED",
        event_type="trailer",
        reviewed=True,
        published_at=utcnow(),
        categories=[db.scalar(select(Category).where(Category.slug == "movies"))],
    )
    db.add(event)
    db.flush()
    for lang, title in [
        ("en", "Fixture film trailer"),
        ("ru", "Тестовый трейлер фильма"),
        ("ky", "Тасманын тесттик трейлери"),
    ]:
        db.add(
            NewsTranslation(
                event=event,
                language=lang,
                title=title,
                summary="Editorial test summary " + lang,
                body="Original test brief. Second paragraph.",
                content_html="<p>Original test brief.</p><p>Second paragraph.</p>",
                status="ready",
                engine="test",
                manual_edit=True,
            )
        )
    db.add(
        SourceArticle(
            source_id=source.id,
            event=event,
            guid="fixture-1",
            canonical_url="https://animecorner.me/fixture-1/",
            title="Source fixture headline",
            language="en",
            excerpt="Fixture source evidence.",
            scrape_status="ready",
            rights_snapshot={"publication_mode": "full", "allow_media": False, "allow_embeds": False},
        )
    )
    db.commit()
    return event
