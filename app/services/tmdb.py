from datetime import timedelta
import httpx
from sqlalchemy import select
from slugify import slugify
from app.core.config import get_settings
from app.db.models import Work, WorkAlias, utcnow
from app.services.deduplication import aware


class TMDBService:
    def __init__(self, client=None, settings=None):
        self.settings = settings or get_settings()
        self.client = client

    async def get(self, path, **params):
        if not self.settings.tmdb_api_key:
            return None

        async def fetch(client):
            r = await client.get(
                f"https://api.themoviedb.org/3/{path}",
                params={"api_key": self.settings.tmdb_api_key, "language": "en-US", **params},
                timeout=20,
            )
            r.raise_for_status()
            return r.json()

        if self.client:
            return await fetch(self.client)
        async with httpx.AsyncClient() as client:
            return await fetch(client)

    async def search(self, title):
        result = await self.get("search/multi", query=title, include_adult="false")
        return [x for x in (result or {}).get("results", []) if x.get("media_type") in ("movie", "tv")]

    async def cache_work(self, db, tmdb_id, kind):
        if kind not in ("movie", "tv"):
            raise ValueError("TMDB kind must be movie or tv")
        fmt = "film" if kind == "movie" else "series"
        work = db.scalar(select(Work).where(Work.tmdb_id == tmdb_id, Work.format == fmt))
        if (
            work
            and work.metadata_updated_at
            and aware(work.metadata_updated_at) > utcnow() - timedelta(days=7)
        ):
            return work
        data = await self.get(f"{kind}/{tmdb_id}", append_to_response="alternative_titles")
        if not data:
            return work
        title = data.get("title") or data.get("name")
        work = work or Work(tmdb_id=tmdb_id, format=fmt)
        work.title, work.original_title = (
            title,
            data.get("original_title") or data.get("original_name") or title,
        )
        work.slug, work.overview = slugify(title)[:220] or "title", data.get("overview", "")
        date = data.get("release_date") or data.get("first_air_date") or ""
        work.year = int(date[:4]) if date[:4].isdigit() else None
        work.genres = [g["name"] for g in data.get("genres", [])]
        work.countries = [c["iso_3166_1"] for c in data.get("production_countries", [])]
        work.production_type = (
            "animation" if any(g["id"] == 16 for g in data.get("genres", [])) else "live_action"
        )
        work.poster_url = (
            "https://image.tmdb.org/t/p/w780" + data["poster_path"] if data.get("poster_path") else None
        )
        work.metadata_updated_at = utcnow()
        db.add(work)
        db.flush()
        aliases = data.get("alternative_titles", {})
        names = {
            work.original_title,
            *(a["title"] for a in aliases.get("titles", aliases.get("results", []))),
        }
        existing = {a.name for a in work.aliases}
        for name in names - existing:
            work.aliases.append(WorkAlias(name=name))
        db.flush()
        return work
