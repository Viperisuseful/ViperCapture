#!/usr/bin/env python3
"""
SHOT — Screenshot Tool launcher
--------------------------------
Run this directly with Python or via run.bat.
Handles venv setup, dependency install, browser install,
server startup, and opening your browser automatically.
"""

from __future__ import annotations
import os
import sys
import subprocess
import socket
import time
import webbrowser
from pathlib import Path

ROOT        = Path(__file__).parent.resolve()
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
HOST        = "127.0.0.1"
PORT        = 8000
URL         = f"http://{HOST}:{PORT}/"


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
    input("\n  Press Enter to close...")
    sys.exit(code)


# ── Setup steps ───────────────────────────────────────────────

def ensure_venv() -> None:
    """
    If the venv doesn't exist yet, create it.
    If we're not running from the venv Python, re-launch with it so
    all subsequent imports and subprocess calls use the right Python.
    """
    if not VENV_PYTHON.exists():
        print("  [1/3] Creating Python environment (first run only)...")
        run(sys.executable, "-m", "venv", ROOT / ".venv",
            label="venv creation")

    this = Path(sys.executable).resolve()
    want = VENV_PYTHON.resolve()
    if this != want:
        # Hand off to the venv Python — this process becomes just a waiter.
        result = subprocess.run([str(VENV_PYTHON), __file__] + sys.argv[1:])
        sys.exit(result.returncode)


def ensure_deps() -> None:
    print("  [2/3] Checking Python packages...")
    run(sys.executable, "-m", "pip", "install", "--upgrade", "pip", "-q",
        label="pip upgrade")
    run(sys.executable, "-m", "pip", "install", "-r", ROOT / "requirements.txt",
        label="pip install")


def ensure_playwright() -> None:
    print("  [3/3] Checking Playwright browser (Chromium)...")
    run(sys.executable, "-m", "playwright", "install", "chromium",
        label="playwright install")


# ── Main ──────────────────────────────────────────────────────

def main() -> None:
    print()
    print("  SHOT - Screenshot Tool")
    print("  ----------------------")
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
        [sys.executable, "-m", "uvicorn", "main:app",
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
