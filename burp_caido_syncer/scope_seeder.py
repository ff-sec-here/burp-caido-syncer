"""Seed Burp target scope into Caido's scopes table."""

import json
import re
import sqlite3
import sys


def _parse_burp_scope_json(raw_json: str) -> tuple[list[str], list[str]]:
    """Parse Burp's exportProjectOptionsAsJson("target") output.

    Burp's scope entries are URL-matching rules with protocol/host/port/file
    regex fields. We convert host patterns to Caido's simpler wildcard format.

    Returns (allowlist, denylist) of Caido-style patterns.
    """
    data = json.loads(raw_json)

    target = data.get("target", data)
    scope = target.get("scope", {})
    advanced = scope.get("advanced_mode", True)

    allowlist = []
    denylist = []

    if advanced:
        for entry in scope.get("include", []):
            pattern = _burp_host_to_caido(entry)
            if pattern:
                allowlist.append(pattern)
        for entry in scope.get("exclude", []):
            pattern = _burp_host_to_caido(entry)
            if pattern:
                denylist.append(pattern)
    else:
        for entry in scope.get("include", []):
            prefix = entry.get("prefix", "")
            if prefix:
                host = _url_to_host_pattern(prefix)
                if host:
                    allowlist.append(host)
        for entry in scope.get("exclude", []):
            prefix = entry.get("prefix", "")
            if prefix:
                host = _url_to_host_pattern(prefix)
                if host:
                    denylist.append(host)

    return allowlist, denylist


def _burp_host_to_caido(entry: dict) -> str | None:
    """Convert a Burp advanced-mode scope entry to a Caido host pattern.

    Burp entry: {"enabled":true, "host":"^example\\.com$", "protocol":"any", ...}
    Caido pattern: "example.com" or "*.example.com"
    """
    if not entry.get("enabled", True):
        return None

    host_regex = entry.get("host", "")
    if not host_regex:
        return None

    host = host_regex
    host = host.removeprefix("^").removesuffix("$")
    host = host.replace("\\.", ".")

    if host == ".*":
        return "*"

    if host.startswith(".*"):
        host = "*" + host[2:]
    if host.endswith(".*"):
        host = host[:-2] + "*"

    if re.search(r'[\\()+\[\]{}|^$]', host):
        host = re.sub(r'[\\()+\[\]{}|^$]', '', host)

    host = host.replace(".", ".").strip()
    return host if host else None


def _url_to_host_pattern(url: str) -> str | None:
    """Extract host pattern from a URL prefix like 'https://example.com/'."""
    url = url.strip()
    if "://" in url:
        url = url.split("://", 1)[1]
    host = url.split("/", 1)[0].split(":", 1)[0]
    return host if host else None


def seed_scope(db_path: str, scope_json_path: str, *,
               scope_name: str = "Burp Import",
               verbose: bool = False) -> bool:
    """Insert Burp scope into Caido's scopes table.

    Args:
        db_path:         Path to database.caido
        scope_json_path: Path to the raw JSON from Burp's exportProjectOptionsAsJson
        scope_name:      Name for the scope entry in Caido
        verbose:         Print detail

    Returns True on success.
    """
    with open(scope_json_path, encoding="utf-8") as f:
        raw = f.read()

    allowlist, denylist = _parse_burp_scope_json(raw)

    if not allowlist and not denylist:
        print("[!] No scope entries found in Burp scope JSON")
        return False

    if verbose or True:
        print(f"[*] Scope: {len(allowlist)} allow, {len(denylist)} deny")
        for a in allowlist:
            print(f"    + {a}")
        for d in denylist:
            print(f"    - {d}")

    definition = json.dumps({"allowlist": allowlist, "denylist": denylist})

    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO scopes (definition, version, indexed, name) VALUES (?, 1, 0, ?)",
            (definition, scope_name),
        )
        db.commit()
        print(f"[+] Scope '{scope_name}' created in Caido")
        return True
    except Exception as e:
        print(f"[!] Failed to insert scope: {e}", file=sys.stderr)
        return False
    finally:
        db.close()
