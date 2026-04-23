#!/usr/bin/env bash
# Interactive room launcher for macOS.

set -euo pipefail

cd "$(dirname "$0")/.."
python3 macos-linux/launch_room_mac.py "$@"
