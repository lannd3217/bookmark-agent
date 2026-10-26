import os
import pathlib
import tempfile

import pytest

# Before anything imports app.main (which loads .env): tests always use SQLite, never the
# DATABASE_URL in .env. load_dotenv doesn't override variables that are already set, and
# db treats an empty DATABASE_URL as unset. Auth is off unless a test turns it on.
os.environ["DATABASE_URL"] = ""
os.environ["AUTH_DISABLED"] = "1"
os.environ["CRON_SECRET"] = ""
os.environ["API_TOKEN"] = ""
os.environ.pop("VERCEL", None)

import httpx

import app.categorize as categorizer
import app.db as db
import app.summarize as summarizer
from app.sources import arxiv_fulltext

# app.main runs db.init() on import; make sure that never touches the real bookmarks.db.
# Tests that need a database point DB_PATH at their own tmp_path.
db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "import.db"


@pytest.fixture(autouse=True)
def no_categorize_api_calls(monkeypatch):
    """Saving a bookmark categorizes it; never call the real model from tests.
    Tests that care about the result patch categorize again with their own fake."""
    monkeypatch.setattr(categorizer, "categorize", lambda item, categories=None: categorizer.FALLBACK)


@pytest.fixture(autouse=True)
def no_summarize_api_or_arxiv_calls(monkeypatch):
    """Summaries never reach the model or arxiv.org in tests; tests that summarize fake both."""
    def real_summarize_called(*args, **kwargs):
        raise AssertionError("a test reached the real summarize(); patch app.summarize.summarize")

    def offline(request):
        raise httpx.ConnectError(f"tests are offline: {request.url}")

    monkeypatch.setattr(summarizer, "summarize", real_summarize_called)
    monkeypatch.setattr(arxiv_fulltext, "_client", lambda: httpx.Client(transport=httpx.MockTransport(offline)))
