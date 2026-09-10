#!/usr/bin/env bash
# Start the built frontend and API together. Compatible with macOS Bash 3.2.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="${EVO_HOST:-127.0.0.1}"
PORT="${EVO_PORT:-8000}"
VENV="${EVO_VENV:-$ROOT/.venv}"
EXTRAS="${EVO_EXTRAS:-studio,harness,mem0,optimizers}"
INSTALL=0
BUILD=1

usage() {
    cat <<'HELP'
Usage: ./start.sh [--host ADDRESS] [--port NUMBER] [--install] [--no-build]

First launch creates .venv, installs Python dependencies and builds the UI.
Later launches reuse dependencies and rebuild the UI. Ctrl+C stops the server.

  --install    Reinstall dependencies (also after changing Python extras).
  --no-build   Use an existing frontend/dist build; Node.js is not required.
  --help       Show this help.

Environment: PYTHON (Python 3.11+ executable), EVO_VENV, EVO_HOST, EVO_PORT,
             EVO_EXTRAS (default: studio,harness,mem0,optimizers).
Optional RAG/tool integrations can be added with EVO_EXTRAS.
HELP
}
fail() { echo "Error: $*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host|--port)
            [[ $# -ge 2 && -n "$2" ]] || fail "$1 requires a value."
            if [[ "$1" == --host ]]; then HOST="$2"; else PORT="$2"; fi
            shift 2 ;;
        --install) INSTALL=1; shift ;;
        --no-build) BUILD=0; shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown option: $1 (see --help)." ;;
    esac
done
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] || fail "Port must be a number between 1 and 65535."
PORT=$((10#$PORT))
(( PORT >= 1 && PORT <= 65535 )) || fail "Port must be between 1 and 65535."

if (( BUILD )); then
    command -v npm >/dev/null 2>&1 || fail "Install Node.js 22 LTS (including npm), then retry."
elif [[ ! -f frontend/dist/index.html ]]; then
    fail "No frontend build found. Run without --no-build first."
fi

if [[ ! -x "$VENV/bin/python" ]]; then
    PYTHON="${PYTHON:-python3}"
    command -v "$PYTHON" >/dev/null 2>&1 || fail "Install Python 3.11+ (3.12 recommended)."
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || fail "Python 3.11+ required; select it with PYTHON=python3.12."
    echo "Creating Python environment: $VENV"
    "$PYTHON" -m venv "$VENV"
fi
PY="$VENV/bin/python"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || fail "The selected environment needs Python 3.11+. Use a new EVO_VENV."

# Detect occupied ports before spending time installing or building. Uvicorn
# still performs the authoritative bind after setup, including race handling.
"$PY" - "$HOST" "$PORT" <<'PY'
import socket
import sys
try:
    with socket.create_server((sys.argv[1], int(sys.argv[2])),
                              family=socket.AF_INET6 if ':' in sys.argv[1] else socket.AF_INET):
        pass
except OSError as exc:
    sys.exit(f"Cannot listen on {sys.argv[1]}:{sys.argv[2]}: {exc}. Try --port 8001.")
PY

fingerprint() {
    "$PY" - "$@" <<'PY'
import hashlib
import pathlib
import sys
h = hashlib.sha256()
for name in sys.argv[1:]:
    h.update(pathlib.Path(name).read_bytes())
print(h.hexdigest())
PY
}
PY_STAMP="$(fingerprint pyproject.toml):$EXTRAS:$ROOT"
if (( INSTALL )) || [[ ! -f "$VENV/.evostudio-dependencies" ]] || [[ "$(cat "$VENV/.evostudio-dependencies")" != "$PY_STAMP" ]]; then
    echo "Installing Python dependencies ($EXTRAS); the first launch may take a while…"
    "$PY" -m pip install -e ".[${EXTRAS}]"
    printf '%s\n' "$PY_STAMP" > "$VENV/.evostudio-dependencies"
fi

if (( BUILD )); then
    JS_STAMP="$(fingerprint frontend/package.json frontend/package-lock.json)"
    if (( INSTALL )) || [[ ! -f frontend/node_modules/.evostudio-dependencies ]] || [[ "$(cat frontend/node_modules/.evostudio-dependencies)" != "$JS_STAMP" ]]; then
        npm --prefix frontend ci
        printf '%s\n' "$JS_STAMP" > frontend/node_modules/.evostudio-dependencies
    fi
    npm --prefix frontend run build
fi

# Never overwrite existing credentials and never execute .env as shell code.
if [[ ! -e backend/llm/providers.json ]]; then
    (umask 077; cp backend/llm/providers.example.json backend/llm/providers.json)
    echo "Created local backend/llm/providers.json. Set your API host and model there."
fi
if [[ ! -e .env ]]; then
    (umask 077; printf '# Set your model API key here. This file is not committed.\nLLM_API_KEY=\n' > .env)
    echo "Created local .env. Add LLM_API_KEY before using model features."
fi

echo "Starting EvoStudio at http://$HOST:$PORT — press Ctrl+C to stop."
exec "$PY" -m uvicorn backend.api.app:app --host "$HOST" --port "$PORT"
