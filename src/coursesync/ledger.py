"""What has been fetched already (SQLite), so every run only downloads what's new."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("""CREATE TABLE IF NOT EXISTS fetched(
            url TEXT PRIMARY KEY, course TEXT NOT NULL, kind TEXT NOT NULL, path TEXT, name TEXT, week INTEGER,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        self.con.execute("""CREATE TABLE IF NOT EXISTS to_transcribe(
            path TEXT PRIMARY KEY, course TEXT NOT NULL, keep_video INTEGER NOT NULL)""")

    def urls(self, course: str) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT url FROM fetched WHERE course=?", (course,))}

    def bases(self, course: str) -> set[str]:
        """URLs without query string (the same Zoom recording is often re-shared with ?startTime=...)."""
        return {u.split("?")[0] for u in self.urls(course)}

    def basenames(self, course: str) -> set[str]:
        return {u.split("?")[0].rstrip("/").rsplit("/", 1)[-1] for u in self.urls(course)}

    def has(self, course: str, url: str) -> bool:
        return url.split("?")[0] in self.bases(course)

    def record(self, course: str, kind: str, url: str, path: Path | None, week: int | None = None) -> None:
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO fetched(url,course,kind,path,name,week) VALUES (?,?,?,?,?,?)",
                             (url, course, kind, str(path) if path else None, path.name if path else None, week))

    def queue_transcript(self, course: str, path: Path, keep_video: bool) -> None:
        """Survives a failed or interrupted run: a recording stays queued until its transcript exists."""
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO to_transcribe(path,course,keep_video) VALUES (?,?,?)",
                             (str(path), course, keep_video))

    def transcripts_due(self) -> list[tuple[str, str, bool]]:
        return [(r["course"], r["path"], bool(r["keep_video"])) for r in self.con.execute("SELECT * FROM to_transcribe")]

    def transcript_done(self, path: str) -> None:
        with self.con:
            self.con.execute("DELETE FROM to_transcribe WHERE path=?", (path,))

    def names_taken(self, course: str) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT name FROM fetched WHERE course=? AND name IS NOT NULL", (course,))}
