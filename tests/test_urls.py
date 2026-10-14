import pytest
from app.services.urls import normalize_url


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            " HTTPS://Example.COM:443/news/?utm_source=rss&b=2&a=1#section ",
            "https://example.com/news/?a=1&b=2",
        ),
        ("https://EXAMPLE.com/%7euser?q=film&fbclid=abc", "https://example.com/~user?q=film"),
        ("https://example.com/path%2ftitle", "https://example.com/path%2Ftitle"),
        ("http://example.com:80", "http://example.com/"),
        ("https://example.com/?season=2&season=1", "https://example.com/?season=1&season=2"),
    ],
)
def test_normalize(url, expected):
    assert normalize_url(url) == expected
    assert normalize_url(expected) == expected


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "/relative", "https://user:password@example.com/", "data:text/plain,x"]
)
def test_invalid_url(url):
    with pytest.raises(ValueError):
        normalize_url(url)
