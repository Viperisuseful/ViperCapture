"""Exercise the packaged renderer without starting the Tauri window."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DESKTOP_DIR = Path(__file__).resolve().parents[1]
TAURI_DIR = DESKTOP_DIR / "src-tauri"


def target_triple() -> str:
    return subprocess.check_output(
        ["rustc", "--print", "host-tuple"],
        cwd=TAURI_DIR,
        text=True,
    ).strip()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def request(
    base_url: str,
    token: str | None,
    path: str,
    payload: dict[str, object] | None = None,
    timeout: float = 45,
) -> bytes:
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    with urlopen(
        Request(f"{base_url}{path}", data=body, headers=headers), timeout=timeout
    ) as response:
        return response.read()


def main() -> None:
    extension = ".exe" if sys.platform.startswith("win") else ""
    binary = (
        TAURI_DIR
        / "binaries"
        / f"vipercapture-sidecar-{target_triple()}{extension}"
    )
    if not binary.exists():
        raise SystemExit(f"Sidecar is missing: {binary}")

    port = free_port()
    token = "vipercapture-desktop-smoke"
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    ffmpeg = TAURI_DIR / "resources" / "ffmpeg" / (
        "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    )
    if not ffmpeg.is_file():
        raise SystemExit(f"Bundled FFmpeg is missing: {ffmpeg}")
    env.update(
        {
            "VIPERCAPTURE_PORT": str(port),
            "VIPERCAPTURE_DESKTOP_TOKEN": token,
            "VIPERCAPTURE_PARENT_PID": str(os.getpid()),
            "VIPERCAPTURE_CAPTURES_DIR": str(DESKTOP_DIR / ".sidecar-build" / "captures"),
            "PLAYWRIGHT_BROWSERS_PATH": str(TAURI_DIR / "resources" / "playwright"),
            "VIPERCAPTURE_FFMPEG": str(ffmpeg),
        }
    )

    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [str(binary)],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(120):
                if process.poll() is not None:
                    output.seek(0)
                    raise RuntimeError(
                        "Sidecar exited before becoming ready:\n"
                        + output.read().decode(errors="replace")
                    )
                try:
                    if json.loads(request(base_url, token, "/health", timeout=1))["ready"]:
                        break
                except (HTTPError, URLError, TimeoutError, ConnectionError):
                    time.sleep(0.25)
            else:
                output.seek(0)
                raise RuntimeError(
                    "Sidecar did not become ready:\n"
                    + output.read().decode(errors="replace")
                )

            try:
                request(base_url, None, "/app-config")
            except HTTPError as error:
                if error.code != 401:
                    raise
            else:
                raise RuntimeError("Unauthenticated request unexpectedly succeeded")

            app_config = json.loads(request(base_url, token, "/app-config"))

            capture_bytes = {}
            for engine in ("chromium", "firefox", "webkit"):
                print(f"Capturing with {engine}...", flush=True)
                image = request(
                    base_url,
                    token,
                    "/v1/render",
                    {
                        "url": "https://example.com",
                        "engine": engine,
                        "output": "png",
                        "viewport": {
                            "width": 640,
                            "height": 480,
                            "device_scale_factor": 1,
                        },
                        "full_page": False,
                        "lazy_load": "none",
                        "wait_for": {
                            "event": "load",
                            "delay_ms": 0,
                            "timeout_ms": 15_000,
                        },
                    },
                )
                if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError(f"{engine} response was not a PNG")
                capture_bytes[engine] = len(image)
            if "mp4" in app_config.get("output_formats", []):
                video = request(
                    base_url,
                    token,
                    "/v1/render",
                    {
                        "url": "https://example.com",
                        "engine": "chromium",
                        "output": "mp4",
                        "full_page": False,
                        "viewport": {"width": 320, "height": 240},
                        "video": {"duration_ms": 1000},
                        "lazy_load": "none",
                    },
                )
                if b"ftyp" not in video[:32]:
                    raise RuntimeError("MP4 response did not contain an ftyp box")
                capture_bytes["mp4"] = len(video)
            print(
                json.dumps(
                    {
                        "ready": True,
                        "unauthenticated_blocked": True,
                        "capture_bytes": capture_bytes,
                        "port": port,
                    }
                )
            )
        finally:
            try:
                request(base_url, token, "/shutdown", {})
            except (HTTPError, URLError, ConnectionError):
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    main()
