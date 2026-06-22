import sqlite3
from pathlib import Path

_DB = Path("downloads.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS downloaded "
        "(url TEXT PRIMARY KEY, ts REAL DEFAULT (unixepoch('now')))"
    )
    conn.commit()
    return conn


def exists(url: str) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM downloaded WHERE url=?", (url,)
        ).fetchone() is not None


def save(url: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO downloaded (url) VALUES (?)", (url,))
        c.commit()
