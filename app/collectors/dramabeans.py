from app.collectors.base import RSSAdapter


class DramabeansAdapter(RSSAdapter):
    key = "dramabeans"
    allowed_hosts = {"dramabeans.com", "www.dramabeans.com"}
