#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != Darwin ]]; then
  echo 'This installer currently supports macOS only.' >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" - <<'PY'
import sys
import tkinter
if sys.version_info < (3, 12):
    raise SystemExit('Python 3.12 or newer is required.')
PY
command -v swiftc >/dev/null || { echo 'Install Xcode Command Line Tools (swiftc) first.' >&2; exit 1; }

mkdir -p .deps/frida models records build
if [[ ! -x .deps/platform-tools/adb || ! -x .deps/frida/frida-server-17.17.0-android-arm64 ]]; then
  TEMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TEMP_DIR"' EXIT
fi

if [[ ! -x .deps/platform-tools/adb ]]; then
  echo 'Downloading Android SDK Platform-Tools from Google...'
  curl --fail --location --retry 2 --output "$TEMP_DIR/platform-tools.zip" \
    'https://dl.google.com/android/repository/platform-tools-latest-darwin.zip'
  ditto -x -k "$TEMP_DIR/platform-tools.zip" .deps
  .deps/platform-tools/adb version
fi

if [[ ! -x .deps/frida/frida-server-17.17.0-android-arm64 ]]; then
  echo 'Downloading Frida Server 17.17.0 for Android ARM64...'
  curl --fail --location --retry 2 --output "$TEMP_DIR/frida-server.xz" \
    'https://github.com/frida/frida/releases/download/17.17.0/frida-server-17.17.0-android-arm64.xz'
  "$PYTHON_BIN" - "$TEMP_DIR/frida-server.xz" '.deps/frida/frida-server-17.17.0-android-arm64' <<'PY'
import hashlib
import lzma
import sys
from pathlib import Path
archive, target = map(Path, sys.argv[1:])
data = lzma.decompress(archive.read_bytes())
expected = '55ef78c3f3e7a55122ca7e0051e2a356d0ff1d9744d84c1660291f90400588e7'
if hashlib.sha256(data).hexdigest() != expected:
    raise SystemExit('Frida Server SHA-256 verification failed.')
target.write_bytes(data)
target.chmod(0o755)
PY
fi

if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
swiftc ai_client/continuous/window_bounds.swift -o build/continuous_window_bounds
echo 'Installation complete. Add your own .pt model through the GUI and open Start.command.'
