"""Offline rich-article editorial browser check using an isolated synthetic database."""

import struct
import zlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from app.collectors.base import Article
from app.core.security import hasher
from app.db.models import Base, User, Source, SourceArticle, NewsEvent
from app.db.seed import seed
from app.db.session import make_engine
from app.services.ingestion import IngestionService
from app.services.scraping import ArticleScraperService
from app.services.translation import TranslationService


class BrowserFixtureEngine:
    def path(self, source, target):
        return [source, target]

    def translate(self, text, source, target):
        return {"ru": "Проверка интерфейса: ", "ky": "Интерфейс тести: "}.get(target, "") + text


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / ".local/screenshots"
    output.mkdir(parents=True, exist_ok=True)

    def png_chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    # A visibly synthetic local picture, used only in browser fixtures.
    png = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack("!2I5B", 640, 360, 8, 2, 0, 0, 0))
    png += png_chunk(b"IDAT", zlib.compress((b"\x00" + b"\x45\x61\x3e" * 640) * 360)) + png_chunk(
        b"IEND", b""
    )
    password = secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory(prefix="frame-browser-fixtures-") as tmp:
        url = "sqlite:///" + str(Path(tmp) / "fixtures.db")
        engine = make_engine(url)
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        translator = TranslationService(BrowserFixtureEngine())
        service = IngestionService(factory, translator=translator)
        with factory() as db:
            seed(db)
            db.add(User(username="browser-fixture", password_hash=hasher.hash(password), role="admin"))
            source = db.scalar(select(Source).where(Source.key == "anime_corner"))
            source.allow_extraction = source.allow_media = source.allow_embeds = True
            source.publication_mode = "full"
            service.import_article(
                db,
                source,
                Article(
                    "browser-fixture",
                    "https://animecorner.me/browser-fixture/",
                    "Fixture Movie trailer released",
                ),
            )
            db.commit()
            article_id = db.scalar(select(SourceArticle.id))
        html = (root / "tests/fixtures/rich_article.html").read_text()
        service.prepare_article(
            article_id, ArticleScraperService().extract(html, "https://animecorner.me/browser-fixture/")
        )
        with factory() as db:
            event = db.scalar(select(NewsEvent))
            for lang in ("ru", "ky"):
                translator.localize(db, event, lang)
            db.commit()
            assert event.status == "READY_FOR_REVIEW"
            event_id, slug = event.id, event.slug
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=root,
            env={
                **os.environ,
                "DATABASE_URL": url,
                "SECRET_KEY": secrets.token_urlsafe(48),
                "BASE_URL": base,
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        report = {"synthetic_fixture": True, "network": "external requests fulfilled locally", "checks": []}
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError(f"Fixture server exited: {process.returncode}")
                try:
                    if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Fixture server did not start")
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page_timeout = 15000
                context.set_default_navigation_timeout(page_timeout)
                errors = []
                page = context.new_page()
                original_goto = page.goto

                def navigate(url, **kwargs):
                    response = original_goto(url, wait_until="domcontentloaded", **kwargs)
                    expect(page.locator("body")).to_have_css("background-color", "rgb(16, 19, 16)")
                    return response

                page.goto = navigate
                page.on("pageerror", lambda error: errors.append(str(error)))

                def external(route):
                    if route.request.url.startswith(base + "/"):
                        route.continue_()
                        return
                    if route.request.resource_type == "image":
                        route.fulfill(content_type="image/png", body=png)
                    else:
                        route.fulfill(
                            content_type="text/html", body="<!doctype html><p>Offline provider fixture</p>"
                        )

                context.route("**/*", external)
                assert page.goto(f"{base}/en/news/{event_id}-{slug}").status == 404
                page.goto(base + "/admin/login")
                page.locator("[name=username]").fill("browser-fixture")
                page.locator("[name=password]").fill(password)
                page.get_by_role("button", name="Войти").click()
                page.get_by_role("link", name=f"#{event_id} · Fixture Movie trailer released").click()
                assert page.locator(".editor-surface").count() == 3
                assert page.locator(".editor-surface").first.locator("img").count() == 3
                assert page.locator(".editor-surface").first.locator("iframe").count() == 4
                page.screenshot(path=str(output / "rich-editor-desktop.png"), full_page=True)
                for language in ("en", "ru", "ky"):
                    page.goto(f"{base}/admin/events/{event_id}/preview/{language}")
                    assert page.locator(".article-content img").count() == 3
                    assert page.locator(".article-content iframe").count() == 4
                    assert page.locator(".article-content table").count() == 1
                    report["checks"].append(
                        f"{language} SSR preview: paragraphs, pictures, figures, table, four embeds"
                    )
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(f"{base}/admin/events/{event_id}/preview/ru")
                for img in page.locator(".article-content img").all():
                    img.scroll_into_view_if_needed()
                    img.evaluate(
                        'img => img.decode().catch(e => {throw new Error(img.currentSrc + ": " + e.message)})'
                    )
                for index, frame in enumerate(page.locator(".article-content iframe").all()):
                    frame.scroll_into_view_if_needed()
                    expect(
                        page.frame_locator(".article-content iframe")
                        .nth(index)
                        .get_by_text("Offline provider fixture")
                    ).to_be_visible()
                page.evaluate("window.scrollTo(0, 0)")
                overflow = page.evaluate(
                    "[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth && getComputedStyle(e).position!=='fixed').map(e=>({tag:e.tagName, cls:e.className, width:e.getBoundingClientRect().width}))"
                )
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), overflow
                page.screenshot(path=str(output / "rich-preview-mobile.png"), full_page=True)
                page.set_viewport_size({"width": 1440, "height": 1000})
                # Save an unchanged prefilled version through the rich editor. Media must survive.
                page.goto(f"{base}/admin/events/{event_id}")
                page.locator('form[action$="/translation/ru"]').get_by_role(
                    "button", name="Сохранить RU"
                ).click()
                assert page.locator('form[action$="/translation/ru"] .editor-surface iframe').count() == 4
                page.get_by_role("button", name="Одобрить и опубликовать").click()
                response = page.goto(f"{base}/ru/news/{event_id}-{slug}")
                assert response.status == 200
                assert page.locator(".article-content figure").count() == 3
                assert page.locator(".article-content iframe").first.get_attribute("sandbox")
                page.screenshot(path=str(output / "rich-public-desktop.png"), full_page=True)
                report["checks"].extend(
                    [
                        "No publication before approval",
                        "Rich editor preserves embeds on save",
                        "Administrator approval publishes all locales",
                        "Mobile layout has no horizontal overflow",
                        "Sandboxed iframe components load offline provider fixtures",
                    ]
                )
                assert not errors, errors
                report["javascript_errors"] = errors
                browser.close()
            Path("docs/browser-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            process.terminate()
            process.wait(timeout=10)
            engine.dispose()


if __name__ == "__main__":
    main()
