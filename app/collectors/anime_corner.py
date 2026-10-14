from app.collectors.base import RSSAdapter


class AnimeCornerAdapter(RSSAdapter):
    key = "anime_corner"
    allowed_hosts = {"animecorner.me", "www.animecorner.me"}
