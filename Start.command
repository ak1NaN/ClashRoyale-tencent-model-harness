#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python || ! -x .deps/platform-tools/adb || ! -x .deps/frida/frida-server-17.17.0-android-arm64 ]]; then
  ./Install.command
fi
exec .venv/bin/python run_gui.py
