from sqlalchemy import select
from app.db.models import Source, Category

CATEGORIES = ("movies", "series", "anime", "animation", "dramas", "industry")
SOURCES = [
    dict(
        key="variety",
        name="Variety",
        site_url="https://variety.com",
        feed_url="https://variety.com/feed/",
        terms_url="https://www.pmc.com/terms-of-use",
        enabled=False,
        access_status="permission_required",
        policy_note="PMC terms restrict automated collection. Obtain permission before enabling; no article scraping.",
    ),
    dict(
        key="deadline",
        name="Deadline",
        site_url="https://deadline.com",
        feed_url="https://deadline.com/feed/",
        terms_url="https://www.pmc.com/terms-of-use",
        enabled=False,
        access_status="permission_required",
        policy_note="PMC terms restrict automated collection. Obtain permission before enabling; no article scraping.",
    ),
    dict(
        key="anime_corner",
        name="Anime Corner",
        site_url="https://animecorner.me",
        feed_url="https://animecorner.me/feed/",
        terms_url="https://animecorner.me/contact/",
        enabled=True,
        access_status="rss_only",
        policy_note="Public RSS for private editorial review, with attribution. No full-text or image republication. Recheck rights before public use.",
    ),
    dict(
        key="dramabeans",
        name="Dramabeans",
        site_url="https://www.dramabeans.com",
        feed_url="https://www.dramabeans.com/feed/",
        terms_url="https://www.dramabeans.com/",
        enabled=False,
        access_status="unverified",
        policy_note="Access/terms must be verified before enabling. Do not bypass access restrictions.",
    ),
]


def seed(db):
    for slug in CATEGORIES:
        if not db.scalar(select(Category).where(Category.slug == slug)):
            db.add(Category(slug=slug))
    for data in SOURCES:
        if not db.scalar(select(Source).where(Source.key == data["key"])):
            db.add(Source(**data))
    db.commit()
