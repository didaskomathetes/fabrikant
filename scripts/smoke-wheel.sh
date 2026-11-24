#!/usr/bin/env bash
# Smoke test of the *built* wheel in a clean virtualenv (ADR 0069): install, `fabrik init`, validate,
# run a loop, serve the UI. Usage: bash scripts/smoke-wheel.sh [path/to/fabrik-*.whl]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
work="$(mktemp -d)"
trap 'kill "${pid:-0}" 2>/dev/null || true; rm -rf "$work"' EXIT

wheel="${1:-}"
if [ -z "$wheel" ]; then
  uv build --wheel --out-dir "$work/dist" >/dev/null
  wheel="$(ls "$work"/dist/fabrik-*.whl)"
fi

python3 -m venv "$work/venv"
"$work/venv/bin/pip" install -q "$wheel"
export PATH="$work/venv/bin:$PATH"
unset FABRIK_HOME FABRIK_DATA
export XDG_DATA_HOME="$work/xdg"

mkdir "$work/factory" && cd "$work/factory"   # outside any checkout: cwd is not a source tree
fabrik init . >/dev/null
fabrik validate projects/hello.fabrik.yaml
fabrik run hello greet | grep -q "hello from fabrik"
port=$((20000 + RANDOM % 10000))
fabrik serve --port "$port" >"$work/serve.log" 2>&1 &
pid=$!
for _ in $(seq 40); do
  curl -fs "http://127.0.0.1:$port/" -o "$work/index.html" && break
  sleep 0.25
done
grep -q "<html" "$work/index.html"
curl -fs "http://127.0.0.1:$port/app.js" -o /dev/null
[ ! -e "$work/xdg/fabrik" ] || { echo "data leaked into XDG default although cwd is a fabrik home" >&2; exit 1; }
echo "wheel smoke test OK"
