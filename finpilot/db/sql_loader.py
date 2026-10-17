"""Loads analytics SQL from finpilot/db/sql/<name>.sql (kept out of Python strings)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import TextClause, text

SQL_DIR = Path(__file__).resolve().parent / "sql"


@lru_cache
def sql(name: str) -> TextClause:
    path = SQL_DIR / f"{name}.sql"
    if not path.is_file() or path.parent != SQL_DIR:
        raise FileNotFoundError(f"unknown SQL file {name}")
    return text(path.read_text(encoding="utf-8"))
