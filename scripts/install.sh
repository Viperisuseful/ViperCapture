#!/usr/bin/env bash
# Install the vipercapture command for the current user.
#
# Linux and macOS:
#   curl -fsSL https://raw.githubusercontent.com/Viperisuseful/ViperCapture/master/scripts/install.sh | bash
#
# Re-running the command updates the app files and keeps an existing .venv.
# VIPERCAPTURE_REF selects a GitHub branch. VIPERCAPTURE_ARCHIVE_URL overrides
# the tarball. VIPERCAPTURE_SOURCE installs a local checkout instead.
set -euo pipefail

ref="${VIPERCAPTURE_REF:-master}"
prefix="${VIPERCAPTURE_HOME:-$HOME/.vipercapture}"
app="$prefix/app"
bin_dir="${VIPERCAPTURE_BIN_DIR:-$HOME/.local/bin}"
archive_url="${VIPERCAPTURE_ARCHIVE_URL:-https://github.com/Viperisuseful/ViperCapture/archive/refs/heads/${ref}.tar.gz}"
tmpdir=""
python_bin=""
source_dir=""

cleanup() {
    if [ -n "$tmpdir" ] && [ -d "$tmpdir" ]; then
        rm -rf "$tmpdir"
    fi
}
trap cleanup EXIT INT TERM

quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

python_at_least_311() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_python() {
    if [ -n "${VIPERCAPTURE_PYTHON:-}" ]; then
        if python_at_least_311 "$VIPERCAPTURE_PYTHON"; then
            python_bin=$VIPERCAPTURE_PYTHON
            return 0
        fi
        echo "VIPERCAPTURE_PYTHON is not Python 3.11 or newer: $VIPERCAPTURE_PYTHON" >&2
        return 1
    fi
    for candidate in python3 python python3.13 python3.12 python3.11; do
        resolved=$(command -v "$candidate" 2>/dev/null || true)
        if [ -n "$resolved" ] && python_at_least_311 "$resolved"; then
            python_bin=$resolved
            return 0
        fi
    done
    return 1
}

ensure_python() {
    if find_python; then
        return 0
    fi
    echo "  Python 3.11+ was not found. Installing uv to provide Python 3.12..."
    if ! command -v uv >/dev/null 2>&1; then
        curl -fsSL https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
    if ! command -v uv >/dev/null 2>&1; then
        echo "Install Python 3.11 or newer, then run this installer again." >&2
        return 1
    fi
    uv python install 3.12
    python_bin=$(uv python find 3.12)
}

detect_source() {
    if [ -n "${VIPERCAPTURE_SOURCE:-}" ]; then
        if [ ! -f "$VIPERCAPTURE_SOURCE/launch.py" ]; then
            echo "VIPERCAPTURE_SOURCE does not contain launch.py: $VIPERCAPTURE_SOURCE" >&2
            return 1
        fi
        source_dir=$VIPERCAPTURE_SOURCE
        return 0
    fi
    script_path=$0
    if [ -f "$script_path" ]; then
        script_dir=$(CDPATH= cd "$(dirname "$script_path")" && pwd)
        if [ -f "$script_dir/../launch.py" ]; then
            source_dir=$(CDPATH= cd "$script_dir/.." && pwd)
            return 0
        fi
    fi
    return 1
}

stage_tree() {
    from=$1
    kept=""
    if [ -d "$app/.venv" ]; then
        kept=$(mktemp -d)
        mv "$app/.venv" "$kept/venv"
    fi
    # `if ! cmd; then status=$?` stores 0, because the negation succeeds.
    status=0
    {
        rm -rf "$app"
        mkdir -p "$app"
        tar -C "$from" \
            --exclude .git \
            --exclude .venv \
            --exclude node_modules \
            --exclude __pycache__ \
            --exclude .pytest_cache \
            -cf - . | tar -C "$app" -xf -
    } || status=$?
    if [ "$status" -ne 0 ]; then
        if [ -n "$kept" ]; then
            mkdir -p "$app"
            mv "$kept/venv" "$app/.venv"
            rm -rf "$kept"
        fi
        return "$status"
    fi
    if [ -n "$kept" ]; then
        mv "$kept/venv" "$app/.venv"
        rm -rf "$kept"
    fi
    if [ ! -f "$app/launch.py" ]; then
        echo "The installed files do not include launch.py." >&2
        return 1
    fi
}

download_source() {
    echo "  Downloading ViperCapture ($ref)..."
    curl -fsSL "$archive_url" -o "$tmpdir/src.tar.gz"
    tar -xzf "$tmpdir/src.tar.gz" -C "$tmpdir"
    set -- "$tmpdir"/*/
    if [ ! -f "$1/launch.py" ]; then
        echo "The downloaded archive does not include launch.py." >&2
        return 1
    fi
    source_dir=${1%/}
}

rc_file() {
    case "${SHELL:-}" in
        */zsh) printf '%s\n' "$HOME/.zshrc" ;;
        */bash) printf '%s\n' "$HOME/.bashrc" ;;
        *) printf '%s\n' "$HOME/.profile" ;;
    esac
}

ensure_on_path() {
    case ":$PATH:" in
        *":$bin_dir:"*) return 0 ;;
    esac
    rc=$(rc_file)
    marker="# ViperCapture"
    mkdir -p "$(dirname "$rc")"
    touch "$rc"
    if grep -F -q "$marker" "$rc"; then
        echo "  Open a new terminal so vipercapture is on PATH."
        return 0
    fi
    case "$bin_dir" in
        *$'\n'*|*$'\r'*)
            echo "  $bin_dir contains a newline, so it was not added to $rc."
            return 0
            ;;
    esac
    escaped=$(printf '%s' "$bin_dir" | sed -e 's/[\\"$`]/\\&/g')
    printf '\n%s\nexport PATH="%s:$PATH"\n' "$marker" "$escaped" >> "$rc"
    echo "  Added $bin_dir to PATH in $rc."
    echo "  Open a new terminal so vipercapture is on PATH."
}

echo
echo "  ViperCapture installer"
echo "  ----------------------"
echo

tmpdir=$(mktemp -d)
ensure_python
if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
    echo "Could not find a usable Python 3.11+ interpreter." >&2
    exit 1
fi

if detect_source; then
    echo "  Installing from $source_dir"
else
    download_source
fi
stage_tree "$source_dir"

mkdir -p "$bin_dir"
shim="$bin_dir/vipercapture"
cat > "$shim" <<EOF
#!/bin/sh
exec $(quote "$python_bin") $(quote "$app/launch.py") "\$@"
EOF
chmod 755 "$shim"
ensure_on_path

echo
echo "  Installed the vipercapture command."
echo "  Run: vipercapture"
echo "  The first launch installs dependencies and browsers, then starts the API."
echo
