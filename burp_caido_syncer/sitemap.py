"""Caido sitemap_entries tree builder."""

import json
import sqlite3

from .parsers import sha1


class SitemapBuilder:
    """Incrementally builds the sitemap_entries tree in Caido's database.

    Tree structure:  domain → directory → … → request → request_query / request_body

    Hash rules (verified against real Caido data):
        domain:        sha1("{scheme}://{host}:{port}")
        directory:     sha1("/{segment}")
        request:       sha1("/{filename}")
        request_query: sha1("?{full_query}")
        request_body:  sha1(full_body_text)
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.next_id = 1
        # Cache: (parent_id, kind, hash_bytes) -> sitemap_entry_id
        self.cache: dict[tuple, int] = {}

    def load_existing(self):
        """Pre-load existing sitemap entries into cache."""
        for row in self.db.execute(
            "SELECT id, hash, kind, parent_id FROM sitemap_entries"
        ).fetchall():
            key = (row[3], row[2], row[1])
            self.cache[key] = row[0]
            if row[0] >= self.next_id:
                self.next_id = row[0] + 1

    def _insert(self, hash_bytes: bytes, label: str, kind: str,
                request_id: int | None, parent_id: int | None,
                metadata: str | None = None) -> int:
        """Insert a sitemap entry or return existing id from cache."""
        key = (parent_id, kind, hash_bytes)
        if key in self.cache:
            return self.cache[key]

        sid = self.next_id
        self.next_id += 1
        self.db.execute(
            "INSERT INTO sitemap_entries (id, hash, label, kind, request_id, parent_id, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, hash_bytes, label, kind, request_id, parent_id, metadata),
        )
        self.cache[key] = sid
        return sid

    def add_request(self, host: str, port: int, is_tls: bool,
                    path: str, query: str, request_id: int,
                    raw_req: bytes):
        """Add a request to the sitemap tree."""
        scheme = "https" if is_tls else "http"

        # 1. Domain node
        domain_hash = sha1(f"{scheme}://{host}:{port}")
        metadata = json.dumps({"is_tls": is_tls, "port": port})
        domain_id = self._insert(domain_hash, host, "domain", None, None, metadata)

        # 2. Walk path segments
        segments = [s for s in path.split("/") if s]
        parent_id = domain_id

        if not segments:
            req_hash = sha1("/")
            req_node = self._insert(req_hash, "", "request", None, parent_id)
        else:
            for seg in segments[:-1]:
                dir_hash = sha1(f"/{seg}")
                parent_id = self._insert(dir_hash, seg, "directory", None, parent_id)

            last = segments[-1]
            req_hash = sha1(f"/{last}")
            req_node = self._insert(req_hash, last, "request", None, parent_id)

        # 3. Leaf node
        if query:
            leaf_hash = sha1(f"?{query}")
            label = query[:100]
            self._insert(leaf_hash, label, "request_query", request_id, req_node)
        else:
            body = b""
            parts = raw_req.split(b"\r\n\r\n", 1)
            if len(parts) > 1:
                body = parts[1]

            if body:
                body_text = body.decode("utf-8", errors="replace")
                leaf_hash = sha1(body_text)
                label = body_text[:100]
                self._insert(leaf_hash, label, "request_body", request_id, req_node)
            else:
                self.db.execute(
                    "UPDATE sitemap_entries SET request_id = ? WHERE id = ? AND request_id IS NULL",
                    (request_id, req_node),
                )
