from urllib.parse import urlsplit, urlunsplit

PLATFORM_DOMAINS = {
    "x.com": "x",
    "twitter.com": "x",
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "substack.com": "substack",
}

TRACKING_PARAMS = {"fbclid", "gclid"}


def detect_platform(url: str) -> str:
    host = urlsplit(url).hostname or ""
    host = host.removeprefix("www.")
    for domain, platform in PLATFORM_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return "web"


def _is_tracking(param: str) -> bool:
    name = param.split("=", 1)[0]
    return name.startswith("utm_") or name in TRACKING_PARAMS


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    # Lowercase only the host, keeping any userinfo and port as written.
    userinfo, at, hostport = parts.netloc.rpartition("@")
    host, colon, port = hostport.partition(":")
    if hostport.startswith("["):  # IPv6 literal, e.g. [::1]:8000
        host, bracket, rest = hostport.partition("]")
        host, colon, port = host + bracket, rest[:1], rest[1:]
    netloc = userinfo + at + host.lower() + colon + port
    query = "&".join(p for p in parts.query.split("&") if p and not _is_tracking(p))
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, query, ""))
