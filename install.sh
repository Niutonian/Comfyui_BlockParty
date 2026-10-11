#!/bin/sh
set -e
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
  exec python3 install.py "$@"
fi
exec python install.py "$@"
