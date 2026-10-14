"""Prepare publication copy under each publisher's independently recorded rights."""

from html import escape
from app.services.article_html import dom, sanitize_html, content_text, text_html


def publication_copy(article, source, event, english_html=None):
    if source.publication_mode not in ("full", "facts"):
        raise ValueError("Unknown publication mode")
    if source.publication_mode == "full":
        soup = dom(article.sanitized_html)
        if not source.allow_media:
            for tag in soup.find_all(["img", "picture", "video", "audio", "source"]):
                if tag.name:
                    tag.decompose()
            for figure in soup.find_all("figure"):
                if not figure.find(["iframe", "a"]):
                    figure.decompose()
        if not source.allow_embeds:
            from app.services.article_html import fallback_link

            for tag in soup.find_all("iframe"):
                tag.replace_with(dom(fallback_link(tag.get("src", ""))))
        html = sanitize_html(str(soup))
        return article.title, article.subtitle or content_text(html)[:320], html, article.language
    # Facts mode never copies source paragraphs as original prose; media rights are separate.
    # Headline metadata supplies an attributed claim; explicit, parsed facts add
    # detail without inventing relationships (a mentioned date is not a release date).
    title = article.english_title or article.title
    attribution = f"{source.name} reports: {title.rstrip('.')} .".replace(" .", ".")
    paragraphs = [attribution]
    from app.services.factual_brief import extract_facts

    extracted = extract_facts(english_html or article.sanitized_html)
    if not extracted and source.excerpt_limit == 0:
        raise ValueError(
            "Insufficient supported facts for a complete original brief; configure a factual adapter or a permitted excerpt/full reproduction mode"
        )
    paragraphs.extend(fact.text for fact in extracted)
    article.raw_data = {
        **article.raw_data,
        "fact_evidence": [{"text": fact.text, "evidence": fact.evidence} for fact in extracted],
    }
    if event.facts.get("certainty"):
        paragraphs.append("The reported information is unconfirmed.")
    html = text_html("\n".join(paragraphs))
    if source.excerpt_limit > 0:
        excerpt = content_text(english_html or article.sanitized_html)[: source.excerpt_limit]
        if len(article.plain_text) > source.excerpt_limit:
            excerpt = excerpt.rsplit(" ", 1)[0] + "…"
        html += f'<blockquote lang="en">{escape(excerpt)}</blockquote>'

    # Text republication and media licenses are independent. Licensed pictures and
    # widgets can accompany an original factual brief without copying source prose.
    media_doc = dom(english_html or article.sanitized_html)
    media_tags = ["figure", "picture", "img", "video", "audio", "iframe"]
    for root in media_doc.find_all(media_tags):
        if root.find_parent(media_tags):
            continue
        if root.name == "figure":
            parts = []
            for node in root.find_all(media_tags[1:]):
                if node.find_parent(media_tags[1:]):
                    continue
                if (node.name == "iframe" and source.allow_embeds) or (
                    node.name != "iframe" and source.allow_media
                ):
                    parts.append(str(node))
            if parts:
                caption = root.find("figcaption")
                caption_html = (
                    f"<figcaption>{escape(caption.get_text(' ', strip=True))}</figcaption>" if caption else ""
                )
                html += "<figure>" + "".join(parts) + caption_html + "</figure>"
        elif (root.name == "iframe" and source.allow_embeds) or (
            root.name != "iframe" and source.allow_media
        ):
            html += str(root)
            if root.name == "iframe":
                from app.services.article_html import fallback_link

                html += fallback_link(root.get("src", ""))
    html += f'<p><a href="{escape(article.canonical_url, quote=True)}">Source: {escape(source.name)}</a></p>'
    return title, attribution, sanitize_html(html), "en"
