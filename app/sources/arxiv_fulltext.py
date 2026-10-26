"""Full text of one arXiv paper, for summaries: the HTML version first, then the PDF.

Fetched from arxiv.org one paper at a time, only when a summary needs it, and the text is
stored so it's never fetched twice. arxiv.org's robots.txt sets a 15-second crawl delay for
automated clients; a module-wide pacer and lock keep to it and to one connection at a time.
"""
import io
import logging
import re
import threading

import httpx
from bs4 import BeautifulSoup

from . import USER_AGENT
from .arxiv import Pacer

HTML_URL = "https://arxiv.org/html/{id}"
PDF_URL = "https://arxiv.org/pdf/{id}"
CRAWL_DELAY = 15.0              # seconds; arxiv.org robots.txt Crawl-delay for generic agents
TIMEOUT = 60.0
MAX_PDF_BYTES = 30_000_000
MAX_PDF_PAGES = 60
MAX_CHARS = 200_000             # stored text; summaries use far less
MIN_CHARS = 2_000               # less than this isn't a paper's full text (e.g. an error page)

log = logging.getLogger(__name__)
_pacer = Pacer(CRAWL_DELAY)
_lock = threading.Lock()        # one request to arxiv.org at a time, across request threads

# 2610.12345, 2610.12345v2, or old-style ids like hep-th/9901001
ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([a-z\-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", re.I)


class FullTextError(Exception):
    """The paper's full text couldn't be fetched or read."""


def arxiv_id_from_url(url: str | None) -> str | None:
    m = ID_RE.search(url or "")
    return m.group(1) if m else None


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def _get(client: httpx.Client, url: str) -> httpx.Response:
    with _lock:
        _pacer.wait()
        return client.get(url)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text)
    return text.strip()[:MAX_CHARS]


def html_to_text(html: str) -> str:
    """Readable text from arXiv's HTML (LaTeXML) rendering; math becomes its LaTeX source."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("article") or soup.body or soup
    for math in root.find_all("math"):
        math.replace_with(f" {math.get('alttext', '')} ")
    for junk in root.select("script, style, nav, header, footer, .ltx_bibliography, .ltx_page_footer, "
                            ".ltx_authors, .ltx_role_footnote"):
        junk.decompose()
    return _tidy(root.get_text("\n"))


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        pages.append(page.extract_text() or "")
    return _tidy("\n\n".join(pages))


def _from_html(client: httpx.Client, arxiv_id: str) -> str:
    r = _get(client, HTML_URL.format(id=arxiv_id))
    if r.status_code != 200:
        raise FullTextError(f"no HTML version (HTTP {r.status_code})")
    text = html_to_text(r.text)
    if len(text) < MIN_CHARS:
        raise FullTextError("HTML version has too little text")
    return text


def _download_pdf(client: httpx.Client, url: str) -> bytes:
    with _lock:
        _pacer.wait()
        with client.stream("GET", url) as r:
            if r.status_code != 200:
                raise FullTextError(f"PDF unavailable (HTTP {r.status_code})")
            data = bytearray()
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_PDF_BYTES:
                    raise FullTextError(f"PDF is larger than {MAX_PDF_BYTES // 1_000_000} MB")
    if not data.startswith(b"%PDF"):
        raise FullTextError("response isn't a PDF")
    return bytes(data)


def _from_pdf(client: httpx.Client, arxiv_id: str) -> str:
    data = _download_pdf(client, PDF_URL.format(id=arxiv_id))
    try:
        text = pdf_to_text(data)
    except Exception as exc:
        raise FullTextError(f"couldn't read the PDF ({type(exc).__name__})") from exc
    if len(text) < MIN_CHARS:
        raise FullTextError("PDF has too little extractable text")
    return text


def fetch_full_text(arxiv_id: str) -> str:
    """The paper's text from its HTML version, else its PDF. Raises FullTextError."""
    problems = []
    with _client() as client:
        for name, fetch in (("HTML", _from_html), ("PDF", _from_pdf)):
            try:
                return fetch(client, arxiv_id)
            except (FullTextError, httpx.HTTPError) as exc:
                problems.append(f"{name}: {exc or type(exc).__name__}")
                log.info("arXiv %s %s failed: %s", arxiv_id, name, exc)
    raise FullTextError("; ".join(problems))
