#!/usr/bin/env python3
"""Render a public URL as a full-page ViperCapture artifact."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_URL = "https://capture.viperisuseful.cc/v1/render"
FORMATS = {"png", "jpeg", "webp", "pdf"}


class CaptureError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, retryable: bool, request_id: str | None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        self.retry_after = retry_after


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value


def api_key() -> str | None:
    candidates = [
        Path.home() / ".env",
        Path.cwd() / ".env",
        Path.home() / ".codex" / ".env",
    ]
    for candidate in candidates:
        load_dotenv(candidate)
    return os.environ.get("VIPERCAPTURE_API_KEY")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture a public URL as a ViperCapture full-page artifact.")
    parser.add_argument("--url", required=True, help="Public http(s) URL to capture")
    parser.add_argument("--output", choices=sorted(FORMATS), default="png")
    parser.add_argument("--output-path", help="Exact output file path")
    parser.add_argument("--output-dir", default=".", help="Directory for an auto-named artifact")
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=900)
    parser.add_argument("--device-scale-factor", type=float, default=1)
    parser.add_argument("--selector", help="Capture only this rendered element")
    parser.add_argument("--wait-event", choices=["load", "domcontentloaded", "networkidle"])
    parser.add_argument("--wait-selector")
    parser.add_argument("--wait-text")
    parser.add_argument("--delay-ms", type=int)
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--no-cleanup", action="store_true", help="Disable consent/ad/tracker cleanup")
    parser.add_argument("--proceed-on-captcha", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="Print the request body without sending it")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    parsed = urllib.parse.urlparse(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--url must be a complete public http(s) URL")
    if args.max_attempts < 1:
        raise ValueError("--max-attempts must be at least 1")
    if args.viewport_width < 1 or args.viewport_height < 1:
        raise ValueError("viewport dimensions must be positive")
    if args.device_scale_factor <= 0:
        raise ValueError("device scale factor must be positive")
    if args.timeout_ms < 1 or (args.delay_ms is not None and args.delay_ms < 0):
        raise ValueError("wait timeout and delay values must be non-negative")


def request_body(args: argparse.Namespace) -> dict:
    body = {
        "url": args.url,
        "output": args.output,
        "viewport": {
            "width": args.viewport_width,
            "height": args.viewport_height,
            "device_scale_factor": args.device_scale_factor,
        },
        "full_page": True,
        "selector": args.selector,
        "proceed_on_captcha": args.proceed_on_captcha,
    }
    wait_for = {"event": args.wait_event or "load", "timeout_ms": args.timeout_ms}
    if args.wait_selector:
        wait_for["selector"] = args.wait_selector
    if args.wait_text:
        wait_for["text"] = args.wait_text
    if args.delay_ms is not None:
        wait_for["delay_ms"] = args.delay_ms
    body["wait_for"] = wait_for
    if not args.no_cleanup:
        body["cleanup"] = {
            "consent_mode": "reject",
            "block_ads": True,
            "block_trackers": True,
            "block_chats": True,
            "block_newsletters": True,
        }
    return body


def parse_error(status: int, headers, raw: bytes) -> CaptureError:
    request_id = headers.get("X-Request-ID")
    retry_after = None
    retry_header = headers.get("Retry-After")
    if retry_header:
        try:
            retry_after = max(0.0, float(retry_header))
        except ValueError:
            pass
    code = f"http_{status}"
    message = f"ViperCapture returned HTTP {status}"
    retryable = status == 429 or status >= 500
    try:
        payload = json.loads(raw.decode("utf-8"))
        detail = payload.get("error", payload)
        if isinstance(detail, dict):
            code = str(detail.get("code", code))
            message = str(detail.get("message", message))
            retryable = bool(detail.get("retryable", retryable))
            request_id = detail.get("request_id") or request_id
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return CaptureError(status, code, message, retryable, request_id, retry_after)


def send_request(key: str, body: dict) -> tuple[bytes, str | None]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
            "User-Agent": "codex-viper-full-page-capture/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read(), response.headers.get("X-Request-ID")
    except urllib.error.HTTPError as error:
        raise parse_error(error.code, error.headers, error.read()) from None
    except urllib.error.URLError as error:
        raise CaptureError(0, "network_error", str(error.reason), True, None) from None


def output_path(args: argparse.Namespace) -> Path:
    if args.output_path:
        return Path(args.output_path).expanduser()
    host = urllib.parse.urlparse(args.url).netloc.lower().split(":", 1)[0]
    host = re.sub(r"[^a-z0-9.-]+", "-", host).strip(".-") or "capture"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(args.output_dir).expanduser() / f"{host}-full-page-{stamp}.{args.output}"


def main() -> int:
    args = build_parser().parse_args()
    try:
        validate_args(args)
        body = request_body(args)
        if args.dry_run:
            print(json.dumps(body, indent=2, ensure_ascii=False))
            return 0
        key = api_key()
        if not key:
            raise ValueError("VIPERCAPTURE_API_KEY is not set")
        target = output_path(args)
        last_error: CaptureError | None = None
        for attempt in range(1, args.max_attempts + 1):
            try:
                content, request_id = send_request(key, body)
                if not content:
                    raise CaptureError(0, "empty_response", "ViperCapture returned an empty artifact", True, request_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                print(json.dumps({"path": str(target.resolve()), "format": args.output, "request_id": request_id}, ensure_ascii=False))
                return 0
            except CaptureError as error:
                last_error = error
                if not error.retryable or attempt >= args.max_attempts:
                    break
                delay = error.retry_after or min(30, 0.5 * (2 ** (attempt - 1)) + random.random() * 0.25)
                time.sleep(delay)
        assert last_error is not None
        suffix = f" (request_id={last_error.request_id})" if last_error.request_id else ""
        raise ValueError(f"{last_error.code}: {last_error.message}{suffix}")
    except (ValueError, OSError) as error:
        print(f"ViperCapture error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
