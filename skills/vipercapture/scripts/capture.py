#!/usr/bin/env python3
"""Render a ViperCapture artifact without third-party dependencies."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_API_URL = "https://capture.viperisuseful.cc/v1/render"
FORMATS = ("png", "jpeg", "webp", "pdf", "html", "markdown")
EXTENSIONS = {"jpeg": "jpg"}
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class CaptureError(RuntimeError):
    """A normalized ViperCapture or network error."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        retryable: bool,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        self.retry_after = retry_after


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a URL, HTML file, or Markdown file with ViperCapture."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Public HTTP(S) URL to render")
    source.add_argument("--html-file", type=Path, help="UTF-8 HTML file to render")
    source.add_argument(
        "--markdown-file", type=Path, help="UTF-8 Markdown file to render"
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("VIPERCAPTURE_API_URL", DEFAULT_API_URL),
        help="Render endpoint (default: hosted ViperCapture API)",
    )
    parser.add_argument("--base-url", help="Public base URL for HTML or Markdown")
    parser.add_argument("--output", choices=FORMATS, default="png")
    parser.add_argument("--output-path", type=Path, help="Exact artifact path")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("."), help="Auto-named artifact folder"
    )
    parser.add_argument("--force", action="store_true", help="Replace output-path")
    parser.add_argument("--viewport-only", action="store_true")
    parser.add_argument("--selector", help="Capture one visible element")
    parser.add_argument("--viewport-width", type=int, default=1280)
    parser.add_argument("--viewport-height", type=int, default=720)
    parser.add_argument("--device-scale-factor", type=float, default=1)
    parser.add_argument("--quality", type=int)
    parser.add_argument("--transparent-background", action="store_true")
    parser.add_argument(
        "--wait-event",
        choices=("domcontentloaded", "load", "networkidle"),
        default="load",
    )
    parser.add_argument("--wait-selector")
    parser.add_argument("--wait-text")
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--timeout-ms", type=int, default=15_000)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Reject consent and block common page overlays",
    )
    parser.add_argument("--proceed-on-captcha", action="store_true")
    parser.add_argument(
        "--extract-mode", choices=("document", "article"), default="document"
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print request JSON without sending"
    )
    return parser


