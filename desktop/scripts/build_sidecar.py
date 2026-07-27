"""Build the platform-local Python renderer and Playwright runtime for Tauri."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


DESKTOP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = DESKTOP_DIR.parent
TAURI_DIR = DESKTOP_DIR / "src-tauri"
BINARIES_DIR = TAURI_DIR / "binaries"
PLAYWRIGHT_DIR = TAURI_DIR / "resources" / "playwright"
BUILD_DIR = DESKTOP_DIR / ".sidecar-build"


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=REPO_DIR, env=env, check=True)


def target_triple() -> str:
    return subprocess.check_output(
        ["rustc", "--print", "host-tuple"],
        cwd=TAURI_DIR,
        text=True,
    ).strip()


def main() -> None:
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    PLAYWRIGHT_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    browser_env = os.environ.copy()
    browser_env["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_DIR)
    run(
        [sys.executable, "-m", "playwright", "install", "--only-shell", "chromium"],
        env=browser_env,
    )

    raw_name = "vipercapture-sidecar"
    extension = ".exe" if sys.platform.startswith("win") else ""
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            raw_name,
            "--paths",
            str(REPO_DIR),
            "--distpath",
            str(BUILD_DIR / "dist"),
            "--workpath",
            str(BUILD_DIR / "work"),
            "--specpath",
            str(BUILD_DIR),
            str(DESKTOP_DIR / "sidecar" / "server.py"),
        ]
    )

    source = BUILD_DIR / "dist" / f"{raw_name}{extension}"
    destination = BINARIES_DIR / f"{raw_name}-{target_triple()}{extension}"
    if destination.exists():
        destination.unlink()
    shutil.copy2(source, destination)
    if not sys.platform.startswith("win"):
        destination.chmod(0o755)
    print(f"Sidecar: {destination}")
    print(f"Playwright: {PLAYWRIGHT_DIR}")


if __name__ == "__main__":
    main()
