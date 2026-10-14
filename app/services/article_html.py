"""One trust boundary for scraped, translated, edited and rendered article fragments."""

from dataclasses import dataclass
from html import escape
import ipaddress
import re
from urllib.parse import urljoin, urlsplit, parse_qs

from bs4 import BeautifulSoup
import nh3


def dom(html):
    return BeautifulSoup(html or "", "html.parser")


def safe_url(value, base=""):
    value = str(value or "").strip()
    if not value or re.search(r"[\x00-\x20\\]", value):
        return None
    try:
        value = urljoin(base, value)
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
            return None
        if parts.port not in (None, 80, 443):
            return None
        host = parts.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")) or "." not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return value
    except ValueError:
        return None


@dataclass(frozen=True)
class EmbedProvider:
    name: str
    hosts: frozenset[str]
    frame_host: str
    frame_path: str
    sandbox: str = "allow-scripts allow-same-origin allow-presentation"


# Adding a provider also expands the CSP from this registry. Paths must be constrained.
PROVIDERS = {
    "youtube": EmbedProvider(
        "youtube",
        frozenset({"youtube.com", "www.youtube.com", "youtu.be", "www.youtube-nocookie.com"}),
        "www.youtube-nocookie.com",
        r"/embed/[A-Za-z0-9_-]{6,64}",
    ),
    "twitter": EmbedProvider(
        "twitter",
        frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com", "platform.twitter.com"}),
        "platform.twitter.com",
        r"/embed/Tweet.html",
    ),
    "instagram": EmbedProvider(
        "instagram",
        frozenset({"instagram.com", "www.instagram.com"}),
        "www.instagram.com",
        r"/(?:p|reel)/[A-Za-z0-9_-]+/embed/",
    ),
    "tiktok": EmbedProvider(
        "tiktok", frozenset({"tiktok.com", "www.tiktok.com"}), "www.tiktok.com", r"/player/v1/\d+"
    ),
    "vimeo": EmbedProvider(
        "vimeo",
        frozenset({"vimeo.com", "www.vimeo.com", "player.vimeo.com"}),
        "player.vimeo.com",
        r"/video/\d+",
    ),
}


def embed_url(url):
    url = safe_url(url)
    if not url:
        return None, None
    p = urlsplit(url)
    for name, provider in PROVIDERS.items():
        if p.hostname not in provider.hosts:
            continue
        if name == "youtube":
            video = (
                p.path.strip("/")
                if p.hostname == "youtu.be"
                else (parse_qs(p.query).get("v", [""])[0] or p.path.split("/")[-1])
            )
            if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", video):
                return name, f"https://{provider.frame_host}/embed/{video}"
        elif name == "twitter":
            match = re.search(r"/status/(\d+)", p.path)
            video = match[1] if match else parse_qs(p.query).get("id", [""])[0]
            if video.isdigit():
                return name, f"https://{provider.frame_host}/embed/Tweet.html?id={video}"
        elif name == "instagram":
            match = re.match(r"/(p|reel)/([A-Za-z0-9_-]+)(?:/|$)", p.path)
            if match:
                return name, f"https://{provider.frame_host}/{match[1]}/{match[2]}/embed/"
        elif name in ("tiktok", "vimeo"):
            match = re.search(r"/(\d+)/?$", p.path)
            if match:
                path = "player/v1" if name == "tiktok" else "video"
                return name, f"https://{provider.frame_host}/{path}/{match[1]}"
        elif p.hostname == provider.frame_host and re.fullmatch(provider.frame_path, p.path) and not p.query:
            return name, f"https://{provider.frame_host}{p.path}"
    return None, None


def frame_provider(url):
    name, canonical = embed_url(url)
    return PROVIDERS[name] if canonical and canonical == url else None


def fallback_link(url, label="Open original media"):
    return (
        f'<p class="embed-fallback"><a href="{escape(url, quote=True)}">{escape(label)}</a></p>'
        if safe_url(url)
        else ""
    )


def frame_html(url, title="Embedded media"):
    provider = frame_provider(url)
    if not provider:
        return ""
    return (
        f'<iframe src="{escape(url, quote=True)}" title="{escape(title, quote=True)}" '
        f'sandbox="{provider.sandbox}" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" '
        'allow="fullscreen; picture-in-picture" allowfullscreen=""></iframe>'
    )


TAGS = set(
    "p div section span h2 h3 h4 h5 h6 strong b em i u s del sub sup br hr ul ol li blockquote a figure figcaption picture img source video audio iframe table thead tbody tfoot tr th td caption pre code time abbr".split()
)
ATTRS = {
    "*": {"title", "lang"},
    "a": {"href"},
    "img": {"src", "srcset", "sizes", "alt", "loading", "width", "height"},
    "source": {"src", "srcset", "sizes", "type", "media"},
    "video": {"src", "poster", "controls", "preload"},
    "audio": {"src", "controls", "preload"},
    "iframe": {"src", "sandbox", "loading", "referrerpolicy", "allow", "allowfullscreen"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "ol": {"start", "reversed"},
    "li": {"value"},
    "time": {"datetime"},
}


def normalize_srcset(value, base=""):
    result = []
    for candidate in (value or "").split(","):
        bits = candidate.split()
        if not bits or len(bits) > 2:
            continue
        url = safe_url(bits[0], base)
        descriptor = bits[1] if len(bits) == 2 else ""
        if url and (not descriptor or re.fullmatch(r"\d+(?:\.\d+)?[wx]", descriptor)):
            result.append(url + (" " + descriptor if descriptor else ""))
    return ", ".join(result)


def sanitize_html(html, base=""):
    soup = dom(html)
    for tag in soup.find_all(["script", "style", "svg", "math", "object", "embed", "form", "template"]):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in ("href", "src", "poster"):
            if attr in tag.attrs:
                url = safe_url(tag[attr], base)
                if url:
                    tag[attr] = url
                else:
                    del tag[attr]
        if "srcset" in tag.attrs:
            tag["srcset"] = normalize_srcset(tag["srcset"], base)
        if tag.name == "iframe":
            provider = frame_provider(tag.get("src", ""))
            if not provider:
                replacement = dom(fallback_link(tag.get("src", "")))
                tag.replace_with(replacement)
                continue
            tag.attrs = {
                "src": tag["src"],
                "title": tag.get("title", "Embedded media"),
                "sandbox": provider.sandbox,
                "loading": "lazy",
                "referrerpolicy": "strict-origin-when-cross-origin",
                "allow": "fullscreen; picture-in-picture",
                "allowfullscreen": "",
            }
        elif tag.name == "img":
            if not tag.get("src"):
                tag.decompose()
                continue
            tag["loading"] = "lazy"
        elif tag.name in ("video", "audio"):
            tag["controls"], tag["preload"] = "", "none"
    return nh3.clean(
        str(soup),
        tags=TAGS,
        attributes=ATTRS,
        url_schemes={"https", "http"},
        clean_content_tags={"script", "style", "svg", "math", "template"},
        allowed_classes={"div": {"gallery", "embed"}, "p": {"embed-fallback"}},
        url_relative="deny",
    )


def content_text(html):
    return dom(html).get_text(" ", strip=True)


def text_html(text):
    return "".join(f"<p>{escape(p)}</p>" for p in text.splitlines() if p.strip())


def validate_html(html):
    """Require a stable sanitized DOM, readable copy, and an actual paragraph/list/quote."""
    soup = dom(html)
    return bool(
        content_text(html).strip()
        and soup.find(["p", "li", "blockquote"])
        and dom(sanitize_html(html)) == soup
    )
