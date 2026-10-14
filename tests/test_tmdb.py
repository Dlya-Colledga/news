import httpx
from app.core.config import Settings
from app.services.tmdb import TMDBService


async def test_optional_tmdb_no_key(db):
    svc = TMDBService(settings=Settings(tmdb_api_key="", _env_file=None))
    assert await svc.search("Fixture") == []
    assert await svc.cache_work(db, 42, "movie") is None


async def test_tmdb_caching_and_aliases(db):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(
            200,
            json={
                "id": 42,
                "title": "Fixture movie",
                "original_title": "Original fixture",
                "release_date": "2026-01-01",
                "genres": [{"id": 16, "name": "Animation"}],
                "production_countries": [{"iso_3166_1": "JP"}],
                "poster_path": "/fixture.jpg",
                "overview": "Fixture metadata",
                "alternative_titles": {"titles": [{"title": "別名"}]},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        svc = TMDBService(client, Settings(tmdb_api_key="fixture-key", _env_file=None))
        work = await svc.cache_work(db, 42, "movie")
        db.commit()
        assert work.year == 2026 and work.countries == ["JP"] and work.production_type == "animation"
        assert "別名" in [a.name for a in work.aliases]
        assert (await svc.cache_work(db, 42, "movie")).id == work.id
    assert len(calls) == 1
