"""Burp export orchestration: build the extension, launch Burp headless, monitor progress."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .constants import (
    BURP_JAR_DEFAULT, EXPORTER_JAR, EXPORTER_JAVA,
    MONTOYA_JAR_DEFAULT, REPEATER_EXPORTER_JAR, REPEATER_EXPORTER_JAVA,
)


def _find_loader_jar(burp_dir: Path) -> Path | None:
    """Find any jar with 'loader' in the name (case insensitive) in the Burp directory."""
    for p in burp_dir.iterdir():
        if p.suffix.lower() == ".jar" and "loader" in p.name.lower():
            return p
    return None


def build_extension(montoya_jar: Path | None = None) -> Path:
    """Compile the BurpHistoryExporter extension jar. Returns path to the jar."""
    jar = montoya_jar or MONTOYA_JAR_DEFAULT
    if not jar.exists():
        sys.exit(f"[✗] Montoya API jar not found at {jar}")
    if not EXPORTER_JAVA.exists():
        sys.exit(f"[✗] Extension source not found at {EXPORTER_JAVA}")

    build_dir = EXPORTER_JAVA.parent / "build"
    build_dir.mkdir(exist_ok=True)

    print("[*] Compiling BurpHistoryExporter...")
    subprocess.run(
        ["javac", "-cp", str(jar), "-d", str(build_dir),
         "-source", "17", "-target", "17", str(EXPORTER_JAVA)],
        check=True, capture_output=True, text=True,
    )

    output_jar = EXPORTER_JAR
    subprocess.run(
        ["jar", "cf", str(output_jar), "-C", str(build_dir), "."],
        check=True, capture_output=True,
    )

    # Clean up
    import shutil
    shutil.rmtree(build_dir, ignore_errors=True)

    print(f"[+] Built: {output_jar}")
    return output_jar


def _make_user_config(exporter_jar: Path) -> str:
    """Create a minimal Burp user config that loads only our extension. Returns path."""
    config = {
        "user_options": {
            "extender": {
                "extensions": [
                    {
                        "errors": "ui",
                        "extension_file": str(exporter_jar),
                        "extension_type": "java",
                        "loaded": True,
                        "name": "BurpHistoryExporter",
                        "output": "ui",
                    }
                ]
            }
        }
    }
    fd, path = tempfile.mkstemp(suffix=".json", prefix="burp-export-config-")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


def _monitor_ndjson(ndjson_path: str, stop_event: threading.Event):
    """Background thread: print progress as NDJSON file grows."""
    last_count = 0
    ticks = 0
    while not stop_event.is_set():
        stop_event.wait(5.0)
        ticks += 1
        try:
            size = os.path.getsize(ndjson_path)
            count = 0
            if size > 0:
                with open(ndjson_path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        count += chunk.count(b"\n")

            if count != last_count:
                size_mb = size / (1024 * 1024)
                delta = count - last_count
                print(f"  [export] {count} items ({size_mb:.1f} MB) +{delta}", flush=True)
                last_count = count
            elif count == 0 and ticks % 6 == 0:
                elapsed = ticks * 5
                print(f"  [export] waiting for Burp to start writing... ({elapsed}s)", flush=True)
        except FileNotFoundError:
            pass
        except Exception:
            pass


def run_burp_export(burp_file: str, ndjson_path: str,
                    burp_jar: Path | None = None,
                    heap: str = "4G",
                    scope_path: str | None = None) -> bool:
    """Launch Burp Pro headless with the exporter extension.

    Returns True on success, False on failure.
    The caller's run.sh has already been edited by the user to include their
    JVM agent flags. We replicate the same launch pattern here so the CLI
    is the single entry point.
    """
    jar = burp_jar or BURP_JAR_DEFAULT
    if not jar.exists():
        print(f"[✗] Burp jar not found: {jar}", file=sys.stderr)
        return False

    if not EXPORTER_JAR.exists():
        print("[*] Extension jar not found, building...", flush=True)
        build_extension()

    config_path = _make_user_config(EXPORTER_JAR)

    # Build the java command.
    burp_dir = jar.parent
    loader_jar = _find_loader_jar(burp_dir)

    cmd = [
        "java",
        "-Djava.awt.headless=true",
        "--add-opens=java.desktop/javax.swing=ALL-UNNAMED",
        "--add-opens=java.base/java.lang=ALL-UNNAMED",
        "--add-opens=java.base/jdk.internal.org.objectweb.asm=ALL-UNNAMED",
        "--add-opens=java.base/jdk.internal.org.objectweb.asm.tree=ALL-UNNAMED",
        "--add-opens=java.base/jdk.internal.org.objectweb.asm.Opcodes=ALL-UNNAMED",
        f"-Xmx{heap}",
    ]

    if loader_jar:
        cmd += [f"-javaagent:{loader_jar}", "-noverify"]

    cmd += [
        "-jar", str(jar),
        f"--project-file={burp_file}",
        f"--user-config-file={config_path}",
        f"exportHistory={ndjson_path}",
    ]

    if scope_path:
        cmd.append(f"exportScope={scope_path}")

    print(f"[*] Launching Burp export, please wait...")
    export_start = time.time()

    # Start the monitor thread
    stop_event = threading.Event()
    # Touch the file so the monitor can find it
    Path(ndjson_path).touch()
    monitor = threading.Thread(target=_monitor_ndjson, args=(ndjson_path, stop_event), daemon=True)
    monitor.start()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(burp_dir),
        )

        def _stream_stderr():
            for line in proc.stderr:
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                if "[burp-caido-syncer]" in text or "[burp2caido]" in text:
                    print(f"  {text}", flush=True)

        def _stream_stdout():
            for line in proc.stdout:
                pass

        stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
        stdout_thread = threading.Thread(target=_stream_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()

        proc.wait()
        stop_event.set()
        monitor.join(timeout=2)
        stderr_thread.join(timeout=2)
        stdout_thread.join(timeout=2)

        if proc.returncode != 0:
            print(f"[✗] Burp exited with code {proc.returncode}", file=sys.stderr)
            return False

        # Verify output
        if not os.path.exists(ndjson_path) or os.path.getsize(ndjson_path) == 0:
            print("[✗] Export produced empty output", file=sys.stderr)
            return False

        size_mb = os.path.getsize(ndjson_path) / (1024 * 1024)
        elapsed = time.time() - export_start
        print(f"[+] Export complete: {ndjson_path} ({size_mb:.1f} MB, {elapsed:.0f}s)")
        return True

    except KeyboardInterrupt:
        proc.kill()
        stop_event.set()
        print("\n[!] Export cancelled by user")
        return False
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass


def build_repeater_extension(montoya_jar: Path | None = None) -> Path:
    """Compile the BurpRepeaterExporter extension jar."""
    jar = montoya_jar or MONTOYA_JAR_DEFAULT
    if not jar.exists():
        sys.exit(f"[✗] Montoya API jar not found at {jar}")
    if not REPEATER_EXPORTER_JAVA.exists():
        sys.exit(f"[✗] Repeater extension source not found at {REPEATER_EXPORTER_JAVA}")

    build_dir = REPEATER_EXPORTER_JAVA.parent / "build"
    build_dir.mkdir(exist_ok=True)

    print("[*] Compiling BurpRepeaterExporter...")
    subprocess.run(
        ["javac", "-cp", str(jar), "-d", str(build_dir),
         "-source", "17", "-target", "17", str(REPEATER_EXPORTER_JAVA)],
        check=True, capture_output=True, text=True,
    )

    output_jar = REPEATER_EXPORTER_JAR
    subprocess.run(
        ["jar", "cf", str(output_jar), "-C", str(build_dir), "."],
        check=True, capture_output=True,
    )

    import shutil
    shutil.rmtree(build_dir, ignore_errors=True)

    print(f"[+] Built: {output_jar}")
    return output_jar


def run_burp_repeater_export(burp_file: str, ndjson_path: str,
                             burp_jar: Path | None = None,
                             heap: str = "4G") -> bool:
    """Launch Burp Pro WITH GUI to export Repeater tabs via Swing walk.

    Repeater export requires a live UI, cannot run headless.
    """
    jar = burp_jar or BURP_JAR_DEFAULT
    if not jar.exists():
        print(f"[✗] Burp jar not found: {jar}", file=sys.stderr)
        return False

    if not REPEATER_EXPORTER_JAR.exists():
        print("[*] Repeater extension jar not found, building...", flush=True)
        build_repeater_extension()

    config = {
        "user_options": {
            "extender": {
                "extensions": [
                    {
                        "errors": "ui",
                        "extension_file": str(REPEATER_EXPORTER_JAR),
                        "extension_type": "java",
                        "loaded": True,
                        "name": "BurpRepeaterExporter",
                        "output": "ui",
                    }
                ]
            }
        }
    }
    fd, config_path = tempfile.mkstemp(suffix=".json", prefix="burp-repeater-config-")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)

    burp_dir = jar.parent
    loader_jar = _find_loader_jar(burp_dir)

    cmd = [
        "java",
        "--add-opens=java.desktop/javax.swing=ALL-UNNAMED",
        "--add-opens=java.base/java.lang=ALL-UNNAMED",
        "--add-opens=java.base/jdk.internal.org.objectweb.asm=ALL-UNNAMED",
        "--add-opens=java.base/jdk.internal.org.objectweb.asm.tree=ALL-UNNAMED",
        "--add-opens=java.base/jdk.internal.org.objectweb.asm.Opcodes=ALL-UNNAMED",
        f"-Xmx{heap}",
    ]

    if loader_jar:
        cmd += [f"-javaagent:{loader_jar}", "-noverify"]

    cmd += [
        "-jar", str(jar),
        f"--project-file={burp_file}",
        f"--user-config-file={config_path}",
        f"exportRepeater={ndjson_path}",
    ]

    print(f"[*] Launching Burp Repeater export, please wait...")
    export_start = time.time()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(burp_dir),
        )

        def _stream_stderr():
            for line in proc.stderr:
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                if "[repeater-export]" in text:
                    print(f"  {text}", flush=True)

        def _stream_stdout():
            for line in proc.stdout:
                pass

        stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
        stdout_thread = threading.Thread(target=_stream_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()

        proc.wait()
        stderr_thread.join(timeout=2)
        stdout_thread.join(timeout=2)

        if proc.returncode != 0:
            print(f"[✗] Burp exited with code {proc.returncode}", file=sys.stderr)
            return False

        if not os.path.exists(ndjson_path) or os.path.getsize(ndjson_path) == 0:
            print("[✗] Repeater export produced empty output", file=sys.stderr)
            return False

        size_kb = os.path.getsize(ndjson_path) / 1024
        elapsed = time.time() - export_start
        print(f"[+] Repeater export complete: {ndjson_path} ({size_kb:.1f} KB, {elapsed:.0f}s)")
        return True

    except KeyboardInterrupt:
        proc.kill()
        print("\n[!] Export cancelled by user")
        return False
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass
