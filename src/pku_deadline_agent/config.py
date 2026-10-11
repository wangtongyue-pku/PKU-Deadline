"""Local storage configuration. No credentials or AI client in stage 1."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def database_path() -> Path:
    configured = os.environ.get("PKU_DATABASE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return PROJECT_ROOT / "data" / "deadlines.sqlite3"


def data_directory() -> Path:
    """Directory for local-only state next to the task database.

    Always inside the project's gitignored ``data/`` (or beside a configured
    database), so browser state and other private files are never committable.
    """
    return database_path().parent
