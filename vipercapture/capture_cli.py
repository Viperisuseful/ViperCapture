"""Agent-friendly ``vipercapture capture`` client.

The saved file path is written to stdout. Errors are one JSON object on
stderr. Exit 0 is success, 2 is a bad command, and 1 is a capture or
network failure. Flag names are the same on Linux, macOS, and Windows;
the shell quotes arguments before Python sees them.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

import launch

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
_OPEN_JOBS = {"queued", "running"}


class CaptureError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        body: object = None,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body
        self.exit_code = exit_code


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


def public_payload(payload: dict[str, object]) -> dict[str, object]:
    """A copy that can be printed. The proxy password is never copied out."""

    def walk(value: object) -> object:
        if isinstance(value, dict):
            printed: dict[str, object] = {}
            for key, item in value.items():
                printed[key] = "***" if key == "password" else walk(item)
            return printed
        if isinstance(value, list):
            return [walk(item) for item in value]
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
            "vipercapture kill."
        ),
    )
    parser.set_defaults(steps=None)
    parser.add_argument("target", nargs="?", help="Page URL. https:// is added when the scheme is omitted.")
    parser.add_argument("--url", help="Page URL. Use this instead of the positional argument.")

    target = parser.add_argument_group("input")
    target.add_argument("--html", help="HTML document text.")
    target.add_argument("--html-file", help="HTML file, or - for stdin.")
    target.add_argument("--markdown", help="Markdown text.")
    target.add_argument("--markdown-file", help="Markdown file, or - for stdin.")
    target.add_argument("--base-url", help="Base URL for HTML or Markdown input.")

    output = parser.add_argument_group("output")
    output.add_argument("--output", help="File to write. Use - for stdout. The extension selects the format.")
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
    wait.add_argument("--wait", dest="wait_selector", help="CSS selector to see before actions.")
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
    _record(actions, "--action-wait", kind="action_wait", help="Wait for a selector after earlier actions.")
    _record(actions, "--pause-ms", kind="pause", type=int, help="Wait this many milliseconds after earlier actions.")
    _record(actions, "--action", kind="json", help="One action as a JSON object. Ordered with the other action flags.")
    _record(actions, "--action-delay-ms", kind="action_delay", type=int, help="delay_ms for every action that follows.")
    _record(actions, "--action-timeout-ms", kind="action_timeout", type=int, help="timeout_ms for every action that follows.")

    environment = parser.add_argument_group("environment")
    environment.add_argument("--color-scheme", choices=_COLOR_SCHEMES, help="light, dark, or no-preference.")
    environment.add_argument("--reduced-motion", choices=_REDUCED_MOTION, help="reduce or no-preference.")
    environment.add_argument("--media", choices=_MEDIA, help="screen or print, applied before load.")
    environment.add_argument("--locale", help="BCP 47 language tag, for example fr-FR.")
    environment.add_argument("--timezone", help="IANA time zone, for example Africa/Casablanca.")
    environment.add_argument("--css", help="CSS injected into the page.")
    environment.add_argument("--css-file", help="CSS file, or - for stdin.")

    network = parser.add_argument_group("network")
    _add_bool(network, "--javascript", "javascript", "true or false. false blocks page scripts. Also --no-javascript.")
    network.add_argument("--user-agent", help="Browser user agent.")
    network.add_argument("--header", action="append", help="Request header as Name: value. Repeatable.")
    network.add_argument("--cookie", action="append", help="Cookie as name=value; domain=host; path=/; Secure; HttpOnly; SameSite=Lax.")
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
    diagnostics.add_argument("--webhook", help="Delivery webhook URL. Used with --async.")

    client = parser.add_argument_group("client")
    client.add_argument("--server", default=f"http://{launch.HOST}:{launch.PORT}", help="API base URL. The default local server is started when needed.")
    client.add_argument("--api-key", help="Bearer token. Otherwise VIPERCAPTURE_API_KEY or VIPERCAPTURE_ADMIN_TOKEN.")
    client.add_argument("--request-id", help="X-Request-Id value.")
    client.add_argument("--idempotency-key", help="Idempotency-Key for --async.")
    client.add_argument("--request-timeout", type=float, help="Seconds to wait for the render. Video defaults to 180 and images to 120.")
    client.add_argument("--async", dest="async_job", action="store_true", help="Submit /v1/jobs, poll, and save the result.")
    client.add_argument("--poll-ms", type=int, default=500, help="Async poll interval in milliseconds.")
    client.add_argument("--no-start", action="store_true", help="Do not start the local server.")
    client.add_argument("--dry-run", action="store_true", help="Print the JSON request and exit.")
    client.add_argument("--json", action="store_true", help="Print a JSON result on stdout instead of the path.")
    client.add_argument("--quiet", action="store_true", help="Print nothing on success.")
    client.add_argument("--verbose", action="store_true", help="Print progress on stderr.")
    return parser


def parse_capture_args(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    file = Path(path)
    if not file.is_file():
        raise ValueError(f"File not found: {path}")
    try:
        return file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc


def _one_of(left: str | None, right: str | None, message: str) -> str | None:
    if left and right:
        raise ValueError(message)
    if right:
        return _read_text(right)
    return left


def infer_format(path: str | None, explicit: str | None) -> str:
    if explicit:
        return _FORMAT_ALIASES.get(explicit, explicit)
    if path and path != "-":
        found = _SUFFIX_FORMAT.get(Path(path).suffix.lower())
        if found:
            return found
    return "png"


def _put(target: dict[str, object], key: str, value: object, default: object) -> None:
    if value is None or value == default:
        return
    target[key] = value


def _action_from(kind: str, values: object) -> dict[str, object]:
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
    if kind == "pause":
        return {"type": "wait", "delay_ms": int(values)}  # type: ignore[arg-type]
    raise ValueError(f"Unknown action {kind}")


def _apply_actions(payload: dict[str, object], steps: list[tuple[str, object]] | None) -> None:
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
        action = _action_from(kind, values)
        if delay and "delay_ms" not in action:
            action["delay_ms"] = delay
        if timeout is not None and "timeout_ms" not in action:
            action["timeout_ms"] = timeout
        actions.append(action)
    if actions:
        payload["actions"] = actions


def build_payload(args: argparse.Namespace) -> dict[str, object]:
    payload: dict[str, object] = {}
    url = args.url or args.target
    if args.url and args.target:
        raise ValueError("Pass the URL once, either as the argument or with --url.")
    html = _one_of(args.html, args.html_file, "Use either --html or --html-file.")
    markdown = _one_of(args.markdown, args.markdown_file, "Use either --markdown or --markdown-file.")
    sources = (url is not None, html is not None, markdown is not None)
    if sum(sources) != 1:
        raise ValueError("Pass a URL, --html, or --markdown.")
    if url is not None:
        payload["url"] = normalize_url(url)
    if html is not None:
        payload["html"] = html
    if markdown is not None:
        payload["markdown"] = markdown
    if args.base_url:
        payload["base_url"] = normalize_url(args.base_url)
    payload["output"] = infer_format(args.output, args.format)
    if args.engine:
        payload["engine"] = args.engine
    _put(payload, "full_page", args.full_page, True)
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
    _put(network, "java_script_enabled", args.javascript, True)
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
    if args.cookie:
        network["cookies"] = [parse_cookie(item) for item in args.cookie]
    if args.block_url:
        network["block_url_patterns"] = list(args.block_url)
    if args.block_resource:
        network["block_resource_types"] = list(args.block_resource)
    _put(network, "bypass_csp", args.bypass_csp, False)
    _put(network, "ignore_https_errors", args.ignore_https_errors, False)
    if network:
        payload["network"] = network
    _apply_actions(payload, args.steps)
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
    if args.webhook:
        payload["delivery"] = {"webhook_url": args.webhook}
    diagnostics: dict[str, object] = {}
    if args.diagnostics or args.har or args.trace or args.warc:
        diagnostics["bundle"] = True
    _put(diagnostics, "include_har", args.har, False)
    _put(diagnostics, "include_trace", args.trace, False)
    _put(diagnostics, "include_warc", args.warc, False)
    _put(diagnostics, "include_console", args.include_console, True)
    _put(diagnostics, "include_network", args.include_network, True)
    if diagnostics:
        payload["diagnostics"] = diagnostics
    deterministic: dict[str, object] = {}
    enabled = args.deterministic
    if enabled is not False and (
        enabled or args.timestamp_ms is not None or args.seed is not None or args.wait_for_fonts is False
    ):
        deterministic["enabled"] = True
    if deterministic.get("enabled") is True:
        if args.timestamp_ms is not None:
            deterministic["timestamp_ms"] = args.timestamp_ms
        if args.seed is not None:
            deterministic["random_seed"] = args.seed
        _put(deterministic, "wait_for_fonts", args.wait_for_fonts, True)
    if deterministic:
        payload["deterministic"] = deterministic
    if args.certify:
        payload["certification"] = {"enabled": True}
    if args.slice_height is not None:
        slices: dict[str, object] = {"height": args.slice_height}
        if args.slice_overlap is not None:
            slices["overlap"] = args.slice_overlap
        payload["slices"] = slices
    elif args.slice_overlap is not None:
        raise ValueError("--slice-overlap requires --slice-height.")
    if args.profile_id:
        payload["profile_id"] = args.profile_id
    _put(payload, "save_profile", args.save_profile, False)
    video: dict[str, object] = {}
    if args.video_duration_ms is not None:
        video["duration_ms"] = args.video_duration_ms
    if args.fps is not None:
        video["fps"] = args.fps
    if args.bitrate_mbps is not None:
        video["bitrate_mbps"] = args.bitrate_mbps
    _put(video, "scroll", args.video_scroll, False)
    if args.scroll_step is not None:
        video["scroll_step"] = args.scroll_step
    if args.scroll_delay_ms is not None:
        video["scroll_delay_ms"] = args.scroll_delay_ms
    _put(video, "transparent_background", args.video_transparent, False)
    if video:
        payload["video"] = video
    _put(payload, "preserve_viewport_width", args.preserve_viewport_width, False)
    if args.lazy_load:
        payload["lazy_load"] = args.lazy_load
    if args.profile:
        payload["profile"] = args.profile
    if args.cache_key:
        payload["cache_key"] = args.cache_key
    if args.cache is True or args.cache_key:
        payload["cache"] = True
    if args.selector:
        payload["selector"] = args.selector
    if args.clip:
        x, y, width, height = args.clip
        payload["clip"] = {"x": x, "y": y, "width": width, "height": height}
    css = _one_of(args.css, args.css_file, "Use either --css or --css-file.")
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
    _put(image, "transparent_background", args.transparent, False)
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
    _put(pdf, "print_background", args.pdf_background, True)
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
    header = _one_of(args.pdf_header, args.pdf_header_file, "Use either --pdf-header or --pdf-header-file.")
    footer = _one_of(args.pdf_footer, args.pdf_footer_file, "Use either --pdf-footer or --pdf-footer-file.")
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
    _put(payload, "include_shadow_dom", args.shadow_dom, False)
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
    _put(wait_for, "images", args.wait_images, False)
    if args.delay_ms is not None:
        wait_for["delay_ms"] = args.delay_ms
    if args.timeout_ms is not None:
        wait_for["timeout_ms"] = args.timeout_ms
    if wait_for:
        payload["wait_for"] = wait_for
    cleanup: dict[str, object] = {}
    if args.consent:
        cleanup["consent_mode"] = args.consent
    _put(cleanup, "block_ads", args.block_ads, False)
    _put(cleanup, "block_trackers", args.block_trackers, False)
    _put(cleanup, "block_chats", args.block_chats, False)
    _put(cleanup, "block_newsletters", args.block_newsletters, False)
    if cleanup:
        payload["cleanup"] = cleanup
    _put(payload, "stealth", args.stealth, True)
    captcha: dict[str, object] = {}
    if args.captcha:
        captcha["action"] = args.captcha
    if args.captcha_solver:
        captcha["solver"] = args.captcha_solver
    if args.captcha_timeout_ms is not None:
        captcha["timeout_ms"] = args.captcha_timeout_ms
    if captcha:
        payload["captcha"] = captcha
    _put(payload, "proceed_on_captcha", args.proceed_on_captcha, False)
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


def validate_payload(payload: dict[str, object]) -> None:
    try:
        from pydantic import ValidationError
        from vipercapture.render_contract import RenderRequest
    except ImportError:
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


def resolve_output(args: argparse.Namespace, payload: dict[str, object]) -> Path | None:
    if args.output == "-":
        return None
    if args.output:
        return Path(args.output)
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
    if args.api_key is not None:
        return _header_token(args.api_key)
    from gui import capture_credential

    return capture_credential(started_server=started_server)


def request_headers(args: argparse.Namespace, *, started_server: bool, json_body: bool) -> dict[str, str]:
    headers = {"Accept": "*/*"}
    if json_body:
        headers["Content-Type"] = "application/json"
    token = bearer_token(args, started_server=started_server)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if args.request_id:
        headers["X-Request-Id"] = args.request_id
    if args.idempotency_key:
        headers["Idempotency-Key"] = args.idempotency_key
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


def _error_from_body(status: int, raw: bytes) -> CaptureError:
    text = raw.decode("utf-8", errors="replace").strip()
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
        )
    return CaptureError(text or f"HTTP {status}", status=status, body=text or None)


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


def _open(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_SameOriginRedirect).open(request, timeout=timeout)


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
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return _open(request, timeout)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(64 * 1024)
        finally:
            exc.close()
        raise _error_from_body(exc.code, raw) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        raise CaptureError(f"The API did not answer: {reason}") from exc
    except TimeoutError as exc:
        raise CaptureError("The API did not answer before the timeout.") from exc


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


def _wait_for_local_server(proc: subprocess.Popen[object], log_path: Path) -> bool:
    """Return True when the process this command started is the listening server."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if launch.port_open():
            pid = launch._is_vipercapture_endpoint()
            if pid is not None:
                if pid != proc.pid:
                    if proc.poll() is None:
                        proc.terminate()
                    launch._clear_server_record(proc.pid)
                return pid == proc.pid
        if proc.poll() is not None and not launch.port_open():
            break
        time.sleep(0.25)
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
    return _wait_for_local_server(proc, log_path)


