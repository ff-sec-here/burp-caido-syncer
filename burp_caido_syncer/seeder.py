"""Batch-insert parsed items into Caido's SQLite databases."""

import sqlite3
import sys
import time

from .constants import BATCH_SIZE
from .dedup import ensure_table, fingerprint, load_existing, record
from .parsers import parse_headers_and_body_position, parse_status_code
from .sitemap import SitemapBuilder


def seed(db_path: str, raw_path: str, items, *,
         total_hint: int = 0, verbose: bool = False) -> tuple[int, int]:
    """Insert items into Caido's databases.

    Args:
        db_path:    Path to database.caido
        raw_path:   Path to database_raw.caido
        items:      Iterable of dicts from a reader
        total_hint: Expected item count (for progress display), 0 if unknown
        verbose:    Print per-request detail

    Returns:
        (ok_count, error_count)
    """
    db = sqlite3.connect(db_path)
    raw_db = sqlite3.connect(raw_path)

    # Optimise for bulk insert
    for conn in (db, raw_db):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB

    # Dedup: load existing fingerprints
    known_fps = load_existing(db)
    if known_fps:
        print(f"[*] Dedup: {len(known_fps)} previously imported items tracked")
    import_ts = int(time.time() * 1000)
    skipped = 0

    sitemap = SitemapBuilder(db)
    sitemap.load_existing()

    ok = 0
    errors = 0
    start_time = time.time()

    raw_db.execute("BEGIN")
    db.execute("BEGIN")

    try:
        for item in items:
            try:
                # Dedup check
                fp = fingerprint(item)
                if fp in known_fps:
                    skipped += 1
                    continue

                # 1. Raw request
                req_headers, req_body_pos = parse_headers_and_body_position(item["raw_req"])
                cur = raw_db.execute(
                    "INSERT INTO requests_raw (data, alteration, source, headers, body_position) "
                    "VALUES (?, 'none', 'intercept', ?, ?)",
                    (item["raw_req"], req_headers, req_body_pos),
                )
                req_raw_id = cur.lastrowid

                # 2. Raw response
                resp_raw_id = None
                if item["raw_resp"]:
                    resp_headers, resp_body_pos = parse_headers_and_body_position(item["raw_resp"])
                    cur = raw_db.execute(
                        "INSERT INTO responses_raw (data, alteration, source, headers, body_position) "
                        "VALUES (?, 'none', 'intercept', ?, ?)",
                        (item["raw_resp"], resp_headers, resp_body_pos),
                    )
                    resp_raw_id = cur.lastrowid

                # 3. Response metadata
                response_id = None
                if item["raw_resp"] and resp_raw_id:
                    status = parse_status_code(item["raw_resp"])
                    cur = db.execute(
                        "INSERT INTO responses (status_code, raw_id, length, alteration, edited, "
                        "roundtrip_time, created_at) VALUES (?, ?, ?, 'none', 0, ?, ?)",
                        (status, resp_raw_id, len(item["raw_resp"]),
                         item["roundtrip_ms"], item["timestamp_ms"]),
                    )
                    response_id = cur.lastrowid

                # 4. Request metadata (highlight)
                cur = db.execute(
                    "INSERT INTO requests_metadata (color) VALUES (?)",
                    (item["highlight"],),
                )
                metadata_id = cur.lastrowid

                # 5. Request
                cur = db.execute(
                    "INSERT INTO requests (host, method, path, length, port, raw_id, is_tls, query, "
                    "file_extension, source, response_id, alteration, edited, created_at, "
                    "metadata_id, sni) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'intercept', ?, 'none', ?, ?, ?, ?)",
                    (item["host"], item["method"], item["path"], len(item["raw_req"]),
                     item["port"], req_raw_id, item["is_tls"], item["query"],
                     item["file_extension"], response_id, item["edited"],
                     item["timestamp_ms"], metadata_id, item["host"]),
                )
                request_id = cur.lastrowid

                # 6. Intercept entry
                db.execute(
                    "INSERT INTO intercept_entries (id, request_id) VALUES (?, ?)",
                    (request_id, request_id),
                )

                # 7. Sitemap
                sitemap.add_request(
                    host=item["host"], port=item["port"],
                    is_tls=bool(item["is_tls"]), path=item["path"],
                    query=item["query"], request_id=request_id,
                    raw_req=item["raw_req"],
                )

                # Track fingerprint
                record(db, fp, import_ts)
                known_fps.add(fp)
                ok += 1

                # Batch commit + progress
                if ok % BATCH_SIZE == 0:
                    raw_db.execute("COMMIT")
                    db.execute("COMMIT")
                    raw_db.execute("BEGIN")
                    db.execute("BEGIN")

                    elapsed = time.time() - start_time
                    rate = ok / elapsed if elapsed > 0 else 0
                    skip_msg = f", {skipped} skipped" if skipped else ""
                    if total_hint:
                        pct = (ok + skipped) / total_hint * 100
                        print(f"  [{ok} new, {pct:.0f}% processed{skip_msg}, {rate:.0f} req/s]")
                    else:
                        print(f"  [{ok} imported{skip_msg}, {rate:.0f} req/s]")

                if verbose and ok % 100 == 0:
                    print(f"    {item['host']}:{item['port']} {item['method']} {item['path'][:60]}")

            except Exception as e:
                errors += 1
                print(
                    f"[!] Error #{ok + errors}: {item.get('host', '?')}:{item.get('port', '?')}: {e}",
                    file=sys.stderr,
                )

        # Final commit
        raw_db.execute("COMMIT")
        db.execute("COMMIT")

        # Update snapshot counter
        max_id = db.execute("SELECT MAX(id) FROM requests").fetchone()[0] or 0
        db.execute("UPDATE __caido_snapshot SET value = ?", (max_id * 6,))
        db.commit()

    except Exception:
        raw_db.execute("ROLLBACK")
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()
        raw_db.close()

    elapsed = time.time() - start_time
    rate = ok / elapsed if elapsed > 0 else 0
    skip_msg = f", {skipped} duplicates skipped" if skipped else ""
    print(f"\n[+] Seeded: {ok} new, {errors} failed{skip_msg} ({elapsed:.1f}s, {rate:.0f} req/s)")

    return ok, errors
