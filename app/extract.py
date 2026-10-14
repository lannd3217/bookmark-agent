import asyncio
from urllib.parse import urljoin

import httpx
import trafilatura
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
TIMEOUT = 15.0


async def fetch_page(url: str) -> dict:
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as client:
        r = await client.get(url)
    if r.status_code != 200:
        raise httpx.HTTPStatusError(
            f"HTTP {r.status_code} fetching {r.url}", request=r.request, response=r
        )
    # Parsing is CPU-bound; keep it off the event loop.
    return await asyncio.to_thread(_parse, r.text, str(r.url))


def _meta(soup: BeautifulSoup, **attrs) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    content = (tag.get("content") or "").strip() if tag else ""
    return content or None


def _parse(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title = _meta(soup, property="og:title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip() or None

    image = _meta(soup, property="og:image")
    if image:
        image = urljoin(url, image)

    text = trafilatura.extract(html, url=url) or _meta(soup, property="og:description")

    return {
        "title": title,
        "author": _meta(soup, name="author"),
        "text": text,
        "image": image,
    }
