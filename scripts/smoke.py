"""Run a small end-to-end check against ViperCapture."""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
    return parsed.scheme, parsed.hostname, port


class SameOriginRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        if origin(request.full_url) != origin(new_url):
            raise HTTPError(
                new_url,
                code,
                "cross-origin redirect refused",
                headers,
                file,
            )
        return super().redirect_request(
            request,
            file,
            code,
            message,
            headers,
            new_url,
        )


def fetch(
    url: str,
    *,
    body: dict[str, object] | None = None,
    token: str | None = None,
) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data else "GET",
    )
    try:
        with build_opener(SameOriginRedirects).open(request, timeout=30) as response:
            return response.read()
    except HTTPError as exc:
        raise RuntimeError(
            f"{request.method} {url} returned {exc.code}: {exc.read().decode(errors='replace')}"
        ) from exc


def wait_until_ready(
    base_url: str,
    *,
    process: subprocess.Popen[bytes] | None = None,
    token: str | None = None,
) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"ViperCapture exited with status {process.returncode}")
        try:
            document = json.loads(fetch(f"{base_url}/ready", token=token))
            if document.get("ready") is True:
                return
        except (OSError, URLError, RuntimeError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    raise RuntimeError("ViperCapture did not become ready within 90 seconds")


def check(base_url: str, *, token: str | None = None) -> None:
    if json.loads(fetch(f"{base_url}/health", token=token)) != {"ready": True}:
        raise RuntimeError("health response is invalid")
    schema = json.loads(fetch(f"{base_url}/openapi.json", token=token))
    if "/v1/render" not in schema.get("paths", {}):
        raise RuntimeError("OpenAPI does not expose POST /v1/render")

    for engine in ("chromium", "firefox", "webkit"):
        image = fetch(
            f"{base_url}/v1/render",
            token=token,
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

    metadata = json.loads(
        fetch(
            f"{base_url}/v1/render",
            token=token,
            body={
                "html": "<h1 id='title'>Ready <em>now</em></h1><a href='/docs'>Docs</a>",
                "output": "metadata",
                "elements": [{"selector": "h1"}, {"selector": "a[href]"}],
            },
        )
    )
    extracted = metadata.get("elements")
    if not isinstance(extracted, list) or len(extracted) != 2:
        raise RuntimeError("metadata element extraction is missing")
    if extracted[0]["results"][0]["text"] != "Ready now":
        raise RuntimeError("metadata element text is invalid")
    if extracted[1]["results"][0]["attributes"] != [
        {"name": "href", "value": "/docs"}
    ]:
        raise RuntimeError("metadata element attributes are invalid")

    bundle = fetch(
        f"{base_url}/v1/render",
        token=token,
        body={
            "html": "<h1>Ready <em>now</em></h1><a href='/docs'>Docs</a>",
            "output": "png",
            "full_page": False,
            "viewport": {"width": 320, "height": 240},
            "side_outputs": ["html", "markdown", "metadata", "mhtml"],
            "elements": [{"selector": "h1"}],
            "image": {"thumbnails": [{"name": "small", "width": 160}]},
        },
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        expected = {
            "vipercapture.png",
            "vipercapture.html",
            "vipercapture.md",
            "vipercapture-metadata.json",
            "page.mhtml",
            "thumbnails/small.png",
            "manifest.json",
        }
        if set(archive.namelist()) != expected:
            raise RuntimeError("multi-artifact bundle entries are invalid")
        manifest = json.loads(archive.read("manifest.json"))
        if len(manifest.get("outputs", [])) != 6:
            raise RuntimeError("multi-artifact manifest is invalid")
        side_metadata = json.loads(archive.read("vipercapture-metadata.json"))
        if side_metadata["elements"][0]["results"][0]["text"] != "Ready now":
            raise RuntimeError("multi-artifact metadata is invalid")
        if b"Ready <em>now</em>" not in archive.read("vipercapture.html"):
            raise RuntimeError("multi-artifact HTML is invalid")
        if b"# Ready *now*" not in archive.read("vipercapture.md"):
            raise RuntimeError("multi-artifact Markdown is invalid")
        if b"MIME-Version: 1.0" not in archive.read("page.mhtml"):
            raise RuntimeError("multi-artifact MHTML is invalid")
        thumbnail = archive.read("thumbnails/small.png")
        if struct.unpack(">II", thumbnail[16:24]) != (160, 120):
            raise RuntimeError("multi-artifact thumbnail dimensions are invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    parser.add_argument("--token", default=os.getenv("VIPERCAPTURE_SMOKE_TOKEN"))
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/") if args.base_url else ""
    process: subprocess.Popen[bytes] | None = None
    data_directory: tempfile.TemporaryDirectory[str] | None = None
    if args.base_url is None:
        with socket.socket() as available_port:
            available_port.bind(("127.0.0.1", 0))
            port = available_port.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
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
                str(port),
            ],
            env={
                **os.environ,
                "VIPERCAPTURE_ASYNC_JOBS": "0",
                "VIPERCAPTURE_DATA_DIR": data_directory.name,
                "VIPERCAPTURE_SCHEDULES": "0",
            },
        )
    try:
        wait_until_ready(base_url, process=process, token=args.token)
        check(base_url, token=args.token)
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
