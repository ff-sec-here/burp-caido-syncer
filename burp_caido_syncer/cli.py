"""CLI entry point for burp-caido-syncer.

Accepts a .burp project file, NDJSON, or XML and drives the full pipeline:
  .burp  ->  [export stage]  ->  NDJSON  ->  [seed stage]  ->  Caido SQLite
  .ndjson                                ->  [seed stage]  ->  Caido SQLite
  .xml                                   ->  [seed stage]  ->  Caido SQLite
"""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .constants import BURP_JAR_DEFAULT
from .exporter import build_extension, run_burp_export, run_burp_repeater_export
from .healthcheck import (
    postflight_seed,
    preflight_export,
    print_checks,
)
from .project import backup_project, find_project_by_name, find_project_db, list_projects
from .readers import count_ndjson_lines, read_burp_xml, read_ndjson
from .seeder import seed

# ANSI color codes
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_ORANGE = "\033[38;5;208m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_WHITE = "\033[97m"
_GRAY = "\033[90m"


def _banner():
    """Print the styled tool banner."""
    print(f"""
{_ORANGE}{_BOLD}  ┏┓ ╻ ╻┏━┓┏━┓   ┏━╸┏━┓╻╺┳┓┏━┓   ┏━┓╻ ╻┏┓╻┏━╸┏━╸┏━┓
  ┣┻┓┃ ┃┣┳┛┣━┛╺━╸┃  ┣━┫┃ ┃┃┃ ┃╺━╸┗━┓┗┳┛┃┗┫┃  ┣╸ ┣┳┛
  ┗━┛┗━┛╹┗╸╹     ┗━╸╹ ╹╹╺┻┛┗━┛   ┗━┛ ╹ ╹ ╹┗━╸┗━╸╹┗╸{_RESET}
{_DIM}  v{__version__}{_RESET}
""")


def detect_format(path: str) -> str:
    """Auto-detect input format from file extension and magic bytes."""
    lower = path.lower()
    if lower.endswith(".ndjson") or lower.endswith(".jsonl"):
        return "ndjson"
    if lower.endswith(".xml"):
        return "xml"

    # Check magic bytes for .burp project file
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic == b"\x66\x85\x82\x80":
            return "burp"
    except OSError:
        pass

    # Default: if it ends with .burp or has no extension, assume burp
    if lower.endswith(".burp"):
        return "burp"

    # Fall through to ndjson
    return "ndjson"


def cmd_import(args):
    """Main import command: handles the full pipeline."""
    if getattr(args, "import_all", False):
        args.scope = True
        args.repeater = True

    input_path = args.input
    fmt = args.format or detect_format(input_path)
    fmt_original = fmt

    if not os.path.exists(input_path):
        sys.exit(f"[✗] File not found: {input_path}")

    size_mb = os.path.getsize(input_path) / (1024 * 1024)
    _banner()
    print(f"Input: {input_path} ({size_mb:.0f} MB, {fmt})")

    # Pre-flight: system checks
    from .healthcheck import (
        check_caido_installed,
        check_caido_not_running,
    )

    checks = [
        check_caido_installed(),
        check_caido_not_running(),
    ]
    if not args.force:
        if not print_checks(checks, "Pre-flight (system):"):
            sys.exit(1)
    else:
        print_checks(checks, "Pre-flight (system) [--force, continuing]:")

    # Stage 1: Export (only for .burp files)
    ndjson_path = None
    tmp_ndjson = None
    scope_tmp = None

    burp_copy = None

    if fmt == "burp":
        print(f"\n{_CYAN}{_BOLD}[Stage 1] Export from Burp{_RESET}")

        burp_jar = Path(args.burp_jar) if args.burp_jar else BURP_JAR_DEFAULT
        checks = preflight_export(input_path, burp_jar)
        if not print_checks(checks, "Pre-flight (export):"):
            sys.exit(1)

        # Copy .burp file to a temp location so we never touch the original
        print(f"[*] Copying project file to temp dir...")
        fd, burp_copy = tempfile.mkstemp(suffix=".burp", prefix="burp-caido-syncer-")
        os.close(fd)
        shutil.copy2(input_path, burp_copy)
        size_mb = os.path.getsize(burp_copy) / (1024 * 1024)
        print(f"[+] Working copy: {burp_copy} ({size_mb:.0f} MB)")

        # Output NDJSON path
        if args.ndjson_out:
            ndjson_path = args.ndjson_out
        else:
            fd, ndjson_path = tempfile.mkstemp(suffix=".ndjson", prefix="burp-caido-syncer-")
            os.close(fd)
            tmp_ndjson = ndjson_path

        # Scope temp file (exported alongside history in same Burp launch)
        scope_tmp = None
        scope_export_path = None

        if getattr(args, "scope", False):
            fd, scope_export_path = tempfile.mkstemp(suffix=".json", prefix="burp-caido-syncer-scope-")
            os.close(fd)
            scope_tmp = scope_export_path

        ok = run_burp_export(
            burp_file=burp_copy,
            ndjson_path=ndjson_path,
            burp_jar=burp_jar,
            heap=args.heap,
            scope_path=scope_export_path,
        )
        if not ok:
            sys.exit(1)

        fmt = "ndjson"
        input_path = ndjson_path

    # Stage 2: Seed into Caido
    print(f"\n{_CYAN}{_BOLD}[Stage 2] Seed into Caido{_RESET}")

    # Determine target project
    if args.project_name:
        db_path, raw_path = find_project_by_name(args.project_name)
    elif args.project:
        db_path, raw_path = find_project_db(args.project)
    else:
        db_path, raw_path = find_project_db(None)

    if not args.no_backup:
        backup_project(db_path)

    # Read input
    print(f"[*] Reading {fmt.upper()}: {input_path}")
    total_hint = 0
    if fmt == "ndjson":
        total_hint = count_ndjson_lines(input_path)
        print(f"    {total_hint} lines")
        items = read_ndjson(input_path)
    else:
        items = read_burp_xml(input_path)

    # Seed
    ok_count, err_count = seed(
        db_path, raw_path, items,
        total_hint=total_hint,
        verbose=args.verbose,
    )

    # Stage 2b: Scope (optional)
    if getattr(args, "scope", False) and fmt_original == "burp":
        if scope_tmp and os.path.exists(scope_tmp) and os.path.getsize(scope_tmp) > 0:
            print(f"\n{_CYAN}{_BOLD}[Stage 2b] Import scope into Caido{_RESET}")
            from .scope_seeder import seed_scope
            seed_scope(db_path, scope_tmp, verbose=args.verbose)
        elif getattr(args, "scope", False):
            print("[!] Scope export produced no data", file=sys.stderr)
    elif getattr(args, "scope", False):
        print("\n[!] --scope requires a .burp input file", file=sys.stderr)

    # Stage 3: Repeater tabs (optional)
    if args.repeater and fmt_original == "burp":
        print(f"\n{_CYAN}{_BOLD}[Stage 3] Export Repeater tabs from Burp{_RESET}")
        from .replay_seeder import read_repeater_ndjson, seed_replay

        repeater_ndjson = None
        try:
            fd, repeater_ndjson = tempfile.mkstemp(suffix=".ndjson", prefix="burp-caido-syncer-repeater-")
            os.close(fd)

            burp_jar = Path(args.burp_jar) if args.burp_jar else BURP_JAR_DEFAULT
            ok = run_burp_repeater_export(
                burp_file=burp_copy,
                ndjson_path=repeater_ndjson,
                burp_jar=burp_jar,
                heap=args.heap,
            )
            if ok:
                items = read_repeater_ndjson(repeater_ndjson)
                ok_count_r, err_count_r = seed_replay(db_path, items, raw_path=raw_path, verbose=args.verbose)
                if err_count_r > 0 and repeater_ndjson:
                    p = Path(repeater_ndjson)
                    kept = str(p.parent / f"kept-{p.name}")
                    os.rename(repeater_ndjson, kept)
                    repeater_ndjson = None
                    print(f"[*] Kept repeater NDJSON for debugging: {kept}")
                    print(f"    Re-run: burp-caido-syncer replay {kept} --project-name ... -v")
            else:
                print("[!] Repeater export failed, skipping replay session import", file=sys.stderr)
        finally:
            if repeater_ndjson and os.path.exists(repeater_ndjson):
                os.unlink(repeater_ndjson)
    elif args.repeater:
        print("\n[!] --repeater requires a .burp input file", file=sys.stderr)

    # Post-flight
    checks = postflight_seed(db_path, raw_path)
    print_checks(checks, "Post-flight verification:")

    # Clean up temp files
    for tmp in (tmp_ndjson, scope_tmp, burp_copy):
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    print(f"[*] Cleaned up temp files")

    if ok_count > 0:
        print(f"\n{_GREEN}{_BOLD}Done. Open Caido and switch to the project to see {ok_count} requests.{_RESET}")
    elif err_count > 0:
        print(f"\n\033[31m{_BOLD}All {err_count} items failed.{_RESET}", file=sys.stderr)
        sys.exit(1)


def cmd_build(args):
    """Build the Burp exporter extension jar."""
    montoya_jar = Path(args.montoya_jar) if args.montoya_jar else None
    build_extension(montoya_jar)


def cmd_projects(args):
    """List Caido projects."""
    projects = list_projects()
    if not projects:
        print("No Caido projects found.")
        return
    print(f"{'UUID':<38} {'Name':<25} {'Status':<8} {'Updated'}")
    print("=" * 100)
    for p in projects:
        print(f"{p['id']:<38} {p['name']:<25} {p['status']:<8} {p['updated_at'][:19]}")


def cmd_replay(args):
    """Import a repeater NDJSON into Caido Replay sessions."""
    from .replay_seeder import read_repeater_ndjson, seed_replay

    if not os.path.exists(args.input):
        sys.exit(f"[✗] File not found: {args.input}")

    _banner()
    print(f"Input: {args.input} (repeater ndjson)")

    if args.project_name:
        db_path, raw_path = find_project_by_name(args.project_name)
    elif args.project:
        db_path, raw_path = find_project_db(args.project)
    else:
        db_path, raw_path = find_project_db(None)

    if not args.no_backup:
        backup_project(db_path)

    items = read_repeater_ndjson(args.input)
    seed_replay(db_path, items, raw_path=raw_path, verbose=args.verbose)


def cmd_scope(args):
    """Import a Burp scope JSON file into Caido."""
    from .scope_seeder import seed_scope

    if not os.path.exists(args.input):
        sys.exit(f"[✗] File not found: {args.input}")

    _banner()
    print(f"Input: {args.input} (scope json)")

    if args.project_name:
        db_path, raw_path = find_project_by_name(args.project_name)
    elif args.project:
        db_path, raw_path = find_project_db(args.project)
    else:
        db_path, raw_path = find_project_db(None)

    if not args.no_backup:
        backup_project(db_path)

    seed_scope(db_path, args.input, scope_name=args.scope_name, verbose=args.verbose)


def cmd_check(args):
    """Run all health checks."""
    _banner()
    print("Health check\n")

    from .healthcheck import (
        check_burp_jar,
        check_caido_installed,
        check_caido_not_running,
        check_exporter_jar,
        check_java,
    )

    checks = [
        check_java(),
        check_burp_jar(),
        check_exporter_jar(),
        check_caido_installed(),
        check_caido_not_running(),
    ]
    print_checks(checks, "System checks:")


def main():
    parser = argparse.ArgumentParser(
        prog="burp-caido-syncer",
        description="Sync Burp Suite projects into Caido: history, scope, and repeater tabs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # import
    p_import = sub.add_parser(
        "import",
        help="Import a .burp project, NDJSON, or XML into Caido",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  burp-caido-syncer import project.burp --project-name "pentest-2024"
  burp-caido-syncer import project.burp --ndjson-out history.ndjson
  burp-caido-syncer import history.ndjson --project <uuid>
  burp-caido-syncer import export.xml --format xml --project-name "from-xml"
""",
    )
    p_import.add_argument("input", help=".burp project file, .ndjson, or .xml")
    p_import.add_argument("--format", choices=["burp", "ndjson", "xml"],
                          help="Input format (auto-detected if omitted)")
    p_import.add_argument("--project", metavar="UUID",
                          help="Target Caido project UUID")
    p_import.add_argument("--project-name", metavar="NAME",
                          help="Target Caido project by name (create it in Caido UI first)")
    p_import.add_argument("--burp-jar", metavar="PATH",
                          help=f"Path to burpsuite_pro.jar (default: {BURP_JAR_DEFAULT})")
    p_import.add_argument("--heap", default="4G",
                          help="JVM heap size for Burp (default: 4G)")
    p_import.add_argument("--ndjson-out", metavar="PATH",
                          help="Save the exported NDJSON to this path (instead of a temp file)")
    p_import.add_argument("--force", action="store_true",
                          help="Skip Caido-running safety check")
    p_import.add_argument("--no-backup", action="store_true",
                          help="Skip backup when writing to an existing project")
    p_import.add_argument("--repeater", action="store_true",
                          help="Export Burp Repeater tabs into Caido Replay sessions (requires GUI)")
    p_import.add_argument("--scope", action="store_true",
                          help="Export Burp target scope into Caido")
    p_import.add_argument("--all", action="store_true", dest="import_all",
                          help="Export everything: history + scope + repeater (GUI)")
    p_import.add_argument("-v", "--verbose", action="store_true",
                          help="Print per-request detail during seeding")
    p_import.set_defaults(func=cmd_import)

    # build
    p_build = sub.add_parser("build", help="Build the Burp exporter extension jar")
    p_build.add_argument("--montoya-jar", metavar="PATH",
                         help="Path to the Montoya API jar")
    p_build.set_defaults(func=cmd_build)

    # projects
    p_proj = sub.add_parser("projects", help="List Caido projects")
    p_proj.set_defaults(func=cmd_projects)

    # replay
    p_replay = sub.add_parser(
        "replay",
        help="Import a repeater NDJSON into Caido Replay sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  burp-caido-syncer replay repeater.ndjson --project-name "pentest-2024"
""",
    )
    p_replay.add_argument("input", help="Repeater NDJSON file")
    p_replay.add_argument("--project", metavar="UUID",
                          help="Target Caido project UUID")
    p_replay.add_argument("--project-name", metavar="NAME",
                          help="Target Caido project by name")
    p_replay.add_argument("--no-backup", action="store_true")
    p_replay.add_argument("-v", "--verbose", action="store_true")
    p_replay.set_defaults(func=cmd_replay)

    # scope
    p_scope = sub.add_parser(
        "scope",
        help="Import a Burp scope JSON into Caido",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  burp-caido-syncer scope scope.json --project-name "pentest-2024"
""",
    )
    p_scope.add_argument("input", help="Burp scope JSON file")
    p_scope.add_argument("--project", metavar="UUID",
                         help="Target Caido project UUID")
    p_scope.add_argument("--project-name", metavar="NAME",
                         help="Target Caido project by name")
    p_scope.add_argument("--scope-name", default="Burp Import",
                         help="Name for the scope in Caido (default: 'Burp Import')")
    p_scope.add_argument("--no-backup", action="store_true")
    p_scope.add_argument("-v", "--verbose", action="store_true")
    p_scope.set_defaults(func=cmd_scope)

    # all
    p_all = sub.add_parser(
        "all",
        help="Import everything from a .burp file: history + scope + repeater",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  burp-caido-syncer all project.burp --project-name "pentest-2024"
""",
    )
    p_all.add_argument("input", help=".burp project file")
    p_all.add_argument("--project", metavar="UUID",
                       help="Target Caido project UUID")
    p_all.add_argument("--project-name", metavar="NAME",
                       help="Target Caido project by name (create it in Caido UI first)")
    p_all.add_argument("--burp-jar", metavar="PATH",
                       help=f"Path to burpsuite_pro.jar (default: {BURP_JAR_DEFAULT})")
    p_all.add_argument("--heap", default="4G",
                       help="JVM heap size for Burp (default: 4G)")
    p_all.add_argument("--ndjson-out", metavar="PATH",
                       help="Save the exported NDJSON to this path")
    p_all.add_argument("--force", action="store_true",
                       help="Skip Caido-running safety check")
    p_all.add_argument("--no-backup", action="store_true",
                       help="Skip backup when writing to an existing project")
    p_all.add_argument("-v", "--verbose", action="store_true",
                       help="Print per-request detail during seeding")
    p_all.set_defaults(func=cmd_import, import_all=True, format=None,
                       repeater=True, scope=True)

    # check
    p_check = sub.add_parser("check", help="Run health checks")
    p_check.set_defaults(func=cmd_check)

    # parse args
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
