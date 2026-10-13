"""Deduplication for recurring imports.

Tracks every imported item by a content fingerprint stored in a
`_burp2caido_imported` table inside the Caido database. On subsequent
imports the seeder calls `is_duplicate()` per item, O(1) via an
indexed BLOB column, and skips matches.

Fingerprint = SHA-256(host || port || method || path || query ||
                      timestamp_ms || raw_request_bytes)

The timestamp from Burp's proxy history anchors identity: the same raw
request recorded at different times is a distinct proxy history entry.
"""

import hashlib
import sqlite3

_TABLE = "_burp2caido_imported"

_CREATE = f"""\
CREATE TABLE IF NOT EXISTS "{_TABLE}" (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  fp        BLOB    NOT NULL UNIQUE,
  import_ts BIGINT  NOT NULL
);
"""


def fingerprint(item: dict) -> bytes:
    """Compute a 32-byte SHA-256 fingerprint for one parsed item."""
    h = hashlib.sha256()
    h.update(item["host"].encode())
    h.update(str(item["port"]).encode())
    h.update(item["method"].encode())
    h.update(item["path"].encode())
    h.update((item["query"] or "").encode())
    h.update(str(item["timestamp_ms"]).encode())
    h.update(item["raw_req"])
    return h.digest()


def ensure_table(db: sqlite3.Connection):
    """Create the tracking table if it doesn't exist."""
    db.executescript(_CREATE)


def load_existing(db: sqlite3.Connection) -> set[bytes]:
    """Load all known fingerprints into a set for fast lookup."""
    ensure_table(db)
    rows = db.execute(f"SELECT fp FROM \"{_TABLE}\"").fetchall()
    return {r[0] for r in rows}


def record(db: sqlite3.Connection, fp: bytes, import_ts: int):
    """Record a fingerprint after successful insertion."""
    db.execute(
        f"INSERT OR IGNORE INTO \"{_TABLE}\" (fp, import_ts) VALUES (?, ?)",
        (fp, import_ts),
    )
