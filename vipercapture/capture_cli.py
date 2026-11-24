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
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import TextIO
from urllib.parse import urljoin, urlsplit

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
    client.add_argument("--request-timeout", type=float, help="Seconds to wait for the render. Video defaults to 180 and images to 120.")
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
            settings["videoDuration"] = round(float(video["duration_ms"]) / 1000.0, 3)
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
        if isinstance(wait_for.get("delay_ms"), (int, float)):
            settings["waitDelay"] = round(float(wait_for["delay_ms"]) / 1000.0, 3)
        if isinstance(wait_for.get("selector"), str):
            settings["waitSelector"] = wait_for["selector"]
        if isinstance(wait_for.get("text"), str):
            settings["waitText"] = wait_for["text"]
        if isinstance(wait_for.get("timeout_ms"), (int, float)):
            settings["waitTimeout"] = round(float(wait_for["timeout_ms"]) / 1000.0, 3)
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
    if "fullPage" in settings:
        full_page = _coerce_bool(settings["fullPage"], True)
        if not full_page:
            payload["full_page"] = False
    else:
        full_page = True
    if full_page and _coerce_bool(settings.get("preserveViewportWidth"), False):
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

    selector = str(settings.get("selector") or "").strip()
    if selector and not full_page:
        payload["selector"] = selector
    if _coerce_bool(settings.get("clipEnabled"), False) and not full_page:
        payload["clip"] = {
            "x": float(settings.get("clipX", 0)),
            "y": float(settings.get("clipY", 0)),
            "width": float(settings.get("clipWidth", 640)),
            "height": float(settings.get("clipHeight", 480)),
        }

    is_image = fmt in {"png", "jpeg", "webp", "avif"}
    is_video = fmt in {"webm", "mp4", "gif"}
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

    if fmt in {"html", "markdown"} and settings.get("extractMode") in _EXTRACT:
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
            if any(not _FAST_SIMPLE_LABEL.fullmatch(label) for label in labels):
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
    "deterministic", "certification", "image", "wait_for", "cleanup", "captcha", "headers",
})
_FALLBACK_NULLABLE_DICT_SECTIONS = frozenset({"slices", "video", "clip", "pdf"})
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


