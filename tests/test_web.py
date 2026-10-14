import re
from sqlalchemy import select
from app.db.models import NewsTranslation, Work, WorkAlias, NewsEvent
from app.core.i18n import MESSAGES


def csrf(client, path="/admin/login"):
    response = client.get(path)
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match, response.text[:500]
    return match[1]


def login(client):
    token = csrf(client)
    r = client.post(
        "/admin/login", data={"csrf": token, "username": "editor", "password": "correct-horse-battery"}
    )
    assert r.status_code == 200
    return csrf(client, "/admin/")


def test_main_ssr_routes(client, published, db):
    work = Work(
        title="Fixture",
        original_title="Original fixture",
        slug="fixture",
        format="film",
        aliases=[WorkAlias(name="Альтернативное название")],
    )
    published.works.append(work)
    db.commit()
    for lang in ("en", "ru", "ky"):
        for path in [
            f"/{lang}/",
            f"/{lang}/movies/",
            f"/{lang}/series/",
            f"/{lang}/anime/",
            f"/{lang}/animation/",
            f"/{lang}/dramas/",
            f"/{lang}/industry/",
            f"/{lang}/search/",
            f"/{lang}/news/{published.id}-{published.slug}",
            f"/{lang}/title/{work.id}-{work.slug}",
        ]:
            r = client.get(path)
            assert r.status_code == 200, (path, r.text[:300])
            assert f'<html lang="{lang}">' in r.text
            assert 'rel="canonical"' in r.text
            assert MESSAGES[lang]["search"] in r.text
    assert client.get("/sitemap.xml").status_code == 200
    assert "/admin/" in client.get("/robots.txt").text
    assert client.get("/healthz").json() == {"status": "ok"}


def test_language_switch_and_unavailable_seo(client, published, db):
    path = f"/ru/news/{published.id}-{published.slug}"
    r = client.get(path)
    for lang in ("en", "ru", "ky"):
        assert f"/{lang}/news/{published.id}-{published.slug}" in r.text
        assert f'hreflang="{lang}"' in r.text
    ky = db.scalar(
        select(NewsTranslation).where(
            NewsTranslation.event_id == published.id, NewsTranslation.language == "ky"
        )
    )
    ky.status = "missing_model"
    db.commit()
    r = client.get(path)
    assert 'hreflang="ky"' not in r.text
    missing = client.get(f"/ky/news/{published.id}-{published.slug}")
    assert missing.status_code == 404 and "noindex" in missing.text
    assert f"/en/news/{published.id}-{published.slug}" in missing.text
    assert f"/ky/news/{published.id}-{published.slug}" not in client.get("/sitemap.xml").text
    assert client.get(f"/en/news/{published.id}-obsolete", follow_redirects=False).status_code == 308


def test_search_alias_pagination_filters(client, published, db, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "page_size", 1)
    work = Work(
        title="Unique fixture",
        slug="unique-fixture",
        format="film",
        aliases=[WorkAlias(name="Alternate secret name")],
    )
    published.works.append(work)
    db.commit()
    r = client.get("/en/search/?q=Alternate+secret+name")
    assert "Fixture film trailer" in r.text
    assert client.get("/en/search/?q=not-a-match").text.count("No stories match") == 1
    assert client.get("/en/search/?page=2").status_code == 404
    assert "Fixture film trailer" not in client.get("/en/movies/?event_type=delay").text
    assert client.get("/en/movies/?start=2026-10-20&end=2026-10-01").status_code == 422
    assert client.get("/en/search/?content_language=zz").status_code == 422
    assert client.get("/en/movies/?event_type=trailer&start=&end=&sort=newest").status_code == 200
    r = client.get("/ru/search/?q=Fixture&content_language=en")
    assert "Fixture film trailer" in r.text and "/en/news/" in r.text


def test_404_drafts_and_html_escaping(client, published, db):
    assert client.get("/xx/").status_code == 404
    assert client.get("/ru/unknown/").status_code == 404
    assert client.get("/ru/news/999999-missing").status_code == 404
    t = next(t for t in published.translations if t.language == "en")
    t.content_html = "<p>Visible article</p><script>alert(1)</script>"
    db.commit()
    r = client.get(f"/en/news/{published.id}-{published.slug}")
    assert "Visible article" in r.text and "<script>alert(1)</script>" not in r.text
    published.status = "READY_FOR_REVIEW"
    db.commit()
    assert client.get(f"/en/news/{published.id}-{published.slug}").status_code == 404


