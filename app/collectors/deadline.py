from app.collectors.base import RSSAdapter


class DeadlineAdapter(RSSAdapter):
    key = "deadline"
    allowed_hosts = {"deadline.com", "www.deadline.com"}
