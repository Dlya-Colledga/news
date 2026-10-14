"""Publication readiness shared by workers, editor actions and approval gates."""

from app.services.article_html import validate_html, content_text, dom, safe_url

STATUSES = (
    "DISCOVERED",
    "SCRAPING",
    "PROCESSING",
    "TRANSLATING",
    "READY_FOR_REVIEW",
    "APPROVED",
    "PUBLISHED",
    "REJECTED",
    "FAILED",
)
PROTECTED = {"APPROVED", "PUBLISHED", "REJECTED"}


def readiness_errors(event):
    errors = []
    if not event.articles or not any(a.scrape_status == "ready" for a in event.articles):
        errors.append("A successfully scraped source article is required")
    allowed_media, allowed_embeds = set(), set()
    for article in event.articles:
        if article.source.allow_media:
            for asset in article.assets:
                if asset.permitted:
                    allowed_media.add(asset.url)
                    allowed_media.update(c.strip().split()[0] for c in asset.srcset.split(",") if c.strip())
        if article.source.allow_embeds:
            allowed_embeds.update(e.embed_url for e in article.embeds if e.permitted and e.embed_url)
    covers = {
        article.featured_image
        for article in event.articles
        if safe_url(article.featured_image)
        and any(asset.kind == "img" and asset.url == article.featured_image for asset in article.assets)
    }
    if event.image_url and event.image_url not in allowed_media | covers:
        errors.append("Featured image is not a verified source image")
    for language in ("en", "ru", "ky"):
        translation = next((t for t in event.translations if t.language == language), None)
        if not translation or translation.status != "ready":
            errors.append(f"{language.upper()} translation is not ready")
        elif not all((translation.title.strip(), translation.summary.strip(), translation.body.strip())):
            errors.append(f"{language.upper()} is incomplete")
        elif (
            not validate_html(translation.content_html)
            or content_text(translation.content_html) != translation.body
        ):
            errors.append(f"{language.upper()} HTML is invalid or inconsistent")
        if translation and translation.content_html:
            for tag in dom(translation.content_html).find_all(["img", "source", "audio", "video", "iframe"]):
                urls = {tag[k] for k in ("src", "poster") if tag.get(k)}
                urls.update(c.strip().split()[0] for c in tag.get("srcset", "").split(",") if c.strip())
                allowed = allowed_embeds if tag.name == "iframe" else allowed_media
                if not urls <= allowed:
                    errors.append(f"{language.upper()} contains media without recorded source permission")
    # Policy revocation must take effect before approval, including already prepared media.
    for article in event.articles:
        if article.scrape_status != "ready":
            continue
        source, rights = article.source, article.rights_snapshot
        if rights.get("publication_mode") == "full" and source.publication_mode != "full":
            errors.append("Full republication permission was revoked; retry processing")
        if rights.get("excerpt_limit", 0) > source.excerpt_limit:
            errors.append("Excerpt permission was reduced; retry processing")
        if rights.get("allow_media") and not source.allow_media:
            errors.append("Media permission was revoked; retry processing")
        if rights.get("allow_embeds") and not source.allow_embeds:
            errors.append("Embed permission was revoked; retry processing")
    return errors


def refresh_readiness(event):
    if event.status in PROTECTED:
        return
    errors = readiness_errors(event)
    event.status = "TRANSLATING" if errors else "READY_FOR_REVIEW"
    event.editorial_note = (
        "; ".join(errors) if errors else "EN / RU / KY готовы. Требуется одобрение администратора."
    )
