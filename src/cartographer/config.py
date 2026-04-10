"""Project configuration for graph-context."""

from __future__ import annotations

import json
from pathlib import Path

GRAPH_CONTEXT_DIR = ".graph-context"
DB_DIR = "db"
META_FILE = "meta.json"


def get_project_dir(repo_path: str | Path) -> Path:
    return Path(repo_path) / GRAPH_CONTEXT_DIR


def get_db_path(repo_path: str | Path) -> Path:
    return get_project_dir(repo_path) / DB_DIR


def get_meta_path(repo_path: str | Path) -> Path:
    return get_project_dir(repo_path) / META_FILE


def load_meta(repo_path: str | Path) -> dict:
    meta_path = get_meta_path(repo_path)
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    return {}


def save_meta(repo_path: str | Path, meta: dict) -> None:
    meta_path = get_meta_path(repo_path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