def validate_http_url(value: str, option: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{option} must be a complete HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{option} must not contain embedded credentials")


def validate_args(args: argparse.Namespace) -> None:
    validate_http_url(args.api_url, "--api-url")
    if args.url:
        validate_http_url(args.url, "--url")
    if args.base_url:
        validate_http_url(args.base_url, "--base-url")
    if args.max_attempts < 1 or args.max_attempts > 5:
        raise ValueError("--max-attempts must be between 1 and 5")
    if not 1 <= args.viewport_width <= 7680:
        raise ValueError("--viewport-width must be between 1 and 7680")
    if not 1 <= args.viewport_height <= 4320:
        raise ValueError("--viewport-height must be between 1 and 4320")
    if not 0.1 <= args.device_scale_factor <= 4:
        raise ValueError("--device-scale-factor must be between 0.1 and 4")
    if not 1 <= args.timeout_ms <= 30_000:
        raise ValueError("--timeout-ms must be between 1 and 30000")
    if not 0 <= args.delay_ms <= 15_000:
        raise ValueError("--delay-ms must be between 0 and 15000")
    if args.quality is not None:
        if args.output not in {"jpeg", "webp"}:
            raise ValueError("--quality is supported only for JPEG or WebP")
        if not 1 <= args.quality <= 100:
            raise ValueError("--quality must be between 1 and 100")
    if args.transparent_background and args.output not in {"png", "webp"}:
        raise ValueError(
            "--transparent-background is supported only for PNG or WebP"
        )
    if args.extract_mode != "document" and args.output not in {"html", "markdown"}:
        raise ValueError("--extract-mode applies only to HTML or Markdown output")
    for option in ("html_file", "markdown_file"):
        path = getattr(args, option)
        if path is not None and not path.is_file():
            raise ValueError(f"--{option.replace('_', '-')} must name a file")


def read_source(path: Path, option: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{option} must be UTF-8") from error


def request_body(args: argparse.Namespace) -> dict:
    body: dict = {
        "output": args.output,
        "viewport": {
            "width": args.viewport_width,
            "height": args.viewport_height,
            "device_scale_factor": args.device_scale_factor,
        },
        "full_page": not args.viewport_only and args.selector is None,
        "image": {
            "quality": args.quality,
            "transparent_background": args.transparent_background,
        },
        "wait_for": {
            "event": args.wait_event,
            "selector": args.wait_selector,
            "text": args.wait_text,
            "delay_ms": args.delay_ms,
            "timeout_ms": args.timeout_ms,
        },
        "proceed_on_captcha": args.proceed_on_captcha,
    }
    if args.url:
        body["url"] = args.url
    elif args.html_file:
        body["html"] = read_source(args.html_file, "--html-file")
    else:
        body["markdown"] = read_source(args.markdown_file, "--markdown-file")
    if args.base_url:
        body["base_url"] = args.base_url
    if args.selector:
        body["selector"] = args.selector
    if args.output in {"html", "markdown"}:
        body["extract_mode"] = args.extract_mode
    if args.cleanup:
        body["cleanup"] = {
            "consent_mode": "reject",
            "block_ads": True,
            "block_trackers": True,
            "block_chats": True,
            "block_newsletters": True,
        }
    return body


def request_id(headers) -> str | None:
    return headers.get("X-Request-ID") or headers.get("X-Request-Id")


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def parse_error(status: int, headers, raw: bytes) -> CaptureError:
    code = f"http_{status}"
    message = f"ViperCapture returned HTTP {status}"
    retryable = status in RETRYABLE_STATUSES
    result_request_id = request_id(headers)
    try:
        payload = json.loads(raw.decode("utf-8"))
        detail = payload.get("error", payload)
        if isinstance(detail, dict):
            code = str(detail.get("code", code))
            message = str(detail.get("message", message))
            retryable = bool(detail.get("retryable", retryable))
            result_request_id = detail.get("request_id") or result_request_id
    except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return CaptureError(
        status,
        code,
        message,
        retryable,
        result_request_id,
        retry_after_seconds(headers.get("Retry-After")),
    )


def send_request(api_url: str, key: str | None, body: dict) -> tuple[bytes, str | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/octet-stream",
        "User-Agent": "vipercapture-agent-skill/1.0",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        api_url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read(), request_id(response.headers)
    except urllib.error.HTTPError as error:
        raise parse_error(error.code, error.headers, error.read()) from None
    except urllib.error.URLError as error:
        raise CaptureError(
            0, "network_error", str(error.reason), True, None
        ) from None


def source_label(args: argparse.Namespace) -> str:
    if args.url:
        value = urllib.parse.urlparse(args.url).netloc.split(":", 1)[0]
    elif args.html_file:
        value = args.html_file.stem
    else:
        value = args.markdown_file.stem
    return re.sub(r"[^a-z0-9.-]+", "-", value.lower()).strip(".-") or "capture"


def output_path(args: argparse.Namespace) -> Path:
    if args.output_path:
        return args.output_path.expanduser()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    extension = EXTENSIONS.get(args.output, args.output)
    name = f"{source_label(args)}-{timestamp}.{extension}"
    return args.output_dir.expanduser() / name


def hosted_endpoint(api_url: str) -> bool:
    return urllib.parse.urlparse(api_url).hostname == "capture.viperisuseful.cc"


def validate_artifact(output: str, content: bytes) -> None:
    signatures = {
        "png": b"\x89PNG\r\n\x1a\n",
        "jpeg": b"\xff\xd8\xff",
        "webp": b"RIFF",
        "pdf": b"%PDF-",
    }
    signature = signatures.get(output)
    if signature and not content.startswith(signature):
        raise CaptureError(
            0,
            "invalid_artifact",
            f"response is not a valid {output.upper()} artifact",
            False,
        )
    if output == "webp" and content[8:12] != b"WEBP":
        raise CaptureError(
            0, "invalid_artifact", "response is not a valid WebP artifact", False
        )


def main() -> int:
    args = build_parser().parse_args()
    try:
        validate_args(args)
        body = request_body(args)
        target = output_path(args)
        if target.exists() and not args.force:
            raise ValueError(f"output already exists: {target} (use --force to replace)")
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "api_url": args.api_url,
                        "output_path": str(target.resolve()),
                        "request": body,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        key = os.environ.get("VIPERCAPTURE_API_KEY")
        if hosted_endpoint(args.api_url) and not key:
            raise ValueError("VIPERCAPTURE_API_KEY is required for the hosted API")
        target.parent.mkdir(parents=True, exist_ok=True)

        last_error: CaptureError | None = None
        for attempt in range(1, args.max_attempts + 1):
            try:
                content, result_request_id = send_request(args.api_url, key, body)
                if not content:
                    raise CaptureError(
                        0,
                        "empty_response",
                        "ViperCapture returned an empty artifact",
                        True,
                        result_request_id,
                    )
                validate_artifact(args.output, content)
                with target.open("wb" if args.force else "xb") as artifact:
                    artifact.write(content)
                print(
                    json.dumps(
                        {
                            "path": str(target.resolve()),
                            "format": args.output,
                            "request_id": result_request_id,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            except CaptureError as error:
                last_error = error
                if not error.retryable or attempt == args.max_attempts:
                    break
                delay = (
                    error.retry_after
                    if error.retry_after is not None
                    else min(30.0, 0.5 * 2 ** (attempt - 1) + random.random() * 0.25)
                )
                time.sleep(min(delay, 60.0))

        assert last_error is not None
        suffix = (
            f" (request_id={last_error.request_id})"
            if last_error.request_id
            else ""
        )
        raise ValueError(f"{last_error.code}: {last_error.message}{suffix}")
    except (CaptureError, OSError, ValueError) as error:
        print(f"ViperCapture error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
