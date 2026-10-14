from app.collectors.base import RSSAdapter


class VarietyAdapter(RSSAdapter):
    key = "variety"
    allowed_hosts = {"variety.com", "www.variety.com"}
