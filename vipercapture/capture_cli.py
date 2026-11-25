"""Agent-friendly ``vipercapture capture`` client.

The saved file path is written to stdout. Errors are one JSON object on
stderr. Exit 0 is success, 2 is a bad command, and 1 is a capture or
network failure. Flag names are the same on Linux, macOS, and Windows;
the shell quotes arguments before Python sees them.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import http.client
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import time
from collections import deque
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import TextIO
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import launch
from vipercapture.cli_config import load_cli_config

EXAMPLE = (
    "vipercapture capture https://dimaths.ma "
    "--output dimaths.png "
    "--viewport 1440 900 "
    "--scale 2 "
    "--full-page=false "
    "--color-scheme dark "
    "--wait h1 "
    "--delay-ms 2500 "
    "--click \"[aria-label='Activer le thème sombre']\""
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_FORMAT_ALIASES = {"jpg": "jpeg", "md": "markdown"}
_SUFFIX_FORMAT = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
    ".avif": "avif",
    ".pdf": "pdf",
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "metadata",
    ".webm": "webm",
    ".mp4": "mp4",
    ".gif": "gif",
}
_EXTENSIONS = {
    "png": ".png",
    "jpeg": ".jpg",
    "webp": ".webp",
    "avif": ".avif",
    "pdf": ".pdf",
    "html": ".html",
    "markdown": ".md",
    "metadata": ".json",
    "webm": ".webm",
    "mp4": ".mp4",
    "gif": ".gif",
}
_MEDIA_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/markdown": ".md",
    "application/json": ".json",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "image/gif": ".gif",
    "application/zip": ".zip",
}
_FORMATS = (
    "png", "jpeg", "jpg", "webp", "avif", "pdf", "html", "markdown", "md",
    "metadata", "webm", "mp4", "gif",
)
_ENGINES = ("chromium", "firefox", "webkit")
_DEVICES = ("desktop", "iphone_14", "pixel_7", "ipad")
_COLOR_SCHEMES = ("light", "dark", "no-preference")
_REDUCED_MOTION = ("reduce", "no-preference")
_MEDIA = ("screen", "print")
_LAZY = ("none", "adaptive", "thorough")
_PROFILES = ("preview", "accurate")
_WAIT_EVENTS = ("domcontentloaded", "load", "networkidle")
_WAIT_STATES = ("visible", "attached", "hidden", "detached")
_CONSENT = ("none", "reject", "accept", "hide")
_CAPTCHA = ("error", "capture", "external")
_PDF_MODES = ("print", "single_page")
_PAPER = ("A0", "A1", "A2", "A3", "A4", "A5", "A6", "Legal", "Letter", "Tabloid")
_ORIENTATION = ("portrait", "landscape")
_EXTRACT = ("document", "article")
_SIDES = ("html", "markdown", "metadata", "mhtml")
_RESOURCES = (
    "document", "stylesheet", "image", "media", "font", "script", "texttrack",
    "xhr", "fetch", "eventsource", "websocket", "manifest", "other",
)
_NAMED_VIEWPORT = re.compile(
    r"^(?P<name>[A-Za-z0-9_-]{1,32}):(?P<width>\d+)x(?P<height>\d+)"
    r"(?:@(?P<first>[A-Za-z0-9_.-]+))?(?:@(?P<second>[A-Za-z0-9_.-]+))?$"
)
_THUMBNAIL = re.compile(
    r"^(?P<name>[A-Za-z0-9_-]{1,32}):(?:(?P<width>\d+)?x(?P<height>\d+)|(?P<only>\d+))$"
)
_COOKIE_KEYS = {
    "name": "name",
    "value": "value",
    "domain": "domain",
    "path": "path",
    "expires": "expires",
    "samesite": "same_site",
    "secure": "secure",
    "httponly": "http_only",
}
_SENSITIVE_HEADER_NAMES = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
    "x-csrf-token",
    "x-xsrf-token",
})
_SENSITIVE_HEADER_TOKENS = (
    "auth",
    "token",
    "secret",
    "password",
    "passwd",
    "cookie",
    "api-key",
    "apikey",
    "credential",
    "session",
    "private-key",
)


def _is_sensitive_header_name(name: str) -> bool:
    lowered = name.strip().lower()
    if lowered in _SENSITIVE_HEADER_NAMES:
        return True
    return any(tok in lowered for tok in _SENSITIVE_HEADER_TOKENS)
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_OPEN_JOBS = {"queued", "running"}
_STARTED_SERVER_HANDLE: tuple[object, int | None] | None = None


class CaptureError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        body: object = None,
        exit_code: int = 1,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body
        self.exit_code = exit_code
        self.retry_after = retry_after


class OptionalBool(argparse.Action):
    """Accept ``--flag``, ``--flag=true``, and ``--flag=false``."""

    def __init__(self, option_strings, dest, default=None, help=None):
        super().__init__(
            option_strings,
            dest,
            nargs="?",
            const="true",
            default=default,
            metavar="BOOL",
            help=help,
        )

    def __call__(self, parser, namespace, values, option_string=None):
        text = "true" if values is None else str(values).strip().lower()
        if text in _TRUE:
            setattr(namespace, self.dest, True)
        elif text in _FALSE:
            setattr(namespace, self.dest, False)
        else:
            parser.error(f"{option_string} expects true or false, not {values!r}")


class Record(argparse.Action):
    """Keep action flags in the order they appeared on the command line."""

    def __init__(self, *args, **kwargs):
        self.kind = kwargs.pop("kind")
        super().__init__(*args, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        if self.kind == "select" and (not isinstance(values, list) or len(values) < 2):
            parser.error("--select needs a selector and at least one value")
        steps = namespace.steps
        if steps is None:
            steps = []
            namespace.steps = steps
        steps.append((self.kind, values))


class CaptureParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        self.exit(2, _json_line({"ok": False, "error": message}) + "\n")


def wants_help(argv: list[str]) -> bool:
    return any(arg in {"-h", "--help"} for arg in argv)


def safe_filename(value: str) -> str:
    """A single path segment that is legal on Windows and every other OS."""
    cleaned = _BAD_FILENAME.sub("_", value).strip(" .")
    if not cleaned:
        return "capture"
    if cleaned.upper() in _RESERVED_FILENAMES:
        return f"_{cleaned}"
    return cleaned[:80]


def normalize_url(text: str) -> str:
    value = text.strip()
    if not value:
        raise ValueError("Pass a URL, --html, or --markdown.")
    if "://" not in value:
        scheme_like = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):(.*)$", value)
        if scheme_like and not re.match(r"\d{1,5}($|[/?#])", scheme_like.group(2)):
            raise ValueError("URL must start with http:// or https://.")
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must start with http:// or https://.")
    return value


_MAX_REQUEST_TIMEOUT_S = 2_147_483.0
_MAX_INT_OPTION = 2_147_483_647
_MAX_RETRIES = 1_000


def _validate_header_value(value: str, label: str) -> str:
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError(f"{label} must not contain newlines or NUL bytes.")
    return value


def _safe_ms_to_seconds(value: object, field: str) -> float:
    try:
        converted = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} is out of numeric range.") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{field} must be a finite number.")
    return round(converted / 1000.0, 3)


def positive_finite_float(value: str) -> float:
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from exc
    if not math.isfinite(parsed) or parsed <= 0 or parsed > _MAX_REQUEST_TIMEOUT_S:
        raise argparse.ArgumentTypeError(
            f"must be a finite number greater than 0 and at most {int(_MAX_REQUEST_TIMEOUT_S)}"
        )
    return parsed


def paper_size(value: str) -> str:
    text = value.strip()
    upper = text.upper()
    if upper in {"A0", "A1", "A2", "A3", "A4", "A5", "A6"}:
        return upper
    titled = text.title()
    if titled in {"Legal", "Letter", "Tabloid"}:
        return titled
    raise argparse.ArgumentTypeError(
        "paper must be A0-A6, Legal, Letter, or Tabloid"
    )


def parse_named_viewport(value: str) -> dict[str, object]:
    match = _NAMED_VIEWPORT.fullmatch(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "named viewport must look like mobile:390x844 or mobile:390x844@iphone_14"
        )
    item: dict[str, object] = {
        "name": match.group("name"),
        "width": int(match.group("width")),
        "height": int(match.group("height")),
    }
    extras = [match.group("first"), match.group("second")]
    for extra in extras:
        if extra is None:
            continue
        if extra in _DEVICES:
            item["device"] = extra
            continue
        try:
            item["device_scale_factor"] = float(extra)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "named viewport scale or device is invalid"
            ) from exc
    return item


def parse_thumbnail(value: str) -> dict[str, object]:
    match = _THUMBNAIL.fullmatch(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "thumbnail must look like card:640 or preview:320x180"
        )
    item: dict[str, object] = {"name": match.group("name")}
    if match.group("only") is not None:
        item["width"] = int(match.group("only"))
    else:
        if match.group("width") is not None:
            item["width"] = int(match.group("width"))
        if match.group("height") is not None:
            item["height"] = int(match.group("height"))
    return item


def _normalize_same_site(value: object) -> str:
    normalized = str(value or "Lax").strip().lower().replace("_", "-")
    return {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no-restriction": "None",
        "unspecified": "Lax",
    }.get(normalized, "Lax")


def _cookie_dict_to_render_cookie(
    item: dict[str, object],
    *,
    default_domain: str | None = None,
) -> dict[str, object]:
    if item.get("partitionKey") is not None or item.get("partition_key") is not None:
        raise ValueError(
            "Partitioned cookies (partitionKey) cannot be sent via --cookie-file; "
            "use 'vipercapture profile import' to preserve cookie partitioning."
        )
    name = str(item.get("name") or "").strip()
    domain = str(item.get("domain") or default_domain or "").strip()
    if not name or not domain:
        raise ValueError("Each cookie requires a name and domain.")
    cookie: dict[str, object] = {
        "name": name,
        "value": str(item.get("value") if item.get("value") is not None else ""),
        "domain": domain,
        "path": str(item.get("path") or "/"),
    }
    raw_expires = item.get("expires", item.get("expirationDate"))
    if raw_expires not in (None, ""):
        try:
            expires = float(raw_expires)
        except (TypeError, ValueError) as exc:
            raise ValueError("Cookie expires must be a number.") from exc
        if expires > 0:
            cookie["expires"] = expires
    http_only = item.get("http_only", item.get("httpOnly", False))
    if bool(http_only):
        cookie["http_only"] = True
    if bool(item.get("secure", False)):
        cookie["secure"] = True
    raw_same_site = item.get("same_site", item.get("sameSite"))
    if raw_same_site is not None:
        cookie["same_site"] = _normalize_same_site(raw_same_site)
    return cookie


def parse_cookie(text: str) -> dict[str, object]:
    parts = [part.strip() for part in text.split(";") if part.strip()]
    if not parts:
        raise ValueError("--cookie is empty")
    cookie: dict[str, object] = {}
    for part in parts:
        if "=" not in part:
            flag = part.lower()
            if flag == "secure":
                cookie["secure"] = True
            elif flag == "httponly":
                cookie["http_only"] = True
            else:
                raise ValueError("--cookie flags are secure and httponly")
            continue
        raw_key, raw_value = part.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        mapped = _COOKIE_KEYS.get(key.lower())
        if mapped is None:
            if "name" in cookie:
                raise ValueError(f"--cookie key {key!r} is unknown")
            cookie["name"] = key
            cookie["value"] = value
            continue
        if mapped == "expires":
            try:
                cookie["expires"] = float(value)
            except ValueError as exc:
                raise ValueError("--cookie expires must be a number") from exc
        elif mapped in {"secure", "http_only"}:
            cookie[mapped] = value.lower() in _TRUE
        elif mapped == "same_site":
            folded = value.title()
            if folded not in {"Strict", "Lax", "None"}:
                raise ValueError("--cookie SameSite must be Strict, Lax, or None")
            cookie["same_site"] = folded
        else:
            cookie[mapped] = value
    if "name" not in cookie or "value" not in cookie or "domain" not in cookie:
        raise ValueError("--cookie needs a name, value, and domain")
    return cookie


def parse_cookie_file_content(
    content: str,
    *,
    default_domain: str | None = None,
    default_secure: bool = False,
) -> list[dict[str, object]]:
    """Parse Playwright JSON, Cookie-Editor JSON, Netscape cookies.txt, or header lines."""
    text = content.removeprefix("\ufeff").strip()
    if not text:
        raise ValueError("--cookie-file is empty.")
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--cookie-file is not valid JSON: {exc}") from exc
        if isinstance(parsed, dict):
            if isinstance(parsed.get("cookies"), list):
                items = parsed["cookies"]
            elif "name" in parsed:
                items = [parsed]
            else:
                raise ValueError("--cookie-file JSON object must contain a 'cookies' list or a cookie object.")
        elif isinstance(parsed, list):
            items = parsed
        else:
            raise ValueError("--cookie-file JSON must be a cookie object, array, or Playwright storage state.")
        cookies: list[dict[str, object]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("--cookie-file JSON entries must be objects.")
            cookies.append(_cookie_dict_to_render_cookie(item, default_domain=default_domain))
        return cookies

    if "\t" in text or text.startswith("# Netscape"):
        cookies = []
        for number, raw_line in enumerate(text.splitlines(), 1):
            line = raw_line.strip("\r\n")
            http_only = line.startswith("#HttpOnly_")
            if not line.strip() or (line.startswith("#") and not http_only):
                continue
            if http_only:
                line = line.removeprefix("#HttpOnly_")
            parts = line.split("\t")
            if len(parts) != 7:
                raise ValueError(f"Invalid Netscape cookie on line {number}.")
            domain, include_subdomains, path, secure, expires, name, value = parts
            if include_subdomains.upper() == "TRUE" and not domain.startswith("."):
                domain = f".{domain}"
            cookies.append(
                _cookie_dict_to_render_cookie(
                    {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": path or "/",
                        "secure": secure.upper() == "TRUE",
                        "http_only": http_only,
                        "expires": expires,
                    },
                    default_domain=default_domain,
                )
            )
        if not cookies:
            raise ValueError("--cookie-file did not contain any cookies.")
        return cookies

    cookies = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if re.search(r"(?:^|;\s*)domain\s*=", line, flags=re.IGNORECASE):
            cookies.append(parse_cookie(line))
            continue
        header = line
        if header.lower().startswith("cookie:"):
            header = header.partition(":")[2].strip()
        if not default_domain:
            raise ValueError(
                "Cookie header entries in --cookie-file require a target URL or explicit domain= attribute."
            )
        jar = SimpleCookie()
        try:
            jar.load(header)
        except CookieError as exc:
            raise ValueError("--cookie-file contains a malformed Cookie header.") from exc
        if not jar:
            raise ValueError("--cookie-file did not contain any cookies.")
        for name, morsel in jar.items():
            cookie: dict[str, object] = {
                "name": name,
                "value": morsel.value,
                "domain": default_domain,
                "path": "/",
            }
            if default_secure:
                cookie["secure"] = True
            cookies.append(cookie)
    if not cookies:
        raise ValueError("--cookie-file did not contain any cookies.")
    return cookies


def parse_header(text: str) -> tuple[str, str]:
    colon = text.find(":")
    equals = text.find("=")
    if colon < 0 and equals < 0:
        raise ValueError("--header needs Name: value")
    if colon < 0:
        index = equals
    elif equals < 0:
        index = colon
    else:
        index = min(colon, equals)
    name = text[:index].strip()
    value = text[index + 1 :].strip()
    if not name:
        raise ValueError("--header needs a name")
    return name, value


def _json_line(document: dict[str, object]) -> str:
    return json.dumps(document, ensure_ascii=False)


def _redact_headers_string(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return ""
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return "***"
    if not isinstance(parsed, dict):
        return "***"
    redacted: dict[str, object] = {}
    for key, val in parsed.items():
        if _is_sensitive_header_name(str(key)):
            redacted[str(key)] = "***"
        else:
            redacted[str(key)] = val
    return json.dumps(redacted, ensure_ascii=False)


def public_payload(payload: dict[str, object]) -> dict[str, object]:
    """A copy that can be printed. Passwords, cookie values, and auth headers are redacted."""

    def walk(value: object, *, parent_key: str = "") -> object:
        if isinstance(value, dict):
            printed: dict[str, object] = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if lowered == "password":
                    printed[key] = "***"
                elif lowered == "headers" and isinstance(item, str):
                    printed[key] = _redact_headers_string(item)
                elif parent_key == "headers" and _is_sensitive_header_name(lowered):
                    printed[key] = "***"
                elif parent_key == "cookies" and lowered == "value":
                    printed[key] = "***"
                else:
                    printed[key] = walk(item, parent_key=lowered)
            return printed
        if isinstance(value, list):
            return [walk(item, parent_key=parent_key) for item in value]
        return value

    printed = walk(payload)
    if not isinstance(printed, dict):
        return {}
    return printed


def write_line(stream: TextIO, text: str) -> None:
    try:
        stream.write(text + "\n")
        stream.flush()
    except UnicodeEncodeError:
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            stream.write(text.encode("utf-8", errors="replace").decode("ascii", "replace") + "\n")
            stream.flush()
            return
        buffer.write((text + "\n").encode("utf-8"))
        buffer.flush()


def fail(
    message: str,
    *,
    status: int | None = None,
    code: str | None = None,
    body: object = None,
    exit_code: int = 1,
) -> int:
    document: dict[str, object] = {"ok": False, "error": message}
    if code:
        document["code"] = code
    if status is not None:
        document["status"] = status
    if body is not None:
        document["body"] = body
    write_line(sys.stderr, _json_line(document))
    return exit_code


def _add_bool(group: argparse._ArgumentGroup, option: str, dest: str, help_text: str) -> None:
    group.add_argument(option, dest=dest, action=OptionalBool, help=help_text)
    group.add_argument(
        "--no-" + option[2:],
        dest=dest,
        action="store_const",
        const=False,
        help=argparse.SUPPRESS,
    )


def _record(group: argparse._ArgumentGroup, *option: str, kind: str, **kwargs) -> None:
    group.add_argument(*option, action=Record, kind=kind, dest=kind, **kwargs)


def add_render_option_groups(
    parser: argparse.ArgumentParser,
    *,
    include_timezone: bool = True,
    include_webhook: bool = True,
    include_wait_flag: bool = True,
) -> None:
    """Attach shared RenderRequest option groups so capture, bulk, schedule, sign, and preset stay in sync."""
    parser.set_defaults(steps=None)

    target = parser.add_argument_group("input")
    target.add_argument("--html", help="HTML document text.")
    target.add_argument("--html-file", help="HTML file, or - for stdin.")
    target.add_argument("--markdown", help="Markdown text.")
    target.add_argument("--markdown-file", help="Markdown file, or - for stdin.")
    target.add_argument("--base-url", help="Base URL for HTML or Markdown input.")
    target.add_argument("--from-json", help="Base RenderRequest JSON file, or - for stdin. CLI flags override fields.")
    target.add_argument("--preset", help="Load a saved preset by name as base settings.")

    output = parser.add_argument_group("output")
    output.add_argument("--format", choices=_FORMATS, help="png, jpeg, webp, avif, pdf, html, markdown, metadata, webm, mp4, or gif.")
    output.add_argument("--engine", choices=_ENGINES, help="chromium, firefox, or webkit.")
    _add_bool(output, "--full-page", "full_page", "true or false. false captures the viewport. Also --no-full-page.")
    output.add_argument("--viewport", nargs=2, type=int, metavar=("WIDTH", "HEIGHT"), help="Viewport size in pixels.")
    output.add_argument("--scale", type=float, help="Device scale factor, for example 2.")
    output.add_argument("--device", choices=_DEVICES, help="desktop, iphone_14, pixel_7, or ipad.")
    output.add_argument("--selector", help="Element screenshot. Requires --full-page=false.")
    output.add_argument("--clip", nargs=4, type=float, metavar=("X", "Y", "WIDTH", "HEIGHT"), help="Viewport clip. Requires --full-page=false.")
    _add_bool(output, "--preserve-viewport-width", "preserve_viewport_width", "Clip a full-page image to the viewport width.")
    output.add_argument("--lazy-load", choices=_LAZY, help="none, adaptive, or thorough.")
    output.add_argument("--profile", choices=_PROFILES, help="preview or accurate.")
    _add_bool(output, "--cache", "cache", "Reuse an exact cached image. Also --no-cache.")
    output.add_argument("--cache-key", help="Cache variant. Implies --cache.")
    output.add_argument("--named-viewport", action="append", type=parse_named_viewport, help="name:WIDTHxHEIGHT, optionally @device and @scale. Repeat for a ZIP.")
    output.add_argument("--slice-height", type=int, help="Full-page slice height in pixels.")
    output.add_argument("--slice-overlap", type=int, help="Slice overlap. Requires --slice-height.")

    wait = parser.add_argument_group("wait")
    if include_wait_flag:
        wait.add_argument("--wait", dest="wait_selector", help="CSS selector to see before actions.")
    else:
        wait.add_argument("--wait-selector", dest="wait_selector", help="CSS selector to see before actions.")
    wait.add_argument("--wait-text", help="Text to see before actions.")
    wait.add_argument("--wait-event", choices=_WAIT_EVENTS, help="domcontentloaded, load, or networkidle.")
    wait.add_argument("--wait-state", choices=_WAIT_STATES, help="visible, attached, hidden, or detached.")
    _add_bool(wait, "--wait-images", "wait_images", "Wait until current images have loaded.")
    wait.add_argument("--delay-ms", type=int, help="Milliseconds to wait after navigation, before actions.")
    wait.add_argument("--timeout-ms", type=int, help="Navigation wait timeout in milliseconds.")

    actions = parser.add_argument_group("actions")
    _record(actions, "--click", kind="click", help="Click a selector. Repeat in order.")
    _record(actions, "--hover", kind="hover", help="Hover a selector.")
    _record(actions, "--hide", kind="hide", help="Hide a selector.")
    _record(actions, "--fill", kind="fill", nargs=2, metavar=("SELECTOR", "VALUE"), help="Fill a field.")
    _record(actions, "--press", kind="press", help="Press a key, for example Enter or Control+P.")
    _record(actions, "--press-on", kind="press_on", nargs=2, metavar=("SELECTOR", "KEY"), help="Press a key on a selector.")
    _record(actions, "--select", kind="select", nargs="+", metavar=("SELECTOR", "VALUE"), help="Select one or more option values.")
    _record(actions, "--scroll", kind="scroll", nargs=2, type=float, metavar=("X", "Y"), help="Scroll by x and y pixels.")
    _record(actions, "--scroll-to", kind="scroll_to", help="Scroll a selector into view.")
    _record(actions, "--js", kind="js", help="JavaScript action. The server must set VIPERCAPTURE_ALLOW_SCRIPTS=1.")
    _record(actions, "--js-file", kind="js_file", metavar="PATH", help="JavaScript file action, or - for stdin.")
    _record(actions, "--action-wait", kind="action_wait", help="Wait for a selector after earlier actions.")
    _record(actions, "--action-wait-text", kind="action_wait_text", metavar="TEXT", help="Wait for text after earlier actions.")
    _record(actions, "--pause-ms", kind="pause", type=int, help="Wait this many milliseconds after earlier actions.")
    _record(actions, "--action", kind="json", help="One action as a JSON object. Ordered with the other action flags.")
    _record(actions, "--action-delay-ms", kind="action_delay", type=int, help="delay_ms for every action that follows.")
    _record(actions, "--action-timeout-ms", kind="action_timeout", type=int, help="timeout_ms for every action that follows.")

    environment = parser.add_argument_group("environment")
    environment.add_argument("--color-scheme", choices=_COLOR_SCHEMES, help="light, dark, or no-preference.")
    environment.add_argument("--reduced-motion", choices=_REDUCED_MOTION, help="reduce or no-preference.")
    environment.add_argument("--media", choices=_MEDIA, help="screen or print, applied before load.")
    environment.add_argument("--locale", help="BCP 47 language tag, for example fr-FR.")
    if include_timezone:
        environment.add_argument("--timezone", help="IANA time zone, for example Africa/Casablanca.")
    else:
        environment.add_argument("--capture-timezone", dest="timezone", help="Browser IANA time zone for the capture.")
    environment.add_argument("--css", help="CSS injected into the page.")
    environment.add_argument("--css-file", help="CSS file, or - for stdin.")

    network = parser.add_argument_group("network")
    _add_bool(network, "--javascript", "javascript", "true or false. false blocks page scripts. Also --no-javascript.")
    network.add_argument("--user-agent", help="Browser user agent.")
    network.add_argument("--header", action="append", help="Request header as Name: value. Repeatable.")
    network.add_argument("--cookie", action="append", help="Cookie as name=value; domain=host; path=/; Secure; HttpOnly; SameSite=Lax.")
    network.add_argument("--cookie-file", action="append", metavar="PATH", help="Cookie file (Playwright, Cookie-Editor JSON, Netscape, or Cookie header), or - for stdin.")
    network.add_argument("--block-url", action="append", help="URL glob to block. Repeatable.")
    network.add_argument("--block-resource", action="append", choices=_RESOURCES, help="Resource type to block. Repeatable.")
    network.add_argument("--geolocation", nargs=2, type=float, metavar=("LAT", "LON"), help="Latitude and longitude.")
    network.add_argument("--geo-accuracy", type=float, help="Geolocation accuracy in meters.")
    network.add_argument("--proxy", help="http, https, socks4, or socks5 proxy URL without credentials.")
    network.add_argument("--proxy-username", help="Proxy username.")
    network.add_argument("--proxy-password", help="Proxy password. VIPERCAPTURE_PROXY_PASSWORD is used when this is omitted.")
    network.add_argument("--proxy-bypass", help="Proxy bypass list.")
    _add_bool(network, "--bypass-csp", "bypass_csp", "Bypass the page Content-Security-Policy.")
    _add_bool(network, "--ignore-https-errors", "ignore_https_errors", "Continue when the target certificate is invalid.")

    cleanup = parser.add_argument_group("cleanup and checks")
    cleanup.add_argument("--consent", choices=_CONSENT, help="none, reject, accept, or hide.")
    _add_bool(cleanup, "--block-ads", "block_ads", "Block ads.")
    _add_bool(cleanup, "--block-trackers", "block_trackers", "Block trackers.")
    _add_bool(cleanup, "--block-chats", "block_chats", "Block chat widgets.")
    _add_bool(cleanup, "--block-newsletters", "block_newsletters", "Block newsletter prompts.")
    cleanup.add_argument("--assert-includes", action="append", help="Text the page must contain. Repeatable.")
    cleanup.add_argument("--assert-excludes", action="append", help="Text the page must not contain. Repeatable.")
    cleanup.add_argument("--assert-request-failure", action="append", help="URL glob that must fail. Repeatable.")
    cleanup.add_argument("--fail-on", dest="fail_on", action="append", type=int, help="HTTP status that fails the capture. Repeatable.")
    _add_bool(cleanup, "--stealth", "stealth", "true or false. Also --no-stealth.")
    cleanup.add_argument("--captcha", choices=_CAPTCHA, help="error, capture, or external.")
    cleanup.add_argument("--captcha-solver", help="Operator handler name. Requires --captcha external.")
    cleanup.add_argument("--captcha-timeout-ms", type=int, help="Captcha handler timeout.")
    _add_bool(cleanup, "--proceed-on-captcha", "proceed_on_captcha", "Capture a blocking challenge instead of failing.")

    image = parser.add_argument_group("image")
    image.add_argument("--quality", type=int, help="JPEG, WebP, or AVIF quality from 1 to 100.")
    image.add_argument("--image-width", type=int, help="Maximum output width. Aspect ratio is kept.")
    image.add_argument("--image-height", type=int, help="Maximum output height. Aspect ratio is kept.")
    _add_bool(image, "--transparent", "transparent", "Keep a transparent background for PNG, WebP, or AVIF.")
    _add_bool(image, "--optimize-for-speed", "optimize_for_speed", "Use the faster Chromium encoder. Also --no-optimize-for-speed.")
    image.add_argument("--thumbnail", action="append", type=parse_thumbnail, help="name:WIDTH or name:WIDTHxHEIGHT. Repeat up to five times.")

    pdf = parser.add_argument_group("pdf")
    pdf.add_argument("--pdf-mode", choices=_PDF_MODES, help="print or single_page.")
    pdf.add_argument("--paper", type=paper_size, help="A0-A6, Legal, Letter, or Tabloid.")
    pdf.add_argument("--orientation", choices=_ORIENTATION, help="portrait or landscape.")
    _add_bool(pdf, "--pdf-background", "pdf_background", "Print background graphics. Also --no-pdf-background.")
    _add_bool(pdf, "--pdf-tagged", "pdf_tagged", "true or false. Controls Chromium structure tags.")
    pdf.add_argument("--pdf-margin", nargs="+", type=float, metavar="INCH", help="One value for every side, or top right bottom left.")
    pdf.add_argument("--pdf-header", help="PDF header template.")
    pdf.add_argument("--pdf-header-file", help="PDF header template file.")
    pdf.add_argument("--pdf-footer", help="PDF footer template.")
    pdf.add_argument("--pdf-footer-file", help="PDF footer template file.")
    pdf.add_argument("--page-ranges", help="Pages to print, for example 1-3,5.")

    video = parser.add_argument_group("video")
    video.add_argument("--video-duration-ms", type=int, help="Recording length from 1000 to 30000.")
    video.add_argument("--fps", type=int, help="Frames per second from 1 to 60.")
    video.add_argument("--bitrate-mbps", type=int, help="WebM or MP4 bitrate from 1 to 100.")
    _add_bool(video, "--video-scroll", "video_scroll", "Scroll the viewport while recording.")
    video.add_argument("--scroll-step", type=int, help="Video scroll step in pixels.")
    video.add_argument("--scroll-delay-ms", type=int, help="Delay between video scroll steps.")
    _add_bool(video, "--video-transparent", "video_transparent", "Transparent full-page WebM or GIF.")

    extract = parser.add_argument_group("extraction")
    extract.add_argument("--extract", choices=_EXTRACT, help="document or article.")
    _add_bool(extract, "--shadow-dom", "shadow_dom", "Embed open shadow roots in HTML or Markdown.")
    extract.add_argument("--element", action="append", help="Metadata CSS selector. Repeatable.")
    extract.add_argument("--side", action="append", choices=_SIDES, help="Extra artifact: html, markdown, metadata, or mhtml.")

    diagnostics = parser.add_argument_group("diagnostics")
    _add_bool(diagnostics, "--diagnostics", "diagnostics", "Return a diagnostic ZIP.")
    _add_bool(diagnostics, "--har", "har", "Include a redacted HAR. Implies --diagnostics.")
    _add_bool(diagnostics, "--trace", "trace", "Include a redacted Playwright trace. Implies --diagnostics.")
    _add_bool(diagnostics, "--warc", "warc", "Include a WARC. Implies --diagnostics.")
    diagnostics.add_argument("--no-console", dest="include_console", action="store_const", const=False, help="Omit console logs from the diagnostic bundle.")
    diagnostics.add_argument("--no-network", dest="include_network", action="store_const", const=False, help="Omit network logs from the diagnostic bundle.")
    _add_bool(diagnostics, "--deterministic", "deterministic", "Freeze time and random values.")
    diagnostics.add_argument("--timestamp-ms", type=int, help="Deterministic timestamp. Enables --deterministic.")
    diagnostics.add_argument("--seed", type=int, help="Deterministic random seed. Enables --deterministic.")
    diagnostics.add_argument("--no-wait-for-fonts", dest="wait_for_fonts", action="store_const", const=False, help="Do not wait for fonts in deterministic mode.")
    _add_bool(diagnostics, "--certify", "certify", "Return an Ed25519-certified bundle.")
    diagnostics.add_argument("--profile-id", help="Saved browser profile id.")
    _add_bool(diagnostics, "--save-profile", "save_profile", "Write the browser profile back. Requires --profile-id.")
    if include_webhook:
        diagnostics.add_argument("--webhook", help="Delivery webhook URL. Used with --async.")


def build_parser() -> CaptureParser:
    parser = CaptureParser(
        prog="vipercapture capture",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Render a page through the local ViperCapture API and save the file. "
            "Stdout is the saved path. Stderr errors are JSON."
        ),
        epilog=(
            "Example (bash, PowerShell, and cmd accept the same flags):\n"
            f"  {EXAMPLE}\n\n"
            "--delay-ms waits after navigation and before actions. "
            "Repeat --click, --fill, and the other action flags in the order "
            "they should run. --full-page=false and --no-full-page match. "
            "A background server stays up after a capture; stop it with "
            "vipercapture kill (or pass --stop-server)."
        ),
    )
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="URL",
        help="Page URL(s). https:// is added when the scheme is omitted.",
    )
    parser.add_argument("--url", help="Page URL. Use this instead of the positional argument.")
    parser.add_argument("--urls-file", help="File with one URL per line, or - for stdin.")

    add_render_option_groups(parser)

    files = parser.add_argument_group("output files")
    files.add_argument("--output", help="File to write. Use - for stdout. The extension selects the format.")
    files.add_argument("--output-dir", help="Directory for saved capture file(s) (required or default for multi-URL).")
    files.add_argument("--output-template", help="Filename template for --output-dir / multi-URL, e.g. '{host}-{index}-{timestamp}{ext}'.")
    files.add_argument("--open", dest="open_output", action="store_true", help="Open the saved file with the OS default viewer.")
    files.add_argument("--copy", dest="copy_output", action="store_true", help="Copy saved content or path to the clipboard.")

    client = parser.add_argument_group("client")
    client.add_argument("--server", default=f"http://{launch.HOST}:{launch.PORT}", help="API base URL. The default local server is started when needed.")
    client.add_argument("--api-key", help="Bearer token. Otherwise VIPERCAPTURE_API_KEY or VIPERCAPTURE_ADMIN_TOKEN.")
    client.add_argument("--request-id", help="X-Request-Id value.")
    client.add_argument("--idempotency-key", help="Idempotency-Key for --async.")
    client.add_argument("--request-timeout", type=positive_finite_float, help="Seconds to wait for the render. Video defaults to 180 and images to 120.")
    client.add_argument("--async", dest="async_job", action="store_true", help="Submit /v1/jobs, poll, and save the result.")
    client.add_argument("--no-wait", dest="no_wait", action="store_true", help="With --async, print the job ID (or JSON) and exit immediately.")
    client.add_argument("--poll-ms", type=int, default=500, help="Async poll interval in milliseconds.")
    client.add_argument("--retries", type=int, default=0, help="Retry count for transient network errors and HTTP 429/502/503/504.")
    client.add_argument("--retry-delay-ms", type=int, default=500, help="Initial retry backoff in milliseconds.")
    client.add_argument("--no-start", action="store_true", help="Do not start the local server.")
    client.add_argument("--stop-server", action="store_true", help="Stop the local server after finishing if this command started it.")
    client.add_argument("--no-config", action="store_true", help="Ignore ~/.vipercapture/config.toml defaults.")
    client.add_argument("--dry-run", action="store_true", help="Print the JSON request and exit.")
    client.add_argument("--json", action="store_true", help="Print a JSON result on stdout instead of the path.")
    client.add_argument("--quiet", action="store_true", help="Print nothing on success.")
    client.add_argument("--verbose", action="store_true", help="Print progress on stderr.")
    return parser


def _flag_was_passed(argv: list[str], flag: str) -> bool:
    prefix = flag + "="
    return any(tok == flag or tok.startswith(prefix) for tok in argv)


def parse_capture_args(argv: list[str]) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    targets = list(getattr(args, "targets", []) or [])
    args.target = targets[0] if len(targets) == 1 else None
    args._explicit_flags = {
        flag
        for flag in (
            "--server",
            "--poll-ms",
            "--retries",
            "--retry-delay-ms",
            "--no-start",
            "--quiet",
            "--verbose",
            "--json",
            "--stop-server",
        )
        if _flag_was_passed(argv, flag)
    }
    return args


def _server_origin(server: str | None) -> tuple[str, str, int] | None:
    if not server or not isinstance(server, str):
        return None
    try:
        root = api_root(server)
    except ValueError:
        return None
    parsed = urlsplit(root)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return (scheme, host, port)


def _config_credential_for_server(
    cfg: dict[str, object],
    key: str,
    server: str | None,
) -> str:
    raw = cfg.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return ""
    cfg_server = cfg.get("server")
    if isinstance(cfg_server, str) and cfg_server.strip():
        cfg_origin = _server_origin(cfg_server)
        target_origin = _server_origin(server)
        if cfg_origin is None or target_origin is None or cfg_origin != target_origin:
            return ""
    return _header_token(raw)


def derive_batch_idempotency_key(base: str, suffix: str, *, max_len: int = 128) -> str:
    """Derive a deterministic idempotency key guaranteed to fit within API length limits."""
    combined = f"{base}-{suffix}"
    if len(combined) <= max_len:
        return combined
    digest = hashlib.sha256(f"{base}:{suffix}".encode("utf-8")).hexdigest()[:16]
    tail = f"-{digest}-{suffix}"
    prefix_len = max(1, max_len - len(tail))
    return f"{base[:prefix_len]}{tail}"[:max_len]


def apply_config_defaults(
    args: argparse.Namespace,
    config: dict[str, object] | None = None,
) -> None:
    """Apply ~/.vipercapture/config.toml defaults without overriding explicit CLI flags."""
    if getattr(args, "no_config", False):
        return
    cfg = load_cli_config() if config is None else config
    if not cfg:
        return
    explicit: set[str] = getattr(args, "_explicit_flags", set())
    flag_map = {
        "server": "--server",
        "poll_ms": "--poll-ms",
        "retries": "--retries",
        "retry_delay_ms": "--retry-delay-ms",
        "no_start": "--no-start",
        "quiet": "--quiet",
        "verbose": "--verbose",
        "json": "--json",
        "stop_server": "--stop-server",
    }
    if "server" in cfg and hasattr(args, "server") and "--server" not in explicit:
        setattr(args, "server", cfg["server"])
    effective_server = getattr(args, "server", None)
    for key, value in cfg.items():
        if key == "server" or not hasattr(args, key):
            continue
        if key in {"api_key", "admin_token", "signing_token"}:
            if getattr(args, key) is None:
                scoped = _config_credential_for_server(cfg, key, effective_server)
                if scoped:
                    setattr(args, key, scoped)
            continue
        if key in flag_map:
            if flag_map[key] not in explicit:
                setattr(args, key, value)
            continue
        if getattr(args, key) is None:
            setattr(args, key, value)


def _read_text(path: str, *, stdin_cache: dict[str, str] | None = None) -> str:
    if path == "-":
        if stdin_cache is not None:
            if "-" not in stdin_cache:
                stdin_cache["-"] = sys.stdin.read()
            return stdin_cache["-"]
        return sys.stdin.read()
    file = Path(path)
    if not file.is_file():
        raise ValueError(f"File not found: {path}")
    try:
        return file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc


def _one_of(
    left: str | None,
    right: str | None,
    message: str,
    *,
    stdin_cache: dict[str, str] | None = None,
) -> str | None:
    if left and right:
        raise ValueError(message)
    if right:
        return _read_text(right, stdin_cache=stdin_cache)
    return left


def infer_format(path: str | None, explicit: str | None) -> str:
    if explicit:
        return _FORMAT_ALIASES.get(explicit, explicit)
    if path and path != "-":
        found = _SUFFIX_FORMAT.get(Path(path).suffix.lower())
        if found:
            return found
    return "png"


def _put(
    target: dict[str, object],
    key: str,
    value: object,
    default: object,
    *,
    force_default: bool = False,
) -> None:
    if value is None:
        return
    if not force_default and value == default:
        return
    target[key] = value


def _deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _local_presets_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_data = os.environ.get("VIPERCAPTURE_DATA_DIR", "").strip()
    if env_data:
        candidates.append(Path(env_data).expanduser() / "presets" / "presets.json")
    candidates.append(Path.home() / ".vipercapture" / "data" / "presets" / "presets.json")
    candidates.append(launch.ROOT / ".vipercapture" / "presets" / "presets.json")
    return candidates


def _decode_preset_headers(raw_headers: object) -> dict[str, str]:
    if isinstance(raw_headers, dict):
        return {str(k): str(v) for k, v in raw_headers.items()}
    if not isinstance(raw_headers, str) or not raw_headers.strip():
        return {}
    text = raw_headers.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Preset headers JSON is invalid: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Preset headers JSON must be an object.")
        return {str(k): str(v) for k, v in parsed.items()}
    headers: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name, val = parse_header(line)
        headers[name] = val
    return headers


_PRESET_CLI_EXTRAS = (
    "engine",
    "profile",
    "stealth",
    "actions",
    "network",
    "assertions",
    "elements",
    "side_outputs",
    "deterministic",
    "certification",
    "slices",
    "profile_id",
    "save_profile",
    "cache",
    "cache_key",
    "include_shadow_dom",
    "captcha",
    "proceed_on_captcha",
    "viewports",
    "base_url",
    "delivery",
)


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE or lowered in {"t", "y"}:
            return True
        if lowered in _FALSE or lowered in {"f", "n"}:
            return False
    return default


def payload_to_preset_settings(payload: dict[str, object]) -> dict[str, object]:
    """Serialize a CLI render payload into Web UI PresetSettings keys while preserving CLI extras."""
    settings: dict[str, object] = {}
    fmt = str(payload.get("output") or "png").lower()
    settings["output"] = _FORMAT_ALIASES.get(fmt, fmt)
    if "full_page" in payload:
        settings["fullPage"] = _coerce_bool(payload["full_page"], True)
    if _coerce_bool(payload.get("preserve_viewport_width"), False):
        settings["preserveViewportWidth"] = True

    vp = payload.get("viewport")
    if isinstance(vp, dict):
        if isinstance(vp.get("width"), (int, float)):
            settings["width"] = int(vp["width"])
        if isinstance(vp.get("height"), (int, float)):
            settings["height"] = int(vp["height"])
        if isinstance(vp.get("device_scale_factor"), (int, float)):
            settings["density"] = vp["device_scale_factor"]

    if isinstance(payload.get("selector"), str) and payload["selector"].strip():
        settings["selector"] = payload["selector"]
    clip = payload.get("clip")
    if isinstance(clip, dict):
        settings["clipEnabled"] = True
        for src, dst in (("x", "clipX"), ("y", "clipY"), ("width", "clipWidth"), ("height", "clipHeight")):
            if isinstance(clip.get(src), (int, float)):
                settings[dst] = clip[src]

    image = payload.get("image")
    if isinstance(image, dict):
        if isinstance(image.get("quality"), int) and not isinstance(image.get("quality"), bool):
            settings["quality"] = image["quality"]
        if "transparent_background" in image:
            settings["transparent"] = _coerce_bool(image.get("transparent_background"), False)
        if "optimize_for_speed" in image:
            settings["optimizePng"] = _coerce_bool(image.get("optimize_for_speed"), False)
        extra_img = {k: v for k, v in image.items() if k not in {"quality", "transparent_background", "optimize_for_speed"}}
        if extra_img:
            settings["image"] = copy.deepcopy(extra_img)

    if isinstance(payload.get("lazy_load"), str):
        settings["lazyLoad"] = payload["lazy_load"]
    diag = payload.get("diagnostics")
    if isinstance(diag, dict):
        if "bundle" in diag:
            settings["diagnostics"] = _coerce_bool(diag.get("bundle"), False)
        extra_diag = {k: v for k, v in diag.items() if k != "bundle"}
        if extra_diag:
            settings["diagnostics_extra"] = copy.deepcopy(extra_diag)

    video = payload.get("video")
    if isinstance(video, dict):
        if isinstance(video.get("duration_ms"), (int, float)) and not isinstance(video.get("duration_ms"), bool):
            settings["videoDuration"] = _safe_ms_to_seconds(video["duration_ms"], "video.duration_ms")
        if isinstance(video.get("fps"), int) and not isinstance(video.get("fps"), bool):
            settings["videoFps"] = video["fps"]
        if isinstance(video.get("bitrate_mbps"), int) and not isinstance(video.get("bitrate_mbps"), bool):
            settings["videoBitrate"] = video["bitrate_mbps"]
        if "scroll" in video:
            settings["videoScroll"] = _coerce_bool(video.get("scroll"), False)
        if "transparent_background" in video and (fmt in {"webm", "mp4", "gif"} or "transparent" not in settings):
            settings["transparent"] = _coerce_bool(video.get("transparent_background"), False)
        extra_vid = {
            k: v for k, v in video.items()
            if k not in {"duration_ms", "fps", "bitrate_mbps", "scroll", "transparent_background"}
        }
        if extra_vid:
            settings["video"] = copy.deepcopy(extra_vid)

    pdf = payload.get("pdf")
    if isinstance(pdf, dict):
        if isinstance(pdf.get("mode"), str):
            settings["pdfMode"] = pdf["mode"]
        if isinstance(pdf.get("paper_size"), str):
            settings["paperSize"] = pdf["paper_size"]
        if isinstance(pdf.get("orientation"), str):
            settings["orientation"] = pdf["orientation"]
        margins = pdf.get("margins")
        extra_pdf = {k: v for k, v in pdf.items() if k not in {"mode", "paper_size", "orientation", "margins"}}
        if isinstance(margins, dict):
            vals = [margins.get(side) for side in ("top", "right", "bottom", "left")]
            if all(isinstance(v, (int, float)) for v in vals) and len(set(vals)) == 1:
                settings["pdfMargin"] = vals[0]
            else:
                extra_pdf["margins"] = copy.deepcopy(margins)
        if extra_pdf:
            settings["pdf"] = copy.deepcopy(extra_pdf)

    if isinstance(payload.get("extract_mode"), str):
        settings["extractMode"] = payload["extract_mode"]

    wait_for = payload.get("wait_for")
    if isinstance(wait_for, dict):
        if isinstance(wait_for.get("event"), str):
            settings["waitEvent"] = wait_for["event"]
        if isinstance(wait_for.get("delay_ms"), (int, float)) and not isinstance(wait_for.get("delay_ms"), bool):
            settings["waitDelay"] = _safe_ms_to_seconds(wait_for["delay_ms"], "wait_for.delay_ms")
        if isinstance(wait_for.get("selector"), str):
            settings["waitSelector"] = wait_for["selector"]
        if isinstance(wait_for.get("text"), str):
            settings["waitText"] = wait_for["text"]
        if isinstance(wait_for.get("timeout_ms"), (int, float)) and not isinstance(wait_for.get("timeout_ms"), bool):
            settings["waitTimeout"] = _safe_ms_to_seconds(wait_for["timeout_ms"], "wait_for.timeout_ms")
        extra_wait = {
            k: v for k, v in wait_for.items()
            if k not in {"event", "delay_ms", "selector", "text", "timeout_ms"}
        }
        if extra_wait:
            settings["wait_for"] = copy.deepcopy(extra_wait)

    env = payload.get("environment")
    if isinstance(env, dict):
        if isinstance(env.get("device"), str):
            settings["device"] = env["device"]
        if isinstance(env.get("color_scheme"), str):
            settings["colorScheme"] = env["color_scheme"]
        if isinstance(env.get("reduced_motion"), str):
            settings["reducedMotion"] = env["reduced_motion"]
        if isinstance(env.get("locale"), str):
            settings["locale"] = env["locale"]
        if isinstance(env.get("timezone"), str):
            settings["timezone"] = env["timezone"]
        if isinstance(env.get("media"), str):
            settings["media"] = env["media"]

    cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
    settings["consent"] = (
        cleanup["consent_mode"]
        if isinstance(cleanup.get("consent_mode"), str) and cleanup["consent_mode"] in _CONSENT
        else "none"
    )
    settings["blockAds"] = _coerce_bool(cleanup.get("block_ads"), False)
    settings["blockTrackers"] = _coerce_bool(cleanup.get("block_trackers"), False)
    settings["blockChats"] = _coerce_bool(cleanup.get("block_chats"), False)
    settings["blockNewsletters"] = _coerce_bool(cleanup.get("block_newsletters"), False)

    if isinstance(payload.get("custom_css"), str):
        settings["customCss"] = payload["custom_css"]
    fail_on = payload.get("fail_on_status")
    if isinstance(fail_on, list) and fail_on:
        settings["failStatuses"] = ",".join(str(s) for s in fail_on)
    raw_headers = payload.get("headers")
    if isinstance(raw_headers, dict) and raw_headers:
        settings["headers"] = json.dumps(raw_headers, ensure_ascii=False)
    elif isinstance(raw_headers, str) and raw_headers.strip():
        settings["headers"] = raw_headers

    for extra_key in _PRESET_CLI_EXTRAS:
        if extra_key in payload:
            settings[extra_key] = copy.deepcopy(payload[extra_key])
    return settings


def preset_settings_to_payload(settings: dict[str, object]) -> dict[str, object]:
    """Convert either a Web UI PresetSettings dict or a RenderRequest dict into RenderRequest shape."""
    web_keys = {
        "width", "height", "fullPage", "preserveViewportWidth", "lazyLoad", "optimizePng",
        "quality", "transparent", "locale", "timezone", "device", "consent", "orientation", "media",
        "videoDuration", "videoFps", "videoBitrate", "videoScroll",
        "waitEvent", "waitDelay", "waitSelector", "waitText", "waitTimeout",
        "blockAds", "blockTrackers", "blockChats", "blockNewsletters",
        "colorScheme", "reducedMotion", "customCss", "failStatuses",
        "clipEnabled", "clipX", "clipY", "clipWidth", "clipHeight",
        "extractMode", "pdfMode", "paperSize", "pdfMargin", "density",
    }
    is_flat_preset = bool(set(settings.keys()) & web_keys) or isinstance(settings.get("diagnostics"), bool)
    if not is_flat_preset:
        copied = copy.deepcopy(settings)
        if "headers" in copied:
            decoded_headers = _decode_preset_headers(copied.get("headers"))
            if decoded_headers:
                copied["headers"] = decoded_headers
            else:
                copied.pop("headers", None)
        return copied

    payload: dict[str, object] = {}
    fmt = str(settings.get("output") or "png").lower()
    fmt = _FORMAT_ALIASES.get(fmt, fmt)
    payload["output"] = fmt
    is_image = fmt in {"png", "jpeg", "webp", "avif"}
    is_video = fmt in {"webm", "mp4", "gif"}
    selector = str(settings.get("selector") or "").strip()
    clip_enabled = _coerce_bool(settings.get("clipEnabled"), False)
    if "fullPage" in settings:
        full_page = _coerce_bool(settings["fullPage"], True)
    else:
        full_page = True
    if is_image and (selector or clip_enabled):
        full_page = False
    if not full_page:
        payload["full_page"] = False
    if is_image and full_page and not selector and not clip_enabled and _coerce_bool(settings.get("preserveViewportWidth"), False):
        payload["preserve_viewport_width"] = True

    viewport: dict[str, object] = {}
    if isinstance(settings.get("width"), (int, float)) and not isinstance(settings.get("width"), bool) and int(settings["width"]) > 0:
        viewport["width"] = int(settings["width"])
    if isinstance(settings.get("height"), (int, float)) and not isinstance(settings.get("height"), bool) and int(settings["height"]) > 0:
        viewport["height"] = int(settings["height"])
    if isinstance(settings.get("density"), (int, float)) and not isinstance(settings.get("density"), bool) and float(settings["density"]) != 1.0:
        viewport["device_scale_factor"] = float(settings["density"])
    if viewport:
        payload["viewport"] = viewport

    if is_image and clip_enabled:
        payload["clip"] = {
            "x": float(settings.get("clipX", 0)),
            "y": float(settings.get("clipY", 0)),
            "width": float(settings.get("clipWidth", 640)),
            "height": float(settings.get("clipHeight", 480)),
        }
    elif is_image and selector:
        payload["selector"] = selector

    image: dict[str, object] = {}
    if fmt in {"jpeg", "webp", "avif"} and isinstance(settings.get("quality"), int) and not isinstance(settings.get("quality"), bool):
        image["quality"] = int(settings["quality"])
    if fmt in {"png", "webp", "avif"} and _coerce_bool(settings.get("transparent"), False):
        image["transparent_background"] = True
    if fmt in {"png", "webp"} and "optimizePng" in settings:
        image["optimize_for_speed"] = _coerce_bool(settings["optimizePng"], False)
    if image:
        payload["image"] = image

    if settings.get("lazyLoad") in _LAZY:
        payload["lazy_load"] = settings["lazyLoad"]
    if _coerce_bool(settings.get("diagnostics"), False):
        payload["diagnostics"] = {"bundle": True}

    if is_video:
        video: dict[str, object] = {}
        if isinstance(settings.get("videoDuration"), (int, float)) and not isinstance(settings.get("videoDuration"), bool):
            video["duration_ms"] = int(round(float(settings["videoDuration"]) * 1000))
        if isinstance(settings.get("videoFps"), int) and not isinstance(settings.get("videoFps"), bool):
            video["fps"] = int(settings["videoFps"])
        if isinstance(settings.get("videoBitrate"), int) and not isinstance(settings.get("videoBitrate"), bool):
            video["bitrate_mbps"] = int(settings["videoBitrate"])
        if _coerce_bool(settings.get("videoScroll"), False):
            video["scroll"] = True
        if full_page and fmt in {"webm", "gif"} and _coerce_bool(settings.get("transparent"), False):
            video["transparent_background"] = True
        if video:
            payload["video"] = video

    if fmt == "pdf":
        pdf: dict[str, object] = {}
        if settings.get("pdfMode") in _PDF_MODES:
            pdf["mode"] = settings["pdfMode"]
        if settings.get("paperSize") in _PAPER:
            pdf["paper_size"] = settings["paperSize"]
        if settings.get("orientation") in _ORIENTATION:
            pdf["orientation"] = settings["orientation"]
        if isinstance(settings.get("pdfMargin"), (int, float)):
            margin = float(settings["pdfMargin"])
            pdf["margins"] = {"top": margin, "right": margin, "bottom": margin, "left": margin}
        if pdf:
            payload["pdf"] = pdf

    side_outputs = settings.get("side_outputs")
    has_text_side = isinstance(side_outputs, list) and any(
        str(item).lower() in {"html", "markdown", "md"} for item in side_outputs
    )
    if (fmt in {"html", "markdown"} or has_text_side) and settings.get("extractMode") in _EXTRACT:
        payload["extract_mode"] = settings["extractMode"]

    wait_for: dict[str, object] = {}
    if settings.get("waitEvent") in _WAIT_EVENTS and settings["waitEvent"] != "load":
        wait_for["event"] = settings["waitEvent"]
    if isinstance(settings.get("waitDelay"), (int, float)) and float(settings["waitDelay"]) > 0:
        wait_for["delay_ms"] = int(round(float(settings["waitDelay"]) * 1000))
    if isinstance(settings.get("waitSelector"), str) and settings["waitSelector"].strip():
        wait_for["selector"] = settings["waitSelector"].strip()
    if isinstance(settings.get("waitText"), str) and settings["waitText"].strip():
        wait_for["text"] = settings["waitText"].strip()
    if isinstance(settings.get("waitTimeout"), (int, float)) and float(settings["waitTimeout"]) != 15:
        wait_for["timeout_ms"] = int(round(float(settings["waitTimeout"]) * 1000))
    if wait_for:
        payload["wait_for"] = wait_for

    env: dict[str, object] = {}
    if settings.get("device") in _DEVICES and settings["device"] != "desktop":
        env["device"] = settings["device"]
    if settings.get("colorScheme") in _COLOR_SCHEMES:
        env["color_scheme"] = settings["colorScheme"]
    if settings.get("reducedMotion") in _REDUCED_MOTION:
        env["reduced_motion"] = settings["reducedMotion"]
    if isinstance(settings.get("locale"), str) and settings["locale"].strip():
        env["locale"] = settings["locale"].strip()
    if isinstance(settings.get("timezone"), str) and settings["timezone"].strip():
        env["timezone"] = settings["timezone"].strip()
    if env:
        payload["environment"] = env

    cleanup: dict[str, object] = {}
    if settings.get("consent") in _CONSENT and settings["consent"] != "none":
        cleanup["consent_mode"] = settings["consent"]
    if _coerce_bool(settings.get("blockAds"), False):
        cleanup["block_ads"] = True
    if _coerce_bool(settings.get("blockTrackers"), False):
        cleanup["block_trackers"] = True
    if _coerce_bool(settings.get("blockChats"), False):
        cleanup["block_chats"] = True
    if _coerce_bool(settings.get("blockNewsletters"), False):
        cleanup["block_newsletters"] = True
    if cleanup:
        payload["cleanup"] = cleanup

    if isinstance(settings.get("customCss"), str) and settings["customCss"].strip():
        payload["custom_css"] = settings["customCss"]

    raw_fail = settings.get("failStatuses")
    if isinstance(raw_fail, str) and raw_fail.strip():
        statuses: list[int] = []
        for token in re.split(r"[,\s]+", raw_fail.strip()):
            if token:
                statuses.append(int(token))
        if statuses:
            payload["fail_on_status"] = statuses

    decoded_headers = _decode_preset_headers(settings.get("headers"))
    if decoded_headers:
        payload["headers"] = decoded_headers

    if isinstance(settings.get("media"), str) and settings["media"] in _MEDIA:
        env = dict(payload.get("environment") or {})  # type: ignore[arg-type]
        env["media"] = settings["media"]
        payload["environment"] = env
    for section_key in ("image", "video", "pdf", "wait_for"):
        extra_section = settings.get(section_key)
        if isinstance(extra_section, dict) and extra_section:
            merged_section = dict(payload.get(section_key) or {})  # type: ignore[arg-type]
            merged_section.update(copy.deepcopy(extra_section))
            payload[section_key] = merged_section
    extra_diag = settings.get("diagnostics_extra")
    if isinstance(extra_diag, dict) and extra_diag:
        merged_diag = dict(payload.get("diagnostics") or {})  # type: ignore[arg-type]
        merged_diag.update(copy.deepcopy(extra_diag))
        payload["diagnostics"] = merged_diag
    for extra_key in _PRESET_CLI_EXTRAS:
        if extra_key in settings:
            payload[extra_key] = copy.deepcopy(settings[extra_key])
    return payload


def _is_loopback_server(server: str | None) -> bool:
    if not server:
        return True
    try:
        root = api_root(server)
    except ValueError:
        return False
    host = (urlsplit(root).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def load_preset_payload(name: str, *, server: str | None = None) -> dict[str, object]:
    """Fetch a named preset from /local/presets or local presets.json fallback (loopback only)."""
    target_name = name.strip()
    if not target_name:
        raise ValueError("Preset name cannot be empty.")

    presets: list[dict[str, object]] | None = None
    if server:
        try:
            root = api_root(server)
            resp = open_response(
                root + "/local/presets",
                method="GET",
                payload=None,
                headers={"Accept": "application/json"},
                timeout=3.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            raw = doc.get("presets")
            if isinstance(raw, list):
                presets = [item for item in raw if isinstance(item, dict)]
        except (CaptureError, ValueError):
            presets = None

    if presets is None and _is_loopback_server(server):
        for candidate in _local_presets_candidates():
            if not candidate.is_file():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, list):
                presets = [item for item in raw if isinstance(item, dict)]
                break

    if presets is None:
        if server and not _is_loopback_server(server):
            raise ValueError(
                f"Preset {target_name!r} could not be fetched from remote server "
                "(local presets.json fallback is disabled for remote servers)."
            )
        raise ValueError(f"Preset {target_name!r} was not found (no reachable server or local presets file).")
    for item in presets:
        if item.get("name") == target_name and isinstance(item.get("settings"), dict):
            return preset_settings_to_payload(item["settings"])
    raise ValueError(f"Preset {target_name!r} was not found.")


def _action_from(
    kind: str,
    values: object,
    *,
    stdin_cache: dict[str, str] | None = None,
) -> dict[str, object]:
    if kind == "json":
        try:
            parsed = json.loads(str(values))
        except json.JSONDecodeError as exc:
            raise ValueError("--action is not a JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--action must be a JSON object")
        return parsed
    if kind in {"click", "hover", "hide", "scroll_to", "action_wait"}:
        action_type = {
            "click": "click",
            "hover": "hover",
            "hide": "hide",
            "scroll_to": "scroll",
            "action_wait": "wait",
        }[kind]
        return {"type": action_type, "selector": values}
    if kind == "action_wait_text":
        return {"type": "wait", "value": str(values)}
    if kind == "fill":
        selector, value = values  # type: ignore[misc]
        return {"type": "fill", "selector": selector, "value": value}
    if kind == "press":
        return {"type": "press", "key": values}
    if kind == "press_on":
        selector, key = values  # type: ignore[misc]
        return {"type": "press", "selector": selector, "key": key}
    if kind == "select":
        items = list(values)  # type: ignore[arg-type]
        return {"type": "select", "selector": items[0], "values": items[1:]}
    if kind == "scroll":
        x, y = values  # type: ignore[misc]
        return {"type": "scroll", "x": x, "y": y}
    if kind == "js":
        return {"type": "javascript", "value": values}
    if kind == "js_file":
        return {"type": "javascript", "value": _read_text(str(values), stdin_cache=stdin_cache)}
    if kind == "pause":
        return {"type": "wait", "delay_ms": int(values)}  # type: ignore[arg-type]
    raise ValueError(f"Unknown action {kind}")


def _apply_actions(
    payload: dict[str, object],
    steps: list[tuple[str, object]] | None,
    *,
    stdin_cache: dict[str, str] | None = None,
) -> None:
    actions: list[dict[str, object]] = []
    delay = 0
    timeout: int | None = None
    for kind, values in steps or []:
        if kind == "action_delay":
            delay = int(values)  # type: ignore[arg-type]
            continue
        if kind == "action_timeout":
            timeout = int(values)  # type: ignore[arg-type]
            continue
        action = _action_from(kind, values, stdin_cache=stdin_cache)
        if delay and "delay_ms" not in action:
            action["delay_ms"] = delay
        if timeout is not None and "timeout_ms" not in action:
            action["timeout_ms"] = timeout
        actions.append(action)
    if actions:
        payload["actions"] = actions


def _load_base_document(
    args: argparse.Namespace,
    *,
    stdin_cache: dict[str, str] | None = None,
) -> tuple[dict[str, object], bool]:
    base: dict[str, object] = {}
    has_base = False
    preset_name = getattr(args, "preset", None)
    if preset_name:
        base = load_preset_payload(preset_name, server=getattr(args, "server", None))
        has_base = True
    from_json = getattr(args, "from_json", None)
    if from_json:
        raw = _read_text(from_json, stdin_cache=stdin_cache)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--from-json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--from-json must contain a JSON object.")
        base = _deep_merge(base, parsed) if has_base else parsed
        has_base = True
    return base, has_base


def build_payload(
    args: argparse.Namespace,
    *,
    override_url: str | None = None,
    require_source: bool = True,
    base_document: dict[str, object] | None = None,
    allow_default_cookie_domain: bool = True,
    stdin_cache: dict[str, str] | None = None,
) -> dict[str, object]:
    if base_document is not None:
        base, has_base = copy.deepcopy(base_document), True
    else:
        base, has_base = _load_base_document(args, stdin_cache=stdin_cache)

    payload: dict[str, object] = {}
    cli_target = getattr(args, "target", None)
    cli_url = getattr(args, "url", None)
    if override_url is not None:
        url: str | None = override_url
    else:
        if cli_url and cli_target:
            raise ValueError("Pass the URL once, either as the argument or with --url.")
        url = cli_url or cli_target

    html = _one_of(args.html, args.html_file, "Use either --html or --html-file.", stdin_cache=stdin_cache)
    markdown = _one_of(args.markdown, args.markdown_file, "Use either --markdown or --markdown-file.", stdin_cache=stdin_cache)
    cli_sources = (url is not None, html is not None, markdown is not None)
    if sum(cli_sources) > 1:
        raise ValueError("Pass a URL, --html, or --markdown.")

    if sum(cli_sources) == 1:
        for source_key in ("url", "html", "markdown"):
            base.pop(source_key, None)
        if url is not None:
            payload["url"] = normalize_url(url)
        if html is not None:
            payload["html"] = html
        if markdown is not None:
            payload["markdown"] = markdown
    elif require_source:
        base_sources = (
            base.get("url") is not None,
            base.get("html") is not None,
            base.get("markdown") is not None,
        )
        if sum(base_sources) != 1:
            raise ValueError("Pass a URL, --html, or --markdown.")

    effective_url = payload.get("url") or base.get("url")
    default_domain: str | None = None
    default_secure = False
    if allow_default_cookie_domain and isinstance(effective_url, str) and effective_url:
        parsed_target = urlsplit(effective_url)
        default_domain = parsed_target.hostname
        default_secure = parsed_target.scheme.lower() == "https"

    if args.base_url:
        payload["base_url"] = normalize_url(args.base_url)

    output_arg = getattr(args, "output", None)
    if args.format:
        payload["output"] = infer_format(output_arg, args.format)
    elif output_arg and output_arg != "-" and Path(output_arg).suffix.lower() in _SUFFIX_FORMAT:
        payload["output"] = infer_format(output_arg, None)
    elif not has_base or "output" not in base:
        payload["output"] = infer_format(output_arg, None)

    if args.engine:
        payload["engine"] = args.engine
    _put(payload, "full_page", args.full_page, True, force_default=has_base)
    viewport: dict[str, object] = {}
    if args.viewport:
        viewport["width"], viewport["height"] = args.viewport
    if args.scale is not None:
        viewport["device_scale_factor"] = args.scale
    if viewport:
        payload["viewport"] = viewport
    if args.named_viewport:
        payload["viewports"] = list(args.named_viewport)
    environment: dict[str, object] = {}
    if args.device:
        environment["device"] = args.device
    if args.color_scheme:
        environment["color_scheme"] = args.color_scheme
    if args.reduced_motion:
        environment["reduced_motion"] = args.reduced_motion
    if args.media:
        environment["media"] = args.media
    if args.locale:
        environment["locale"] = args.locale
    if args.timezone:
        environment["timezone"] = args.timezone
    if environment:
        payload["environment"] = environment
    network: dict[str, object] = {}
    _put(network, "java_script_enabled", args.javascript, True, force_default=has_base)
    if args.user_agent:
        network["user_agent"] = args.user_agent
    if args.geolocation:
        geo: dict[str, object] = {
            "latitude": args.geolocation[0],
            "longitude": args.geolocation[1],
        }
        if args.geo_accuracy is not None:
            geo["accuracy"] = args.geo_accuracy
        network["geolocation"] = geo
    elif args.geo_accuracy is not None:
        raise ValueError("--geo-accuracy requires --geolocation.")
    if args.proxy:
        proxy: dict[str, object] = {"server": args.proxy}
        if args.proxy_username:
            proxy["username"] = args.proxy_username
        password = args.proxy_password
        if not password:
            password = os.environ.get("VIPERCAPTURE_PROXY_PASSWORD", "")
        if password:
            proxy["password"] = password
        if args.proxy_bypass:
            proxy["bypass"] = args.proxy_bypass
        network["proxy"] = proxy
    elif any((args.proxy_username, args.proxy_password, args.proxy_bypass)):
        raise ValueError("--proxy-username, --proxy-password, and --proxy-bypass require --proxy.")

    cookies: list[dict[str, object]] = []
    if getattr(args, "cookie_file", None):
        for cookie_path in args.cookie_file:
            cookies.extend(
                parse_cookie_file_content(
                    _read_text(cookie_path, stdin_cache=stdin_cache),
                    default_domain=default_domain,
                    default_secure=default_secure,
                )
            )
    if args.cookie:
        cookies.extend(parse_cookie(item) for item in args.cookie)
    if cookies:
        network["cookies"] = cookies
    if args.block_url:
        network["block_url_patterns"] = list(args.block_url)
    if args.block_resource:
        network["block_resource_types"] = list(args.block_resource)
    _put(network, "bypass_csp", args.bypass_csp, False, force_default=has_base)
    _put(network, "ignore_https_errors", args.ignore_https_errors, False, force_default=has_base)
    if network:
        payload["network"] = network
    _apply_actions(payload, args.steps, stdin_cache=stdin_cache)
    assertions: dict[str, object] = {}
    if args.assert_includes:
        assertions["content_includes"] = list(args.assert_includes)
    if args.assert_excludes:
        assertions["content_excludes"] = list(args.assert_excludes)
    if args.assert_request_failure:
        assertions["request_failures"] = list(args.assert_request_failure)
    if assertions:
        payload["assertions"] = assertions
    if args.element:
        payload["elements"] = [{"selector": selector} for selector in args.element]
    if args.side:
        payload["side_outputs"] = list(args.side)
    if getattr(args, "webhook", None):
        payload["delivery"] = {"webhook_url": args.webhook}
    diagnostics: dict[str, object] = {}
    if args.diagnostics or args.har or args.trace or args.warc:
        diagnostics["bundle"] = True
    elif has_base and args.diagnostics is False:
        diagnostics["bundle"] = False
    _put(diagnostics, "include_har", args.har, False, force_default=has_base)
    _put(diagnostics, "include_trace", args.trace, False, force_default=has_base)
    _put(diagnostics, "include_warc", args.warc, False, force_default=has_base)
    _put(diagnostics, "include_console", args.include_console, True, force_default=has_base)
    _put(diagnostics, "include_network", args.include_network, True, force_default=has_base)
    if diagnostics:
        payload["diagnostics"] = diagnostics
    deterministic: dict[str, object] = {}
    enabled = args.deterministic
    if enabled is not False and (
        enabled or args.timestamp_ms is not None or args.seed is not None or args.wait_for_fonts is False
    ):
        deterministic["enabled"] = True
    elif has_base and enabled is False:
        deterministic["enabled"] = False
    if deterministic.get("enabled") is True:
        if args.timestamp_ms is not None:
            deterministic["timestamp_ms"] = args.timestamp_ms
        if args.seed is not None:
            deterministic["random_seed"] = args.seed
        _put(deterministic, "wait_for_fonts", args.wait_for_fonts, True, force_default=has_base)
    if deterministic:
        payload["deterministic"] = deterministic
    if args.certify:
        payload["certification"] = {"enabled": True}
    elif has_base and args.certify is False:
        payload["certification"] = {"enabled": False}
    if args.slice_height is not None:
        slices: dict[str, object] = {"height": args.slice_height}
        if args.slice_overlap is not None:
            slices["overlap"] = args.slice_overlap
        payload["slices"] = slices
    elif args.slice_overlap is not None:
        raise ValueError("--slice-overlap requires --slice-height.")
    if args.profile_id:
        payload["profile_id"] = args.profile_id
    _put(payload, "save_profile", args.save_profile, False, force_default=has_base)
    video: dict[str, object] = {}
    if args.video_duration_ms is not None:
        video["duration_ms"] = args.video_duration_ms
    if args.fps is not None:
        video["fps"] = args.fps
    if args.bitrate_mbps is not None:
        video["bitrate_mbps"] = args.bitrate_mbps
    _put(video, "scroll", args.video_scroll, False, force_default=has_base)
    if args.scroll_step is not None:
        video["scroll_step"] = args.scroll_step
    if args.scroll_delay_ms is not None:
        video["scroll_delay_ms"] = args.scroll_delay_ms
    _put(video, "transparent_background", args.video_transparent, False, force_default=has_base)
    if video:
        payload["video"] = video
    _put(payload, "preserve_viewport_width", args.preserve_viewport_width, False, force_default=has_base)
    if args.lazy_load:
        payload["lazy_load"] = args.lazy_load
    if args.profile:
        payload["profile"] = args.profile
    if args.cache_key:
        payload["cache_key"] = args.cache_key
    if args.cache is True or args.cache_key:
        payload["cache"] = True
    elif has_base and args.cache is False:
        payload["cache"] = False
    if args.selector:
        payload["selector"] = args.selector
    if args.clip:
        x, y, width, height = args.clip
        payload["clip"] = {"x": x, "y": y, "width": width, "height": height}
    css = _one_of(args.css, args.css_file, "Use either --css or --css-file.", stdin_cache=stdin_cache)
    if css is not None:
        payload["custom_css"] = css
    if args.fail_on:
        payload["fail_on_status"] = list(args.fail_on)
    image: dict[str, object] = {}
    if args.quality is not None:
        image["quality"] = args.quality
    if args.image_width is not None:
        image["width"] = args.image_width
    if args.image_height is not None:
        image["height"] = args.image_height
    _put(image, "transparent_background", args.transparent, False, force_default=has_base)
    if args.optimize_for_speed is not None:
        image["optimize_for_speed"] = args.optimize_for_speed
    if args.thumbnail:
        image["thumbnails"] = list(args.thumbnail)
    if image:
        payload["image"] = image
    pdf: dict[str, object] = {}
    if args.pdf_mode:
        pdf["mode"] = args.pdf_mode
    if args.paper:
        pdf["paper_size"] = args.paper
    if args.orientation:
        pdf["orientation"] = args.orientation
    _put(pdf, "print_background", args.pdf_background, True, force_default=has_base)
    if args.pdf_tagged is not None:
        pdf["tagged"] = args.pdf_tagged
    if args.pdf_margin:
        margins = list(args.pdf_margin)
        if len(margins) == 1:
            top = right = bottom = left = margins[0]
        elif len(margins) == 4:
            top, right, bottom, left = margins
        else:
            raise ValueError("--pdf-margin needs one value or top right bottom left.")
        pdf["margins"] = {"top": top, "right": right, "bottom": bottom, "left": left}
    header = _one_of(args.pdf_header, args.pdf_header_file, "Use either --pdf-header or --pdf-header-file.", stdin_cache=stdin_cache)
    footer = _one_of(args.pdf_footer, args.pdf_footer_file, "Use either --pdf-footer or --pdf-footer-file.", stdin_cache=stdin_cache)
    if header is not None:
        pdf["header_template"] = header
    if footer is not None:
        pdf["footer_template"] = footer
    if args.page_ranges:
        pdf["page_ranges"] = args.page_ranges
    if pdf:
        payload["pdf"] = pdf
    if args.extract:
        payload["extract_mode"] = args.extract
    _put(payload, "include_shadow_dom", args.shadow_dom, False, force_default=has_base)
    if args.header:
        headers: dict[str, str] = {}
        seen: set[str] = set()
        for item in args.header:
            name, value = parse_header(item)
            lowered = name.lower()
            if lowered in seen:
                raise ValueError(f"Header {name} is repeated.")
            seen.add(lowered)
            headers[name] = value
        payload["headers"] = headers
    wait_for: dict[str, object] = {}
    if args.wait_selector:
        wait_for["selector"] = args.wait_selector
    if args.wait_text:
        wait_for["text"] = args.wait_text
    if args.wait_event:
        wait_for["event"] = args.wait_event
    if args.wait_state:
        wait_for["selector_state"] = args.wait_state
    _put(wait_for, "images", args.wait_images, False, force_default=has_base)
    if args.delay_ms is not None:
        wait_for["delay_ms"] = args.delay_ms
    if args.timeout_ms is not None:
        wait_for["timeout_ms"] = args.timeout_ms
    if wait_for:
        payload["wait_for"] = wait_for
    cleanup: dict[str, object] = {}
    if args.consent:
        cleanup["consent_mode"] = args.consent
    _put(cleanup, "block_ads", args.block_ads, False, force_default=has_base)
    _put(cleanup, "block_trackers", args.block_trackers, False, force_default=has_base)
    _put(cleanup, "block_chats", args.block_chats, False, force_default=has_base)
    _put(cleanup, "block_newsletters", args.block_newsletters, False, force_default=has_base)
    if cleanup:
        payload["cleanup"] = cleanup
    _put(payload, "stealth", args.stealth, True, force_default=has_base)
    captcha: dict[str, object] = {}
    if args.captcha:
        captcha["action"] = args.captcha
    if args.captcha_solver:
        captcha["solver"] = args.captcha_solver
    if args.captcha_timeout_ms is not None:
        captcha["timeout_ms"] = args.captcha_timeout_ms
    if captcha:
        payload["captcha"] = captcha
    _put(payload, "proceed_on_captcha", args.proceed_on_captcha, False, force_default=has_base)
    if has_base:
        return _deep_merge(base, payload)
    return payload


def _validation_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    parts: list[str] = []
    for error in errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = str(error.get("msg", "invalid"))
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or "invalid render request"


_FAST_VALID_KEYS = frozenset({
    "url", "html", "markdown", "output", "engine", "full_page", "viewport",
    "environment", "wait_for", "actions", "cache", "cache_key", "lazy_load",
    "profile", "stealth",
})
_FAST_IMAGE_OUTPUTS = frozenset({"png", "jpeg", "webp", "avif"})
_FAST_SIMPLE_OUTPUTS = frozenset({"png", "jpeg", "webp", "avif", "html", "markdown", "metadata"})
_FAST_ENGINES = frozenset(_ENGINES)
_FAST_DEVICES = frozenset(_DEVICES)
_FAST_COLOR_SCHEMES = frozenset(_COLOR_SCHEMES)
_FAST_REDUCED_MOTION = frozenset(_REDUCED_MOTION)
_FAST_MEDIA = frozenset(_MEDIA)
_FAST_LAZY = frozenset(_LAZY)
_FAST_PROFILES = frozenset(_PROFILES)
_FAST_WAIT_EVENTS = frozenset(_WAIT_EVENTS)
_FAST_WAIT_STATES = frozenset(_WAIT_STATES)
_FAST_LOCALE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_FAST_CACHE_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_FAST_SIMPLE_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_FAST_SELECTOR_ACTIONS = frozenset({"click", "hover", "fill", "select", "hide"})
_FAST_ALL_ACTIONS = frozenset({
    "click", "hover", "fill", "press", "select", "scroll", "wait", "hide", "javascript",
})


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_number(value: object) -> bool:
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    return False


def _fast_validate_simple_payload(payload: dict[str, object]) -> bool:
    """Return True when payload is guaranteed valid under RenderRequest.

    Anything outside this strict subset returns False and falls back to full
    Pydantic validation so error messages and edge cases remain identical.
    """
    if not isinstance(payload, dict) or not payload.keys() <= _FAST_VALID_KEYS:
        return False
    has_url = "url" in payload
    has_html = "html" in payload
    has_md = "markdown" in payload
    if has_url + has_html + has_md != 1:
        return False
    if has_url:
        url = payload["url"]
        if not isinstance(url, str) or not url or len(url) > 2083:
            return False
        if any(ord(ch) <= 32 or ord(ch) >= 127 for ch in url):
            return False
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        host = parsed.hostname or ""
        if not host or len(host) > 253:
            return False
        if host != "localhost":
            if "." not in host:
                return False
            labels = host.split(".")
            if any(
                not _FAST_SIMPLE_LABEL.fullmatch(label) or label.lower().startswith("xn--")
                for label in labels
            ):
                return False
            if not labels[-1].isalpha() or len(labels[-1]) < 2:
                return False
        try:
            port = parsed.port
        except ValueError:
            return False
        if port is not None and not (1 <= port <= 65535):
            return False
    if has_html:
        html = payload["html"]
        if not isinstance(html, str) or len(html.encode("utf-8")) > 5 * 1024 * 1024:
            return False
    if has_md:
        md = payload["markdown"]
        if not isinstance(md, str) or len(md.encode("utf-8")) > 5 * 1024 * 1024:
            return False

    output = payload.get("output", "png")
    if not isinstance(output, str) or output not in _FAST_SIMPLE_OUTPUTS:
        return False
    if "engine" in payload:
        if not isinstance(payload["engine"], str) or payload["engine"] not in _FAST_ENGINES:
            return False
    if "full_page" in payload and type(payload["full_page"]) is not bool:
        return False
    if "stealth" in payload and type(payload["stealth"]) is not bool:
        return False
    if "lazy_load" in payload and (
        not isinstance(payload["lazy_load"], str) or payload["lazy_load"] not in _FAST_LAZY
    ):
        return False
    if "profile" in payload and (
        not isinstance(payload["profile"], str) or payload["profile"] not in _FAST_PROFILES
    ):
        return False

    cache = payload.get("cache", False)
    if type(cache) is not bool:
        return False
    if cache and output not in _FAST_IMAGE_OUTPUTS:
        return False
    if "cache_key" in payload:
        key = payload["cache_key"]
        if not cache or not isinstance(key, str) or not _FAST_CACHE_KEY.fullmatch(key):
            return False

    if "viewport" in payload:
        vp = payload["viewport"]
        if not isinstance(vp, dict) or not vp.keys() <= {"width", "height", "device_scale_factor"}:
            return False
        if "width" in vp and (not _is_int(vp["width"]) or not (1 <= vp["width"] <= 65_535)):
            return False
        if "height" in vp and (not _is_int(vp["height"]) or not (1 <= vp["height"] <= 65_535)):
            return False
        if "device_scale_factor" in vp:
            dsf = vp["device_scale_factor"]
            if not _is_number(dsf) or not (0.1 <= dsf <= 8.0):
                return False

    if "environment" in payload:
        env = payload["environment"]
        if not isinstance(env, dict) or not env.keys() <= {
            "device", "color_scheme", "reduced_motion", "media", "locale"
        }:
            return False
        if "device" in env and (not isinstance(env["device"], str) or env["device"] not in _FAST_DEVICES):
            return False
        if "color_scheme" in env and (
            not isinstance(env["color_scheme"], str) or env["color_scheme"] not in _FAST_COLOR_SCHEMES
        ):
            return False
        if "reduced_motion" in env and (
            not isinstance(env["reduced_motion"], str) or env["reduced_motion"] not in _FAST_REDUCED_MOTION
        ):
            return False
        if "media" in env and (not isinstance(env["media"], str) or env["media"] not in _FAST_MEDIA):
            return False
        if "locale" in env:
            loc = env["locale"]
            if not isinstance(loc, str) or not (2 <= len(loc) <= 64) or not _FAST_LOCALE.fullmatch(loc):
                return False

    if "wait_for" in payload:
        wf = payload["wait_for"]
        if not isinstance(wf, dict) or not wf.keys() <= {
            "event", "selector", "selector_state", "text", "images", "delay_ms", "timeout_ms"
        }:
            return False
        if "event" in wf and (not isinstance(wf["event"], str) or wf["event"] not in _FAST_WAIT_EVENTS):
            return False
        if "selector" in wf:
            sel = wf["selector"]
            if not isinstance(sel, str) or not (1 <= len(sel) <= 2_048):
                return False
        if "selector_state" in wf:
            st = wf["selector_state"]
            if not isinstance(st, str) or st not in _FAST_WAIT_STATES:
                return False
            if st != "visible" and "selector" not in wf:
                return False
        if "text" in wf:
            txt = wf["text"]
            if not isinstance(txt, str) or not (1 <= len(txt) <= 4_096):
                return False
        if "images" in wf and type(wf["images"]) is not bool:
            return False
        if "delay_ms" in wf and (not _is_int(wf["delay_ms"]) or not (0 <= wf["delay_ms"] <= 15_000)):
            return False
        if "timeout_ms" in wf and (not _is_int(wf["timeout_ms"]) or not (1 <= wf["timeout_ms"] <= 30_000)):
            return False

    if "actions" in payload:
        actions = payload["actions"]
        if not isinstance(actions, list) or len(actions) > 32:
            return False
        action_keys = {"type", "selector", "value", "values", "key", "x", "y", "delay_ms", "timeout_ms"}
        for act in actions:
            if not isinstance(act, dict) or not act.keys() <= action_keys:
                return False
            kind = act.get("type")
            if not isinstance(kind, str) or kind not in _FAST_ALL_ACTIONS:
                return False
            sel = act.get("selector")
            if "selector" in act and (not isinstance(sel, str) or not (1 <= len(sel) <= 2_048)):
                return False
            val = act.get("value")
            if "value" in act and (not isinstance(val, str) or len(val) > 16_384):
                return False
            vals = act.get("values")
            if "values" in act:
                if (
                    not isinstance(vals, list)
                    or not (1 <= len(vals) <= 32)
                    or any(not isinstance(item, str) or len(item) > 16_384 for item in vals)
                ):
                    return False
            key = act.get("key")
            if "key" in act and (not isinstance(key, str) or not (1 <= len(key) <= 128)):
                return False
            if "x" in act and (not _is_number(act["x"]) or not (-100_000 <= act["x"] <= 100_000)):
                return False
            if "y" in act and (not _is_number(act["y"]) or not (-100_000 <= act["y"] <= 100_000)):
                return False
            if "delay_ms" in act and (not _is_int(act["delay_ms"]) or not (0 <= act["delay_ms"] <= 15_000)):
                return False
            if "timeout_ms" in act and (not _is_int(act["timeout_ms"]) or not (1 <= act["timeout_ms"] <= 30_000)):
                return False
            if kind in _FAST_SELECTOR_ACTIONS and "selector" not in act:
                return False
            if kind == "fill" and "value" not in act:
                return False
            if kind == "press" and "key" not in act:
                return False
            if kind == "select" and not vals:
                return False
            if kind == "scroll" and "selector" not in act and "x" not in act and "y" not in act:
                return False
            if kind == "wait" and "selector" not in act and "value" not in act and not act.get("delay_ms"):
                return False
            if kind == "javascript" and "value" not in act:
                return False
    return True


_FALLBACK_DICT_SECTIONS = frozenset({
    "viewport", "environment", "network", "assertions", "delivery", "diagnostics",
    "deterministic", "certification", "wait_for", "cleanup", "captcha", "headers",
})
_FALLBACK_NULLABLE_DICT_SECTIONS = frozenset({"image", "slices", "video", "clip", "pdf"})
_FALLBACK_LIST_SECTIONS = frozenset({"actions", "fail_on_status"})
_FALLBACK_NULLABLE_LIST_SECTIONS = frozenset({"viewports", "elements", "side_outputs"})
_FALLBACK_NULLABLE_STR_FIELDS = frozenset({
    "url", "html", "markdown", "base_url", "profile_id", "cache_key", "selector", "custom_css",
})
_FALLBACK_BOOL_FIELDS = frozenset({
    "full_page", "stealth", "cache", "include_shadow_dom", "preserve_viewport_width",
    "save_profile", "proceed_on_captcha",
})
_FALLBACK_ALLOWED_KEYS = (
    _FALLBACK_DICT_SECTIONS
    | _FALLBACK_NULLABLE_DICT_SECTIONS
    | _FALLBACK_LIST_SECTIONS
    | _FALLBACK_NULLABLE_LIST_SECTIONS
    | _FALLBACK_NULLABLE_STR_FIELDS
    | _FALLBACK_BOOL_FIELDS
    | frozenset({"output", "engine", "lazy_load", "profile", "extract_mode"})
)
_CONTRACT_OUTPUTS = frozenset({
    "png", "jpeg", "webp", "avif", "pdf", "html", "markdown", "metadata", "webm", "mp4", "gif",
})
_CONTRACT_SIDE_OUTPUTS = frozenset(_SIDES)
_CONTRACT_RESOURCES = frozenset(_RESOURCES)
_CONTRACT_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CONTRACT_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONTRACT_CACHE_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONTRACT_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_CONTRACT_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CONTRACT_SOLVER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTRACT_SAME_SITE_RE = re.compile(r"^(?:Strict|Lax|None)$")
_CONTRACT_PAGE_RANGES_RE = re.compile(r"^\s*\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*\s*$")
_CONTRACT_BLOCKED_HEADERS = frozenset({
    "connection", "content-length", "forwarded", "host", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
})
_CONTRACT_BLOCKED_HEADER_PREFIXES = ("proxy-", "sec-", "x-forwarded-")
_CONTRACT_PAPER_DIMS = {
    "A0": (33.1, 46.8),
    "A1": (23.4, 33.1),
    "A2": (16.5, 23.4),
    "A3": (11.7, 16.5),
    "A4": (8.27, 11.69),
    "A5": (5.83, 8.27),
    "A6": (4.13, 5.83),
    "Legal": (8.5, 14.0),
    "Letter": (8.5, 11.0),
    "Tabloid": (11.0, 17.0),
}


def _check_strict_keys(obj: dict[str, object], allowed: frozenset[str], field: str) -> None:
    unknown = sorted(set(obj.keys()) - allowed)
    if unknown:
        raise ValueError(f"Unknown {field} field(s): {', '.join(unknown)}")


def _parse_contract_str(
    value: object,
    field: str,
    *,
    min_len: int | None = None,
    max_len: int | None = None,
    pattern: re.Pattern[str] | None = None,
) -> str:
    """Validate and coerce a string matching Pydantic v2 non-strict str semantics."""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{field} must be a UTF-8 string.") from exc
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    if min_len is not None and len(value) < min_len:
        raise ValueError(f"{field} must be at least {min_len} characters.")
    if max_len is not None and len(value) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters.")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"{field} has an invalid format.")
    return value


_CONTRACT_LIST_TYPES = (list, tuple, set, frozenset, deque)
_CONTRACT_FORBIDDEN_HOST_CHARS = frozenset("#/:<>?@[\\]^|%")
_CONTRACT_WHATWG_C0_SPACE = "".join(chr(cp) for cp in range(0x21))
_CONTRACT_RUST_WS = (
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
_CONTRACT_IDNA_DOTS = ("\u3002", "\uff0e", "\uff61")


def _validate_punycode_label_syntax(label: str, field: str) -> None:
    """Check basic RFC 3492 punycode syntax on xn-- labels without emulating full UTS-46 tables."""
    if not label.lower().startswith("xn--"):
        return
    raw_puny = label[4:]
    if not raw_puny or not raw_puny.isascii():
        raise ValueError(f"{field} has an invalid punycode host label.")
    try:
        decoded = raw_puny.lower().encode("ascii").decode("punycode")
        reencoded = decoded.encode("punycode").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"{field} has an invalid punycode host label.") from exc
    if (
        decoded.isascii()
        or reencoded.lower() != raw_puny.lower()
        or any(
            ch.isspace()
            or ord(ch) <= 0x20
            or 0x7F <= ord(ch) <= 0x9F
            or 0xD800 <= ord(ch) <= 0xDFFF
            or ch in _CONTRACT_FORBIDDEN_HOST_CHARS
            for ch in decoded
        )
    ):
        raise ValueError(f"{field} has an invalid punycode host label.")


def _parse_whatwg_ipv4_number(part: str) -> tuple[bool, int | None]:
    """Parse one WHATWG IPv4 number segment. Returns (is_numeric_syntax, value_or_None)."""
    if not part:
        return (False, None)
    lowered = part.lower()
    if lowered.startswith("0x"):
        digits = lowered[2:]
        if not digits:
            return (True, 0)
        if all(ch in "0123456789abcdef" for ch in digits):
            return (True, int(digits, 16))
        return (False, None)
    if not part.isascii() or not part.isdigit():
        return (False, None)
    if len(part) > 1 and part[0] == "0":
        if all(ch in "01234567" for ch in part):
            return (True, int(part, 8))
        return (True, None)
    return (True, int(part, 10))


def _parse_contract_enum(value: object, field: str, allowed: frozenset[str] | tuple[str, ...]) -> str:
    """Validate and coerce a str Enum matching Pydantic v2 non-strict semantics."""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{field} is invalid.") from exc
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}")
    return value


def _parse_contract_http_url(value: object, field: str) -> str:
    """Best-effort stdlib HTTP(S) URL check when Pydantic is unavailable.

    Validates UTF-8 encoding, 2083-byte cap, WHATWG C0/space trimming, scheme,
    authority/port, IPv6 (no zone IDs), IPv4, forbidden host characters, and
    basic punycode label syntax. Full UTS-46 IDNA / Unicode 15.1 host validation
    is deferred to RenderRequest (Pydantic HttpUrl) and the server so valid
    internationalized domains are never falsely rejected in stdlib-only mode.
    """
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{field} must be a valid HTTP(S) URL.") from exc
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a valid HTTP(S) URL.")
    try:
        raw_utf8 = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8.") from exc
    if len(raw_utf8) > 2_083:
        raise ValueError(f"{field} must be at most 2083 UTF-8 bytes.")
    cleaned = value.strip(_CONTRACT_WHATWG_C0_SPACE).replace("\t", "").replace("\r", "").replace("\n", "")
    if not cleaned:
        raise ValueError(f"{field} must be a valid HTTP(S) URL.")
    colon_idx = cleaned.find(":")
    if colon_idx <= 0:
        raise ValueError(f"{field} must be a valid HTTP(S) URL.")
    scheme = cleaned[:colon_idx].lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{field} must be a valid HTTP(S) URL.")
    after_scheme = cleaned[colon_idx + 1 :].lstrip("/\\")
    if not after_scheme:
        raise ValueError(f"{field} must include a host.")
    auth_end = len(after_scheme)
    for sep in ("/", "\\", "?", "#"):
        idx = after_scheme.find(sep)
        if 0 <= idx < auth_end:
            auth_end = idx
    authority = after_scheme[:auth_end]
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    if not authority:
        raise ValueError(f"{field} must include a host.")
    if authority.startswith("["):
        end_bracket = authority.find("]")
        if end_bracket < 0:
            raise ValueError(f"{field} has an invalid IPv6 host.")
        ipv6_text = authority[1:end_bracket]
        rest = authority[end_bracket + 1 :]
        if "%" in ipv6_text:
            raise ValueError(f"{field} IPv6 host may not include a zone identifier.")
        if rest:
            if not rest.startswith(":"):
                raise ValueError(f"{field} has an invalid IPv6 host.")
            port_str = rest[1:]
            if port_str and (not port_str.isascii() or not port_str.isdigit() or int(port_str) > 65_535):
                raise ValueError(f"{field} has an invalid port number.")
        try:
            ipaddress.IPv6Address(ipv6_text)
        except ValueError as exc:
            raise ValueError(f"{field} has an invalid IPv6 host.") from exc
        return cleaned
    if "[" in authority or "]" in authority:
        raise ValueError(f"{field} has an invalid host.")
    if ":" in authority:
        raw_host, port_str = authority.split(":", 1)
        if port_str and (not port_str.isascii() or not port_str.isdigit() or int(port_str) > 65_535):
            raise ValueError(f"{field} has an invalid port number.")
    else:
        raw_host = authority
    if not raw_host:
        raise ValueError(f"{field} must include a host.")
    if "%" in raw_host:
        i = 0
        decoded_bytes = bytearray()
        while i < len(raw_host):
            if raw_host[i] == "%":
                if i + 2 >= len(raw_host):
                    raise ValueError(f"{field} has an invalid percent-escape in host.")
                hex_pair = raw_host[i + 1 : i + 3]
                if not all(ch in "0123456789abcdefABCDEF" for ch in hex_pair):
                    raise ValueError(f"{field} has an invalid percent-escape in host.")
                decoded_bytes.append(int(hex_pair, 16))
                i += 3
            else:
                decoded_bytes.extend(raw_host[i].encode("utf-8"))
                i += 1
        try:
            host_text = decoded_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{field} has an invalid host.") from exc
    else:
        host_text = raw_host
    if any(
        ch.isspace()
        or ord(ch) <= 0x20
        or ord(ch) == 0x7F
        or ch in _CONTRACT_FORBIDDEN_HOST_CHARS
        for ch in host_text
    ):
        raise ValueError(f"{field} contains forbidden host characters.")
    for dot in _CONTRACT_IDNA_DOTS:
        if dot in host_text:
            host_text = host_text.replace(dot, ".")
    if not host_text:
        raise ValueError(f"{field} must include a host.")
    parts = host_text.split(".")
    for label in parts:
        _validate_punycode_label_syntax(label, field)
    ipv4_parts = parts[:-1] if parts and parts[-1] == "" else parts
    if ipv4_parts:
        last_is_num, _ = _parse_whatwg_ipv4_number(ipv4_parts[-1])
        if last_is_num:
            if len(ipv4_parts) > 4:
                raise ValueError(f"{field} has an invalid IPv4 host.")
            nums: list[int] = []
            for part in ipv4_parts:
                is_num, num_val = _parse_whatwg_ipv4_number(part)
                if not is_num or num_val is None:
                    raise ValueError(f"{field} has an invalid IPv4 host.")
                nums.append(num_val)
            if any(n >= 256 for n in nums[:-1]) or nums[-1] >= 256 ** (5 - len(nums)):
                raise ValueError(f"{field} has an invalid IPv4 host.")
    return cleaned


def _parse_contract_bool(value: object, field: str) -> bool:
    """Validate and coerce a boolean matching Pydantic v2 non-strict bool semantics."""
    if isinstance(value, bool):
        return value
    if type(value) in {int, float} and value in {0, 1}:
        return bool(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{field} must be a boolean.") from exc
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in _TRUE or lowered in {"t", "y"}:
            return True
        if lowered in _FALSE or lowered in {"f", "n"}:
            return False
    raise ValueError(f"{field} must be a boolean.")


def _parse_contract_int(value: object, field: str, lo: int | None, hi: int | None) -> int:
    """Validate and coerce an integer matching Pydantic v2 non-strict int semantics."""
    if isinstance(value, bool):
        parsed = int(value)
    elif type(value) is int:
        parsed = value
    elif type(value) is float:
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field} must be an integer.")
        parsed = int(value)
    else:
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{field} must be an integer.") from exc
        if not isinstance(value, str):
            raise ValueError(f"{field} must be an integer.")
        stripped = value.strip(_CONTRACT_RUST_WS)
        match = re.fullmatch(r"([+-]?)([0-9](?:_?[0-9])*)(?:\.(0+))?", stripped)
        if match is None:
            raise ValueError(f"{field} must be an integer.")
        sign, int_part = match.group(1), match.group(2).replace("_", "")
        sig = int_part.lstrip("0") or "0"
        if hi is not None and len(sig) > 30:
            raise ValueError(f"{field} is out of integer range.")
        try:
            parsed = -int(sig) if sign == "-" else int(sig)
        except ValueError as exc:
            raise ValueError(f"{field} is out of integer range.") from exc
    if lo is not None and parsed < lo:
        raise ValueError(f"{field} must be at least {lo}.")
    if hi is not None and parsed > hi:
        raise ValueError(f"{field} must be at most {hi}.")
    return parsed


def _parse_contract_float(
    value: object,
    field: str,
    lo: float | None = None,
    hi: float | None = None,
    *,
    gt_lo: bool = False,
) -> float:
    """Validate and coerce a float matching Pydantic v2 non-strict float semantics."""
    if isinstance(value, bool):
        parsed = float(int(value))
    elif type(value) in {int, float}:
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except OverflowError as exc:
            raise ValueError(f"{field} is out of numeric range.") from exc
    else:
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{field} must be a number.") from exc
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a number.")
        stripped = value.strip(_CONTRACT_RUST_WS)
        if not stripped or not stripped.isascii():
            raise ValueError(f"{field} must be a number.")
        if stripped.startswith("_") or stripped.endswith("_") or "__" in stripped:
            raise ValueError(f"{field} must be a number.")
        try:
            parsed = float(stripped.replace("_", ""))
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"{field} must be a number.") from exc
    if lo is not None or hi is not None:
        if not math.isfinite(parsed):
            raise ValueError(f"{field} must be a finite number.")
        if lo is not None and (parsed <= lo if gt_lo else parsed < lo):
            raise ValueError(f"{field} is below minimum {lo}.")
        if hi is not None and parsed > hi:
            raise ValueError(f"{field} exceeds maximum {hi}.")
    return parsed


def _validate_contract_headers_fallback(headers: dict[object, object]) -> None:
    if len(headers) > 32:
        raise ValueError("headers may contain at most 32 entries.")
    total = 0
    seen: set[str] = set()
    for raw_name, raw_value in headers.items():
        name = _parse_contract_str(raw_name, "headers key")
        value = _parse_contract_str(raw_value, f"headers[{name!r}]")
        name_bytes = name.encode("utf-8")
        value_bytes = value.encode("utf-8")
        lowered = name.lower()
        if not name_bytes or len(name_bytes) > 128:
            raise ValueError("header names must be 1 through 128 bytes.")
        if not _CONTRACT_HEADER_NAME_RE.fullmatch(name):
            raise ValueError("header names must use HTTP token characters.")
        if lowered in seen:
            raise ValueError("header names must be unique case-insensitively.")
        seen.add(lowered)
        if lowered in _CONTRACT_BLOCKED_HEADERS or lowered.startswith(_CONTRACT_BLOCKED_HEADER_PREFIXES):
            raise ValueError(f"header {name!r} is managed by ViperCapture.")
        if len(value_bytes) > 4_096:
            raise ValueError("individual header values may not exceed 4096 bytes.")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("header values may not contain control characters.")
        total += len(name_bytes) + len(value_bytes) + 4
    if total > 16_384:
        raise ValueError("serialized headers may not exceed 16384 bytes.")


def _validate_payload_fallback(payload: dict[str, object]) -> None:
    """Best-effort stdlib fallback validator used only when Pydantic is unavailable.

    Mirrors RenderRequest schema, strict keys, bounds, coercion, cross-field
    rules, and ASCII/IPv4/IPv6 URL checks; full UTS-46 IDNA host validation is
    performed by RenderRequest when Pydantic is installed and by the server.
    """
    if not isinstance(payload, dict):
        raise ValueError("Render payload must be a JSON object.")
    _check_strict_keys(payload, _FALLBACK_ALLOWED_KEYS, "render request")

    url_val = _parse_contract_http_url(payload["url"], "url") if payload.get("url") is not None else None
    html_val = _parse_contract_str(payload["html"], "html") if payload.get("html") is not None else None
    md_val = _parse_contract_str(payload["markdown"], "markdown") if payload.get("markdown") is not None else None
    base_url_val = _parse_contract_http_url(payload["base_url"], "base_url") if payload.get("base_url") is not None else None
    if sum(v is not None for v in (url_val, html_val, md_val)) != 1:
        raise ValueError("Render payload must specify exactly one of url, html, or markdown.")
    for src in (html_val, md_val):
        if src is not None and len(src.encode("utf-8")) > 5 * 1024 * 1024:
            raise ValueError("HTML and Markdown input may not exceed 5242880 bytes.")
    if base_url_val is not None and url_val is not None:
        raise ValueError("base_url is accepted only with html or markdown input.")

    profile_id_val = (
        _parse_contract_str(payload["profile_id"], "profile_id", pattern=_CONTRACT_PROFILE_ID_RE)
        if payload.get("profile_id") is not None
        else None
    )
    cache_key_val = (
        _parse_contract_str(payload["cache_key"], "cache_key", min_len=1, max_len=128, pattern=_CONTRACT_CACHE_KEY_RE)
        if payload.get("cache_key") is not None
        else None
    )
    selector_val = (
        _parse_contract_str(payload["selector"], "selector", min_len=1, max_len=2_048)
        if payload.get("selector") is not None
        else None
    )
    custom_css_val = (
        _parse_contract_str(payload["custom_css"], "custom_css")
        if payload.get("custom_css") is not None
        else None
    )
    if custom_css_val is not None and len(custom_css_val.encode("utf-8")) > 65_536:
        raise ValueError("custom_css may not exceed 65536 UTF-8 bytes.")

    for dict_key in _FALLBACK_DICT_SECTIONS:
        if dict_key in payload and not isinstance(payload[dict_key], dict):
            raise ValueError(f"{dict_key} must be an object.")
    for ndict_key in _FALLBACK_NULLABLE_DICT_SECTIONS:
        if ndict_key in payload and payload[ndict_key] is not None and not isinstance(payload[ndict_key], dict):
            raise ValueError(f"{ndict_key} must be an object or null.")
    for list_key in _FALLBACK_LIST_SECTIONS:
        if list_key in payload and not isinstance(payload[list_key], _CONTRACT_LIST_TYPES):
            raise ValueError(f"{list_key} must be a list.")
    for nlist_key in _FALLBACK_NULLABLE_LIST_SECTIONS:
        if nlist_key in payload and payload[nlist_key] is not None and not isinstance(payload[nlist_key], _CONTRACT_LIST_TYPES):
            raise ValueError(f"{nlist_key} must be a list or null.")

    output_fmt = _parse_contract_enum(payload["output"], "output", _CONTRACT_OUTPUTS) if "output" in payload else "png"
    engine_name = _parse_contract_enum(payload["engine"], "engine", _ENGINES) if "engine" in payload else "chromium"
    if "lazy_load" in payload:
        _parse_contract_enum(payload["lazy_load"], "lazy_load", _LAZY)
    if "profile" in payload:
        _parse_contract_enum(payload["profile"], "profile", _PROFILES)
    extract_mode_val = (
        _parse_contract_enum(payload["extract_mode"], "extract_mode", _EXTRACT)
        if "extract_mode" in payload
        else "document"
    )

    coerced_bools: dict[str, bool] = {}
    for bool_key in _FALLBACK_BOOL_FIELDS:
        if bool_key in payload:
            coerced_bools[bool_key] = _parse_contract_bool(payload[bool_key], bool_key)
    full_page_val = coerced_bools.get("full_page", True)
    preserve_vp_width = coerced_bools.get("preserve_viewport_width", False)
    save_profile_val = coerced_bools.get("save_profile", False)
    cache_val = coerced_bools.get("cache", False)
    include_shadow_dom_val = coerced_bools.get("include_shadow_dom", False)
    proceed_on_captcha_val = coerced_bools.get("proceed_on_captcha", False)

    is_image_fmt = output_fmt in {"png", "jpeg", "webp", "avif"}
    is_video_fmt = output_fmt in {"webm", "mp4", "gif"}
    document_outputs = {"html", "markdown"}

    if "viewport" in payload:
        vp = payload["viewport"]
        assert isinstance(vp, dict)
        _check_strict_keys(vp, frozenset({"width", "height", "device_scale_factor"}), "viewport")
        for dim in ("width", "height"):
            if dim in vp:
                _parse_contract_int(vp[dim], f"viewport.{dim}", 1, 65_535)
        if "device_scale_factor" in vp:
            _parse_contract_float(vp["device_scale_factor"], "viewport.device_scale_factor", 0.1, 8.0)

    side_outputs_vals: list[str] = []
    if payload.get("side_outputs") is not None:
        raw_sides = payload["side_outputs"]
        assert isinstance(raw_sides, _CONTRACT_LIST_TYPES)
        if not raw_sides or len(raw_sides) > 4:
            raise ValueError("side_outputs must contain 1 to 4 entries.")
        for item in raw_sides:
            side_outputs_vals.append(_parse_contract_enum(item, "side_outputs", _CONTRACT_SIDE_OUTPUTS))
        if len(set(side_outputs_vals)) != len(side_outputs_vals):
            raise ValueError("side_outputs must be unique.")
    side_outputs_set = set(side_outputs_vals)
    if "mhtml" in side_outputs_set and engine_name != "chromium":
        raise ValueError("MHTML side output requires the Chromium engine.")

    img_raw = payload.get("image")
    img_dict = img_raw if isinstance(img_raw, dict) else {}
    thumbnails_raw = img_dict.get("thumbnails") if img_dict else None
    if img_dict:
        _check_strict_keys(
            img_dict,
            frozenset({"quality", "width", "height", "transparent_background", "optimize_for_speed", "thumbnails"}),
            "image",
        )
        if "quality" in img_dict and img_dict["quality"] is not None:
            _parse_contract_int(img_dict["quality"], "image.quality", 1, 100)
            if output_fmt not in {"jpeg", "webp", "avif"}:
                raise ValueError("quality is accepted only for JPEG, WebP, or AVIF.")
        for dim in ("width", "height"):
            if dim in img_dict and img_dict[dim] is not None:
                _parse_contract_int(img_dict[dim], f"image.{dim}", 1, 65_535)
        if img_dict.get("width") is not None or img_dict.get("height") is not None:
            if not is_image_fmt or payload.get("viewports") is not None or payload.get("slices") is not None:
                raise ValueError("image width and height require a single image output.")
        img_transparent = (
            _parse_contract_bool(img_dict["transparent_background"], "image.transparent_background")
            if "transparent_background" in img_dict
            else False
        )
        img_fast = (
            _parse_contract_bool(img_dict["optimize_for_speed"], "image.optimize_for_speed")
            if "optimize_for_speed" in img_dict
            else False
        )
        if img_transparent and output_fmt not in {"png", "webp", "avif"}:
            raise ValueError("transparent_background is accepted only for PNG, WebP, or AVIF.")
        if img_fast:
            if output_fmt not in {"png", "webp"}:
                raise ValueError("optimize_for_speed is accepted only for PNG or WebP.")
            if engine_name != "chromium":
                raise ValueError("optimize_for_speed requires the Chromium engine.")
        if thumbnails_raw is not None:
            if not isinstance(thumbnails_raw, _CONTRACT_LIST_TYPES) or not (1 <= len(thumbnails_raw) <= 5):
                raise ValueError("image.thumbnails must contain 1 to 5 entries.")
            thumb_names: list[str] = []
            for thumb in thumbnails_raw:
                if not isinstance(thumb, dict):
                    raise ValueError("Each thumbnail must be an object.")
                _check_strict_keys(thumb, frozenset({"name", "width", "height"}), "thumbnail")
                if "name" not in thumb:
                    raise ValueError("thumbnail.name is required.")
                t_name = _parse_contract_str(thumb["name"], "thumbnail.name", pattern=_CONTRACT_ARTIFACT_NAME_RE)
                t_width = _parse_contract_int(thumb["width"], "thumbnail.width", 10, 2_000) if thumb.get("width") is not None else None
                t_height = _parse_contract_int(thumb["height"], "thumbnail.height", 10, 2_000) if thumb.get("height") is not None else None
                if t_width is None and t_height is None:
                    raise ValueError("thumbnail width or height is required.")
                thumb_names.append(t_name)
            if len(set(thumb_names)) != len(thumb_names):
                raise ValueError("thumbnail names must be unique.")

    diag = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    if diag:
        _check_strict_keys(
            diag,
            frozenset({"bundle", "include_console", "include_network", "include_har", "include_trace", "include_warc"}),
            "diagnostics",
        )
    diag_bundle = _parse_contract_bool(diag["bundle"], "diagnostics.bundle") if "bundle" in diag else False
    if "include_console" in diag:
        _parse_contract_bool(diag["include_console"], "diagnostics.include_console")
    if "include_network" in diag:
        _parse_contract_bool(diag["include_network"], "diagnostics.include_network")
    diag_har = _parse_contract_bool(diag["include_har"], "diagnostics.include_har") if "include_har" in diag else False
    diag_trace = _parse_contract_bool(diag["include_trace"], "diagnostics.include_trace") if "include_trace" in diag else False
    diag_warc = _parse_contract_bool(diag["include_warc"], "diagnostics.include_warc") if "include_warc" in diag else False
    if (diag_har or diag_trace or diag_warc) and not diag_bundle:
        raise ValueError("HAR, trace, and WARC require diagnostics.bundle=true.")

    cert = payload.get("certification") if isinstance(payload.get("certification"), dict) else {}
    if cert:
        _check_strict_keys(cert, frozenset({"enabled"}), "certification")
    cert_enabled = _parse_contract_bool(cert["enabled"], "certification.enabled") if "enabled" in cert else False

    if "deterministic" in payload:
        det = payload["deterministic"]
        assert isinstance(det, dict)
        _check_strict_keys(det, frozenset({"enabled", "timestamp_ms", "random_seed", "wait_for_fonts"}), "deterministic")
        if "enabled" in det:
            _parse_contract_bool(det["enabled"], "deterministic.enabled")
        if "timestamp_ms" in det:
            _parse_contract_int(det["timestamp_ms"], "deterministic.timestamp_ms", 0, None)
        if "random_seed" in det:
            _parse_contract_int(det["random_seed"], "deterministic.random_seed", 0, 2**32 - 1)
        if "wait_for_fonts" in det:
            _parse_contract_bool(det["wait_for_fonts"], "deterministic.wait_for_fonts")

    if "delivery" in payload:
        deliv = payload["delivery"]
        assert isinstance(deliv, dict)
        _check_strict_keys(deliv, frozenset({"webhook_url"}), "delivery")
        if deliv.get("webhook_url") is not None:
            _parse_contract_http_url(deliv["webhook_url"], "delivery.webhook_url")

    has_artifact_bundle = bool(side_outputs_vals or thumbnails_raw)
    if has_artifact_bundle:
        if not is_image_fmt:
            raise ValueError("side outputs and thumbnails require an image output.")
        if any((
            payload.get("viewports") is not None,
            payload.get("slices") is not None,
            diag_bundle,
            cert_enabled,
            cache_val,
        )):
            raise ValueError(
                "artifact bundles cannot use viewports, slices, diagnostics, certification, or cache."
            )

    if payload.get("elements") is not None:
        elems = payload["elements"]
        assert isinstance(elems, _CONTRACT_LIST_TYPES)
        if not (1 <= len(elems) <= 32):
            raise ValueError("elements must contain 1 to 32 entries.")
        elem_selectors: list[str] = []
        for elem in elems:
            if not isinstance(elem, dict):
                raise ValueError("Each element extraction must be an object.")
            _check_strict_keys(elem, frozenset({"selector"}), "element")
            if "selector" not in elem:
                raise ValueError("element.selector is required.")
            elem_selectors.append(_parse_contract_str(elem["selector"], "element.selector", min_len=1, max_len=2_048))
        if output_fmt != "metadata" and "metadata" not in side_outputs_set:
            raise ValueError("elements requires primary or side metadata output.")
        if len(set(elem_selectors)) != len(elem_selectors):
            raise ValueError("elements selectors must be unique.")

    if "fail_on_status" in payload:
        statuses = payload["fail_on_status"]
        assert isinstance(statuses, _CONTRACT_LIST_TYPES)
        if len(statuses) > 32:
            raise ValueError("fail_on_status may contain at most 32 entries.")
        parsed_statuses = [_parse_contract_int(st, "fail_on_status", 100, 599) for st in statuses]
        if len(set(parsed_statuses)) != len(parsed_statuses):
            raise ValueError("fail_on_status values must be unique.")

    if preserve_vp_width:
        if not full_page_val:
            raise ValueError("preserve_viewport_width requires full_page=true.")
        if not is_image_fmt:
            raise ValueError("preserve_viewport_width requires an image output.")
    if save_profile_val and profile_id_val is None:
        raise ValueError("save_profile requires profile_id.")
    if cache_val:
        if not is_image_fmt or payload.get("viewports") is not None:
            raise ValueError("cache is accepted only for a single PNG, JPEG, WebP, or AVIF output.")
        if diag_bundle:
            raise ValueError("cache cannot be combined with a diagnostic bundle.")
        if profile_id_val is not None:
            raise ValueError("cache cannot be combined with a persistent profile.")
    if cache_key_val is not None and not cache_val:
        raise ValueError("cache_key requires cache=true.")
    if extract_mode_val != "document" and output_fmt not in document_outputs and not side_outputs_set.intersection(document_outputs):
        raise ValueError("article extraction requires HTML or Markdown output.")
    if include_shadow_dom_val and output_fmt not in document_outputs and not side_outputs_set.intersection(document_outputs):
        raise ValueError("include_shadow_dom requires HTML or Markdown output.")

    captcha_action = "error"
    if "captcha" in payload:
        cap = payload["captcha"]
        assert isinstance(cap, dict)
        _check_strict_keys(cap, frozenset({"action", "solver", "timeout_ms"}), "captcha")
        if "action" in cap:
            captcha_action = _parse_contract_enum(cap["action"], "captcha.action", _CAPTCHA)
        cap_solver = (
            _parse_contract_str(cap["solver"], "captcha.solver", min_len=1, max_len=64, pattern=_CONTRACT_SOLVER_RE)
            if cap.get("solver") is not None
            else None
        )
        if "timeout_ms" in cap:
            _parse_contract_int(cap["timeout_ms"], "captcha.timeout_ms", 1_000, 300_000)
        if cap_solver is not None and captcha_action != "external":
            raise ValueError("captcha.solver requires action=external.")
    if proceed_on_captcha_val and captcha_action == "external":
        raise ValueError("proceed_on_captcha cannot be combined with captcha.action=external.")

    if selector_val is not None and (full_page_val or not is_image_fmt):
        raise ValueError("selector requires full_page=false and an image output.")
    if payload.get("clip") is not None and selector_val is not None:
        raise ValueError("clip and selector are mutually exclusive.")

    if payload.get("viewports") is not None:
        vps = payload["viewports"]
        assert isinstance(vps, _CONTRACT_LIST_TYPES)
        if not is_image_fmt:
            raise ValueError("viewports requires PNG, JPEG, WebP, or AVIF output.")
        if full_page_val:
            raise ValueError("viewports requires full_page=false.")
        if selector_val is not None or payload.get("clip") is not None:
            raise ValueError("viewports cannot be combined with selector or clip.")
        if diag_bundle:
            raise ValueError("diagnostic bundles cannot be combined with multi-viewports.")
        if cert_enabled:
            raise ValueError("certification cannot be combined with multi-viewports.")
        if len(vps) < 2 or len(vps) > 3:
            raise ValueError("viewports must contain 2 or 3 entries.")
        vp_names: list[str] = []
        for entry in vps:
            if not isinstance(entry, dict):
                raise ValueError("Each viewport must be an object.")
            _check_strict_keys(entry, frozenset({"name", "device", "width", "height", "device_scale_factor"}), "viewports")
            if "name" not in entry:
                raise ValueError("viewports.name is required.")
            vp_name = _parse_contract_str(entry["name"], "viewports.name", min_len=1, max_len=32, pattern=_CONTRACT_ARTIFACT_NAME_RE)
            if "device" in entry:
                _parse_contract_enum(entry["device"], "viewports.device", _DEVICES)
            for dim in ("width", "height"):
                if dim in entry:
                    _parse_contract_int(entry[dim], f"viewports.{dim}", 1, 65_535)
            if "device_scale_factor" in entry:
                _parse_contract_float(entry["device_scale_factor"], "viewports.device_scale_factor", 0.1, 8.0)
            vp_names.append(vp_name)
        if len(set(vp_names)) != len(vp_names):
            raise ValueError("viewports names must be unique.")

    if payload.get("slices") is not None:
        sl = payload["slices"]
        assert isinstance(sl, dict)
        _check_strict_keys(sl, frozenset({"height", "overlap"}), "slices")
        if not is_image_fmt or not full_page_val or payload.get("viewports") is not None:
            raise ValueError("slices require a single full-page image output.")
        if "height" not in sl:
            raise ValueError("slices.height is required.")
        sl_height = _parse_contract_int(sl["height"], "slices.height", 100, 10_000)
        sl_overlap = _parse_contract_int(sl["overlap"], "slices.overlap", 0, 1_000) if "overlap" in sl else 0
        if sl_overlap >= sl_height:
            raise ValueError("slice overlap must be smaller than slice height.")

    if is_video_fmt:
        if selector_val is not None or payload.get("clip") is not None or payload.get("viewports") is not None:
            raise ValueError("video cannot use selector, clip, or multi-viewports.")
        if diag_trace:
            raise ValueError("video diagnostics support console, network, HAR, and WARC.")
    if payload.get("video") is not None:
        vid = payload["video"]
        assert isinstance(vid, dict)
        _check_strict_keys(
            vid,
            frozenset({"duration_ms", "fps", "bitrate_mbps", "scroll", "scroll_step", "scroll_delay_ms", "transparent_background"}),
            "video",
        )
        if not is_video_fmt:
            raise ValueError("video settings require WebM, MP4, or GIF output.")
        if "duration_ms" in vid:
            _parse_contract_int(vid["duration_ms"], "video.duration_ms", 1_000, 30_000)
        if "fps" in vid:
            _parse_contract_int(vid["fps"], "video.fps", 1, 60)
        if "bitrate_mbps" in vid:
            _parse_contract_int(vid["bitrate_mbps"], "video.bitrate_mbps", 1, 100)
        if "scroll_step" in vid:
            _parse_contract_int(vid["scroll_step"], "video.scroll_step", 1, 4_320)
        if "scroll_delay_ms" in vid:
            _parse_contract_int(vid["scroll_delay_ms"], "video.scroll_delay_ms", 50, 2_000)
        if "scroll" in vid:
            _parse_contract_bool(vid["scroll"], "video.scroll")
        vid_transparent = (
            _parse_contract_bool(vid["transparent_background"], "video.transparent_background")
            if "transparent_background" in vid
            else False
        )
        if vid_transparent:
            if not full_page_val:
                raise ValueError("transparent video requires full_page=true.")
            if output_fmt == "mp4":
                raise ValueError("MP4 does not support transparent video.")

    if payload.get("pdf") is not None:
        if output_fmt != "pdf":
            raise ValueError("pdf settings require PDF output.")
        pdf_cfg = payload["pdf"]
        assert isinstance(pdf_cfg, dict)
        _check_strict_keys(
            pdf_cfg,
            frozenset({
                "mode", "paper_size", "orientation", "print_background", "tagged",
                "margins", "header_template", "footer_template", "page_ranges",
            }),
            "pdf",
        )
        pdf_mode = _parse_contract_enum(pdf_cfg["mode"], "pdf.mode", _PDF_MODES) if "mode" in pdf_cfg else "print"
        pdf_paper = _parse_contract_enum(pdf_cfg["paper_size"], "pdf.paper_size", _PAPER) if "paper_size" in pdf_cfg else "A4"
        pdf_orient = _parse_contract_enum(pdf_cfg["orientation"], "pdf.orientation", _ORIENTATION) if "orientation" in pdf_cfg else "portrait"
        if "print_background" in pdf_cfg:
            _parse_contract_bool(pdf_cfg["print_background"], "pdf.print_background")
        if pdf_cfg.get("tagged") is not None:
            _parse_contract_bool(pdf_cfg["tagged"], "pdf.tagged")
        if pdf_cfg.get("header_template") is not None:
            _parse_contract_str(pdf_cfg["header_template"], "pdf.header_template", max_len=16_384)
        if pdf_cfg.get("footer_template") is not None:
            _parse_contract_str(pdf_cfg["footer_template"], "pdf.footer_template", max_len=16_384)
        margins = {"top": 0.4, "right": 0.4, "bottom": 0.4, "left": 0.4}
        if "margins" in pdf_cfg:
            raw_m = pdf_cfg["margins"]
            if not isinstance(raw_m, dict):
                raise ValueError("pdf.margins must be an object.")
            _check_strict_keys(raw_m, frozenset({"top", "right", "bottom", "left"}), "pdf.margins")
            for side in ("top", "right", "bottom", "left"):
                if side in raw_m:
                    margins[side] = _parse_contract_float(raw_m[side], f"pdf.margins.{side}", 0.0, 4.0)
        if pdf_mode != "single_page":
            pw, ph = _CONTRACT_PAPER_DIMS[pdf_paper]
            if pdf_orient == "landscape":
                pw, ph = ph, pw
            if margins["left"] + margins["right"] >= pw:
                raise ValueError("horizontal margins must leave printable page width.")
            if margins["top"] + margins["bottom"] >= ph:
                raise ValueError("vertical margins must leave printable page height.")
        if pdf_cfg.get("page_ranges") is not None:
            pr = _parse_contract_str(pdf_cfg["page_ranges"], "pdf.page_ranges", max_len=256, pattern=_CONTRACT_PAGE_RANGES_RE)
            ranges: list[tuple[int, int]] = []
            for item in pr.split(","):
                bounds = [int(part.strip()) for part in item.split("-", 1)]
                start, end = bounds[0], bounds[-1]
                if start < 1 or end < start:
                    raise ValueError("page_ranges must contain ascending positive pages.")
                ranges.append((start, end))
            ranges.sort()
            selected = 0
            cur_start, cur_end = ranges[0]
            for start, end in ranges[1:]:
                if start <= cur_end + 1:
                    cur_end = max(cur_end, end)
                else:
                    selected += cur_end - cur_start + 1
                    cur_start, cur_end = start, end
            selected += cur_end - cur_start + 1
            if selected > 50:
                raise ValueError("page_ranges may select at most 50 pages.")
            if pdf_mode == "single_page":
                raise ValueError("single-page PDF cannot use page_ranges.")
    if output_fmt == "pdf" and engine_name != "chromium":
        raise ValueError("PDF output requires the Chromium engine.")

    if payload.get("clip") is not None:
        clip = payload["clip"]
        assert isinstance(clip, dict)
        _check_strict_keys(clip, frozenset({"x", "y", "width", "height"}), "clip")
        if full_page_val or not is_image_fmt:
            raise ValueError("clip requires full_page=false and an image output.")
        for coord in ("x", "y"):
            if coord in clip:
                _parse_contract_float(clip[coord], f"clip.{coord}", 0.0, 100_000.0)
        for dim in ("width", "height"):
            if dim not in clip:
                raise ValueError(f"clip.{dim} is required.")
            _parse_contract_float(clip[dim], f"clip.{dim}", 0.0, 100_000.0, gt_lo=True)

    if "environment" in payload:
        env = payload["environment"]
        assert isinstance(env, dict)
        _check_strict_keys(
            env,
            frozenset({"device", "color_scheme", "reduced_motion", "media", "locale", "timezone"}),
            "environment",
        )
        if "device" in env:
            _parse_contract_enum(env["device"], "environment.device", _DEVICES)
        if env.get("color_scheme") is not None:
            _parse_contract_enum(env["color_scheme"], "environment.color_scheme", _COLOR_SCHEMES)
        if env.get("reduced_motion") is not None:
            _parse_contract_enum(env["reduced_motion"], "environment.reduced_motion", _REDUCED_MOTION)
        if env.get("media") is not None:
            _parse_contract_enum(env["media"], "environment.media", _MEDIA)
        if env.get("locale") is not None:
            _parse_contract_str(env["locale"], "environment.locale", min_len=2, max_len=64, pattern=_CONTRACT_LOCALE_RE)
        if env.get("timezone") is not None:
            tz = _parse_contract_str(env["timezone"], "environment.timezone", min_len=1, max_len=64)
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("timezone must be a valid IANA time zone.") from exc

    if "network" in payload:
        net = payload["network"]
        assert isinstance(net, dict)
        _check_strict_keys(
            net,
            frozenset({
                "java_script_enabled", "user_agent", "geolocation", "proxy", "cookies",
                "block_url_patterns", "block_resource_types", "bypass_csp", "ignore_https_errors",
            }),
            "network",
        )
        if "java_script_enabled" in net:
            _parse_contract_bool(net["java_script_enabled"], "network.java_script_enabled")
        if net.get("user_agent") is not None:
            _parse_contract_str(net["user_agent"], "network.user_agent", min_len=1, max_len=1_024)
        if net.get("geolocation") is not None:
            geo = net["geolocation"]
            if not isinstance(geo, dict):
                raise ValueError("network.geolocation must be an object.")
            _check_strict_keys(geo, frozenset({"latitude", "longitude", "accuracy"}), "network.geolocation")
            if "latitude" not in geo or "longitude" not in geo:
                raise ValueError("network.geolocation requires latitude and longitude.")
            _parse_contract_float(geo["latitude"], "network.geolocation.latitude", -90.0, 90.0)
            _parse_contract_float(geo["longitude"], "network.geolocation.longitude", -180.0, 180.0)
            if "accuracy" in geo:
                _parse_contract_float(geo["accuracy"], "network.geolocation.accuracy", 0.0, 100_000.0)
        if net.get("proxy") is not None:
            prx = net["proxy"]
            if not isinstance(prx, dict):
                raise ValueError("network.proxy must be an object.")
            _check_strict_keys(prx, frozenset({"server", "username", "password", "bypass"}), "network.proxy")
            if "server" not in prx:
                raise ValueError("network.proxy.server is required.")
            srv = _parse_contract_str(prx["server"], "network.proxy.server", min_len=1, max_len=2_048)
            try:
                parsed_srv = urlsplit(srv)
                _ = parsed_srv.port
            except ValueError as exc:
                raise ValueError("proxy server authority is invalid.") from exc
            if (
                parsed_srv.scheme.lower() not in {"http", "https", "socks4", "socks5"}
                or not parsed_srv.hostname
                or parsed_srv.username
                or parsed_srv.password
                or parsed_srv.query
                or parsed_srv.fragment
                or parsed_srv.path not in {"", "/"}
            ):
                raise ValueError("proxy server must use http, https, socks4, or socks5.")
            if prx.get("username") is not None:
                _parse_contract_str(prx["username"], "network.proxy.username", max_len=1_024)
            if prx.get("password") is not None:
                _parse_contract_str(prx["password"], "network.proxy.password", max_len=4_096)
            if prx.get("bypass") is not None:
                _parse_contract_str(prx["bypass"], "network.proxy.bypass", max_len=4_096)
        if "cookies" in net:
            cks = net["cookies"]
            if not isinstance(cks, _CONTRACT_LIST_TYPES) or len(cks) > 64:
                raise ValueError("network.cookies must be a list of at most 64 cookies.")
            for ck in cks:
                if not isinstance(ck, dict):
                    raise ValueError("Each cookie must be an object.")
                _check_strict_keys(
                    ck,
                    frozenset({"name", "value", "domain", "path", "expires", "http_only", "secure", "same_site"}),
                    "cookie",
                )
                for req in ("name", "value", "domain"):
                    if req not in ck:
                        raise ValueError(f"cookie.{req} is required.")
                _parse_contract_str(ck["name"], "cookie.name", min_len=1, max_len=256)
                _parse_contract_str(ck["value"], "cookie.value", max_len=4_096)
                dom = _parse_contract_str(ck["domain"], "cookie.domain", min_len=1, max_len=253)
                bare_dom = dom.removeprefix(".")
                try:
                    ipaddress.ip_address(bare_dom)
                except ValueError:
                    labels = bare_dom.split(".")
                    if any(
                        not label
                        or len(label) > 63
                        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                        for label in labels
                    ):
                        raise ValueError("cookie domain must be a hostname or IP address.")
                if "path" in ck:
                    cpath = _parse_contract_str(ck["path"], "cookie.path", min_len=1, max_len=1_024)
                    if not cpath.startswith("/"):
                        raise ValueError("cookie path must begin with /.")
                if ck.get("expires") is not None:
                    _parse_contract_float(ck["expires"], "cookie.expires")
                if "http_only" in ck:
                    _parse_contract_bool(ck["http_only"], "cookie.http_only")
                if "secure" in ck:
                    _parse_contract_bool(ck["secure"], "cookie.secure")
                if "same_site" in ck:
                    _parse_contract_str(ck["same_site"], "cookie.same_site", pattern=_CONTRACT_SAME_SITE_RE)
        if "block_url_patterns" in net:
            pats = net["block_url_patterns"]
            if not isinstance(pats, _CONTRACT_LIST_TYPES) or len(pats) > 64:
                raise ValueError("network.block_url_patterns must be a list of at most 64 patterns.")
            parsed_pats = [_parse_contract_str(p, "block_url_patterns", min_len=1, max_len=2_048) for p in pats]
            if len(set(parsed_pats)) != len(parsed_pats):
                raise ValueError("block URL patterns must be unique.")
        if "block_resource_types" in net:
            rtypes = net["block_resource_types"]
            if not isinstance(rtypes, (list, set, frozenset, tuple)):
                raise ValueError("network.block_resource_types must be a collection.")
            for rt in rtypes:
                _parse_contract_enum(rt, "network.block_resource_types", _CONTRACT_RESOURCES)
        if "bypass_csp" in net:
            _parse_contract_bool(net["bypass_csp"], "network.bypass_csp")
        if "ignore_https_errors" in net:
            _parse_contract_bool(net["ignore_https_errors"], "network.ignore_https_errors")

    if "assertions" in payload:
        asr = payload["assertions"]
        assert isinstance(asr, dict)
        _check_strict_keys(asr, frozenset({"content_includes", "content_excludes", "request_failures"}), "assertions")
        for akey in ("content_includes", "content_excludes", "request_failures"):
            if akey in asr:
                alist = asr[akey]
                if not isinstance(alist, _CONTRACT_LIST_TYPES) or len(alist) > 32:
                    raise ValueError(f"assertions.{akey} must be a list of at most 32 entries.")
                parsed_a = [_parse_contract_str(x, f"assertions.{akey}", min_len=1, max_len=4_096) for x in alist]
                if len(set(parsed_a)) != len(parsed_a):
                    raise ValueError("assertions must be unique.")

    if "cleanup" in payload:
        cln = payload["cleanup"]
        assert isinstance(cln, dict)
        _check_strict_keys(
            cln,
            frozenset({"consent_mode", "block_ads", "block_trackers", "block_chats", "block_newsletters"}),
            "cleanup",
        )
        if "consent_mode" in cln:
            _parse_contract_enum(cln["consent_mode"], "cleanup.consent_mode", _CONSENT)
        for cbool in ("block_ads", "block_trackers", "block_chats", "block_newsletters"):
            if cbool in cln:
                _parse_contract_bool(cln[cbool], f"cleanup.{cbool}")

    if "wait_for" in payload:
        wf = payload["wait_for"]
        assert isinstance(wf, dict)
        _check_strict_keys(
            wf,
            frozenset({"event", "selector", "selector_state", "text", "images", "delay_ms", "timeout_ms"}),
            "wait_for",
        )
        if "event" in wf:
            _parse_contract_enum(wf["event"], "wait_for.event", _WAIT_EVENTS)
        wf_sel = _parse_contract_str(wf["selector"], "wait_for.selector", min_len=1, max_len=2_048) if wf.get("selector") is not None else None
        wf_state = _parse_contract_enum(wf["selector_state"], "wait_for.selector_state", _WAIT_STATES) if "selector_state" in wf else "visible"
        if wf_sel is None and wf_state != "visible":
            raise ValueError("selector_state requires wait_for.selector.")
        if wf.get("text") is not None:
            _parse_contract_str(wf["text"], "wait_for.text", min_len=1, max_len=4_096)
        if "images" in wf:
            _parse_contract_bool(wf["images"], "wait_for.images")
        if "delay_ms" in wf:
            _parse_contract_int(wf["delay_ms"], "wait_for.delay_ms", 0, 15_000)
        if "timeout_ms" in wf:
            _parse_contract_int(wf["timeout_ms"], "wait_for.timeout_ms", 1, 30_000)

    if "actions" in payload:
        acts = payload["actions"]
        assert isinstance(acts, _CONTRACT_LIST_TYPES)
        if len(acts) > 32:
            raise ValueError("actions may contain at most 32 entries.")
        act_keys = frozenset({"type", "selector", "value", "values", "key", "x", "y", "delay_ms", "timeout_ms"})
        for act in acts:
            if not isinstance(act, dict):
                raise ValueError("Each action must be an object.")
            _check_strict_keys(act, act_keys, "action")
            if "type" not in act:
                raise ValueError("action.type is required.")
            atype = _parse_contract_enum(act["type"], "action.type", _FAST_ALL_ACTIONS)
            asel = _parse_contract_str(act["selector"], "action.selector", min_len=1, max_len=2_048) if act.get("selector") is not None else None
            aval = _parse_contract_str(act["value"], "action.value", max_len=16_384) if act.get("value") is not None else None
            avals: list[str] | None = None
            if act.get("values") is not None:
                raw_avals = act["values"]
                if not isinstance(raw_avals, _CONTRACT_LIST_TYPES) or not (1 <= len(raw_avals) <= 32):
                    raise ValueError("action.values must contain 1 to 32 entries.")
                avals = [_parse_contract_str(v, "action.values", max_len=16_384) for v in raw_avals]
            akey = _parse_contract_str(act["key"], "action.key", min_len=1, max_len=128) if act.get("key") is not None else None
            ax = _parse_contract_float(act["x"], "action.x", -100_000.0, 100_000.0) if act.get("x") is not None else None
            ay = _parse_contract_float(act["y"], "action.y", -100_000.0, 100_000.0) if act.get("y") is not None else None
            adelay = _parse_contract_int(act["delay_ms"], "action.delay_ms", 0, 15_000) if "delay_ms" in act else 0
            if "timeout_ms" in act:
                _parse_contract_int(act["timeout_ms"], "action.timeout_ms", 1, 30_000)
            if atype in _FAST_SELECTOR_ACTIONS and asel is None:
                raise ValueError(f"{atype} action requires selector.")
            if atype == "fill" and aval is None:
                raise ValueError("fill action requires value.")
            if atype == "press" and akey is None:
                raise ValueError("press action requires key.")
            if atype == "select" and not avals:
                raise ValueError("select action requires values.")
            if atype == "scroll" and asel is None and ax is None and ay is None:
                raise ValueError("scroll action requires selector, x, or y.")
            if atype == "wait" and asel is None and aval is None and not adelay:
                raise ValueError("wait action requires selector, text value, or delay_ms.")
            if atype == "javascript" and aval is None:
                raise ValueError("javascript action requires value.")

    if "headers" in payload:
        hdrs = payload["headers"]
        assert isinstance(hdrs, dict)
        if hdrs and url_val is None and base_url_val is None:
            raise ValueError("base_url is required for raw input custom headers.")
        _validate_contract_headers_fallback(hdrs)


def validate_payload(payload: dict[str, object]) -> None:
    if _fast_validate_simple_payload(payload):
        return
    try:
        from pydantic import ValidationError
        from vipercapture.render_contract import RenderRequest
    except ImportError:
        _validate_payload_fallback(payload)
        return
    try:
        RenderRequest.model_validate(copy.deepcopy(payload))
    except ValidationError as exc:
        raise ValueError(_validation_message(exc)) from exc


def artifact_is_archive(payload: dict[str, object]) -> bool:
    image = payload.get("image")
    thumbnails = image.get("thumbnails") if isinstance(image, dict) else None
    diagnostics = payload.get("diagnostics")
    certification = payload.get("certification")
    return bool(
        payload.get("viewports")
        or payload.get("slices")
        or payload.get("side_outputs")
        or thumbnails
        or (isinstance(diagnostics, dict) and _coerce_bool(diagnostics.get("bundle"), False))
        or (isinstance(certification, dict) and _coerce_bool(certification.get("enabled"), False))
    )


def render_output_template(
    template: str,
    *,
    payload: dict[str, object],
    index: int,
    timestamp: str | None = None,
) -> str:
    fmt = str(payload.get("output", "png"))
    ext = ".zip" if artifact_is_archive(payload) else _EXTENSIONS.get(fmt, ".png")
    url_text = str(payload.get("url") or "")
    parsed = urlsplit(url_text) if url_text else None
    raw_host = safe_filename(parsed.hostname if parsed and parsed.hostname else "capture")
    host = raw_host[:56].rstrip(" ._-") or "capture"
    raw_slug = safe_filename(parsed.path.strip("/").replace("/", "-")) if parsed and parsed.path.strip("/") else raw_host
    path_slug = raw_slug[:56].rstrip(" ._-") or "capture"
    stamp = timestamp or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fields = {
        "host": host,
        "index": index,
        "slug": path_slug,
        "timestamp": stamp,
        "format": fmt,
        "ext": ext,
    }
    formatter = string.Formatter()
    rendered_parts: list[str] = []
    has_index_field = False
    index_formatted: str | None = None
    index_spans: list[tuple[int, int]] = []
    cursor = 0
    try:
        parsed_parts = list(formatter.parse(template))
        for literal, field_name, format_spec, conversion in parsed_parts:
            if "/" in literal or "\\" in literal:
                raise ValueError("--output-template must produce a filename without directory separators.")
            rendered_parts.append(literal)
            cursor += len(literal)
            if field_name is not None:
                val, _ = formatter.get_field(field_name, (), fields)
                val = formatter.convert_field(val, conversion)
                piece = formatter.format_field(val, format_spec or "")
                is_index_field = field_name == "index" or field_name.startswith(("index.", "index["))
                if is_index_field:
                    has_index_field = True
                    cleaned_piece = _BAD_FILENAME.sub("_", piece).strip(" .")
                    if cleaned_piece != piece or not cleaned_piece or len(cleaned_piece) > 16:
                        piece = str(index)
                        cleaned_piece = piece
                    if index_formatted is None:
                        index_formatted = cleaned_piece
                    start = cursor
                    cursor += len(piece)
                    index_spans.append((start, cursor))
                else:
                    cursor += len(piece)
                rendered_parts.append(piece)
    except ValueError as exc:
        if "--output-template must produce a filename without directory separators." in str(exc):
            raise
        raise ValueError(
            f"Invalid --output-template {template!r}: {exc}. "
            "Supported placeholders: {host}, {index}, {slug}, {timestamp}, {format}, {ext}."
        ) from exc
    except (KeyError, IndexError, AttributeError, TypeError) as exc:
        raise ValueError(
            f"Invalid --output-template {template!r}: {exc}. "
            "Supported placeholders: {host}, {index}, {slug}, {timestamp}, {format}, {ext}."
        ) from exc
    rendered = "".join(rendered_parts)
    if "/" in rendered or "\\" in rendered or rendered in {"", ".", ".."}:
        raise ValueError("--output-template must produce a filename without directory separators.")
    candidate = Path(rendered)
    suffix = candidate.suffix
    if suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        raw_stem = candidate.stem
    else:
        # Keep non-extension trailing text (such as `{host}{ext}-{index}` -> `.png-1`) in the
        # stem so `{index}` and literal suffixes are never silently discarded.
        raw_stem = rendered
        suffix = ""
    stem = safe_filename(raw_stem)
    if has_index_field:
        lead_strip = len(raw_stem) - len(_BAD_FILENAME.sub("_", raw_stem).lstrip(" ."))
        survived = any(
            (start - lead_strip) >= 0 and (end - lead_strip) <= 80 and end <= len(raw_stem)
            for start, end in index_spans
        )
        if not survived:
            idx_token = index_formatted or str(index)
            idx_suffix = f"-{idx_token}"
            prefix_limit = max(1, 80 - len(idx_suffix))
            prefix = safe_filename(raw_stem[:prefix_limit]).rstrip(" ._-") or "capture"
            stem = f"{prefix}{idx_suffix}"
    return f"{stem}{suffix}"


def resolve_output(
    args: argparse.Namespace,
    payload: dict[str, object],
    *,
    index: int = 1,
    multi: bool = False,
    timestamp: str | None = None,
) -> Path | None:
    if not multi and args.output == "-":
        return None
    if not multi and args.output:
        return Path(args.output)
    output_dir = getattr(args, "output_dir", None)
    output_template = getattr(args, "output_template", None)
    if multi or output_dir or output_template:
        directory = Path(output_dir) if output_dir else Path.cwd()
        template = output_template or ("{host}-{index}{ext}" if multi else "{host}{ext}")
        filename = render_output_template(template, payload=payload, index=index, timestamp=timestamp)
        return directory / filename
    fmt = str(payload.get("output", "png"))
    if payload.get("url"):
        stem = urlsplit(str(payload["url"])).hostname or "capture"
    else:
        stem = "capture"
    extension = ".zip" if artifact_is_archive(payload) else _EXTENSIONS[fmt]
    return Path.cwd() / f"{safe_filename(stem)}{extension}"


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)


def api_root(server: str) -> str:
    base = server.strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--server needs an http or https URL.")
    if parsed.query or parsed.fragment:
        raise ValueError("--server does not take a query string or fragment.")
    path = parsed.path.rstrip("/")
    for suffix in ("/v1/render", "/v1/jobs", "/v1"):
        if path == suffix or path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    # Userinfo is not sent. A password in this URL would be printed by
    # --verbose, and urllib would treat "user:password@host" as the hostname.
    return f"{parsed.scheme}://{host}{port}{path}"


def display_server(server: str) -> str:
    """The server URL with any userinfo removed, for logs and error text."""
    parsed = urlsplit(server)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


def is_local_default(server: str) -> bool:
    try:
        root = api_root(server)
    except ValueError:
        return False
    parsed = urlsplit(root)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return (
        parsed.scheme == "http"
        and host in {"127.0.0.1", "localhost"}
        and port == launch.PORT
        and parsed.path in {"", "/"}
    )


def endpoint(server: str, route: str) -> str:
    return api_root(server) + route


def load_local_env() -> None:
    path = launch.ROOT / ".env.local"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _header_token(value: str) -> str:
    token = value.strip()
    if any(char in token for char in "\r\n\x00"):
        return ""
    return token


def bearer_token(args: argparse.Namespace, *, started_server: bool) -> str:
    api_key = getattr(args, "api_key", None)
    if api_key is not None:
        return _validate_header_value(str(api_key), "--api-key").strip()
    server = getattr(args, "server", None)
    project_key = _header_token(os.environ.get("VIPERCAPTURE_API_KEY", ""))
    if not project_key and not getattr(args, "no_config", False):
        try:
            cfg = load_cli_config()
        except ValueError:
            cfg = {}
        project_key = _config_credential_for_server(cfg, "api_key", server)
    admin = _header_token(os.environ.get("VIPERCAPTURE_ADMIN_TOKEN", ""))
    if started_server and admin:
        return admin
    if project_key:
        return project_key
    if server is None or is_local_default(str(server)):
        return admin
    return ""


def request_headers(
    args: argparse.Namespace,
    *,
    started_server: bool,
    json_body: bool,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = {"Accept": "*/*"}
    if json_body:
        headers["Content-Type"] = "application/json"
    token = bearer_token(args, started_server=started_server)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api_key = getattr(args, "api_key", None)
    if api_key is not None:
        _validate_header_value(str(api_key), "--api-key")
    req_id = getattr(args, "request_id", None)
    if req_id:
        headers["X-Request-Id"] = _validate_header_value(str(req_id), "--request-id")
    idem = idempotency_key if idempotency_key is not None else getattr(args, "idempotency_key", None)
    if idem:
        headers["Idempotency-Key"] = _validate_header_value(str(idem), "--idempotency-key")
    return headers


def max_capture_bytes() -> int:
    raw = os.environ.get("VIPERCAPTURE_CLI_MAX_BYTES", "")
    if not raw:
        return 512 * 1024 * 1024
    try:
        value = int(raw)
    except ValueError:
        return 512 * 1024 * 1024
    if value < 1:
        return 512 * 1024 * 1024
    return value


def _parse_retry_after(headers) -> float | None:
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return max(0.0, min(30.0, value))


def _error_from_body(status: int, raw: bytes, headers=None) -> CaptureError:
    text = raw.decode("utf-8", errors="replace").strip()
    retry_after = _parse_retry_after(headers)
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
        error = parsed["error"]
        message = str(error.get("message") or f"HTTP {status}")
        code = error.get("code")
        return CaptureError(
            message,
            status=status,
            code=code if isinstance(code, str) else None,
            body=parsed,
            retry_after=retry_after,
        )
    if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
        return CaptureError(
            parsed["detail"],
            status=status,
            body=parsed,
            retry_after=retry_after,
        )
    return CaptureError(text or f"HTTP {status}", status=status, body=text or None, retry_after=retry_after)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_LOCAL_OPENER = None
_REMOTE_OPENER = None


def _build_redirect_handler():
    import urllib.request

    class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
        """Follow a redirect only when it stays on the host that issued it.

        urllib copies Authorization onto the next request. A 302 to another
        host or port would send the API token there.
        """

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            try:
                resolve_api_url(req.full_url, newurl)
            except CaptureError:
                fp.close()
                raise CaptureError(
                    "The API redirected to another host. This command will not follow it."
                ) from None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return _SameOriginRedirect


def _open(request, timeout: float):
    global _LOCAL_OPENER, _REMOTE_OPENER
    import urllib.request

    host = (urlsplit(request.full_url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        if _LOCAL_OPENER is None:
            _LOCAL_OPENER = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _build_redirect_handler(),
            )
        return _LOCAL_OPENER.open(request, timeout=timeout)
    if _REMOTE_OPENER is None:
        _REMOTE_OPENER = urllib.request.build_opener(_build_redirect_handler())
    return _REMOTE_OPENER.open(request, timeout=timeout)


class _LocalHttpResponse:
    def __init__(self, conn, resp) -> None:
        self._conn = conn
        self._resp = resp
        self.status = resp.status
        self.headers = resp.headers

    def read(self, amt: int = -1) -> bytes:
        return self._resp.read(amt)

    def close(self) -> None:
        try:
            self._resp.close()
        finally:
            self._conn.close()


def _open_local_http(
    url: str,
    *,
    method: str,
    data: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> _LocalHttpResponse:
    import http.client

    current_url = url
    current_method = method
    current_data = data
    current_headers = dict(headers)
    for _ in range(10):
        parsed = urlsplit(current_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request(current_method, path, body=current_data, headers=current_headers)
            resp = conn.getresponse()
        except TimeoutError as exc:
            conn.close()
            raise CaptureError("The API did not answer before the timeout.") from exc
        except OverflowError as exc:
            conn.close()
            raise CaptureError("Request timeout is out of range.", exit_code=2) from exc
        except ValueError as exc:
            conn.close()
            raise CaptureError(f"Invalid request header or URL: {exc}", exit_code=2) from exc
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise CaptureError(f"The API did not answer: {exc}") from exc
        if resp.status in {301, 302, 303, 307, 308}:
            location = resp.getheader("Location")
            try:
                resp.read(4096)
            except (http.client.HTTPException, OSError, TimeoutError):
                pass
            conn.close()
            if not location:
                raise CaptureError(f"HTTP {resp.status}", status=resp.status)
            candidate = location.strip()
            if not candidate or any(char in candidate for char in "\r\n\x00\\"):
                raise CaptureError(
                    "The API redirected to another host. This command will not follow it."
                )
            try:
                next_url = resolve_api_url(current_url, urljoin(current_url, candidate))
            except (CaptureError, ValueError):
                raise CaptureError(
                    "The API redirected to another host. This command will not follow it."
                ) from None
            if resp.status in {301, 302, 303} and current_method == "POST":
                current_method = "GET"
                current_data = None
                current_headers = {
                    k: v for k, v in current_headers.items() if k.lower() != "content-type"
                }
            current_url = next_url
            continue
        if not (200 <= resp.status < 300):
            try:
                try:
                    raw = resp.read(64 * 1024)
                except (http.client.HTTPException, OSError, TimeoutError):
                    raw = b""
            finally:
                conn.close()
            raise _error_from_body(resp.status, raw, resp.headers)
        return _LocalHttpResponse(conn, resp)
    raise CaptureError("The API redirected too many times.")


def open_raw_response(
    url: str,
    *,
    method: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout: float,
):
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
        or float(timeout) > _MAX_REQUEST_TIMEOUT_S
    ):
        raise CaptureError(
            f"Request timeout must be a finite number greater than 0 and at most {int(_MAX_REQUEST_TIMEOUT_S)}.",
            exit_code=2,
        )
    for hdr_name, hdr_val in headers.items():
        if any(ch in str(hdr_name) or ch in str(hdr_val) for ch in "\r\n\x00"):
            raise CaptureError(f"Header {hdr_name!r} contains invalid control characters.", exit_code=2)
    parsed = urlsplit(url)
    if parsed.scheme == "http" and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS:
        return _open_local_http(
            url,
            method=method,
            data=body,
            headers=headers,
            timeout=timeout,
        )
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        return _open(request, timeout)
    except urllib.error.HTTPError as exc:
        try:
            try:
                raw = exc.read(64 * 1024)
            except (http.client.HTTPException, OSError, TimeoutError):
                raw = b""
        finally:
            exc.close()
        raise _error_from_body(exc.code, raw, exc.headers) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        raise CaptureError(f"The API did not answer: {reason}") from exc
    except TimeoutError as exc:
        raise CaptureError("The API did not answer before the timeout.") from exc
    except OverflowError as exc:
        raise CaptureError("Request timeout is out of range.", exit_code=2) from exc
    except ValueError as exc:
        raise CaptureError(f"Invalid request header or URL: {exc}", exit_code=2) from exc
    except (http.client.HTTPException, OSError) as exc:
        raise CaptureError(f"The API did not answer: {exc}") from exc


def open_response(
    url: str,
    *,
    method: str,
    payload: dict[str, object] | None,
    headers: dict[str, str],
    timeout: float,
):
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return open_raw_response(
        url,
        method=method,
        body=data,
        headers=headers,
        timeout=timeout,
    )


def open_response_with_retries(
    url: str,
    *,
    method: str,
    payload: dict[str, object] | None = None,
    body: bytes | None = None,
    headers: dict[str, str],
    timeout: float,
    retries: int = 0,
    retry_delay_ms: int = 500,
    verbose: bool = False,
):
    attempts = max(0, int(retries)) + 1
    base_delay = max(0, int(retry_delay_ms)) / 1000.0
    last_exc: CaptureError | None = None
    for attempt in range(attempts):
        try:
            if body is not None:
                return open_raw_response(
                    url,
                    method=method,
                    body=body,
                    headers=headers,
                    timeout=timeout,
                )
            return open_response(
                url,
                method=method,
                payload=payload,
                headers=headers,
                timeout=timeout,
            )
        except CaptureError as exc:
            last_exc = exc
            retryable = exc.status is None or exc.status in _RETRYABLE_STATUSES
            if not retryable or attempt + 1 >= attempts:
                raise
            delay = exc.retry_after if exc.retry_after is not None else (base_delay * (2 ** min(attempt, 30)))
            if verbose:
                write_line(
                    sys.stderr,
                    f"Retry {attempt + 1}/{attempts - 1} in {delay:.2f}s after error: {exc}",
                )
            if delay > 0:
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def encode_multipart_form(
    fields: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str | None]] | None = None,
) -> tuple[str, bytes]:
    """Encode multipart/form-data fields and binary files using stdlib only."""
    boundary = f"----ViperCaptureBoundary{secrets.token_hex(16)}"
    parts: list[bytes] = []
    for name, value in (fields or {}).items():
        safe_name = name.replace('"', "")
        parts.append(f"--{boundary}\r\n".encode("ascii"))
        parts.append(
            f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'.encode("utf-8")
        )
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    for name, (filename, data, content_type) in (files or {}).items():
        safe_name = name.replace('"', "")
        clean_file = safe_filename(Path(filename).name)
        guessed = content_type or mimetypes.guess_type(clean_file)[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\n".encode("ascii"))
        parts.append(
            f'Content-Disposition: form-data; name="{safe_name}"; filename="{clean_file}"\r\n'.encode("utf-8")
        )
        parts.append(f"Content-Type: {guessed}\r\n\r\n".encode("ascii"))
        parts.append(data)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def _expected_content_length(response) -> int | None:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", 200)
    if status in {204, 304}:
        return None
    headers = getattr(response, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    transfer_encoding = str(headers.get("Transfer-Encoding") or headers.get("transfer-encoding") or "").lower()
    if "chunked" in transfer_encoding:
        return None
    raw_values: list[str] = []
    if hasattr(headers, "get_all"):
        for item in headers.get_all("Content-Length") or []:
            raw_values.append(str(item))
    else:
        raw = headers.get("Content-Length")
        if raw is None:
            raw = headers.get("content-length")
        if raw is not None:
            raw_values.append(str(raw))
    if not raw_values:
        return None
    parsed_values: list[int] = []
    for raw_item in raw_values:
        parts = raw_item.split(",")
        for part in parts:
            token = part.strip()
            if not token or len(token) > 4096 or not (token.isascii() and token.isdigit()):
                raise CaptureError(f"Invalid Content-Length header from API: {raw_item[:64]!r}.")
            normalized = token.lstrip("0") or "0"
            if len(normalized) > 20:
                raise CaptureError(f"Invalid Content-Length header from API: {raw_item[:64]!r}.")
            try:
                parsed_values.append(int(normalized))
            except (ValueError, OverflowError) as exc:
                raise CaptureError(f"Invalid Content-Length header from API: {raw_item[:64]!r}.") from exc
    if not parsed_values:
        raise CaptureError("Invalid Content-Length header from API.")
    if len(set(parsed_values)) > 1:
        raise CaptureError("Conflicting Content-Length headers from API.")
    return parsed_values[0]


def _read_bounded(response, handle) -> int:
    limit = max_capture_bytes()
    expected_length = _expected_content_length(response)
    if expected_length is not None and expected_length > limit:
        raise CaptureError(f"The capture is larger than {limit} bytes.")
    total = 0
    while True:
        if expected_length is not None:
            remaining = expected_length - total
            if remaining <= 0:
                return total
            chunk_size = min(1024 * 1024, remaining)
        else:
            chunk_size = 1024 * 1024
        try:
            block = response.read(chunk_size)
        except (http.client.IncompleteRead, http.client.HTTPException, TimeoutError, OSError) as exc:
            raise CaptureError(f"Incomplete response from API: {exc}") from exc
        if not block:
            if expected_length is not None and total < expected_length:
                raise CaptureError(
                    f"Incomplete response: expected {expected_length} bytes, received {total}."
                )
            return total
        total += len(block)
        if total > limit:
            raise CaptureError(f"The capture is larger than {limit} bytes.")
        handle.write(block)


def read_response_bytes(response) -> bytes:
    import io

    buf = io.BytesIO()
    total = _read_bounded(response, buf)
    if total <= 0:
        raise CaptureError("The API returned an empty response.")
    return buf.getvalue()


def write_bytes_atomic(path: Path, data: bytes) -> int:
    if not data:
        raise CaptureError("The API returned an empty capture.")
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_dir():
        raise CaptureError(f"{path} is a directory.")
    import tempfile

    descriptor, name = tempfile.mkstemp(prefix=".vipercapture-", suffix=".part", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return len(data)


def write_response(response, path: Path | None) -> int:
    if path is None:
        total = _read_bounded(response, sys.stdout.buffer)
        if total <= 0:
            raise CaptureError("The API returned an empty capture.")
        return total
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_dir():
        raise CaptureError(f"{path} is a directory.")
    import tempfile

    descriptor, name = tempfile.mkstemp(prefix=".vipercapture-", suffix=".part", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            total = _read_bounded(response, handle)
        if total <= 0:
            raise CaptureError("The API returned an empty capture.")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return total


def _header_map(headers) -> dict[str, str]:
    kept: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in {"content-type", "x-request-id"} or lowered.startswith("x-vipercapture-"):
            kept[key] = value
    return kept


def _media_type(headers) -> str:
    value = headers.get("Content-Type", "")
    return value.split(";", 1)[0].strip()


def filename_from_headers(headers, fallback: str) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^";\r\n]+)"?', disposition)
    if match:
        raw_name = Path(match.group(1)).name
        candidate = Path(raw_name)
        stem = safe_filename(candidate.stem)
        suffix = candidate.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", candidate.suffix) else ""
        return f"{stem}{suffix}"
    media = _media_type(headers).lower()
    ext = _MEDIA_EXTENSIONS.get(media, "")
    return f"{safe_filename(fallback)}{ext}"


def open_saved_file(path: Path) -> None:
    resolved = str(path.resolve())
    try:
        if sys.platform == "win32":
            os.startfile(resolved)  # type: ignore[attr-defined]
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if shutil.which(opener) is None:
            raise CaptureError(f"Could not open file: {opener} is not installed.")
        subprocess.Popen(
            [opener, resolved],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except CaptureError:
        raise
    except OSError as exc:
        raise CaptureError(f"Could not open {path}: {exc}") from exc


def copy_output_to_clipboard(path: Path, media_type: str = "") -> str:
    """Copy text output content or file path to the OS clipboard (best effort)."""
    resolved = path.resolve()
    is_text = (
        media_type.startswith("text/")
        or media_type == "application/json"
        or path.suffix.lower() in {".html", ".htm", ".md", ".markdown", ".json", ".txt"}
    )
    if is_text:
        try:
            text_payload = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CaptureError(f"Could not read {path} for clipboard copy: {exc}") from exc
        mode = "content"
    else:
        text_payload = str(resolved)
        mode = "path"

    if sys.platform == "win32":
        if shutil.which("clip"):
            proc = subprocess.run(
                ["clip"],
                input=text_payload.encode("utf-16-le"),
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0:
                return mode
        pwsh = shutil.which("powershell.exe") or shutil.which("pwsh")
        if pwsh:
            proc = subprocess.run(
                [pwsh, "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                input=text_payload,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0:
                return mode
        raise CaptureError("Clipboard copy failed: neither clip nor PowerShell Set-Clipboard is available.")

    if sys.platform == "darwin":
        if shutil.which("pbcopy"):
            proc = subprocess.run(
                ["pbcopy"],
                input=text_payload.encode("utf-8"),
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0:
                return mode
        raise CaptureError("Clipboard copy failed: pbcopy is not available.")

    for cmd in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        if shutil.which(cmd[0]):
            proc = subprocess.run(
                cmd,
                input=text_payload.encode("utf-8"),
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0:
                return mode
    raise CaptureError("Clipboard copy failed: install wl-copy, xclip, or xsel.")


def emit_success(args: argparse.Namespace, path: Path | None, total: int, headers) -> None:
    kept = _header_map(headers)
    request_id = headers.get("X-Request-Id")
    if path is None:
        if args.json:
            write_line(sys.stderr, _json_line({
                "ok": True,
                "path": "-",
                "bytes": total,
                "media_type": _media_type(headers),
                "request_id": request_id,
                "headers": kept,
            }))
        return
    shown = display_path(path)
    if args.json:
        write_line(sys.stdout, _json_line({
            "ok": True,
            "path": shown,
            "absolute": str(path.resolve()),
            "bytes": total,
            "media_type": _media_type(headers),
            "request_id": request_id,
            "headers": kept,
        }))
        return
    if not args.quiet:
        write_line(sys.stdout, shown)


def _log_tail(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-40:])[-2000:]


def _is_descendant_process(child_pid: int, ancestor_pid: int) -> bool:
    if child_pid == ancestor_pid:
        return True
    if sys.platform == "win32":
        return launch._is_descendant_pid(child_pid, ancestor_pid)
    elif sys.platform == "linux":
        try:
            cur = child_pid
            while cur > 1:
                stat_path = Path(f"/proc/{cur}/stat")
                if not stat_path.exists():
                    break
                content = stat_path.read_text()
                ppid = int(content[content.rfind(")") + 1 :].split()[1])
                if ppid == ancestor_pid:
                    return True
                cur = ppid
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            cur = child_pid
            while cur > 1:
                out = subprocess.check_output(
                    ["ps", "-o", "ppid=", "-p", str(cur)],
                    text=True,
                    errors="ignore",
                ).strip()
                if not out:
                    break
                ppid = int(out)
                if ppid == ancestor_pid:
                    return True
                cur = ppid
        except Exception:
            pass
    return False


def _wait_for_local_server(
    proc,
    log_path: Path,
    *,
    token: str = "",
) -> bool:
    """Return True when the process this command started is the listening server."""
    global _STARTED_SERVER_HANDLE
    started = time.monotonic()
    deadline = started + 60
    while time.monotonic() < deadline:
        if launch.port_open():
            pid = launch._is_vipercapture_endpoint()
            if pid is not None:
                is_ours = (pid == proc.pid) or _is_descendant_process(pid, proc.pid)
                if not is_ours:
                    if proc.poll() is None:
                        proc.terminate()
                    launch._clear_server_record(proc.pid)
                    return False
                if pid != proc.pid and token:
                    launch._record_server_process(pid, token, launch.PORT)
                _STARTED_SERVER_HANDLE = (proc, pid if pid != proc.pid else None)
                return True
        if proc.poll() is not None and not launch.port_open():
            break
        elapsed = time.monotonic() - started
        time.sleep(0.03 if elapsed < 3.0 else 0.08)
    detail = _log_tail(log_path)
    if proc.poll() is None:
        proc.terminate()
    launch._clear_server_record(proc.pid)
    message = "ViperCapture did not start."
    if detail:
        message = f"{message} {detail}"
    raise CaptureError(message)


def prepare_private_data_dir(env: dict[str, str], *, home: Path | None = None) -> None:
    """Point a server this command starts at an owner-only data directory.

    The API refuses a data path whose parent is group- or world-writable.
    A checkout created with umask 002 is group-writable, so the default
    directory inside that checkout cannot start the server. Windows keeps
    the process environment; its directory modes are not POSIX permissions.
    """
    if os.name == "nt" or env.get("VIPERCAPTURE_DATA_DIR"):
        return
    root = (home or Path.home()) / ".vipercapture"
    data = root / "data"
    root.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    for directory in (root, data):
        try:
            if directory.stat().st_uid == os.getuid():
                os.chmod(directory, 0o700)
        except OSError:
            pass
    env["VIPERCAPTURE_DATA_DIR"] = str(data)


def start_local_server() -> bool:
    env, token = launch._new_server_environment()
    prepare_private_data_dir(env)
    log_path = Path.home() / ".vipercapture" / "requests.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(log_fd, 0o600)
    log_handle = open(log_fd, "w", encoding="utf-8", buffering=1)
    python = str(launch.VENV_PYTHON if launch.VENV_PYTHON.exists() else Path(sys.executable))
    command = [
        python,
        "-m",
        "uvicorn",
        "vipercapture.main:app",
        "--host",
        launch.HOST,
        "--port",
        str(launch.PORT),
        "--ws",
        "none",
    ]
    kwargs: dict[str, object] = {
        "cwd": str(launch.ROOT),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    windows_flags = 0
    if os.name == "nt":
        windows_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        windows_flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        kwargs["creationflags"] = windows_flags | breakaway
    else:
        kwargs["start_new_session"] = True
    try:
        try:
            proc = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
        except OSError:
            if os.name != "nt":
                raise
            kwargs["creationflags"] = windows_flags
            proc = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
    finally:
        log_handle.close()
    launch._record_server_process(proc.pid, token, launch.PORT)
    return _wait_for_local_server(proc, log_path, token=token)


def stop_started_server() -> None:
    """Stop the local background server if this process started it."""
    global _STARTED_SERVER_HANDLE
    handle = _STARTED_SERVER_HANDLE
    _STARTED_SERVER_HANDLE = None
    if handle is None:
        return
    proc, worker_pid = handle
    launch.stop_server(proc, announce=False, worker_pid=worker_pid)


def ensure_server(args: argparse.Namespace) -> bool:
    if not is_local_default(args.server):
        return False
    if launch.port_open():
        if launch._is_vipercapture_endpoint() is not None:
            return False
        raise CaptureError(
            f"Port {launch.PORT} is open, but it is not ViperCapture. "
            "Choose another API with --server."
        )
    if getattr(args, "no_start", False):
        raise CaptureError(
            "ViperCapture is not running. Start it with vipercapture, or omit --no-start."
        )
    with _stderr_progress():
        launch.ensure_deps()
        launch.ensure_playwright()
    return start_local_server()


class _stderr_progress:
    def __enter__(self):
        self._stdout = sys.stdout
        sys.stdout = sys.stderr
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout = self._stdout


def request_timeout(args: argparse.Namespace, payload: dict[str, object]) -> float:
    if args.request_timeout is not None:
        return args.request_timeout
    if args.async_job or payload.get("output") in {"gif", "webm", "mp4"}:
        return 180
    return 120


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https":
        port = parsed.port or 443
    elif scheme == "http":
        port = parsed.port or 80
    else:
        port = parsed.port or 0
    return scheme, host, port


def resolve_api_url(root: str, url: str) -> str:
    """Follow a job URL only when it stays on the API the command was given."""
    text = url.strip()
    if not text or text.startswith("//") or any(char in text for char in "\r\n\x00\\"):
        raise CaptureError("The API returned a URL this command will not follow.")
    if "://" in text:
        parsed = urlsplit(text)
        if (
            parsed.username
            or parsed.password
            or parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or _origin(text) != _origin(root)
        ):
            raise CaptureError("The API returned a URL on a different host.")
        return text
    if ":" in text:
        raise CaptureError("The API returned a URL this command will not follow.")
    base = urlsplit(root)
    if text.startswith("/"):
        return f"{base.scheme}://{base.netloc}{text}"
    return root.rstrip("/") + "/" + text


_MAX_JSON_BYTES = 1024 * 1024


def _read_json_value(response) -> object:
    expected_length = _expected_content_length(response)
    if expected_length is not None and expected_length > _MAX_JSON_BYTES:
        raise CaptureError("The API returned a JSON document that is too large.")
    if expected_length is None:
        try:
            raw = response.read(_MAX_JSON_BYTES + 1)
        except (http.client.IncompleteRead, http.client.HTTPException, OSError, TimeoutError) as exc:
            raise CaptureError(f"Incomplete response from API: {exc}") from exc
        if len(raw) > _MAX_JSON_BYTES:
            raise CaptureError("The API returned a JSON document that is too large.")
    else:
        chunks: list[bytes] = []
        total = 0
        while total < expected_length:
            to_read = min(64 * 1024, expected_length - total)
            try:
                block = response.read(to_read)
            except (http.client.IncompleteRead, http.client.HTTPException, OSError, TimeoutError) as exc:
                raise CaptureError(f"Incomplete response from API: {exc}") from exc
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > _MAX_JSON_BYTES:
                raise CaptureError("The API returned a JSON document that is too large.")
        if total < expected_length:
            raise CaptureError(
                f"Incomplete response: expected {expected_length} bytes, received {total}."
            )
        raw = b"".join(chunks)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CaptureError("The API did not return JSON.") from exc


def _read_json(response) -> dict[str, object]:
    parsed = _read_json_value(response)
    if not isinstance(parsed, dict):
        raise CaptureError("The API did not return a JSON object.")
    return parsed


def _open_json_with_retries(
    url: str,
    *,
    method: str,
    payload: dict[str, object] | None,
    headers: dict[str, str],
    timeout_fn,
    retries: int = 0,
    retry_delay_ms: int = 500,
    verbose: bool = False,
    deadline: float | None = None,
) -> tuple[object, dict[str, object]]:
    base_delay = max(0, int(retry_delay_ms)) / 1000.0
    for attempt in range(max(0, int(retries)) + 1):
        opened_ok = False
        try:
            resp = open_response_with_retries(
                url,
                method=method,
                payload=payload,
                headers=headers,
                timeout=timeout_fn(),
                retries=0,
                retry_delay_ms=retry_delay_ms,
                verbose=verbose,
            )
            opened_ok = True
            try:
                doc = _read_json(resp)
                resp_headers = resp.headers
            finally:
                resp.close()
            return resp_headers, doc
        except CaptureError as exc:
            if opened_ok:
                retryable = "Incomplete response" in str(exc)
            else:
                retryable = exc.status is None or exc.status in _RETRYABLE_STATUSES
            if not retryable or attempt >= retries:
                raise
            delay_s = exc.retry_after if exc.retry_after is not None else (base_delay * (2 ** min(attempt, 30)))
            if verbose:
                write_line(
                    sys.stderr,
                    f"Retry {attempt + 1}/{retries} in {delay_s:.2f}s after error: {exc}",
                )
            if delay_s > 0:
                sleep_s = min(delay_s, _remaining(deadline)) if deadline is not None else delay_s
                time.sleep(sleep_s)
    raise CaptureError("Request failed after retries.")


def _execute_one(
    args: argparse.Namespace,
    payload: dict[str, object],
    *,
    root: str,
    output: Path | None,
    started_server: bool,
    idempotency_key: str | None = None,
) -> tuple[object, int, dict[str, object] | None]:
    timeout = request_timeout(args, payload)
    retries = max(0, int(getattr(args, "retries", 0) or 0))
    retry_delay_ms = max(0, int(getattr(args, "retry_delay_ms", 500) or 0))
    effective_idem = idempotency_key if idempotency_key is not None else getattr(args, "idempotency_key", None)
    if args.async_job and retries > 0 and not effective_idem:
        import uuid

        effective_idem = f"vc-retry-{uuid.uuid4().hex}"
    if args.verbose and not args.quiet:
        route = "/v1/jobs" if args.async_job else "/v1/render"
        write_line(sys.stderr, f"POST {display_server(root)}{route}")
    if args.async_job:
        if getattr(args, "no_wait", False):
            headers, job = _open_json_with_retries(
                root + "/v1/jobs",
                method="POST",
                payload=payload,
                headers=request_headers(
                    args,
                    started_server=started_server,
                    json_body=True,
                    idempotency_key=effective_idem,
                ),
                timeout_fn=lambda: min(30.0, timeout),
                retries=retries,
                retry_delay_ms=retry_delay_ms,
                verbose=bool(args.verbose and not args.quiet),
            )
            return headers, 0, job
        response_headers, total = _execute_async(
            args,
            payload,
            root=root,
            output=output,
            timeout=timeout,
            started_server=started_server,
            retries=retries,
            retry_delay_ms=retry_delay_ms,
            idempotency_key=effective_idem,
        )
        return response_headers, total, None

    base_delay = retry_delay_ms / 1000.0
    for attempt in range(retries + 1):
        opened_ok = False
        try:
            response = open_response_with_retries(
                root + "/v1/render",
                method="POST",
                payload=payload,
                headers=request_headers(
                    args,
                    started_server=started_server,
                    json_body=True,
                    idempotency_key=effective_idem,
                ),
                timeout=timeout,
                retries=0,
                retry_delay_ms=retry_delay_ms,
                verbose=bool(args.verbose and not args.quiet),
            )
            opened_ok = True
            try:
                total = write_response(response, output)
                response_headers = response.headers
                return response_headers, total, None
            finally:
                response.close()
        except CaptureError as exc:
            if opened_ok:
                is_truncated = "Incomplete response" in str(exc) or "empty capture" in str(exc)
                retryable = output is not None and is_truncated
            else:
                retryable = exc.status is None or exc.status in _RETRYABLE_STATUSES
            if not retryable or attempt >= retries:
                raise
            delay_s = exc.retry_after if exc.retry_after is not None else (base_delay * (2 ** min(attempt, 30)))
            if args.verbose and not args.quiet:
                write_line(
                    sys.stderr,
                    f"Retry {attempt + 1}/{retries} in {delay_s:.2f}s after error: {exc}",
                )
            if delay_s > 0:
                time.sleep(delay_s)
    raise CaptureError("Capture failed after retries.")


def execute(args: argparse.Namespace, payload: dict[str, object], *, started_server: bool) -> int:
    root = api_root(args.server)
    output = None if (args.async_job and getattr(args, "no_wait", False)) else resolve_output(args, payload)
    response_headers, total, async_job_doc = _execute_one(
        args,
        payload,
        root=root,
        output=output,
        started_server=started_server,
    )
    if async_job_doc is not None:
        if args.json:
            write_line(sys.stdout, _json_line({"ok": True, **async_job_doc}))
        elif not args.quiet:
            write_line(sys.stdout, str(async_job_doc.get("id") or ""))
        return 0
    if output is not None:
        if getattr(args, "copy_output", False):
            copy_output_to_clipboard(output, _media_type(response_headers))
        if getattr(args, "open_output", False):
            open_saved_file(output)
    emit_success(args, output, total, response_headers)
    return 0


def _execute_async(
    args: argparse.Namespace,
    payload: dict[str, object],
    *,
    root: str,
    output: Path | None,
    timeout: float,
    started_server: bool,
    retries: int = 0,
    retry_delay_ms: int = 500,
    idempotency_key: str | None = None,
) -> tuple[object, int]:
    deadline = time.monotonic() + timeout
    verbose = bool(args.verbose and not args.quiet)
    base_delay = max(0, int(retry_delay_ms)) / 1000.0
    _, job = _open_json_with_retries(
        root + "/v1/jobs",
        method="POST",
        payload=payload,
        headers=request_headers(
            args,
            started_server=started_server,
            json_body=True,
            idempotency_key=idempotency_key,
        ),
        timeout_fn=lambda: min(30, _remaining(deadline)),
        retries=retries,
        retry_delay_ms=retry_delay_ms,
        verbose=verbose,
        deadline=deadline,
    )
    while True:
        status = str(job.get("status") or "")
        if status == "succeeded":
            result_url = job.get("result_url")
            if not isinstance(result_url, str) or not result_url:
                raise CaptureError("The async job did not include a result URL.")
            resolved_result_url = resolve_api_url(root, result_url)
            for attempt in range(max(0, int(retries)) + 1):
                opened_ok = False
                try:
                    result = open_response_with_retries(
                        resolved_result_url,
                        method="GET",
                        payload=None,
                        headers=request_headers(args, started_server=started_server, json_body=False),
                        timeout=_remaining(deadline),
                        retries=0,
                        retry_delay_ms=retry_delay_ms,
                        verbose=verbose,
                    )
                    opened_ok = True
                    try:
                        return result.headers, write_response(result, output)
                    finally:
                        result.close()
                except CaptureError as exc:
                    if opened_ok:
                        is_truncated = "Incomplete response" in str(exc) or "empty capture" in str(exc)
                        retryable = output is not None and is_truncated
                    else:
                        retryable = exc.status is None or exc.status in _RETRYABLE_STATUSES
                    if not retryable or attempt >= retries:
                        raise
                    delay_s = exc.retry_after if exc.retry_after is not None else (base_delay * (2 ** min(attempt, 30)))
                    if verbose:
                        write_line(
                            sys.stderr,
                            f"Retry {attempt + 1}/{retries} in {delay_s:.2f}s after error: {exc}",
                        )
                    if delay_s > 0:
                        time.sleep(min(delay_s, _remaining(deadline)))
            raise CaptureError("Async result download failed after retries.")
        if status not in _OPEN_JOBS:
            error = job.get("error")
            message = status or "The async job failed."
            code = None
            if isinstance(error, dict):
                message = str(error.get("message") or message)
                if isinstance(error.get("code"), str):
                    code = error["code"]
            raise CaptureError(message, code=code, body=job)
        status_url = job.get("status_url")
        if not isinstance(status_url, str) or not status_url:
            raise CaptureError("The async job did not include a status URL.")
        time.sleep(min(max(args.poll_ms, 1) / 1000, _remaining(deadline)))
        _, job = _open_json_with_retries(
            resolve_api_url(root, status_url),
            method="GET",
            payload=None,
            headers=request_headers(args, started_server=started_server, json_body=False),
            timeout_fn=lambda: min(30, _remaining(deadline)),
            retries=retries,
            retry_delay_ms=retry_delay_ms,
            verbose=verbose,
            deadline=deadline,
        )


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise CaptureError("The capture timed out.")
    return left


def _read_urls_file(path: str) -> list[str]:
    text = _read_text(path)
    urls: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    if not urls:
        raise ValueError("--urls-file did not contain any URLs.")
    return urls


def _collect_multi_urls(args: argparse.Namespace) -> list[str] | None:
    targets = list(getattr(args, "targets", []) or [])
    urls_file = getattr(args, "urls_file", None)
    if not urls_file and len(targets) <= 1:
        return None
    if args.url:
        raise ValueError("Do not combine --url with multiple positional URLs or --urls-file.")
    if args.html or args.html_file or args.markdown or args.markdown_file:
        raise ValueError("Multi-URL capture cannot be combined with --html or --markdown.")
    urls: list[str] = list(targets)
    if urls_file:
        urls.extend(_read_urls_file(urls_file))
    if not urls:
        raise ValueError("Pass at least one URL.")
    return urls


def _validate_client_options(args: argparse.Namespace) -> None:
    if getattr(args, "no_wait", False) and not args.async_job:
        raise ValueError("--no-wait requires --async.")
    raw_timeout = getattr(args, "request_timeout", None)
    if raw_timeout is not None:
        if (
            not isinstance(raw_timeout, (int, float))
            or isinstance(raw_timeout, bool)
            or not math.isfinite(float(raw_timeout))
            or float(raw_timeout) <= 0
            or float(raw_timeout) > _MAX_REQUEST_TIMEOUT_S
        ):
            raise ValueError(
                f"--request-timeout must be a finite number greater than 0 and at most {int(_MAX_REQUEST_TIMEOUT_S)}."
            )
    poll_ms = getattr(args, "poll_ms", 500)
    if poll_ms is not None and (int(poll_ms) < 1 or int(poll_ms) > _MAX_INT_OPTION):
        raise ValueError(f"--poll-ms must be between 1 and {_MAX_INT_OPTION}.")
    if getattr(args, "retries", 0) is not None and (int(args.retries) < 0 or int(args.retries) > _MAX_RETRIES):
        raise ValueError(f"--retries must be between 0 and {_MAX_RETRIES}.")
    if getattr(args, "retry_delay_ms", 500) is not None and (
        int(args.retry_delay_ms) < 0 or int(args.retry_delay_ms) > _MAX_INT_OPTION
    ):
        raise ValueError(f"--retry-delay-ms must be between 0 and {_MAX_INT_OPTION}.")
    api_key = getattr(args, "api_key", None)
    if api_key is not None:
        _validate_header_value(str(api_key), "--api-key")
    req_id = getattr(args, "request_id", None)
    if req_id is not None:
        _validate_header_value(str(req_id), "--request-id")
    idem = getattr(args, "idempotency_key", None)
    if idem is not None:
        _validate_header_value(str(idem), "--idempotency-key")
    if args.output == "-" and getattr(args, "open_output", False):
        raise ValueError("--open cannot be used with --output -.")
    if args.output == "-" and getattr(args, "copy_output", False):
        raise ValueError("--copy cannot be used with --output -.")


def _validate_single_stdin_source(args: argparse.Namespace) -> None:
    sources: list[str] = []
    for attr, flag in (
        ("urls_file", "--urls-file"),
        ("from_json", "--from-json"),
        ("html_file", "--html-file"),
        ("markdown_file", "--markdown-file"),
        ("css_file", "--css-file"),
        ("pdf_header_file", "--pdf-header-file"),
        ("pdf_footer_file", "--pdf-footer-file"),
    ):
        if getattr(args, attr, None) == "-":
            sources.append(flag)
    if any(p == "-" for p in (getattr(args, "cookie_file", None) or [])):
        sources.append("--cookie-file")
    if any(kind == "js_file" and val == "-" for kind, val in (getattr(args, "steps", None) or [])):
        sources.append("--js-file")
    if len(sources) > 1:
        raise ValueError(f"Only one option can read from stdin ('-'); got {', '.join(sources)}.")


def run_capture(args: argparse.Namespace) -> int:
    try:
        apply_config_defaults(args)
        _validate_client_options(args)
        _validate_single_stdin_source(args)
        stdin_cache: dict[str, str] = {}
        multi_urls = _collect_multi_urls(args)
        multi_targets: list[Path | None] | None = None
        if multi_urls is not None:
            if args.output:
                raise ValueError("Use --output-dir (and optionally --output-template) instead of --output for multi-URL captures.")
            if getattr(args, "copy_output", False):
                raise ValueError("--copy is only supported for single-URL captures.")
            normalized_urls = [normalize_url(u) for u in multi_urls]
            distinct_hosts = {(urlsplit(u).hostname or "").lower() for u in normalized_urls}
            allow_default_cookie_domain = len(distinct_hosts) <= 1
            base_doc, has_base = _load_base_document(args, stdin_cache=stdin_cache)
            payloads = [
                build_payload(
                    args,
                    override_url=u,
                    base_document=base_doc if has_base else None,
                    allow_default_cookie_domain=allow_default_cookie_domain,
                    stdin_cache=stdin_cache,
                )
                for u in normalized_urls
            ]
            for item in payloads:
                validate_payload(item)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            if not (args.async_job and getattr(args, "no_wait", False)):
                multi_targets = []
                seen_paths: set[str] = set()
                for index, item_payload in enumerate(payloads, start=1):
                    target_path = resolve_output(args, item_payload, index=index, multi=True, timestamp=stamp)
                    assert target_path is not None
                    norm_key = os.path.normcase(str(target_path.resolve()))
                    if norm_key in seen_paths:
                        raise ValueError(
                            f"Multiple URLs resolve to the same output file ({target_path}). "
                            "Include {index} or {slug} in --output-template."
                        )
                    seen_paths.add(norm_key)
                    multi_targets.append(target_path)
            else:
                multi_targets = [None for _ in payloads]
        else:
            payload = build_payload(args, stdin_cache=stdin_cache)
            validate_payload(payload)
            if not args.dry_run and not (args.async_job and getattr(args, "no_wait", False)):
                resolve_output(args, payload)
            payloads = None
    except (ValueError, OverflowError) as exc:
        return fail(str(exc), exit_code=2)

    if args.dry_run:
        if payloads is not None:
            write_line(
                sys.stdout,
                json.dumps([public_payload(item) for item in payloads], ensure_ascii=False, indent=2),
            )
        else:
            write_line(sys.stdout, json.dumps(public_payload(payload), ensure_ascii=False, indent=2))
        return 0

    started = False
    try:
        load_local_env()
        started = ensure_server(args)
        if started and not args.quiet:
            write_line(
                sys.stderr,
                f"ViperCapture is listening on http://{launch.HOST}:{launch.PORT}",
            )
            if not getattr(args, "stop_server", False):
                write_line(sys.stderr, "Stop it with: vipercapture kill")

        if payloads is None:
            return execute(args, payload, started_server=started)

        root = api_root(args.server)
        results: list[dict[str, object]] = []
        failures = 0
        base_idem = getattr(args, "idempotency_key", None)
        assert multi_targets is not None
        for index, (item_payload, target_path) in enumerate(zip(payloads, multi_targets), start=1):
            item_idem = derive_batch_idempotency_key(base_idem, str(index)) if base_idem else None
            try:
                headers, total, job_doc = _execute_one(
                    args,
                    item_payload,
                    root=root,
                    output=target_path,
                    started_server=started,
                    idempotency_key=item_idem,
                )
                if job_doc is not None:
                    entry: dict[str, object] = {
                        "ok": True,
                        "url": item_payload.get("url"),
                        "job": job_doc,
                        "id": job_doc.get("id"),
                    }
                    results.append(entry)
                    if not args.json and not args.quiet:
                        write_line(sys.stdout, str(job_doc.get("id") or ""))
                else:
                    assert target_path is not None
                    shown = display_path(target_path)
                    if getattr(args, "open_output", False):
                        open_saved_file(target_path)
                    results.append({
                        "ok": True,
                        "url": item_payload.get("url"),
                        "path": shown,
                        "absolute": str(target_path.resolve()),
                        "bytes": total,
                        "media_type": _media_type(headers),
                    })
                    if not args.json and not args.quiet:
                        write_line(sys.stdout, shown)
            except (CaptureError, OSError) as exc:
                failures += 1
                err_status = exc.status if isinstance(exc, CaptureError) else None
                err_code = exc.code if isinstance(exc, CaptureError) else None
                err_body = exc.body if isinstance(exc, CaptureError) else None
                results.append({
                    "ok": False,
                    "url": item_payload.get("url"),
                    "error": str(exc),
                    "status": err_status,
                    "code": err_code,
                })
                if not args.json:
                    fail(str(exc), status=err_status, code=err_code, body=err_body)
        if args.json:
            write_line(
                sys.stdout,
                _json_line({
                    "ok": failures == 0,
                    "count": len(results),
                    "succeeded": len(results) - failures,
                    "failed": failures,
                    "results": results,
                }),
            )
        return 0 if failures == 0 else 1
    except (ValueError, OverflowError) as exc:
        return fail(str(exc), exit_code=2)
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)
    except OSError as exc:
        return fail(str(exc))
    except BrokenPipeError:
        return 0
    finally:
        if started and getattr(args, "stop_server", False):
            stop_started_server()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    try:
        args = parse_capture_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    try:
        return run_capture(args)
    except KeyboardInterrupt:
        return fail("Capture cancelled.", exit_code=130)


if __name__ == "__main__":
    raise SystemExit(main())
