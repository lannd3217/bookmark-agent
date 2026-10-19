import os
import pathlib
import tempfile

# Before anything imports app.main (which loads .env): tests always use SQLite, never the
# DATABASE_URL in .env. load_dotenv doesn't override variables that are already set, and
# db treats an empty DATABASE_URL as unset. Auth is off unless a test turns it on.
os.environ["DATABASE_URL"] = ""
os.environ["AUTH_DISABLED"] = "1"

import app.db as db

# app.main runs db.init() on import; make sure that never touches the real bookmarks.db.
# Tests that need a database point DB_PATH at their own tmp_path.
db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "import.db"
