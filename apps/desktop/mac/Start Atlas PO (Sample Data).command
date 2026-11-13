#!/bin/bash
# Double-click in Finder to start Atlas PO ERP with the sample data, straight from this folder, on any Mac.
# Needs Python 3.12 or newer (python.org). The first start installs what the program needs, which takes a few minutes
# and needs internet. Later starts take seconds. Log in as owner, password 123.
# The sample data lives in Documents/Atlas PO ERP Sample. Delete that folder to start the sample over.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=""
for candidate in python3.14 python3.13 python3.12 python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Atlas PO ERP needs Python 3.12 or newer."
  echo "Install it from https://www.python.org/downloads/macos/ and double-click this file again."
  open "https://www.python.org/downloads/macos/" || true
  read -r -p "Press Return to close." _
  exit 1
fi

ENV_DIR="$HOME/Library/Application Support/Atlas PO ERP/python-env"
STAMP="$ENV_DIR/requirements.txt"
if [ ! -x "$ENV_DIR/bin/python" ] || ! cmp -s requirements.txt "$STAMP"; then
  echo "Setting up Atlas PO ERP for the first time. This takes a few minutes..."
  "$PY" -m venv "$ENV_DIR"
  "$ENV_DIR/bin/python" -m pip install --upgrade pip --quiet
  # The tools for tests and building aren't needed to run the program.
  grep -v -i -E '^(pytest|pyinstaller)' requirements.txt > "$ENV_DIR/run-requirements.txt"
  "$ENV_DIR/bin/python" -m pip install -r "$ENV_DIR/run-requirements.txt"
  cp requirements.txt "$STAMP"
fi

echo "Starting Atlas PO ERP (sample data). Log in as owner, password 123."
echo "The very first start builds the sample business, which takes a few seconds."
"$ENV_DIR/bin/python" -E "Atlas PO ERP (Sample Data).pyw"