def ensure_server(args: argparse.Namespace) -> bool:
    if not is_local_default(args.server):
        return False
    if launch._is_vipercapture_endpoint() is not None:
        return False
    if launch.port_open():
        raise CaptureError(
            f"Port {launch.PORT} is open, but it is not ViperCapture. "
            "Choose another API with --server."
        )
    if args.no_start:
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


def _read_json(response) -> dict[str, object]:
    raw = response.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise CaptureError("The API returned a JSON document that is too large.")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CaptureError("The API did not return JSON.") from exc
    if not isinstance(parsed, dict):
        raise CaptureError("The API did not return a JSON object.")
    return parsed


def execute(args: argparse.Namespace, payload: dict[str, object], *, started_server: bool) -> int:
    timeout = request_timeout(args, payload)
    root = api_root(args.server)
    output = resolve_output(args, payload)
    if args.verbose and not args.quiet:
        route = "/v1/jobs" if args.async_job else "/v1/render"
        write_line(sys.stderr, f"POST {display_server(root)}{route}")
    if args.async_job:
        response_headers, total = _execute_async(
            args,
            payload,
            root=root,
            output=output,
            timeout=timeout,
            started_server=started_server,
        )
    else:
        response = open_response(
            root + "/v1/render",
            method="POST",
            payload=payload,
            headers=request_headers(args, started_server=started_server, json_body=True),
            timeout=timeout,
        )
        try:
            total = write_response(response, output)
            response_headers = response.headers
        finally:
            response.close()
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
) -> tuple[object, int]:
    deadline = time.monotonic() + timeout
    submitted = open_response(
        root + "/v1/jobs",
        method="POST",
        payload=payload,
        headers=request_headers(args, started_server=started_server, json_body=True),
        timeout=min(30, _remaining(deadline)),
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
            result = open_response(
                resolve_api_url(root, result_url),
                method="GET",
                payload=None,
                headers=request_headers(args, started_server=started_server, json_body=False),
                timeout=_remaining(deadline),
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
        polled = open_response(
            resolve_api_url(root, status_url),
            method="GET",
            payload=None,
            headers=request_headers(args, started_server=started_server, json_body=False),
            timeout=min(30, _remaining(deadline)),
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


def run_capture(args: argparse.Namespace) -> int:
    try:
        payload = build_payload(args)
        validate_payload(payload)
    except ValueError as exc:
        return fail(str(exc), exit_code=2)
    if args.dry_run:
        write_line(sys.stdout, json.dumps(public_payload(payload), ensure_ascii=False, indent=2))
        return 0
    try:
        load_local_env()
        started = ensure_server(args)
        if started and not args.quiet:
            write_line(
                sys.stderr,
                f"ViperCapture is listening on http://{launch.HOST}:{launch.PORT}",
            )
            write_line(sys.stderr, "Stop it with: vipercapture kill")
        return execute(args, payload, started_server=started)
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)
    except OSError as exc:
        return fail(str(exc))
    except BrokenPipeError:
        return 0


def main(argv: list[str] | None = None) -> int:
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
