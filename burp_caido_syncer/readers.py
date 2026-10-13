"""Input readers: NDJSON (from exporter) and Burp XML (legacy)."""

import base64
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .constants import BURP_TO_CAIDO_COLOR
from .parsers import parse_request_line


def read_ndjson(path: str):
    """Yield parsed items from an NDJSON file, one line at a time."""
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[!] Line {lineno}: JSON parse error: {e}", file=sys.stderr)
                continue

            raw_req = None
            raw_resp = None
            if obj.get("request_b64"):
                raw_req = base64.b64decode(obj["request_b64"])
            if obj.get("response_b64"):
                raw_resp = base64.b64decode(obj["response_b64"])

            if raw_req is None:
                print(f"[!] Line {lineno}: no request data, skipping", file=sys.stderr)
                continue

            yield {
                "host": obj.get("host", ""),
                "port": obj.get("port", 80),
                "is_tls": 1 if obj.get("is_tls", False) else 0,
                "method": obj.get("method", "GET"),
                "path": obj.get("path", "/"),
                "query": obj.get("query", ""),
                "file_extension": obj.get("file_extension"),
                "timestamp_ms": obj.get("timestamp_ms", 0),
                "roundtrip_ms": obj.get("roundtrip_ms", 0),
                "edited": 1 if obj.get("edited", False) else 0,
                "highlight": BURP_TO_CAIDO_COLOR.get(obj.get("highlight", ""), None),
                "notes": obj.get("notes"),
                "raw_req": raw_req,
                "raw_resp": raw_resp,
            }


def count_ndjson_lines(path: str) -> int:
    """Fast line count of an NDJSON file (for progress bars)."""
    count = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            count += chunk.count(b"\n")
    return count


def read_burp_xml(path: str):
    """Yield parsed items from a Burp XML export."""
    tree = ET.parse(path)
    for item in tree.findall(".//item"):
        host_el = item.find("host")
        port_el = item.find("port")
        protocol_el = item.find("protocol")
        request_el = item.find("request")
        response_el = item.find("response")
        time_el = item.find("time")

        if request_el is None or not request_el.text:
            continue

        host = host_el.text.strip() if host_el is not None else ""
        port = int(port_el.text.strip()) if port_el is not None else 80
        is_tls = (
            1
            if (protocol_el is not None and protocol_el.text.strip().lower() == "https")
            else 0
        )

        def decode_el(el):
            if el is None or not el.text:
                return None
            b64 = el.attrib.get("base64", "false").lower() == "true"
            return base64.b64decode(el.text) if b64 else el.text.encode()

        raw_req = decode_el(request_el)
        raw_resp = decode_el(response_el)

        if raw_req is None:
            continue

        timestamp_ms = int(time.time() * 1000)
        if time_el is not None and time_el.text:
            try:
                dt = datetime.strptime(
                    time_el.text.strip(), "%a %b %d %H:%M:%S %Z %Y"
                )
                timestamp_ms = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
            except (ValueError, TypeError):
                pass

        method, path_str, query, ext = parse_request_line(raw_req)

        yield {
            "host": host,
            "port": port,
            "is_tls": is_tls,
            "method": method,
            "path": path_str,
            "query": query,
            "file_extension": ext,
            "timestamp_ms": timestamp_ms,
            "roundtrip_ms": 0,
            "edited": 0,
            "highlight": None,
            "notes": None,
            "raw_req": raw_req,
            "raw_resp": raw_resp,
        }
