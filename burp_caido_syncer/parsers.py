"""Low-level HTTP parsing helpers."""

import hashlib
import json
import os


def sha1(text: str) -> bytes:
    """SHA-1 hash as 20 raw bytes."""
    return hashlib.sha1(text.encode("utf-8")).digest()


def parse_headers_and_body_position(raw: bytes) -> tuple[str | None, int | None]:
    """Parse raw HTTP data into a JSON headers string and body byte offset.

    Returns (headers_json, body_position) matching Caido's raw table format.
    """
    sep = raw.find(b"\r\n\r\n")
    if sep == -1:
        sep = raw.find(b"\n\n")
        if sep == -1:
            return None, None
        body_pos = sep + 2
        header_block = raw[:sep]
        line_sep = b"\n"
    else:
        body_pos = sep + 4
        header_block = raw[:sep]
        line_sep = b"\r\n"

    lines = header_block.split(line_sep)
    headers = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        key = name.decode(errors="replace").strip().lower()
        val = value.decode(errors="replace").strip()
        headers[key] = val

    headers_json = json.dumps(headers, separators=(",", ":")) if headers else None
    return headers_json, body_pos


def parse_request_line(raw: bytes) -> tuple[str, str, str, str | None]:
    """Extract (method, path, query, file_extension) from raw HTTP request."""
    first_line = raw.split(b"\n", 1)[0].rstrip(b"\r")
    parts = first_line.split(b" ")
    method = parts[0].decode(errors="replace") if len(parts) > 0 else "GET"
    full_path = parts[1].decode(errors="replace") if len(parts) > 1 else "/"
    if "?" in full_path:
        path, query = full_path.split("?", 1)
    else:
        path, query = full_path, ""
    last_seg = path.split("/")[-1]
    ext = os.path.splitext(last_seg)[1].lstrip(".") or None
    return method, path, query, ext


def parse_status_code(raw: bytes) -> int:
    """Extract status code from raw HTTP response."""
    first_line = raw.split(b"\n", 1)[0].rstrip(b"\r")
    parts = first_line.split(b" ")
    try:
        return int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return 0