def _validate_payload_fallback(payload: dict[str, object]) -> None:
    """Stdlib fallback validator used when Pydantic/RenderRequest is unavailable."""
    if not isinstance(payload, dict):
        raise ValueError("Render payload must be a JSON object.")
    unknown = sorted(set(payload.keys()) - _FALLBACK_ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"Unknown render request field(s): {', '.join(unknown)}")
    sources = sum(1 for k in ("url", "html", "markdown") if k in payload and payload[k] is not None)
    if sources != 1:
        raise ValueError("Render payload must specify exactly one of url, html, or markdown.")
    for str_key in _FALLBACK_NULLABLE_STR_FIELDS:
        if str_key in payload and payload[str_key] is not None and not isinstance(payload[str_key], str):
            raise ValueError(f"{str_key} must be a string.")
    for dict_key in _FALLBACK_DICT_SECTIONS:
        if dict_key in payload and not isinstance(payload[dict_key], dict):
            raise ValueError(f"{dict_key} must be an object.")
    for ndict_key in _FALLBACK_NULLABLE_DICT_SECTIONS:
        if ndict_key in payload and payload[ndict_key] is not None and not isinstance(payload[ndict_key], dict):
            raise ValueError(f"{ndict_key} must be an object or null.")
    for list_key in _FALLBACK_LIST_SECTIONS:
        if list_key in payload and not isinstance(payload[list_key], list):
            raise ValueError(f"{list_key} must be a list.")
    for nlist_key in _FALLBACK_NULLABLE_LIST_SECTIONS:
        if nlist_key in payload and payload[nlist_key] is not None and not isinstance(payload[nlist_key], list):
            raise ValueError(f"{nlist_key} must be a list or null.")
    if "output" in payload:
        out = payload["output"]
        if not isinstance(out, str) or out not in _FORMATS:
            raise ValueError(f"output must be one of: {', '.join(_FORMATS)}")
    if "engine" in payload:
        eng = payload["engine"]
        if not isinstance(eng, str) or eng not in _ENGINES:
            raise ValueError(f"engine must be one of: {', '.join(_ENGINES)}")
    for bool_key in _FALLBACK_BOOL_FIELDS:
        if bool_key in payload and type(payload[bool_key]) is not bool:
            raise ValueError(f"{bool_key} must be a boolean.")
    if "lazy_load" in payload and (not isinstance(payload["lazy_load"], str) or payload["lazy_load"] not in _LAZY):
        raise ValueError(f"lazy_load must be one of: {', '.join(_LAZY)}")
    if "profile" in payload and (not isinstance(payload["profile"], str) or payload["profile"] not in _PROFILES):
        raise ValueError(f"profile must be one of: {', '.join(_PROFILES)}")
    if "extract_mode" in payload and (not isinstance(payload["extract_mode"], str) or payload["extract_mode"] not in _EXTRACT):
        raise ValueError(f"extract_mode must be one of: {', '.join(_EXTRACT)}")
    if "viewport" in payload:
        vp = payload["viewport"]
        assert isinstance(vp, dict)
        for dim in ("width", "height"):
            if dim in vp and (not _is_int(vp[dim]) or vp[dim] < 1):  # type: ignore[operator]
                raise ValueError(f"viewport.{dim} must be a positive integer.")
        if "device_scale_factor" in vp and (not _is_number(vp["device_scale_factor"]) or vp["device_scale_factor"] <= 0):  # type: ignore[operator]
            raise ValueError("viewport.device_scale_factor must be a positive number.")
    if "environment" in payload:
        env = payload["environment"]
        assert isinstance(env, dict)
        if "device" in env and (not isinstance(env["device"], str) or env["device"] not in _DEVICES):
            raise ValueError("environment.device is invalid.")
        if "color_scheme" in env and env["color_scheme"] is not None and (
            not isinstance(env["color_scheme"], str) or env["color_scheme"] not in _COLOR_SCHEMES
        ):
            raise ValueError("environment.color_scheme is invalid.")
        if "reduced_motion" in env and env["reduced_motion"] is not None and (
            not isinstance(env["reduced_motion"], str) or env["reduced_motion"] not in _REDUCED_MOTION
        ):
            raise ValueError("environment.reduced_motion is invalid.")
        if "media" in env and env["media"] is not None and (
            not isinstance(env["media"], str) or env["media"] not in _MEDIA
        ):
            raise ValueError("environment.media is invalid.")
    if "wait_for" in payload:
        wf = payload["wait_for"]
        assert isinstance(wf, dict)
        if "event" in wf and (not isinstance(wf["event"], str) or wf["event"] not in _WAIT_EVENTS):
            raise ValueError("wait_for.event is invalid.")
        if "selector_state" in wf and (not isinstance(wf["selector_state"], str) or wf["selector_state"] not in _WAIT_STATES):
            raise ValueError("wait_for.selector_state is invalid.")
    if "actions" in payload:
        acts = payload["actions"]
        assert isinstance(acts, list)
        for act in acts:
            if not isinstance(act, dict) or not isinstance(act.get("type"), str) or act["type"] not in _FAST_ALL_ACTIONS:
                raise ValueError("Each action must be an object with a valid type.")
    if "headers" in payload:
        hdrs = payload["headers"]
        assert isinstance(hdrs, dict)
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in hdrs.items()):
            raise ValueError("headers must be a mapping of string names to string values.")


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
        or (isinstance(diagnostics, dict) and diagnostics.get("bundle"))
        or (isinstance(certification, dict) and certification.get("enabled"))
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
    host = safe_filename(parsed.hostname if parsed and parsed.hostname else "capture")
    path_slug = safe_filename(parsed.path.strip("/").replace("/", "-")) if parsed and parsed.path.strip("/") else host
    stamp = timestamp or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        rendered = template.format(
            host=host,
            index=index,
            slug=path_slug,
            timestamp=stamp,
            format=fmt,
            ext=ext,
        )
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError(
            f"Invalid --output-template {template!r}: {exc}. "
            "Supported placeholders: {host}, {index}, {slug}, {timestamp}, {format}, {ext}."
        ) from exc
    if "/" in rendered or "\\" in rendered or rendered in {"", ".", ".."}:
        raise ValueError("--output-template must produce a filename without directory separators.")
    candidate = Path(rendered)
    stem = safe_filename(candidate.stem)
    suffix = candidate.suffix
    if suffix and not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        suffix = ext
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
        return _header_token(api_key)
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
    req_id = getattr(args, "request_id", None)
    if req_id:
        headers["X-Request-Id"] = req_id
    idem = idempotency_key if idempotency_key is not None else getattr(args, "idempotency_key", None)
    if idem:
        headers["Idempotency-Key"] = idem
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
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise CaptureError(f"The API did not answer: {exc}") from exc
        if resp.status in {301, 302, 303, 307, 308}:
            location = resp.getheader("Location")
            resp.read(4096)
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
                raw = resp.read(64 * 1024)
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
            raw = exc.read(64 * 1024)
        finally:
            exc.close()
        raise _error_from_body(exc.code, raw, exc.headers) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        raise CaptureError(f"The API did not answer: {reason}") from exc
    except TimeoutError as exc:
        raise CaptureError("The API did not answer before the timeout.") from exc


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
            delay = exc.retry_after if exc.retry_after is not None else (base_delay * (2**attempt))
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