def test_admin_auth_csrf_and_security_headers(client, admin, published):
    r = client.get("/admin/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert (
        client.post(
            f"/admin/events/{published.id}/publication", data={"action": "publish"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/admin/login", data={"username": "editor", "password": "correct-horse-battery"}
        ).status_code
        == 403
    )
    token = login(client)
    for path in [
        "/admin/",
        "/admin/sources",
        "/admin/translations",
        "/admin/works",
        "/admin/audit",
        f"/admin/events/{published.id}",
    ]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert (
        client.post(
            f"/admin/events/{published.id}/publication", data={"csrf": "invalid", "action": "unpublish"}
        ).status_code
        == 403
    )
    assert client.get(f"/admin/events/{published.id}/publication").status_code == 405
    assert (
        client.post(
            f"/admin/events/{published.id}/publication", data={"csrf": token, "action": "unpublish"}
        ).status_code
        == 200
    )
    response = client.post("/admin/logout", data={"csrf": token}, follow_redirects=False)
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert client.get("/admin/", follow_redirects=False).status_code == 303


def test_login_failures_and_rate_limit(client, admin):
    token = csrf(client)
    for _ in range(8):
        assert (
            client.post(
                "/admin/login", data={"csrf": token, "username": "editor", "password": "wrong"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/admin/login", data={"csrf": token, "username": "editor", "password": "wrong"}
        ).status_code
        == 429
    )


def test_publish_requires_review_and_role(client, admin, published, db):
    token = login(client)
    published.reviewed = False
    published.status = "READY_FOR_REVIEW"
    db.commit()
    r = client.post(
        f"/admin/events/{published.id}/publication",
        data={"csrf": token, "action": "publish"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert f"/admin/events/{published.id}" in r.headers["location"]
    db.expire_all()
    assert published.status == "PUBLISHED" and published.approved_by == admin.id
    admin.role = "editor"
    db.commit()
    assert client.post("/admin/sources/1/toggle", data={"csrf": token}).status_code == 403


def test_approve_all_publishes_only_ready_items(client, admin, published, db):
    token = login(client)
    published.status = "READY_FOR_REVIEW"
    published.reviewed = False
    db.commit()

    dashboard = client.get("/admin/")
    assert "Одобрить всё" in dashboard.text
    response = client.post("/admin/approve-all", data={"csrf": token}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert db.get(NewsEvent, published.id).status == "PUBLISHED"
    assert db.get(NewsEvent, published.id).reviewed is True


def test_editorial_flow(client, admin, published, db):
    token = login(client)
    r = client.post(
        f"/admin/events/{published.id}/translation/en",
        data={
            "csrf": token,
            "title": "Edited fixture title",
            "summary": "Original editorial summary",
            "body": "Verified original editorial content.",
            "reviewed": "yes",
        },
    )
    assert r.status_code == 200
    db.expire_all()
    assert db.get(NewsEvent, published.id).canonical_title == "Edited fixture title"
    assert (
        client.post(
            f"/admin/events/{published.id}/publication", data={"csrf": token, "action": "publish"}
        ).status_code
        == 200
    )
    assert "Edited fixture title" not in client.get("/en/").text
    # Editing invalidates both translations and publication; all three need review again.
    for language in ("ru", "ky"):
        translation = next(t for t in published.translations if t.language == language)
        assert (
            client.post(
                f"/admin/events/{published.id}/translation/{language}",
                data={
                    "csrf": token,
                    "title": translation.title,
                    "summary": translation.summary,
                    "body": translation.content_html,
                },
            ).status_code
            == 200
        )
    assert (
        client.post(
            f"/admin/events/{published.id}/publication", data={"csrf": token, "action": "publish"}
        ).status_code
        == 200
    )
    assert "Edited fixture title" in client.get("/en/").text


def test_filter_switch_preserves_context(client):
    response = client.get("/ru/movies/?event_type=trailer&sort=oldest")
    assert "/ky/movies/?event_type=trailer&amp;sort=oldest" in response.text


def test_production_security_config():
    import pytest
    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(environment="production", _env_file=None)


def test_no_n_plus_one_on_category(client, published, db):
    from sqlalchemy import event as sa_event

    for index in range(5):
        event = NewsEvent(
            canonical_title=f"Fixture {index}",
            slug=f"fixture-{index}",
            status="PUBLISHED",
            published_at=published.published_at,
            categories=list(published.categories),
        )
        db.add(event)
        db.flush()
        db.add(
            NewsTranslation(
                event=event,
                language="en",
                title=f"Fixture {index}",
                summary="Test",
                body="Test",
                status="ready",
            )
        )
    db.commit()
    queries = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    sa_event.listen(db.bind, "before_cursor_execute", count)
    try:
        assert client.get("/en/movies/").status_code == 200
    finally:
        sa_event.remove(db.bind, "before_cursor_execute", count)
    assert len(queries) <= 7


def test_localized_500_page(sessions):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.db.session import get_db

    app = create_app()

    def broken_db():
        raise RuntimeError("private internal database failure")

    app.dependency_overrides[get_db] = broken_db
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/ky/")
    assert r.status_code == 500 and "Көрсөтүү убактылуу токтотулду." in r.text
    assert "private internal database failure" not in r.text


def test_second_page_preserves_search_filters(client, published, db, monkeypatch):
    from datetime import timedelta
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "page_size", 1)
    second = NewsEvent(
        canonical_title="Second fixture",
        slug="second-fixture",
        status="PUBLISHED",
        published_at=published.published_at - timedelta(hours=1),
        categories=list(published.categories),
    )
    db.add(second)
    db.flush()
    db.add(
        NewsTranslation(
            event=second,
            language="en",
            title="Second fixture",
            summary="Fixture",
            body="Fixture",
            status="ready",
        )
    )
    db.commit()
    first = client.get("/en/search/?q=fixture&category=movies&content_language=en")
    assert "q=fixture&amp;category=movies&amp;content_language=en&amp;page=2" in first.text
    second_page = client.get("/en/search/?q=fixture&category=movies&content_language=en&page=2")
    assert second_page.status_code == 200 and "Second fixture" in second_page.text
    assert "Fixture film trailer" not in second_page.text
