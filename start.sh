#!/usr/bin/env bash
# Start the built frontend and API together. Compatible with macOS Bash 3.2.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="${EVO_HOST:-127.0.0.1}"
PORT="${EVO_PORT:-8000}"
CONDA_ENV_NAME="${EVO_CONDA_ENV:-evo}"

# Which Python environment to run in, in order: whatever EVO_VENV names, the
# conda environment this project uses (`evo` by default), then the
# repository's own .venv — created on first launch if none of the above
# exists. One script for every machine: naming an environment is not a thing
# to remember, and the wrong interpreter is how Evolve ends up "unavailable"
# (no dspy) or torch broken.
conda_env_path() {
    local base=""
    if [[ -n "${CONDA_EXE:-}" && -x "${CONDA_EXE:-}" ]]; then
        base="$("$CONDA_EXE" info --base 2>/dev/null || true)"
    elif command -v conda >/dev/null 2>&1; then
        base="$(conda info --base 2>/dev/null || true)"
    fi
    [[ -z "$base" && -d "$HOME/anaconda3" ]] && base="$HOME/anaconda3"
    [[ -z "$base" && -d "$HOME/miniconda3" ]] && base="$HOME/miniconda3"
    [[ -z "$base" ]] && return 1
    local candidate="$base/envs/$1"
    [[ -x "$candidate/bin/python" ]] && printf '%s\n' "$candidate"
}

if [[ -n "${EVO_VENV:-}" ]]; then
    VENV="$EVO_VENV"
elif VENV="$(conda_env_path "$CONDA_ENV_NAME")" && [[ -n "$VENV" ]]; then
    :
else
    VENV="$ROOT/.venv"
fi
EXTRAS="${EVO_EXTRAS:-studio,harness,mem0,optimizers}"
INSTALL=0
BUILD=1

usage() {
    cat <<'HELP'
Usage: ./start.sh [--host ADDRESS] [--port NUMBER] [--install] [--no-build]

Runs in the conda environment named by EVO_CONDA_ENV (default: evo) when it
exists, else in the repository's .venv, created on first launch. EVO_VENV
overrides both. Dependencies are installed there and the UI is rebuilt; a
later launch reuses them. Ctrl+C stops the server.

  --install    Reinstall dependencies (also after changing Python extras).
  --no-build   Use an existing frontend/dist build; Node.js is not required.
  --help       Show this help.

Environment: PYTHON (Python 3.11+ executable, only when creating .venv),
             EVO_CONDA_ENV (default: evo), EVO_VENV (an explicit environment
             path), EVO_HOST, EVO_PORT,
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
echo "Python environment: $VENV"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || fail "The selected environment ($VENV) needs Python 3.11+. Point EVO_VENV or EVO_CONDA_ENV at one that has it."

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

# The LLM transport package is supplied per machine and is not shipped with
# this repository (docs/llm-contract.md), so there is nothing to copy into
# place -- but starting without one fails later, inside the first model call.
if [[ ! -f llm/__init__.py ]] && ! "$PY" -c 'import llm' >/dev/null 2>&1; then
    fail "No LLM transport package found. Put your \`llm/\` package at the repository root (interface: docs/llm-contract.md), then retry."
fi
if [[ -f llm/__init__.py && ! -e llm/providers.json ]]; then
    echo "Note: llm/providers.json is absent; your package may configure providers another way (docs/llm-contract.md)."
fi

# Credentials: the repository's own .env, or one kept beside the checkout
# (a shared .env one level up is a common way to hold company credentials).
# Parsed, never executed as shell code, and a variable already exported wins.
ENV_FILE=""
for candidate in .env ../.env; do
    [[ -f "$candidate" ]] && { ENV_FILE="$candidate"; break; }
done
if [[ -n "$ENV_FILE" ]]; then
    echo "Model credentials: $ENV_FILE"
    ENV_EXPORTS="$(mktemp)"
    trap 'rm -f "$ENV_EXPORTS"' EXIT
    "$PY" - "$ENV_FILE" > "$ENV_EXPORTS" <<'ENVPY'
import shlex
import sys

for raw in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    name = name.strip()
    if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    # Quoted exactly once, by shlex, and only when the shell has not
    # exported one already. Never reshape the value: a secret that arrives
    # one character short fails as "incorrect padding" far from here.
    print(f'if [ -z "${{{name}+x}}" ]; then export {name}={shlex.quote(value)}; fi')
ENVPY
    . "$ENV_EXPORTS"
else
    (umask 077; printf '# Model credentials. Not committed.\n# Each provider names the variables it needs; see llm/providers.json\n# and docs/llm-contract.md.\n' > .env)
    echo "Created local .env. Add the variables your provider needs before using model features."
fi

echo "Starting EvoStudio at http://$HOST:$PORT — press Ctrl+C to stop."
exec "$PY" -m uvicorn backend.api.app:app --host "$HOST" --port "$PORT"
