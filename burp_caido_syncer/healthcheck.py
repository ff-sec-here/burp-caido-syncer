"""Pre-flight and post-flight health checks for both Burp and Caido."""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from .constants import BURP_JAR_DEFAULT, CAIDO_BASE, EXPORTER_JAR, PROJECTS_DIR


class CheckResult:
    """One check's outcome."""

    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail

    def __repr__(self):
        if self.ok:
            msg = f"  \033[32m+\033[0m {self.name}"
        else:
            msg = f"  \033[31m!\033[0m {self.name}"
        if self.detail:
            msg += f"  \033[90m({self.detail})\033[0m"
        return msg


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _proc_running(name: str) -> bool:
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return name.lower() in r.stdout.lower()
        else:
            r = subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=5)
            return r.returncode == 0
    except Exception:
        return False


# Individual checks


def check_java() -> CheckResult:
    path = _which("java")
    if not path:
        return CheckResult("Java", False, "not found in PATH")
    try:
        out = subprocess.check_output(["java", "-version"], stderr=subprocess.STDOUT, timeout=10)
        ver = out.decode().split("\n")[0]
        return CheckResult("Java", True, ver.strip())
    except Exception as e:
        return CheckResult("Java", False, str(e))


def check_burp_jar(burp_jar: Path | None = None) -> CheckResult:
    jar = burp_jar or BURP_JAR_DEFAULT
    if jar.exists():
        size_mb = jar.stat().st_size / (1024 * 1024)
        return CheckResult("Burp Pro jar", True, f"{jar} ({size_mb:.0f} MB)")
    return CheckResult("Burp Pro jar", False, f"not found at {jar}")


def check_exporter_jar() -> CheckResult:
    if EXPORTER_JAR.exists():
        return CheckResult("Exporter extension", True, str(EXPORTER_JAR))
    return CheckResult("Exporter extension", False, f"not found, run build.sh")


def check_caido_installed() -> CheckResult:
    if CAIDO_BASE.exists() and PROJECTS_DIR.exists():
        try:
            count = len(list(PROJECTS_DIR.iterdir()))
            return CheckResult("Caido data dir", True, f"{count} project(s)")
        except Exception:
            return CheckResult("Caido data dir", True, str(CAIDO_BASE))
    return CheckResult("Caido data dir", False, f"not found at {CAIDO_BASE}")


def check_caido_not_running() -> CheckResult:
    if _proc_running("caido"):
        return CheckResult("Caido not running", False, "caido process detected, close it first")
    return CheckResult("Caido not running", True)


def check_burp_file(path: str) -> CheckResult:
    p = Path(path)
    if not p.exists():
        return CheckResult("Burp file", False, f"not found: {path}")
    size_mb = p.stat().st_size / (1024 * 1024)
    # Check magic bytes
    with open(p, "rb") as f:
        magic = f.read(4)
    if magic == b"\x66\x85\x82\x80":
        return CheckResult("Burp file", True, f"{p.name} ({size_mb:.0f} MB, project file)")
    elif path.lower().endswith(".xml"):
        return CheckResult("Burp file", True, f"{p.name} ({size_mb:.0f} MB, XML export)")
    elif path.lower().endswith(".ndjson"):
        return CheckResult("Burp file", True, f"{p.name} ({size_mb:.0f} MB, NDJSON)")
    else:
        return CheckResult("Burp file", True, f"{p.name} ({size_mb:.0f} MB, unknown format)")


# Grouped checks


def preflight_export(burp_file: str, burp_jar: Path | None = None) -> list[CheckResult]:
    """Checks before running the Burp export stage."""
    return [
        check_java(),
        check_burp_jar(burp_jar),
        check_exporter_jar(),
        check_burp_file(burp_file),
    ]


def preflight_seed() -> list[CheckResult]:
    """Checks before running the Caido seed stage."""
    return [
        check_caido_installed(),
        check_caido_not_running(),
    ]


def postflight_seed(db_path: str, raw_path: str) -> list[CheckResult]:
    """Checks after seeding to verify the import."""
    results = []
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        req_count = db.execute("SELECT count(*) FROM requests").fetchone()[0]
        resp_count = db.execute("SELECT count(*) FROM responses").fetchone()[0]
        sitemap_count = db.execute("SELECT count(*) FROM sitemap_entries").fetchone()[0]
        intercept_count = db.execute("SELECT count(*) FROM intercept_entries").fetchone()[0]
        intercept_req_count = db.execute(
            "SELECT count(*) FROM requests WHERE source = 'intercept'"
        ).fetchone()[0]
        db.close()

        results.append(CheckResult("Requests in DB", req_count > 0, str(req_count)))
        results.append(CheckResult("Responses in DB", True, str(resp_count)))
        results.append(CheckResult("Sitemap entries", sitemap_count > 0, str(sitemap_count)))
        intercept_ok = intercept_count == intercept_req_count
        intercept_detail = str(intercept_count)
        if not intercept_ok:
            intercept_detail += f" (expected {intercept_req_count})"
        results.append(CheckResult("Intercept entries", intercept_ok, intercept_detail))
    except Exception as e:
        results.append(CheckResult("DB verification", False, str(e)))

    try:
        raw_db = sqlite3.connect(f"file:{raw_path}?mode=ro", uri=True)
        raw_req = raw_db.execute("SELECT count(*) FROM requests_raw").fetchone()[0]
        raw_resp = raw_db.execute("SELECT count(*) FROM responses_raw").fetchone()[0]
        raw_db.close()
        results.append(CheckResult("Raw requests", raw_req > 0, str(raw_req)))
        results.append(CheckResult("Raw responses", True, str(raw_resp)))
    except Exception as e:
        results.append(CheckResult("Raw DB verification", False, str(e)))

    return results


def print_checks(checks: list[CheckResult], header: str = "") -> bool:
    """Print checks and return True if all passed."""
    if header:
        print(f"\n{header}")
    all_ok = True
    for c in checks:
        print(repr(c))
        if not c.ok:
            all_ok = False
    return all_ok
