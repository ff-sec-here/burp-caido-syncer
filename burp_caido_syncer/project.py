"""Caido project management: find, backup."""

import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .constants import PROJECTS_DB, PROJECTS_DIR


def list_projects() -> list[dict]:
    """List all Caido projects with metadata."""
    if not PROJECTS_DB.exists():
        return []
    db = sqlite3.connect(f"file:{PROJECTS_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, name, version, status, created_at, updated_at FROM projects ORDER BY updated_at DESC"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def find_project_by_name(name: str) -> tuple[str, str]:
    """Find a Caido project by name. Returns (db_path, raw_path)."""
    if not PROJECTS_DB.exists():
        sys.exit("[✗] Caido projects database not found. Is Caido installed?")

    db = sqlite3.connect(f"file:{PROJECTS_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, name FROM projects WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchall()
    db.close()

    if not rows:
        projects = list_projects()
        if projects:
            print("[!] Available projects:", file=sys.stderr)
            for p in projects:
                print(f"    - {p['name']} ({p['id']})", file=sys.stderr)
        sys.exit(f"[✗] No Caido project named '{name}'. Create it in the Caido UI first.")

    if len(rows) > 1:
        print(f"[!] Multiple projects named '{name}':", file=sys.stderr)
        for r in rows:
            print(f"    - {r['id']}", file=sys.stderr)
        sys.exit("[✗] Use --project UUID to pick one.")

    project_id = rows[0]["id"]
    print(f"[+] Found project: {name} ({project_id})")
    return find_project_db(project_id)


def find_project_db(project_id: str | None) -> tuple[str, str]:
    """Find database paths for an existing project."""
    if not PROJECTS_DIR.exists():
        sys.exit("[✗] Caido projects directory not found.")

    if project_id:
        project_dir = PROJECTS_DIR / project_id
    else:
        dirs = sorted(
            PROJECTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if not dirs:
            sys.exit("[✗] No Caido projects found.")
        project_dir = dirs[0]
        print(f"[*] Using project: {project_dir.name}")

    db = str(project_dir / "database.caido")
    raw = str(project_dir / "database_raw.caido")
    if not os.path.exists(db):
        sys.exit(f"[✗] database.caido not found in {project_dir}")
    return db, raw


def backup_project(db_path: str):
    """Backup the project directory containing db_path."""
    project_dir = Path(db_path).parent
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = project_dir.parent / f"{project_dir.name}.backup-{ts}"
    print(f"[*] Backing up to {backup_dir.name}...")
    shutil.copytree(project_dir, backup_dir)
    print(f"[+] Backup complete.")
