import pathlib
import tempfile

import app.db as db

# app.main runs db.init() on import; make sure that never touches the real bookmarks.db.
# Tests that need a database point DB_PATH at their own tmp_path.
db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "import.db"
