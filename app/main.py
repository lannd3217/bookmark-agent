from fastapi import FastAPI, HTTPException
from pathlib import Path
from fastapi.responses import FileResponse
from pydantic import BaseModel
from . import db

app = FastAPI(title="Bookmark Agent")
db.init()

STATIC = Path(__file__).parent.parent / "static"

class SaveIn(BaseModel):
    url: str
    note: str | None = None
@app.get("/")
def hello():
    return FileResponse(STATIC / "index.html")

@app.post("/api/bookmarks")
def save(inp: SaveIn):
    if not inp.url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    return db.insert(inp.url, inp.note)

@app.get("/api/bookmarks")
def list_bookmarks():
    return db.list_all()

@app.delete("/api/bookmarks/{bid}")
def delete_bookmark(bid: int):
    db.delete(bid)
    return {"ok": True}