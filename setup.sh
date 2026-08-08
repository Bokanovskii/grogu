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

if [ -z "$INSTALL_DIR" ]; then
    OLD_IFS=$IFS
    IFS=:
    for directory in ${PATH:-}; do
        if [ -d "$directory" ] && [ -w "$directory" ]; then
            INSTALL_DIR=$directory
            break
        fi
    done
    IFS=$OLD_IFS
fi

INSTALL_DIR=${INSTALL_DIR:-"$HOME/.local/bin"}
if [ ! -d "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR"
fi
INSTALL_DIR=$(CDPATH= cd -P -- "$INSTALL_DIR" && pwd)
TARGET="$INSTALL_DIR/grogu"
LAUNCHER="$ROOT/bin/grogu"

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

# Best-effort: `bin/grogu` itself already prefers the newest 3.10+
# interpreter it finds on PATH (python3.13/.12/.11/.10) over plain `python3`,
# so this only needs to install `mcp` into whichever one grogu will actually
# use — matching that same preference order keeps this in sync with
# `bin/grogu` rather than picking a different interpreter to check/install
# into. Neither check is fatal to setup: the rest of Grogu only needs stdlib.
PYTHON=python3
for candidate in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON=$candidate
        break
    fi
done

if command -v "$PYTHON" >/dev/null 2>&1; then
    if "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        if "$PYTHON" -c 'import mcp' >/dev/null 2>&1; then
            printf '%s\n' "$PYTHON is 3.10+ and 'mcp' is already installed."
        elif "$PYTHON" -m pip install --quiet mcp >/dev/null 2>&1; then
            printf '%s is 3.10+; installed '\''mcp'\'' for codemode MCP tool calls.\n' "$PYTHON"
        else
            printf '%s\n' "Warning: could not install 'mcp' (offline, or pip unavailable)."
            printf 'Run '\''%s -m pip install mcp'\'' later to enable codemode MCP tool calls.\n' "$PYTHON"
        fi
    else
        printf 'Warning: %s is older than 3.10; codemode'\''s MCP\n' "$PYTHON"
        printf '%s\n' "tool calling (grogu codemode exec, mcp_call(...)) needs 3.10+."
        printf '%s\n' "Everything else in Grogu works fine on an older python3."
    fi
fi

if command -v copilot >/dev/null 2>&1; then
    "$TARGET" doctor >/dev/null
    printf '%s\n' "Grogu setup complete; Copilot CLI detected."
else
    printf '%s\n' "Grogu setup complete; Copilot CLI was not found on PATH."
    printf '%s\n' "Install Copilot CLI, then run: grogu doctor"
fi
