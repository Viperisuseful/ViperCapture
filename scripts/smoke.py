"""Run a small end-to-end check against ViperCapture."""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch(url: str, *, body: dict[str, object] | None = None) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except HTTPError as exc:
        raise RuntimeError(
            f"{request.method} {url} returned {exc.code}: {exc.read().decode(errors='replace')}"
        ) from exc


def wait_until_ready(base_url: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            document = json.loads(fetch(f"{base_url}/ready"))
            if document.get("ready") is True:
                return
        except (OSError, URLError, RuntimeError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    raise RuntimeError("ViperCapture did not become ready within 90 seconds")


def check(base_url: str) -> None:
    if json.loads(fetch(f"{base_url}/health")) != {"ready": True}:
        raise RuntimeError("health response is invalid")
    schema = json.loads(fetch(f"{base_url}/openapi.json"))
    if "/v1/render" not in schema.get("paths", {}):
        raise RuntimeError("OpenAPI does not expose POST /v1/render")

    for engine in ("chromium", "firefox", "webkit"):
        image = fetch(
            f"{base_url}/v1/render",
            body={
                "html": "<!doctype html><title>ViperCapture</title><h1>ready</h1>",
                "engine": engine,
                "output": "png",
                "full_page": False,
                "viewport": {"width": 320, "height": 240},
            },
        )
        if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) < 24:
            raise RuntimeError(f"{engine} did not return a PNG")
        if struct.unpack(">II", image[16:24]) != (320, 240):
            raise RuntimeError(f"{engine} returned unexpected dimensions")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    args = parser.parse_args()
    base_url = (args.base_url or "http://127.0.0.1:8000").rstrip("/")
    process: subprocess.Popen[bytes] | None = None
    data_directory: tempfile.TemporaryDirectory[str] | None = None
    if args.base_url is None:
        data_directory = tempfile.TemporaryDirectory(prefix="vipercapture-smoke-")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "vipercapture.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            env={
                **os.environ,
                "VIPERCAPTURE_ASYNC_JOBS": "0",
                "VIPERCAPTURE_DATA_DIR": data_directory.name,
                "VIPERCAPTURE_SCHEDULES": "0",
            },
        )
    try:
        wait_until_ready(base_url)
        check(base_url)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if data_directory is not None:
            data_directory.cleanup()
    print("ViperCapture smoke check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
