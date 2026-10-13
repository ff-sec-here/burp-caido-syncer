"""Seed Burp Repeater tabs into Caido Replay sessions."""

import base64
import json
import sqlite3
import sys
import time

from .parsers import parse_headers_and_body_position, parse_request_line, parse_status_code


def _next_rank(last_rank: str) -> str:
    if not last_rank:
        return "n"
    return last_rank + "n"


def _ensure_default_collection(db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT id FROM replay_session_collections WHERE name = 'Default Collection'"
    ).fetchone()
    if row:
        return row[0]
    cur = db.execute(
        "INSERT INTO replay_session_collections (name, rank) VALUES ('Default Collection', 'a')"
    )
    return cur.lastrowid


def _get_last_rank(db: sqlite3.Connection, collection_id: int) -> str:
    row = db.execute(
        "SELECT rank FROM replay_sessions WHERE collection_id = ? ORDER BY rank DESC LIMIT 1",
        (collection_id,),
    ).fetchone()
    return row[0] if row else ""


def _default_editor_state() -> str:
    return json.dumps({
        "selection": {"ranges": [{"anchor": 0, "head": 0}], "main": 0},
        "search": {
            "isOpen": False,
            "query": {
                "search": "", "caseSensitive": False, "literal": False,
                "regexp": False, "replace": "", "valid": False, "wholeWord": False,
            },
        },
        "pretty": {"kind": "Enabled"},
    })


def seed_replay(db_path: str, items, *, raw_path: str | None = None,
                verbose: bool = False) -> tuple[int, int]:
    """Insert Repeater tab items into Caido's replay session tables.

    Args:
        db_path:   Path to database.caido
        items:     Iterable of dicts with tab_name, host, port, is_tls,
                   request_b64, response_b64
        raw_path:  Path to database_raw.caido (needed to store responses)
        verbose:   Print per-tab detail

    Returns:
        (ok_count, error_count)
    """
    db = sqlite3.connect(db_path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")

    raw_db = None
    if raw_path:
        raw_db = sqlite3.connect(raw_path, isolation_level=None)
        raw_db.execute("PRAGMA journal_mode=WAL")
        raw_db.execute("PRAGMA synchronous=NORMAL")

    collection_id = _ensure_default_collection(db)
    last_rank = _get_last_rank(db, collection_id)
    editor_state = _default_editor_state()

    ok = 0
    errors = 0
    now_ms = int(time.time() * 1000)

    db.execute("BEGIN")
    if raw_db:
        raw_db.execute("BEGIN")
    try:
        for item in items:
            try:
                tab_name = item.get("tab_name", "Untitled")
                host = item.get("host", "")
                port = item.get("port", 443)
                is_tls = item.get("is_tls", True)

                req_b64 = item.get("request_b64")
                if not req_b64:
                    errors += 1
                    continue

                raw_req = base64.b64decode(req_b64)
                connection = json.dumps({
                    "host": host, "port": port, "is_tls": is_tls,
                })

                # 1. Create draft with the request text
                cur = db.execute(
                    "INSERT INTO replay_entry_http_drafts "
                    "(raw, connection, settings, editor_state) "
                    "VALUES (?, ?, '{}', ?)",
                    (raw_req, connection, editor_state),
                )
                draft_id = cur.lastrowid

                # 2. Create the session
                last_rank = _next_rank(last_rank)
                cur = db.execute(
                    "INSERT INTO replay_sessions "
                    "(name, active_replay_entry_id, collection_id, rank, settings, kind) "
                    "VALUES (?, NULL, ?, ?, NULL, 'http')",
                    (tab_name, collection_id, last_rank),
                )
                session_id = cur.lastrowid

                # 3. If we have response data and raw_db, create full request/response chain
                request_id = None
                resp_b64 = item.get("response_b64")
                if raw_db and raw_req:
                    req_hdrs, req_bpos = parse_headers_and_body_position(raw_req)
                    cur = raw_db.execute(
                        "INSERT INTO requests_raw (data, alteration, source, headers, body_position) "
                        "VALUES (?, 'none', 'replay', ?, ?)",
                        (raw_req, req_hdrs, req_bpos),
                    )
                    req_raw_id = cur.lastrowid

                    response_id = None
                    if resp_b64:
                        raw_resp = base64.b64decode(resp_b64)
                        resp_hdrs, resp_bpos = parse_headers_and_body_position(raw_resp)
                        cur = raw_db.execute(
                            "INSERT INTO responses_raw (data, alteration, source, headers, body_position) "
                            "VALUES (?, 'none', 'replay', ?, ?)",
                            (raw_resp, resp_hdrs, resp_bpos),
                        )
                        resp_raw_id = cur.lastrowid

                        status = parse_status_code(raw_resp)
                        cur = db.execute(
                            "INSERT INTO responses (status_code, raw_id, length, alteration, edited, "
                            "roundtrip_time, created_at) VALUES (?, ?, ?, 'none', 0, 0, ?)",
                            (status, resp_raw_id, len(raw_resp), now_ms),
                        )
                        response_id = cur.lastrowid

                    method, path, _, _ = parse_request_line(raw_req)
                    cur = db.execute(
                        "INSERT INTO requests_metadata (color) VALUES (NULL)",
                    )
                    metadata_id = cur.lastrowid
                    cur = db.execute(
                        "INSERT INTO requests (host, method, path, length, port, raw_id, is_tls, query, "
                        "file_extension, source, response_id, alteration, edited, created_at, "
                        "metadata_id, sni) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, '', NULL, 'replay', ?, 'none', 0, ?, ?, ?)",
                        (host, method, path, len(raw_req), port, req_raw_id, is_tls,
                         response_id, now_ms, metadata_id, host),
                    )
                    request_id = cur.lastrowid

                # 4. Create the replay entry
                cur = db.execute(
                    "INSERT INTO replay_entries_http "
                    "(connection, raw, settings, request_id, error, session_id, "
                    "created_at, draft_id) "
                    "VALUES (?, ?, '{}', ?, NULL, ?, ?, ?)",
                    (connection, raw_req, request_id, session_id, now_ms, draft_id),
                )
                entry_id = cur.lastrowid

                # 5. Point session's active entry to this entry
                db.execute(
                    "UPDATE replay_sessions SET active_replay_entry_id = ? WHERE id = ?",
                    (entry_id, session_id),
                )

                ok += 1
                if verbose:
                    resp_tag = " + response" if resp_b64 and raw_db else ""
                    print(f"  [+] {tab_name} -> {host}:{port}{resp_tag}")

            except Exception as e:
                errors += 1
                print(f"[!] Replay seed error: {item.get('tab_name', '?')} -- {e}", file=sys.stderr)

        db.execute("COMMIT")
        if raw_db:
            raw_db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        if raw_db:
            raw_db.execute("ROLLBACK")
        raise
    finally:
        db.close()
        if raw_db:
            raw_db.close()

    print(f"[+] Replay sessions: {ok} created, {errors} failed")
    return ok, errors


def read_repeater_ndjson(path: str):
    """Yield dicts from a repeater NDJSON file."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
