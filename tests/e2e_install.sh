#!/bin/sh
# End to end on macOS/Linux: run install.sh (with a local claude_pace.py, no
# download) in a fake home, run the status line it installed, check the
# launcher, then uninstall and check everything is gone.
#
#   PY=python3 sh tests/e2e_install.sh
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=$(command -v "${PY:-python3}")
FAKE=$(mktemp -d)
trap 'rm -rf "$FAKE"' EXIT

export HOME="$FAKE/home"
mkdir -p "$HOME"
unset CLAUDE_CONFIG_DIR CLAUDE_PACE_DIR 2>/dev/null || true
export CLAUDE_PACE_SOURCE="$ROOT/claude_pace.py" CLAUDE_PACE_SKIP_CHECKSUM=1
export CLAUDE_PACE_PYTHON="$PY" CLAUDE_PACE_NO_ACCOUNT=1

sh "$ROOT/install.sh"

S="$HOME/.claude/settings.json"
[ -f "$S" ] || { echo "no settings.json written"; exit 1; }
"$PY" "$ROOT/tests/run_status_command.py" "$S" "$HOME/.claude"

L="$HOME/.local/bin/claude-pace"
[ -x "$L" ] || { echo "launcher $L missing"; exit 1; }
"$L" --version

sh "$ROOT/install.sh" --uninstall
"$PY" - "$S" <<'PY'
import json, os, sys
p = sys.argv[1]
d = json.load(open(p)) if os.path.exists(p) else {}
assert 'statusLine' not in d, d
PY
[ ! -e "$HOME/.claude-pace" ] || { echo "$HOME/.claude-pace left behind"; exit 1; }
[ ! -e "$L" ] || { echo "launcher left behind"; exit 1; }
echo "e2e install/uninstall ok"
