"""Build the platform-local Python renderer and Playwright runtime for Tauri."""

from __future__ import annotations

import hashlib
import gzip
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


DESKTOP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = DESKTOP_DIR.parent
TAURI_DIR = DESKTOP_DIR / "src-tauri"
BINARIES_DIR = TAURI_DIR / "binaries"
PLAYWRIGHT_DIR = TAURI_DIR / "resources" / "playwright"
FFMPEG_DIR = TAURI_DIR / "resources" / "ffmpeg"
BUILD_DIR = DESKTOP_DIR / ".sidecar-build"
FFMPEG_RELEASE = "b6.1.1"
FFMPEG_RELEASE_URL = (
    f"https://github.com/eugeneware/ffmpeg-static/releases/download/{FFMPEG_RELEASE}"
)
FFMPEG_MIRROR_URL = (
    f"https://cdn.npmmirror.com/binaries/ffmpeg-static/{FFMPEG_RELEASE}"
)
FFMPEG_ASSET_HASHES = {
    "win32-x64": (
        "8883a3dffbd0a16cf4ef95206ea05283f78908dbfb118f73c83f4951dcc06d77",
        "d751d40ef8ba97bf46964ae203bf88e7c0027b5459946ac758403c1bb032523f",
        "9910569b1b42c01b91dd03b85abc8472a15b5aa31188e41e9654ae08cc179d07",
    ),
    "darwin-x64": (
        "929b375c1182d956c51f7ac25e0b2b0411fb01f6f407aa15c9758efeb4242106",
        "07bfe0aa59feabb64665dde54683d4bcfa6901d1b9eb70219ee3d6ce5c5b51a2",
        "63f983e795802452bdb02bd77c27f3dded90a63eec20d1f3e47220823aa8d1b2",
    ),
    "darwin-arm64": (
        "8923876afa8db5585022d7860ec7e589af192f441c56793971276d450ed3bbfa",
        "5995e6d7b7fdb371505351fce50b9d6fd4d051f69d3468ed4dd4cbfc14a5b916",
        "527d4ea64a17e81a72ab57103b3817ee224261b5c07844d5a99af40758315154",
    ),
    "linux-x64": (
        "bfe8a8fc511530457b528c48d77b5737527b504a3797a9bc4866aeca69c2dffa",
        "e6f01cb10f21032b80e78a1b0bd13d6c387d6f18eed9a37f99ea35d7e3f7bb7a",
        "ef9ccbfae3fccc2be1c0face637afb885edc2dfd13901e07ad8541ecc21bd76c",
    ),
}


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=REPO_DIR, env=env, check=True)


def target_triple() -> str:
    return subprocess.check_output(
        ["rustc", "--print", "host-tuple"],
        cwd=TAURI_DIR,
        text=True,
    ).strip()


def ffmpeg_asset_key() -> str:
    system = {"windows": "win32", "darwin": "darwin", "linux": "linux"}.get(
        platform.system().lower()
    )
    machine = platform.machine().lower()
    architecture = (
        "arm64" if machine in {"arm64", "aarch64"}
        else "x64" if machine in {"amd64", "x86_64"}
        else None
    )
    key = f"{system}-{architecture}"
    if key not in FFMPEG_ASSET_HASHES:
        raise RuntimeError(f"No bundled FFmpeg is configured for {key}")
    return key


def download_asset(name: str, digest: str, destination: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
    decompressed: Path | None = None
    try:
        for base_url in (FFMPEG_RELEASE_URL, FFMPEG_MIRROR_URL):
            result = subprocess.run(
                [
                    "curl", "--fail", "--location", "--retry", "3",
                    "--retry-delay", "2", "--silent", "--show-error",
                    "--output", str(temporary), f"{base_url}/{name}.gz",
                ],
                check=False,
            )
            if result.returncode == 0:
                break
        else:
            raise RuntimeError(f"Could not download bundled FFmpeg asset {name}")
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Bundled FFmpeg checksum failed for {name}")
        with (
            gzip.open(temporary, "rb") as source,
            tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output,
        ):
            decompressed = Path(output.name)
            shutil.copyfileobj(source, output)
        os.replace(decompressed, destination)
    finally:
        temporary.unlink(missing_ok=True)
        if decompressed is not None:
            decompressed.unlink(missing_ok=True)


def bundle_ffmpeg() -> Path:
    key = ffmpeg_asset_key()
    binary_hash, license_hash, readme_hash = FFMPEG_ASSET_HASHES[key]
    extension = ".exe" if key.startswith("win32-") else ""
    binary = FFMPEG_DIR / f"ffmpeg{extension}"
    for name, digest, destination in (
        (f"ffmpeg-{key}", binary_hash, binary),
        (f"{key}.LICENSE", license_hash, FFMPEG_DIR / "LICENSE"),
        (f"{key}.README", readme_hash, FFMPEG_DIR / "README"),
    ):
        download_asset(name, digest, destination)
    (FFMPEG_DIR / "LICENSE").chmod(0o644)
    (FFMPEG_DIR / "README").chmod(0o644)
    if not key.startswith("win32-"):
        binary.chmod(0o755)
    encoders = subprocess.check_output(
        [str(binary), "-hide_banner", "-encoders"], stderr=subprocess.STDOUT
    )
    if not all(encoder in encoders for encoder in (b"libx264", b"libvpx", b"libvpx-vp9")):
        raise RuntimeError("Bundled FFmpeg is missing required video encoders")
    return binary


def main() -> None:
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    PLAYWRIGHT_DIR.mkdir(parents=True, exist_ok=True)
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    ffmpeg = bundle_ffmpeg()

    browser_env = os.environ.copy()
    browser_env["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_DIR)
    run(
        [
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
            "firefox",
            "webkit",
        ],
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
            "--collect-data",
            "playwright_stealth",
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
    print(f"FFmpeg: {ffmpeg}")


if __name__ == "__main__":
    main()
