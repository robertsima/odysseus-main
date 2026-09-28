#!/usr/bin/env bash
# End-to-end test of the agent orchestration flow on Linux, with a scripted
# mock model. See README.md in this directory.
#
#   bash scripts/e2e_orchestration/run.sh            # main flow
#   E2E_VARIANT=workaround bash .../run.sh           # pytest in the workspace clone
#   E2E_REMOTE=local bash .../run.sh                 # origin = local bare path
#
# Everything lives under $E2E_ROOT (default ~/e2e), which is wiped at the start
# of each run. The app runs from a copy of this checkout on the Linux
# filesystem, never from the checkout itself, and never on its data/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
E2E_ROOT="${E2E_ROOT:-$HOME/e2e}"
VENV="${E2E_VENV:-$HOME/ci-repro/venv}"
PY="$VENV/bin/python"
VARIANT="${E2E_VARIANT:-main}"          # main | workaround
REMOTE="${E2E_REMOTE:-auto}"            # https | local | auto (https when sudo works)
APP_PORT="${E2E_APP_PORT:-7801}"
MOCK_PORT="${E2E_MOCK_PORT:-7811}"
TIMEOUT="${E2E_TIMEOUT:-300}"
GIT_HOST="git.e2e.test"
SLUG="robertsima/Umni"

[ -x "$PY" ] || { echo "no python venv at $VENV (set E2E_VENV)"; exit 2; }
command -v bwrap >/dev/null || echo "WARNING: bwrap is not installed; bash cannot run sandboxed"
env -i PATH=/usr/local/bin:/usr/bin:/bin python -c 'import pytest' 2>/dev/null || {
  echo "The sandboxed shell sees only /usr, so the worker's 'python -m pytest' needs a system"
  echo "python with pytest (Fedora: sudo dnf install python3-pytest python-unversioned-command)."
  exit 2
}
if [ "$REMOTE" = auto ]; then
  if sudo -n true 2>/dev/null; then REMOTE=https; else REMOTE=local; fi
fi

T0=$(date +%s)
# ── fresh tree ─────────────────────────────────────────────────────────────
if [ -d "$E2E_ROOT" ] && [ ! -f "$E2E_ROOT/.e2e-harness" ]; then
  echo "$E2E_ROOT exists and is not an e2e harness directory; refusing to wipe it"; exit 2
fi
for pidfile in "$E2E_ROOT"/run/*.pid; do
  [ -f "$pidfile" ] || continue
  pid=$(cat "$pidfile"); if [ "$(basename "$pidfile")" = githost.pid ]; then sudo -n kill "$pid" 2>/dev/null || true
  else kill "$pid" 2>/dev/null || true; fi
done
sleep 1
rm -rf "$E2E_ROOT"
mkdir -p "$E2E_ROOT"/{app,data,development,logs,run/home}
touch "$E2E_ROOT/.e2e-harness"
LOGS="$E2E_ROOT/logs"

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  if [ -f "$E2E_ROOT/run/githost.pid" ]; then sudo -n kill "$(cat "$E2E_ROOT/run/githost.pid")" 2>/dev/null || true; fi
}
trap cleanup EXIT

echo "== copying the app from $REPO"
tar -C "$REPO" --exclude=./.git --exclude=./data --exclude=./node_modules --exclude=./zima-local \
    --exclude='*/__pycache__' --exclude=./.venv -cf - . | tar -C "$E2E_ROOT/app" -xf -
(cd "$REPO" && git rev-parse --short HEAD 2>/dev/null && git status --porcelain 2>/dev/null | head -5) \
  > "$LOGS/app_revision.txt" || true

# ── fixture repositories ───────────────────────────────────────────────────
export GIT_AUTHOR_NAME="E2E" GIT_AUTHOR_EMAIL="e2e@example.invalid"
export GIT_COMMITTER_NAME="E2E" GIT_COMMITTER_EMAIL="e2e@example.invalid"
echo "== fixture: dummy Odysseus source repo, Umni remote + clone ($REMOTE)"
SRC="$E2E_ROOT/odysseus-src"
git init -q -b dev "$SRC"; echo "dummy source repository" > "$SRC/README.md"
git -C "$SRC" add -A; git -C "$SRC" commit -qm "dummy"

SEED="$E2E_ROOT/run/seed"
git init -q -b main "$SEED"
mkdir -p "$SEED/umni" "$SEED/tests"
printf '"""Tiny fixture package for the Odysseus orchestration e2e test."""\n\nfrom .core import add\n\n__all__ = ["add"]\n' > "$SEED/umni/__init__.py"
printf '"""Arithmetic helpers."""\n\n\ndef add(a, b):\n    return a + b\n' > "$SEED/umni/core.py"
printf 'from umni.core import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n' > "$SEED/tests/test_core.py"
printf '__pycache__/\n.pytest_cache/\n' > "$SEED/.gitignore"
printf '# Umni (e2e fixture)\n' > "$SEED/README.md"
git -C "$SEED" add -A; git -C "$SEED" commit -qm "Umni fixture"
git clone -q --bare "$SEED" "$E2E_ROOT/remote.git"
rm -rf "$SEED"

