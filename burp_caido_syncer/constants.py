"""Shared constants and paths."""

import os
import sys
from pathlib import Path


def _caido_data_dir() -> Path:
    """Return the platform-appropriate Caido data directory."""
    env = os.environ.get("CAIDO_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "caido"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "caido"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "caido"


def _default_burp_jar() -> Path:
    """Return the default Burp Suite Pro jar path, overridable via $BURP_JAR."""
    env = os.environ.get("BURP_JAR")
    if env:
        return Path(env)
    return Path.home() / "tools" / "Burp-Suite" / "burpsuite_pro.jar"


def _default_montoya_jar() -> Path:
    """Return the default Montoya API jar path, overridable via $MONTOYA_JAR."""
    env = os.environ.get("MONTOYA_JAR")
    if env:
        return Path(env)
    return Path.home() / "tools" / "burpsuite-project-file-parser-all.jar"


CAIDO_BASE = _caido_data_dir()
PROJECTS_DIR = CAIDO_BASE / "projects"
PROJECTS_DB = CAIDO_BASE / "projects.db"

BATCH_SIZE = 10_000

BURP_JAR_DEFAULT = _default_burp_jar()
MONTOYA_JAR_DEFAULT = _default_montoya_jar()

# Extension jars live next to this package
EXPORTER_JAR = Path(__file__).parent / "burp-history-exporter.jar"
EXPORTER_JAVA = Path(__file__).parent / "BurpHistoryExporter.java"
REPEATER_EXPORTER_JAR = Path(__file__).parent / "burp-repeater-exporter.jar"
REPEATER_EXPORTER_JAVA = Path(__file__).parent / "BurpRepeaterExporter.java"

# Burp HighlightColor enum -> Caido colour string
BURP_TO_CAIDO_COLOR = {
    "RED": "red",
    "ORANGE": "orange",
    "YELLOW": "yellow",
    "GREEN": "green",
    "CYAN": "cyan",
    "BLUE": "blue",
    "PINK": "pink",
    "MAGENTA": "magenta",
    "GRAY": "gray",
}
