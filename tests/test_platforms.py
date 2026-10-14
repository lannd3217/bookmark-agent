import pytest

from app.platforms import detect_platform, normalize_url


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://x.com/user/status/1", "x"),
        ("https://www.x.com/user/status/1", "x"),
        ("https://twitter.com/user", "x"),
        ("https://mobile.twitter.com/user", "x"),
        ("https://www.instagram.com/p/abc/", "instagram"),
        ("https://facebook.com/page", "facebook"),
        ("https://m.facebook.com/page", "facebook"),
        ("https://fb.watch/xyz/", "facebook"),
        ("https://substack.com/home", "substack"),
        ("https://foo.substack.com/p/post", "substack"),
        ("https://WWW.X.COM/user", "x"),
        ("https://example.com/article", "web"),
        ("https://notx.com/", "web"),
        ("https://x.com.evil.example/", "web"),
        ("not a url", "web"),
    ],
)
def test_detect_platform(url, expected):
    assert detect_platform(url) == expected


def test_normalize_removes_utm_params():
    url = "https://example.com/post?utm_source=tw&utm_medium=social&utm_campaign=x"
    assert normalize_url(url) == "https://example.com/post"


def test_normalize_no_query_unchanged():
    assert normalize_url("https://example.com/a/b") == "https://example.com/a/b"


def test_normalize_mixed_params_keeps_order():
    url = "https://example.com/p?b=2&utm_source=x&a=1&fbclid=abc&c=3&gclid=def"
    assert normalize_url(url) == "https://example.com/p?b=2&a=1&c=3"


def test_normalize_drops_fragment():
    assert normalize_url("https://example.com/p?id=1#section") == "https://example.com/p?id=1"


def test_normalize_lowercases_scheme_and_host_only():
    url = "HTTPS://Example.COM:8080/Path/To?Key=Value"
    assert normalize_url(url) == "https://example.com:8080/Path/To?Key=Value"


def test_normalize_keeps_param_encoding():
    url = "https://example.com/search?q=hello%20world&utm_term=x&tag=a+b"
    assert normalize_url(url) == "https://example.com/search?q=hello%20world&tag=a+b"