def _read_bounded(response, handle) -> int:
    limit = max_capture_bytes()
    total = 0
    while True:
        block = response.read(1024 * 1024)
        if not block:
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
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if total <= 0:
        path.unlink(missing_ok=True)
        raise CaptureError("The API returned an empty capture.")
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
    raw = response.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise CaptureError("The API returned a JSON document that is too large.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CaptureError("The API did not return JSON.") from exc


def _read_json(response) -> dict[str, object]:
    parsed = _read_json_value(response)
    if not isinstance(parsed, dict):
        raise CaptureError("The API did not return a JSON object.")
    return parsed


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
            submitted = open_response_with_retries(
                root + "/v1/jobs",
                method="POST",
                payload=payload,
                headers=request_headers(
                    args,
                    started_server=started_server,
                    json_body=True,
                    idempotency_key=effective_idem,
                ),
                timeout=min(30.0, timeout),
                retries=retries,
                retry_delay_ms=retry_delay_ms,
                verbose=bool(args.verbose and not args.quiet),
            )
            try:
                job = _read_json(submitted)
                headers = submitted.headers
            finally:
                submitted.close()
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
        retries=retries,
        retry_delay_ms=retry_delay_ms,
        verbose=bool(args.verbose and not args.quiet),
    )
    try:
        total = write_response(response, output)
        response_headers = response.headers
    finally:
        response.close()
    return response_headers, total, None


def execute(args: argparse.Namespace, payload: dict[str, object], *, started_server: bool) -> int:
    root = api_root(args.server)
    output = resolve_output(args, payload)
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
    submitted = open_response_with_retries(
        root + "/v1/jobs",
        method="POST",
        payload=payload,
        headers=request_headers(
            args,
            started_server=started_server,
            json_body=True,
            idempotency_key=idempotency_key,
        ),
        timeout=min(30, _remaining(deadline)),
        retries=retries,
        retry_delay_ms=retry_delay_ms,
        verbose=verbose,
    )
    try:
        job = _read_json(submitted)
    finally:
        submitted.close()
    while True:
        status = str(job.get("status") or "")
        if status == "succeeded":
            result_url = job.get("result_url")
            if not isinstance(result_url, str) or not result_url:
                raise CaptureError("The async job did not include a result URL.")
            result = open_response_with_retries(
                resolve_api_url(root, result_url),
                method="GET",
                payload=None,
                headers=request_headers(args, started_server=started_server, json_body=False),
                timeout=_remaining(deadline),
                retries=retries,
                retry_delay_ms=retry_delay_ms,
                verbose=verbose,
            )
            try:
                return result.headers, write_response(result, output)
            finally:
                result.close()
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
        polled = open_response_with_retries(
            resolve_api_url(root, status_url),
            method="GET",
            payload=None,
            headers=request_headers(args, started_server=started_server, json_body=False),
            timeout=min(30, _remaining(deadline)),
            retries=retries,
            retry_delay_ms=retry_delay_ms,
            verbose=verbose,
        )
        try:
            job = _read_json(polled)
        finally:
            polled.close()


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
    if getattr(args, "retries", 0) is not None and int(args.retries) < 0:
        raise ValueError("--retries must be 0 or greater.")
    if getattr(args, "retry_delay_ms", 500) is not None and int(args.retry_delay_ms) < 0:
        raise ValueError("--retry-delay-ms must be 0 or greater.")
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
            payloads = None
    except ValueError as exc:
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
                    results.append({
                        "ok": True,
                        "url": item_payload.get("url"),
                        "path": shown,
                        "absolute": str(target_path.resolve()),
                        "bytes": total,
                        "media_type": _media_type(headers),
                    })
                    if getattr(args, "open_output", False):
                        open_saved_file(target_path)
                    if not args.json and not args.quiet:
                        write_line(sys.stdout, shown)
            except CaptureError as exc:
                failures += 1
                results.append({
                    "ok": False,
                    "url": item_payload.get("url"),
                    "error": str(exc),
                    "status": exc.status,
                    "code": exc.code,
                })
                if not args.json:
                    fail(str(exc), status=exc.status, code=exc.code, body=exc.body)
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
