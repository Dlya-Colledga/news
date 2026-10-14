import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote

TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}


def normalize_url(url: str) -> str:
    p = urlsplit(url.strip())
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Expected an absolute HTTP(S) URL without credentials")
    host = p.hostname.lower().encode("idna").decode()
    port = p.port
    if port and port not in (80, 443):
        host += f":{port}"
    path = p.path or "/"
    # Decode only unreserved characters: %2F must not change path semantics.
    path = re.sub(
        r"%[0-9a-fA-F]{2}",
        lambda m: unquote(m[0]) if re.match(r"[A-Za-z0-9._~-]", unquote(m[0])) else m[0].upper(),
        path,
    )
    path = quote(path, safe="/%:@!$&'()*+,;=-._~")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING
    )
    return urlunsplit((p.scheme.lower(), host, path, urlencode(query), ""))
