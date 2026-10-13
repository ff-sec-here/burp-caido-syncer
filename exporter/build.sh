#!/usr/bin/env bash
# Build the BurpHistoryExporter extension jar.
# Requires: javac, jar (JDK 17+)
# Usage: ./build.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# The Montoya API classes are bundled inside the parser jar.
# We use it as a compile-time dependency only.
MONTOYA_JAR="${MONTOYA_JAR:-$HOME/tools/burpsuite-project-file-parser-all.jar}"

if [ ! -f "$MONTOYA_JAR" ]; then
    echo "ERROR: Montoya API jar not found at $MONTOYA_JAR"
    echo "Set MONTOYA_JAR=/path/to/jar or place burpsuite-project-file-parser-all.jar in ~/tools/"
    exit 1
fi

BUILD_DIR="$SCRIPT_DIR/build"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "[*] Compiling BurpHistoryExporter.java..."
javac -cp "$MONTOYA_JAR" \
      -d "$BUILD_DIR" \
      -source 17 -target 17 \
      BurpHistoryExporter.java

echo "[*] Packaging burp-history-exporter.jar..."
jar cf "$SCRIPT_DIR/burp-history-exporter.jar" -C "$BUILD_DIR" .

rm -rf "$BUILD_DIR"
echo "[+] Built: $SCRIPT_DIR/burp-history-exporter.jar"
