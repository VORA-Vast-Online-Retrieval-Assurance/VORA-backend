#!/usr/bin/env bash
# One-time local setup for macOS/Linux (also works in Git Bash on Windows).
# Safe to re-run: it never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")"

# Use the first interpreter that actually runs and is new enough. On Windows a
# "python3" on PATH can be a Microsoft Store shortcut rather than Python.
supported() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; }
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3 python; do
    if supported "$candidate"; then PYTHON="$candidate"; break; fi
  done
fi
if [ -z "$PYTHON" ] || ! supported "$PYTHON"; then
  echo "Python 3.11 or newer is required but was not found on PATH (set PYTHON=/path/to/python to choose one)." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment in .venv"
  "$PYTHON" -m venv .venv
fi
if [ -x .venv/bin/python ]; then
  VENV_PYTHON=.venv/bin/python
  ACTIVATE="source .venv/bin/activate"
else
  VENV_PYTHON=.venv/Scripts/python.exe
  ACTIVATE="source .venv/Scripts/activate"
fi

echo "Installing dependencies"
"$VENV_PYTHON" -m pip install --quiet --upgrade pip
"$VENV_PYTHON" -m pip install --quiet -r requirements-dev.txt

if [ -f .env ]; then
  echo ".env already exists; left unchanged"
else
  cp .env.example .env
  echo "Created .env from .env.example"
fi

env_value() {
  grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | tr -d '"'"'"'\r' || true
}
BINARY="$(env_value VORA_BROWSER_BINARY)"
if [ -z "$BINARY" ]; then
  echo "WARNING: VORA_BROWSER_BINARY is empty in .env. Set it to a Chrome or Chromium executable (any works)."
elif [ ! -f "$BINARY" ]; then
  echo "WARNING: VORA_BROWSER_BINARY does not point to a file: $BINARY"
fi
if [ -z "$(env_value GROQ_API_KEY)" ] && [ -z "$(env_value GEMINI_API_KEY)" ]; then
  echo "NOTE: no LLM API key set; the deterministic planner will be used."
fi
if [ -z "$(env_value VORA_SEARCH_API_KEY)" ]; then
  echo "NOTE: no search API key set; searches run in the browser, which search engines often challenge."
  echo "      For dependable results set VORA_SEARCH_API=brave (or google) and VORA_SEARCH_API_KEY in .env."
fi

cat <<EOF

Setup complete. Next steps:
  1. Edit .env and set VORA_BROWSER_BINARY (and optionally an LLM API key)
  2. $ACTIVATE
  3. python app.py            the API is at http://127.0.0.1:8000 (docs at /docs)
     The web app is a separate project (VORA/Frontend); see README, "Web app".
Tests:
  python -m unittest discover -s tests
EOF
