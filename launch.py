#!/usr/bin/env python3
"""
ViperCapture launcher
---------------------
Run this file directly with Python.
Handles venv setup, dependency install, browser install,
server startup, and opening your browser automatically.

Prefers uv (https://docs.astral.sh/uv/) when it is on PATH.
Set VIPERCAPTURE_USE_UV=0 to force the stdlib venv + pip path.
Without uv, the launcher falls back to pip.

On subsequent runs, dependency checks are skipped unless
    requirements.txt has changed (hash-stamped in .venv/).
"""

from __future__ import annotations
import hashlib
from importlib.metadata import version
import os
import shutil
import sys
import subprocess
import socket
import time
import webbrowser
from pathlib import Path

ROOT             = Path(__file__).parent.resolve()
VENV_PYTHON      = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
HOST             = "127.0.0.1"
PORT             = 8000
URL              = f"http://{HOST}:{PORT}/"
DEPS_STAMP       = ROOT / ".venv" / ".deps_stamp"
PLAYWRIGHT_STAMP = ROOT / ".venv" / ".playwright_stamp"


# ── Helpers ───────────────────────────────────────────────────

def port_open() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


def run(*cmd: str | Path, label: str = "") -> None:
    """Run a subprocess and exit hard if it fails."""
    result = subprocess.run([str(c) for c in cmd])
    if result.returncode != 0:
        tag = f" ({label})" if label else ""
        print(f"\n  ERROR{tag}: command exited with code {result.returncode}")
        print(f"  Command: {' '.join(str(c) for c in cmd)}")
        wait_and_exit(1)


def wait_and_exit(code: int = 0) -> None:
    if sys.stdin.isatty():
        input("\n  Press Enter to close...")
    sys.exit(code)


def _req_hash() -> str:
    """MD5 of requirements.txt — used to detect changes between runs."""
    req = ROOT / "requirements.txt"
    return hashlib.md5(req.read_bytes()).hexdigest() if req.exists() else ""


def find_uv() -> str | None:
    """Return the uv executable when it should be used, otherwise None."""
    flag = os.environ.get("VIPERCAPTURE_USE_UV", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None
    return shutil.which("uv")


def venv_command(python: str, venv_dir: Path, uv: str | None) -> list[str]:
    if uv:
        return [uv, "venv", "--python", python, str(venv_dir)]
    return [python, "-m", "venv", str(venv_dir)]


def deps_commands(
    python: str, requirements: Path, uv: str | None
) -> list[tuple[list[str], str]]:
    if uv:
        return [(
            [uv, "pip", "install", "--python", python, "-r", str(requirements)],
            "uv pip install",
        )]
    return [
        ([python, "-m", "pip", "install", "--upgrade", "pip", "-q"], "pip upgrade"),
        ([python, "-m", "pip", "install", "-r", str(requirements)], "pip install"),
    ]


def _venv_has_pip(python: str) -> bool:
    result = subprocess.run(
        [python, "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


# ── Setup steps ───────────────────────────────────────────────

def ensure_venv() -> None:
    """
    If the venv doesn't exist yet, create it.
    If we're not running from the venv Python, re-launch with it so
    all subsequent imports and subprocess calls use the right Python.
    """
    if not VENV_PYTHON.exists():
        uv = find_uv()
        installer = "uv" if uv else "venv"
        print(f"  [1/3] Creating Python environment with {installer} (first run only)...")
        command = venv_command(sys.executable, ROOT / ".venv", uv)
        run(*command, label=f"{installer} venv")

    this = Path(sys.executable).resolve()
    want = VENV_PYTHON.resolve()
    if this != want:
        # Hand off to the venv Python — this process becomes just a waiter.
        result = subprocess.run([str(VENV_PYTHON), __file__] + sys.argv[1:])
        sys.exit(result.returncode)


def ensure_deps() -> None:
    """
    Install packages from requirements.txt.
    Skipped on subsequent runs unless requirements.txt has changed.
    Prefers uv when available; falls back to pip.
    """
    current_hash = _req_hash()
    if DEPS_STAMP.exists() and DEPS_STAMP.read_text().strip() == current_hash:
        print("  [2/3] Python packages already up to date — skipping.")
        return

    uv = find_uv()
    if uv:
        print("  [2/3] Installing Python packages with uv...")
    else:
        if not _venv_has_pip(sys.executable):
            print("\n  ERROR: pip is not available in .venv and uv was not found.")
            print("  Install uv (https://docs.astral.sh/uv/) or delete .venv and rerun.")
            wait_and_exit(1)
        print("  [2/3] Installing Python packages...")

    for command, label in deps_commands(sys.executable, ROOT / "requirements.txt", uv):
        run(*command, label=label)
    DEPS_STAMP.write_text(current_hash)


def ensure_playwright() -> None:
    """
    Install Playwright's Chromium, Firefox, and WebKit browsers.
    Skipped when the installed browser matches the Playwright package version.
    """
    playwright_stamp = f"{version('playwright')}:chromium,firefox,webkit"
    if (
        PLAYWRIGHT_STAMP.exists()
        and PLAYWRIGHT_STAMP.read_text().strip() == playwright_stamp
    ):
        print("  [3/3] Playwright browsers already installed — skipping.")
        return

    print("  [3/3] Installing Playwright browsers...")
    command = [sys.executable, "-m", "playwright", "install", "--no-shell"]
    if sys.platform.startswith("linux"):
        command.append("--with-deps")
    run(*command, "chromium", "firefox", "webkit", label="playwright install")
    PLAYWRIGHT_STAMP.write_text(playwright_stamp)


# ── Main ──────────────────────────────────────────────────────

def main() -> None:
    print()
    print("  ViperCapture")
    print("  ------------")
    print()

    ensure_venv()    # may re-exec this script under the venv Python
    ensure_deps()
    ensure_playwright()

    # Server already running from a previous session?
    if port_open():
        print(f"\n  Server already running. Opening {URL}")
        webbrowser.open(URL)
        return

    # ── Start the server ────────────────────────────────────────
    print(f"\n  Starting server at {URL}")
    print("  Press Ctrl+C here to stop the server.\n")

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "vipercapture.main:app",
         "--host", HOST, "--port", str(PORT)],
        cwd=str(ROOT),
    )

    # ── Wait for port (1 check/sec, 30s max) ────────────────────
    print("  Waiting for server to be ready...", end="", flush=True)
    ready = False
    for _ in range(30):
        if port_open():
            ready = True
            break
        if server.poll() is not None:
            # Server process already exited — don't wait the full 30s
            break
        time.sleep(1)
        print(".", end="", flush=True)
    print()

    if not ready:
        print("\n  ERROR: Server didn't start.")
        print("  Check the output above for details.")
        server.terminate()
        wait_and_exit(1)

    # ── Open browser ────────────────────────────────────────────
    webbrowser.open(URL)
    print(f"\n  Ready! Opened {URL} in your browser.")
    print("  Ctrl+C to stop the server.\n")

    # Keep this window alive — show server logs until Ctrl+C
    try:
        server.wait()
    except KeyboardInterrupt:
        print("\n  Stopping server...")
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        print("  Server stopped.")


if __name__ == "__main__":
    main()
