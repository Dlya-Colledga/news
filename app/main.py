import logging
from pathlib import Path
from app.services.article_html import PROVIDERS
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from app.core.config import get_settings
from app.routers import public, admin

log = logging.getLogger(__name__)


def create_app():
    settings = get_settings()
    app = FastAPI(title="Frame", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="frame_session",
        same_site="lax",
        https_only=settings.cookie_secure,
        max_age=28800,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.middleware("http")
    async def headers(request, call_next):
        if (
            request.headers.get("content-length", "").isdigit()
            and int(request.headers["content-length"]) > 1_000_000
        ):
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse("Request too large", status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https:; media-src https:; style-src 'self'; script-src 'self'; "
            + "frame-src "
            + " ".join("https://" + p.frame_host for p in PROVIDERS.values())
            + "; "
            + "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc):
        if exc.status_code in (301, 302, 303, 307, 308) and exc.headers and "Location" in exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=exc.status_code)
        lang = request.url.path.split("/")[1]
        if lang not in ("en", "ru", "ky"):
            lang = "ru"
        return public.render(
            request,
            "error.html",
            lang,
            status_code=exc.status_code,
            error_code=exc.status_code,
            detail=exc.detail if exc.status_code != 404 else None,
            noindex=True,
            alternates={},
        )

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc):
        log.error("Unhandled request error", exc_info=exc)
        lang = request.url.path.split("/")[1]
        if lang not in ("en", "ru", "ky"):
            lang = "ru"
        return public.render(
            request, "error.html", lang, status_code=500, error_code=500, noindex=True, alternates={}
        )

    app.include_router(admin.router)
    app.include_router(public.router)
    return app


app = create_app()
