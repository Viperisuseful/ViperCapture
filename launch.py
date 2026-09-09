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
import platform
import re
import shutil
import sys
import subprocess
import socket
import time
import webbrowser
from pathlib import Path
from typing import Callable

ROOT             = Path(__file__).parent.resolve()
VENV_PYTHON      = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
HOST             = "127.0.0.1"
PORT             = 8000
URL              = f"http://{HOST}:{PORT}/"
DEPS_STAMP       = ROOT / ".venv" / ".deps_stamp"
PLAYWRIGHT_STAMP = ROOT / ".venv" / ".playwright_stamp"
# PyCA 49.0.0 dropped Intel macOS wheels. Source builds need Rust 1.83+.
# https://cryptography.io/en/latest/installation/#building-cryptography-on-macos
MIN_RUSTC        = (1, 83, 0)
INTEL_MACOS_CRYPTOGRAPHY_DOCS = (
    "https://cryptography.io/en/latest/installation/#building-cryptography-on-macos"
)
INTEL_MACOS_CRYPTOGRAPHY_CHANGELOG = (
    "https://cryptography.io/en/latest/changelog/#v49-0-0"
)


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
    python: str,
    requirements: Path,
    uv: str | None,
    *,
    intel_macos: bool = False,
) -> list[tuple[list[str], str]]:
    source_build = ["--no-binary", "cryptography"] if intel_macos else []
    if uv:
        return [(
            [
                uv,
                "pip",
                "install",
                "--python",
                python,
                "-r",
                str(requirements),
                *source_build,
            ],
            "uv pip install",
        )]
    return [
        ([python, "-m", "pip", "install", "--upgrade", "pip", "-q"], "pip upgrade"),
        (
            [python, "-m", "pip", "install", "-r", str(requirements), *source_build],
            "pip install",
        ),
    ]


def is_intel_macos(
    sys_platform: str = sys.platform,
    machine: str | None = None,
) -> bool:
    return sys_platform == "darwin" and (machine or platform.machine()) == "x86_64"


def parse_rustc_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"rustc\s+(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _command_stdout(
    cmd: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> str | None:
    try:
        result = run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _openssl_prefix_if_present(prefix: str) -> str | None:
    if prefix and (Path(prefix) / "include" / "openssl" / "ssl.h").is_file():
        return prefix
    return None


def probe_intel_macos_cryptography_toolchain(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    environ: dict[str, str] | None = None,
) -> dict[str, object]:
    """Discover compiler, rustc, and a non-Apple OpenSSL prefix for a source build."""
    env = os.environ if environ is None else environ
    compiler = which("cc") or which("clang")
    if compiler is None and _command_stdout(["xcode-select", "-p"], run):
        compiler = "xcode-select"
    rustc = which("rustc")
    rust_version = None
    if rustc:
        rust_version = parse_rustc_version(
            _command_stdout([rustc, "--version"], run) or ""
        )
    openssl_dir = _openssl_prefix_if_present(env.get("OPENSSL_DIR", "").strip())
    brew = which("brew")
    if openssl_dir is None and brew:
        for formula in ("openssl@3", "openssl"):
            prefix = _command_stdout([brew, "--prefix", formula], run)
            openssl_dir = _openssl_prefix_if_present(prefix or "")
            if openssl_dir:
                break
    if openssl_dir is None:
        openssl_dir = _openssl_prefix_if_present("/opt/local")
    if openssl_dir is None:
        pkg_config = which("pkg-config") or which("pkgconf")
        if pkg_config:
            prefix = _command_stdout(
                [pkg_config, "--variable=prefix", "libcrypto"], run
            )
            openssl_dir = _openssl_prefix_if_present(prefix or "")
    return {
        "compiler": compiler,
        "rustc": rustc,
        "rust_version": rust_version,
        "openssl_dir": openssl_dir,
    }


def intel_macos_cryptography_missing(toolchain: dict[str, object]) -> list[str]:
    missing: list[str] = []
    if not toolchain.get("compiler"):
        missing.append("Xcode command line tools (clang)")
    rust_version = toolchain.get("rust_version")
    if not isinstance(rust_version, tuple) or rust_version < MIN_RUSTC:
        missing.append(f"Rust {MIN_RUSTC[0]}.{MIN_RUSTC[1]}.{MIN_RUSTC[2]}+ (rustc)")
    if not toolchain.get("openssl_dir"):
        missing.append("Homebrew/MacPorts OpenSSL 3 (not Apple LibreSSL)")
    return missing


def apply_intel_macos_cryptography_env(
    environ: dict[str, str], openssl_dir: str
) -> dict[str, str]:
    """Point the cryptography sdist at a real OpenSSL prefix (PyCA OPENSSL_DIR)."""
    environ["OPENSSL_DIR"] = openssl_dir
    pkgconfig = Path(openssl_dir) / "lib" / "pkgconfig"
    if pkgconfig.is_dir():
        current = environ.get("PKG_CONFIG_PATH", "")
        prefix = str(pkgconfig)
        parts = [part for part in current.split(":") if part]
        if prefix not in parts:
            environ["PKG_CONFIG_PATH"] = ":".join([prefix, *parts])
    return environ


def format_intel_macos_cryptography_error(missing: list[str]) -> str:
    needed = ", ".join(missing)
    return (
        "\n  ERROR: Intel macOS has no cryptography >=49 wheel on PyPI "
        "(GHSA-jwv3-5hgf-82ww).\n"
        f"  pyca removed x86_64/universal2 wheels in 49.0.0: "
        f"{INTEL_MACOS_CRYPTOGRAPHY_CHANGELOG}\n"
        f"  Source-build tools missing: {needed}\n"
        "\n  Install the official PyCA macOS build dependencies, then rerun:\n"
        "    xcode-select --install\n"
        "    brew install openssl@3 rust\n"
        "    # or: sudo port install openssl rust\n"
        f"  {INTEL_MACOS_CRYPTOGRAPHY_DOCS}\n"
        "  Do not pin cryptography <=48; those releases are vulnerable.\n"
    )


def prepare_intel_macos_cryptography_build(
    *,
    sys_platform: str = sys.platform,
    machine: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """
    On Intel Macs, refuse to pip-install until a source build can succeed.
    Returns missing-tool messages (empty when ready). Exits from ensure_deps.
    """
    if not is_intel_macos(sys_platform, machine):
        return []
    env = os.environ if environ is None else environ
    toolchain = probe_intel_macos_cryptography_toolchain(
        which=which, run=run, environ=env
    )
    missing = intel_macos_cryptography_missing(toolchain)
    if missing:
        return missing
    openssl_dir = toolchain.get("openssl_dir")
    if isinstance(openssl_dir, str) and openssl_dir:
        apply_intel_macos_cryptography_env(env, openssl_dir)
    print(
        "  Intel macOS: building cryptography from the official sdist "
        "(no PyPI x86_64 wheel for 49+)."
    )
    return []


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

    intel_macos = is_intel_macos()
    missing = prepare_intel_macos_cryptography_build()
    if missing:
        print(format_intel_macos_cryptography_error(missing))
        wait_and_exit(1)

    for command, label in deps_commands(
        sys.executable, ROOT / "requirements.txt", uv, intel_macos=intel_macos
    ):
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
