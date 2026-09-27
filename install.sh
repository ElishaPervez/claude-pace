#!/bin/sh
# claude-pace - installer for macOS and Linux.
#
#   curl -fsSL https://github.com/ElishaPervez/claude-pace/releases/latest/download/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --uninstall [--purge]
#
# Downloads claude_pace.py into ~/.claude-pace, checks it against the
# release's SHA256SUMS, then runs `claude_pace.py install`, which points
# Claude Code's status line at it and adds a `claude-pace` command.
# Nothing needs sudo. Anything after the options is passed to install /
# uninstall (e.g. --no-launcher, --purge).
#
# Settings (environment variables):
#   CLAUDE_PACE_VERSION=v1.0.0     install that release instead of the latest
#   CLAUDE_PACE_HOME=DIR           where the script lives (default ~/.claude-pace)
#   CLAUDE_PACE_SOURCE=FILE        copy this local claude_pace.py instead of
#                                   downloading (for testing)
#   CLAUDE_PACE_SKIP_CHECKSUM=1    don't verify the download (for testing)
#   CLAUDE_PACE_PYTHON=PATH        use this Python instead of searching
#   CLAUDE_PACE_BASE_URL=URL       download from here instead of GitHub (testing)
#
# Everything is inside functions and only runs from the last line, so a
# download cut off halfway through `curl | sh` runs nothing at all.
set -eu

REPO="ElishaPervez/claude-pace"

say() { printf '%s\n' "$*"; }
die() { printf 'claude-pace: %s\n' "$*" >&2; exit 1; }

# -- find Python 3.9 or newer --
python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

find_python() {
    if [ -n "${CLAUDE_PACE_PYTHON:-}" ]; then
        python_ok "$CLAUDE_PACE_PYTHON" || return 1
        printf '%s\n' "$CLAUDE_PACE_PYTHON"
        return 0
    fi
    for name in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python \
                /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        p=$(command -v "$name" 2>/dev/null) || continue
        # On a Mac without the command line tools, /usr/bin/python3 is a stub
        # that pops up an install dialog instead of running.
        if [ "$p" = /usr/bin/python3 ] && [ "$(uname -s)" = Darwin ] \
           && ! xcode-select -p >/dev/null 2>&1; then
            continue
        fi
        if python_ok "$p"; then
            printf '%s\n' "$p"
            return 0
        fi
    done
    return 1
}

# pyenv, asdf and mise put "shims" on the PATH that pick a Python by the
# current folder. The status line runs in every project folder, so one that
# pins a Python that isn't installed would break it: use the real Python
# the shim points to right now.
real_python() {
    case "$1" in
        */shims/*)
            real=$("$1" -c 'import sys; print(sys.executable)' 2>/dev/null) || real=
            if [ -n "$real" ] && python_ok "$real"; then
                printf '%s\n' "$real"
                return 0
            fi ;;
    esac
    printf '%s\n' "$1"
}

no_python() {
    say "Python 3.9 or newer is needed and wasn't found."
    case "$(uname -s)" in
        Darwin)
            say "  Install Apple's command line tools (includes Python 3.9):  xcode-select --install"
            say "  or get the latest from https://www.python.org/downloads/macos/"
            say "  or with Homebrew:  brew install python" ;;
        *)
            say "  Debian/Ubuntu:  sudo apt install python3"
            say "  Fedora:         sudo dnf install python3"
            say "  Arch:           sudo pacman -S python" ;;
    esac
    say "Then run this installer again."
    exit 1
}

# Under `curl | sh` this shell's input is the pipe, so give the Python side
# the terminal (or nothing) instead of letting it read the rest of the pipe.
run_script() {
    if [ -t 0 ]; then
        "$PY" "$@"
    elif (: </dev/tty) 2>/dev/null; then
        "$PY" "$@" </dev/tty
    else
        "$PY" "$@" </dev/null
    fi
}

# -- download --
fetch() {  # fetch URL FILE
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        die "curl or wget is needed to download."
    fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        "$PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
    fi
}

cleanup() {
    rm -f "$TMP" "$SUMS"
    # A failed first install leaves no empty folder behind.
    if [ "$MADE_DIR" = 1 ] && [ ! -f "$SCRIPT" ]; then
        rmdir "$APP_DIR" 2>/dev/null || true
    fi
}

main() {
    APP_DIR="${CLAUDE_PACE_HOME:-$HOME/.claude-pace}"
    SCRIPT="$APP_DIR/claude_pace.py"

    ACTION=install
    if [ "${1:-}" = "--uninstall" ]; then
        ACTION=uninstall
        shift
    fi

    PY=$(find_python) || no_python
    PY=$(real_python "$PY")

    # -- uninstall --
    if [ "$ACTION" = uninstall ]; then
        if [ -f "$SCRIPT" ]; then
            run_script "$SCRIPT" uninstall "$@"
        else
            say "Nothing to uninstall: $SCRIPT isn't there."
        fi
        rm -rf "$APP_DIR"
        say "Removed $APP_DIR."
        return 0
    fi

    if [ -n "${CLAUDE_PACE_BASE_URL:-}" ]; then
        BASE="$CLAUDE_PACE_BASE_URL"
    elif [ -n "${CLAUDE_PACE_VERSION:-}" ]; then
        BASE="https://github.com/$REPO/releases/download/$CLAUDE_PACE_VERSION"
    else
        BASE="https://github.com/$REPO/releases/latest/download"
    fi

    MADE_DIR=0
    [ -d "$APP_DIR" ] || MADE_DIR=1
    TMP="$SCRIPT.download.tmp"
    SUMS="$APP_DIR/SHA256SUMS.tmp"
    trap cleanup EXIT
    mkdir -p "$APP_DIR"

    if [ -n "${CLAUDE_PACE_SOURCE:-}" ]; then
        cp "$CLAUDE_PACE_SOURCE" "$TMP"
    else
        say "Downloading claude_pace.py from $BASE ..."
        fetch "$BASE/claude_pace.py" "$TMP" || die "download failed."
    fi

    if [ "${CLAUDE_PACE_SKIP_CHECKSUM:-}" = 1 ]; then
        say "Skipping the checksum check (CLAUDE_PACE_SKIP_CHECKSUM=1)."
    else
        fetch "$BASE/SHA256SUMS" "$SUMS" || die "couldn't download SHA256SUMS."
        want=$(awk '$2 == "claude_pace.py" || $2 == "*claude_pace.py" { print $1 }' "$SUMS")
        [ -n "$want" ] || die "SHA256SUMS has no entry for claude_pace.py."
        got=$(sha256_of "$TMP")
        [ "$want" = "$got" ] || die "checksum mismatch for claude_pace.py - not installing."
    fi

    mv "$TMP" "$SCRIPT"
    chmod +x "$SCRIPT"

    # -- hook it into Claude Code (it says if ~/.local/bin isn't on the PATH) --
    run_script "$SCRIPT" install --python "$PY" "$@"
}

main "$@"
