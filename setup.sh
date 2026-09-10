#!/bin/sh
set -eu

SOURCE=$0
while [ -L "$SOURCE" ]; do
    DIRECTORY=$(CDPATH= cd -P -- "$(dirname -- "$SOURCE")" && pwd)
    SOURCE=$(readlink "$SOURCE")
    case "$SOURCE" in
        /*) ;;
        *) SOURCE="$DIRECTORY/$SOURCE" ;;
    esac
done
ROOT=$(CDPATH= cd -P -- "$(dirname -- "$SOURCE")" && pwd)

INSTALL_DIR=${GROGU_INSTALL_DIR:-}
UPDATE_PATH=${GROGU_UPDATE_PATH:-1}

usage() {
    printf '%s\n' \
        "Usage: ./setup.sh [--install-dir DIR] [--no-path-update]" \
        "" \
        "Installs a symlink named grogu and optionally adds its directory to the" \
        "current user's future shell startup configuration."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-dir)
            [ "$#" -ge 2 ] || { printf '%s\n' "setup.sh: --install-dir needs a directory" >&2; exit 2; }
            INSTALL_DIR=$2
            shift 2
            ;;
        --no-path-update)
            UPDATE_PATH=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'setup.sh: unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

# Default to a stable, user-owned directory rather than scanning PATH for the
# first writable entry: version manager shims (fnm, nvm, pyenv, rbenv, ...)
# are often writable and already on PATH, but their directories are tied to
# whatever toolchain version is currently active and can disappear when that
# version is switched or uninstalled, silently breaking the grogu symlink.
INSTALL_DIR=${INSTALL_DIR:-"$HOME/.local/bin"}
if [ ! -d "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR"
fi
INSTALL_DIR=$(CDPATH= cd -P -- "$INSTALL_DIR" && pwd)
TARGET="$INSTALL_DIR/grogu"
LAUNCHER="$ROOT/bin/grogu"

if [ -t 1 ]; then
    GREEN=$(printf '\033[32m')
    RED=$(printf '\033[31m')
    RESET=$(printf '\033[0m')
else
    GREEN=
    RED=
    RESET=
fi

check_ok() {
    printf '%s✓%s %s\n' "$GREEN" "$RESET" "$1"
}

check_fail() {
    printf '%s✗%s %s\n' "$RED" "$RESET" "$1"
}

if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    CURRENT=$(readlink "$TARGET" 2>/dev/null || true)
    case "$CURRENT" in
        "$LAUNCHER") ;;
        *)
            if [ -L "$TARGET" ] && [ ! -e "$TARGET" ]; then
                # A moved Grogu checkout leaves its old installation broken.
                rm "$TARGET"
                ln -s "$LAUNCHER" "$TARGET"
            else
                printf '%s\n' "setup.sh: refusing to replace existing $TARGET" >&2
                printf '%s\n' "Remove it or choose another --install-dir." >&2
                exit 1
            fi
            ;;
    esac
else
    ln -s "$LAUNCHER" "$TARGET"
fi

path_entry="export PATH=\"$INSTALL_DIR:\$PATH\""
case ":${PATH:-}:" in
    *:"$INSTALL_DIR":*) path_configured=1 ;;
    *) path_configured=0 ;;
esac

if [ "$UPDATE_PATH" = "1" ] && [ "$path_configured" -eq 0 ]; then
    shell_name=$(basename "${SHELL:-sh}")
    case "$shell_name" in
        zsh) shell_file=${ZDOTDIR:-"$HOME"}/.zprofile ;;
        bash)
            if [ -f "$HOME/.bash_profile" ]; then
                shell_file=$HOME/.bash_profile
            else
                shell_file=$HOME/.bashrc
            fi
            ;;
        *) shell_file=$HOME/.profile ;;
    esac
    marker="# Grogu"
    if [ ! -f "$shell_file" ] || ! grep -Fq "$marker" "$shell_file"; then
        printf '\n%s\n%s\n' "$marker" "$path_entry" >> "$shell_file"
        path_configured=1
        printf 'Added %s to %s\n' "$INSTALL_DIR" "$shell_file"
    fi
fi

printf 'Installed %s -> %s\n' "$TARGET" "$LAUNCHER"
case ":${PATH:-}:" in
    *:"$INSTALL_DIR":*) ;;
    *)
        printf 'Open a new shell or run: %s\n' "$path_entry"
        ;;
esac

printf '\n%s\n' "Grogu setup checks:"
check_ok "Core launcher installed"

find_python() {
    best=
    best_major=-1
    best_minor=-1
    old_ifs=$IFS
    IFS=:
    for directory in ${PATH:-}; do
        [ -n "$directory" ] || directory=.
        for candidate in "$directory"/python3*; do
            [ -x "$candidate" ] || continue
            version=$("$candidate" -c 'import sys; print("%d.%d" % (sys.version_info[0], sys.version_info[1]))' 2>/dev/null) || continue
            major=${version%%.*}
            minor=${version#*.}
            case "$major:$minor" in
                ''|*[!0-9:]*|*:*:*) continue ;;
            esac
            if [ "$major" -gt "$best_major" ] ||
               { [ "$major" -eq "$best_major" ] && [ "$minor" -gt "$best_minor" ]; }; then
                best=$candidate
                best_major=$major
                best_minor=$minor
            fi
        done
    done
    IFS=$old_ifs

    if [ -n "$best" ] && [ "$best_major" -ge 3 ] &&
       { [ "$best_major" -gt 3 ] || [ "$best_minor" -ge 10 ]; }; then
        printf '%s\n' "$best"
    else
        command -v python3
    fi
}

PYTHON=$(find_python)

if command -v "$PYTHON" >/dev/null 2>&1; then
    if "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        check_ok "Python 3.10+ available ($PYTHON)"
        if "$PYTHON" -c 'import mcp' >/dev/null 2>&1; then
            check_ok "MCP package available"
        elif "$PYTHON" -m pip install --quiet mcp >/dev/null 2>&1; then
            check_ok "MCP package installed for codemode tool calls"
        else
            check_fail "MCP package unavailable (optional)"
            printf '  Fix: %s -m pip install mcp\n' "$PYTHON"
        fi
    else
        check_fail "Python 3.10+ unavailable (codemode MCP calls disabled)"
        printf '%s\n' "  Fix: install Python 3.10 or newer; core Grogu commands still work."
    fi
else
    check_fail "Python 3 unavailable"
    printf '%s\n' "  Fix: install Python 3.10 or newer."
fi

if command -v copilot >/dev/null 2>&1; then
    check_ok "Copilot CLI detected"
else
    check_fail "Copilot CLI not found (required to launch Grogu)"
    printf '%s\n' "  Fix: install Copilot CLI, then run: grogu doctor"
fi

if [ -f "$ROOT/.github/AGENTS.md" ]; then
    check_ok "Grogu instructions found"
else
    check_fail "Grogu instructions missing"
    printf '%s\n' "  Fix: reinstall Grogu from a complete checkout."
fi

if [ -f "$ROOT/plugin.json" ] &&
   [ -f "$ROOT/.github/skills/grogu-pipeline/SKILL.md" ]; then
    check_ok "Harness Copilot skills found"
else
    check_fail "Harness Copilot skills missing"
    printf '%s\n' "  Fix: reinstall Grogu from a complete checkout."
fi

check_ok "Platform-specific capabilities remain user-configured"
printf '%s\n' "  Add a local Copilot plugin repository with: grogu capability add PATH"

printf '\n%s\n' "Grogu setup complete."