CLONE="$E2E_ROOT/development/umni"
APP_ENV=()
if [ "$REMOTE" = https ]; then
  TLS="$E2E_ROOT/tls"
  "$PY" "$HERE/githost.py" cert --dir "$TLS" --host "$GIT_HOST"
  cat "$("$PY" -c 'import certifi; print(certifi.where())')" "$TLS/ca.pem" > "$TLS/bundle.pem"
  grep -q " $GIT_HOST\$" /etc/hosts || echo "127.0.0.1 $GIT_HOST" | sudo -n tee -a /etc/hosts >/dev/null
  sudo -n "$PY" "$HERE/githost.py" serve --repo "$E2E_ROOT/remote.git" --slug "$SLUG" \
      --cert "$TLS/server.pem" --key "$TLS/server.key" --port 443 > "$LOGS/githost.log" 2>&1 &
  echo $! > "$E2E_ROOT/run/githost.pid"
  for _ in $(seq 1 30); do
    curl -s --cacert "$TLS/ca.pem" "https://$GIT_HOST/$SLUG.git/info/refs?service=git-upload-pack" -o /dev/null && break
    sleep 0.5
  done
  ORIGIN="https://$GIT_HOST/$SLUG.git"
  GIT_SSL_CAINFO="$TLS/ca.pem" git clone -q "$ORIGIN" "$CLONE"
  # The app's dulwich client verifies against the default trust store, which
  # honours SSL_CERT_FILE; GITHUB_HOST makes git.e2e.test the "GitHub" host.
  APP_ENV+=("SSL_CERT_FILE=$TLS/bundle.pem" "GITHUB_HOST=$GIT_HOST")
else
  ORIGIN="$E2E_ROOT/remote.git"
  git clone -q "$ORIGIN" "$CLONE"
fi
git -C "$CLONE" config user.name "E2E"; git -C "$CLONE" config user.email "e2e@example.invalid"
echo "   clone $CLONE  origin=$(git -C "$CLONE" remote get-url origin)"

# ── mock model ─────────────────────────────────────────────────────────────
echo "== starting mock model on :$MOCK_PORT (variant $VARIANT)"
MOCK_LOG="$LOGS/mock_requests.jsonl" MOCK_DUMP_DIR="$LOGS/requests" E2E_CLONE="$CLONE" E2E_DATA_DIR="$E2E_ROOT/data" E2E_VARIANT="$VARIANT" \
  "$PY" "$HERE/mock_model.py" --port "$MOCK_PORT" > "$LOGS/mock.log" 2>&1 &
PIDS+=($!); echo "${PIDS[-1]}" > "$E2E_ROOT/run/mock.pid"
for _ in $(seq 1 40); do curl -sf "http://127.0.0.1:$MOCK_PORT/v1/models" >/dev/null && break; sleep 0.25; done

# ── the app ────────────────────────────────────────────────────────────────
echo "== starting Odysseus on :$APP_PORT (data $E2E_ROOT/data)"
(
  cd "$E2E_ROOT/app"
  env -u VIRTUAL_ENV \
    PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    HOME="$E2E_ROOT/run/home" \
    ODYSSEUS_DATA_DIR="$E2E_ROOT/data" \
    APP_BIND=127.0.0.1 APP_PORT="$APP_PORT" \
    AUTH_ENABLED=true LOCALHOST_BYPASS=false SECURE_COOKIES=false \
    ODYSSEUS_INPROCESS_POLLERS=0 ODYSSEUS_INPROCESS_TASKS=0 \
    ODYSSEUS_AGENT_SOURCE_REPO="$SRC" \
    CHROMADB_HOST=127.0.0.1 CHROMADB_PORT=1 \
    FASTEMBED_CACHE_PATH="$E2E_ROOT/data/fastembed_cache" \
    TZ=UTC LANG=C.UTF-8 \
    "${APP_ENV[@]}" \
    "$PY" app.py
) > "$LOGS/app.stdout" 2>&1 &
PIDS+=($!); echo "${PIDS[-1]}" > "$E2E_ROOT/run/app.pid"

# ── drive it ───────────────────────────────────────────────────────────────
set +e
"$PY" "$HERE/driver.py" --app "http://127.0.0.1:$APP_PORT" --mock "http://127.0.0.1:$MOCK_PORT/v1" \
  --root "$E2E_ROOT" --variant "$VARIANT" --timeout "$TIMEOUT"
RC=$?
set -e
cp -f "$E2E_ROOT/data/logs/app.log" "$LOGS/app.log" 2>/dev/null || true
echo "== remote mode: $REMOTE; wall time $(( $(date +%s) - T0 ))s; logs in $LOGS"
exit $RC
