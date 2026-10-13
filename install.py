#!/usr/bin/env python3
"""Cross-platform installer for burp-caido-syncer.

Works on Linux, macOS, and Windows. Replaces install.sh for portability.
Requires Python 3.10+.
"""

import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
VENV_DIR = SCRIPT_DIR / ".venv"
MIN_PYTHON = (3, 10)
MIN_JDK = 17


def check_python():
    ver = sys.version_info[:2]
    if ver < MIN_PYTHON:
        print(f"ERROR: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found {ver[0]}.{ver[1]}")
        sys.exit(1)
    print(f"[*] Using Python {sys.version.split()[0]} ({sys.executable})")


def create_venv():
    if VENV_DIR.exists():
        print(f"[*] Virtualenv already exists at .venv/")
        return
    print(f"[*] Creating virtualenv at .venv/")
    venv.create(str(VENV_DIR), with_pip=True)


def venv_python() -> str:
    if sys.platform == "win32":
        return str(VENV_DIR / "Scripts" / "python.exe")
    return str(VENV_DIR / "bin" / "python")


def venv_cli() -> str:
    if sys.platform == "win32":
        return str(VENV_DIR / "Scripts" / "burp-caido-syncer.exe")
    return str(VENV_DIR / "bin" / "burp-caido-syncer")


def install_package():
    print("[*] Installing burp-caido-syncer...")
    subprocess.run(
        [venv_python(), "-m", "pip", "install", "-e", str(SCRIPT_DIR), "--quiet"],
        check=True,
    )
    print("[+] Installed into .venv/")

    if sys.platform == "win32":
        activate = r".venv\Scripts\activate"
        direct = r".venv\Scripts\burp-caido-syncer.exe --help"
    else:
        activate = "source .venv/bin/activate"
        direct = ".venv/bin/burp-caido-syncer --help"

    print(f"""
    Activate the venv before use:
      {activate}

    Or run directly:
      {direct}
""")


def get_javac_version() -> int | None:
    javac = shutil.which("javac")
    if not javac:
        return None
    try:
        out = subprocess.check_output(
            ["javac", "-version"], stderr=subprocess.STDOUT, text=True, timeout=10,
        )
        for part in out.split():
            digits = part.split(".")[0]
            if digits.isdigit():
                return int(digits)
    except Exception:
        pass
    return None


def build_exporter():
    ver = get_javac_version()
    if ver is None:
        print("\n[!] javac not found, skipping exporter build.")
        print("    Install JDK 17+ and re-run, or build manually.")
        return
    if ver < MIN_JDK:
        print(f"\n[!] JDK {ver} found, need {MIN_JDK}+. Skipping exporter build.")
        return

    print("\n[*] Building Burp exporter extension...")

    montoya_jar = os.environ.get("MONTOYA_JAR")
    if not montoya_jar:
        montoya_jar = str(Path.home() / "tools" / "burpsuite-project-file-parser-all.jar")
    if not Path(montoya_jar).exists():
        print(f"[!] Montoya API jar not found at {montoya_jar}")
        print("    Set MONTOYA_JAR=/path/to/jar to enable exporter build.")
        return

    exporter_dir = SCRIPT_DIR / "exporter"
    build_dir = exporter_dir / "build"
    build_dir.mkdir(exist_ok=True)

    try:
        subprocess.run(
            ["javac", "-cp", montoya_jar, "-d", str(build_dir),
             "-source", "17", "-target", "17",
             str(exporter_dir / "BurpHistoryExporter.java")],
            check=True, capture_output=True, text=True,
        )
        output_jar = exporter_dir / "burp-history-exporter.jar"
        subprocess.run(
            ["jar", "cf", str(output_jar), "-C", str(build_dir), "."],
            check=True, capture_output=True,
        )
        print(f"[+] Built: {output_jar}")

        dest = SCRIPT_DIR / "burp_caido_syncer" / "burp-history-exporter.jar"
        shutil.copy2(str(output_jar), str(dest))
        print(f"[+] Copied exporter jar into burp_caido_syncer/")
    except subprocess.CalledProcessError as e:
        print(f"[!] Build failed: {e}")
    finally:
        shutil.rmtree(str(build_dir), ignore_errors=True)


def main():
    os.chdir(str(SCRIPT_DIR))
    check_python()
    create_venv()
    install_package()
    build_exporter()
    print("\n[+] Done. Run 'burp-caido-syncer check' to verify your setup.")


if __name__ == "__main__":
    main()
