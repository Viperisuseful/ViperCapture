"""Full-screen capture menu for ``vipercapture --gui``.

The menu stays in this terminal. It does not open a log window, a browser,
or a second tab. Image-capable terminals get the logo and inline PNG
previews; every other terminal gets an ASCII wordmark and text metadata.
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace as _dc_replace
from datetime import datetime, timezone
from pathlib import Path

import launch

FORMATS = (
    "png",
    "jpeg",
    "webp",
    "avif",
    "pdf",
    "gif",
    "mp4",
    "webm",
    "html",
    "markdown",
    "metadata",
)
IMAGE_FORMATS = frozenset({"png", "jpeg", "webp", "avif"})
VIDEO_FORMATS = frozenset({"gif", "mp4", "webm"})
QUALITY_FORMATS = frozenset({"jpeg", "webp", "avif"})
TRANSPARENT_IMAGE_FORMATS = frozenset({"png", "webp", "avif"})
FORMAT_EXTENSIONS: dict[str, str] = {
    "png": "png",
    "jpeg": "jpg",
    "webp": "webp",
    "avif": "avif",
    "pdf": "pdf",
    "gif": "gif",
    "mp4": "mp4",
    "webm": "webm",
    "html": "html",
    "markdown": "md",
    "metadata": "json",
}

# (label, width, height, device)
VIEWPORT_PRESETS: tuple[tuple[str, int, int, str], ...] = (
    ("Full HD", 1920, 1080, "desktop"),
    ("HD", 1280, 720, "desktop"),
    ("Phone", 390, 844, "iphone_14"),
    ("Tablet", 768, 1024, "ipad"),
    ("2K", 2560, 1440, "desktop"),
    ("4K", 3840, 2160, "desktop"),
)
COLOR_SCHEMES = ("system", "dark", "light")
BROWSER_ENGINES = ("chromium", "firefox", "webkit")
VIDEO_DURATIONS_MS = (4_000, 8_000, 15_000, 30_000)
WAIT_DELAYS_MS = (0, 1_000, 2_500, 5_000)
SCALES = (1, 2, 3)
QUALITIES = (70, 80, 90, 95, 100)
PROFILES = ("preview", "accurate")
VIDEO_FPS_CHOICES = (30, 60)
PDF_PAPER_SIZES = ("A4", "Letter", "Legal", "Tabloid")
PDF_ORIENTATIONS = ("portrait", "landscape")

OPTIONS_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("scale", "Scale / DPR", "cycle"),
    ("quality", "Quality", "cycle"),
    ("transparent", "Transparent bg", "cycle"),
    ("profile", "Capture profile", "cycle"),
    ("wait_selector", "Wait selector", "text"),
    ("selector", "Element selector", "text"),
    ("video_scroll", "Video scroll", "cycle"),
    ("video_fps", "Video FPS", "cycle"),
    ("pdf_paper_size", "PDF paper", "cycle"),
    ("pdf_orientation", "PDF orientation", "cycle"),
    ("stealth", "Stealth", "cycle"),
    ("diagnostics", "Diagnostics ZIP", "cycle"),
    ("output_dir", "Output dir", "text"),
    ("gpu_mode", "GPU mode", "gpu"),
)

HELP_ENTRIES: tuple[tuple[str, str], ...] = (
    ("Left / Right", "Move URL cursor"),
    ("Ctrl+Left / Right", "Jump word / URL segment"),
    ("Home / Ctrl+A", "Start of URL"),
    ("End / Ctrl+E", "End of URL"),
    ("Bksp / Delete", "Delete before / at cursor"),
    ("Ctrl+W / Ctrl+U", "Delete word / clear URL"),
    ("Ctrl+V", "Paste from clipboard"),
    ("Up / Down", "Cycle URL history"),
    ("Tab / Shift+Tab", "Next / previous format"),
    ("Ctrl+P", "Toggle full page / viewport"),
    ("Ctrl+R", "Cycle viewport / device"),
    ("Ctrl+T", "Cycle theme (sys/dark/light)"),
    ("Ctrl+B", "Cycle browser engine"),
    ("Ctrl+X", "Toggle page cleanup"),
    ("Ctrl+D", "Cycle duration or delay"),
    ("Enter", "Capture URL"),
    ("Ctrl+O", "Open last capture"),
    ("Ctrl+Y", "Copy last path"),
    ("Ctrl+G", "Advanced options drawer"),
    ("Ctrl+F", "Saved presets picker"),
    ("Ctrl+L", "History & preview pane"),
    ("F1 / Ctrl+/", "Toggle this help"),
    ("Esc", "Cancel / close / quit"),
    ("Ctrl+C", "Quit immediately"),
)

DEFAULT_VIEWPORT = (1920, 1080)
MAX_CAPTURE_BYTES = 200 * 1024 * 1024
MAX_SIXEL_PREVIEW_PIXELS = 4_000_000
MAX_PASTE_BYTES = 4096
VIDEO_DURATION_MS = 4_000
HISTORY_LIMIT = 50
MAX_URL_CHARS = 2048
MAX_FIELD_CHARS = 1024
MAX_PRESET_NAME_CHARS = 40
BRACKETED_PASTE_ON = b"\x1b[?2004h"
BRACKETED_PASTE_OFF = b"\x1b[?2004l"
_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"
_PASTE_TRUNCATED_MARK = b"\x00"

ASCII_LOGO = (
    "                                         █                  ",
    "█  █  █  █▀▀█ █▀▀█ █▀▀▄  █▀▀▀ ▄▀▀█ █▀▀█ ▀█▀▀ █  █ █▀▀▄ █▀▀█ ",
    "█__█  █  █__█ █^^^ █     █___ █__█ █__█  █   █__█ █    █^^^ ",
    " ▀▀   ▀  █▀▀▀ ▀▀▀▀ ▀     ▀▀▀▀ ▀▀▀▀ █▀▀▀  ▀▀  ▀▀▀▀ ▀    ▀▀▀▀ ",
    "ViperCapture",
)


def _draw_wordmark_line(line: str, fg: str, shadow: str, bg: str) -> str:
    reset = "\x1b[0m"
    parts: list[str] = []
    for char in line:
        if char == "_":
            parts.append(f"{bg} {reset}")
        elif char == "^":
            parts.append(f"{fg}{bg}▀{reset}")
        elif char == "~":
            parts.append(f"{shadow}▀{reset}")
        elif char == " ":
            parts.append(" ")
        else:
            parts.append(f"{fg}{char}{reset}")
    return "".join(parts)


def styled_ascii_logo() -> list[str]:
    viper_fg = "\x1b[1;34m"
    capture_fg = "\x1b[1;37m"
    viper_shadow = "\x1b[38;2;60;66;80m"
    viper_bg = "\x1b[48;2;60;66;80m"
    capture_shadow = "\x1b[38;2;20;42;90m"
    capture_bg = "\x1b[48;2;20;42;90m"

    lines: list[str] = []
    split_pos = 25
    for raw in ASCII_LOGO[:4]:
        left = raw[:split_pos]
        right = raw[split_pos:]
        left_styled = _draw_wordmark_line(left, viper_fg, viper_shadow, viper_bg)
        right_styled = _draw_wordmark_line(right, capture_fg, capture_shadow, capture_bg)
        lines.append(f"{left_styled}{right_styled}")
    lines.append(f"\x1b[2m{ASCII_LOGO[4]}\x1b[0m")
    return lines


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TERMINAL_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[@-Z\\-_])"
)
_HOST = re.compile(r"[^A-Za-z0-9.-]+")
_WORD_SEPARATORS = frozenset(" \t/\\.:-?&=+#@,;()[]{}'\"")


def sanitize_terminal_text(text: str) -> str:
    """Strip ANSI/OSC escape sequences and C0/C1 control characters from untrusted text."""
    cleaned = _TERMINAL_ESCAPE.sub("", text)
    return "".join(
        " " if ch in {"\r", "\n", "\t"} else ch
        for ch in cleaned
        if ch in {"\r", "\n", "\t"} or not (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F)
    )


@dataclass(frozen=True)
class CaptureRecord:
    url: str
    output: str
    path: str
    size_bytes: int
    duration_ms: int
    timestamp: str
    full_page: bool = False
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    device: str = "desktop"
    color_scheme: str = "system"
    engine: str = "chromium"
    cleanup: bool = False
    consent_mode: str = "none"
    block_ads: bool = False
    block_trackers: bool = False
    block_chats: bool = False
    block_newsletters: bool = False
    scale: float = 1
    quality: int = 90
    transparent: bool = False
    profile: str = "preview"
    wait_delay_ms: int = 0
    wait_selector: str = ""
    selector: str = ""
    video_duration_ms: int = VIDEO_DURATION_MS
    video_scroll: bool = False
    video_fps: int = 60
    pdf_paper_size: str = "A4"
    pdf_orientation: str = "portrait"
    stealth: bool = True
    diagnostics: bool = False


@dataclass(frozen=True)
class PresetEntry:
    name: str
    settings: dict[str, object]


@dataclass(frozen=True)
class GuiState:
    url: str = ""
    cursor: int = -1
    output: str = "png"
    full_page: bool = False
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    custom_viewport: tuple[int, int] | None = None
    device: str = "desktop"
    color_scheme: str = "system"
    engine: str = "chromium"
    cleanup: bool = False
    consent_mode: str | None = None
    block_ads: bool | None = None
    block_trackers: bool | None = None
    block_chats: bool | None = None
    block_newsletters: bool | None = None
    video_duration_ms: int = VIDEO_DURATION_MS
    video_scroll: bool = False
    video_fps: int = 60
    wait_delay_ms: int = 0
    wait_selector: str = ""
    selector: str = ""
    scale: float = 1
    quality: int = 90
    transparent: bool = False
    profile: str = "preview"
    pdf_paper_size: str = "A4"
    pdf_orientation: str = "portrait"
    stealth: bool = True
    diagnostics: bool = False
    output_dir: str = ""
    error: str = ""
    saved: str = ""
    notice: str = ""
    last_size_bytes: int = 0
    last_duration_ms: int = 0
    server_label: str = "local"
    local_server: bool = True
    gpu_mode: str = "off"
    gpu_hardware: bool = False
    gpu_known: bool = False
    busy: bool = False
    progress: int = 0
    cancelling: bool = False
    url_history: tuple[str, ...] = ()
    history_index: int = -1
    history_draft: str = ""
    drawer: str = ""
    drawer_index: int = 0
    editing_field: str = ""
    edit_buffer: str = ""
    edit_cursor: int = 0
    captures: tuple[CaptureRecord, ...] = ()
    presets: tuple[PresetEntry, ...] = ()
    presets_supported: bool = True
    available_formats: tuple[str, ...] = FORMATS

    def __post_init__(self) -> None:
        length = len(self.url)
        if self.cursor < 0:
            object.__setattr__(self, "cursor", length)
        elif self.cursor > length:
            object.__setattr__(self, "cursor", length)
        edit_len = len(self.edit_buffer)
        if self.edit_cursor < 0:
            object.__setattr__(self, "edit_cursor", edit_len)
        elif self.edit_cursor > edit_len:
            object.__setattr__(self, "edit_cursor", edit_len)


def _replace(state: GuiState, **changes: object) -> GuiState:
    return _dc_replace(state, **changes)  # type: ignore[arg-type]


def normalize_url(text: str) -> str:
    value = text.strip()
    if not value:
        raise ValueError("Enter a website link.")
    if "://" not in value:
        # localhost:8080 is a port. javascript: and data: are schemes.
        scheme_like = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):(.*)$", value)
        if scheme_like and not re.match(r"\d{1,5}($|[/?#])", scheme_like.group(2)):
            raise ValueError("Use a link that starts with http:// or https://.")
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Use a link that starts with http:// or https://.")
    return value


def server_base_url(server: str | None) -> str:
    if not server:
        return launch.URL.rstrip("/")
    return server.strip().rstrip("/")


def is_local_server(base_url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(base_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def is_default_local_server(base_url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = parsed.port
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    resolved_port = port or (443 if scheme == "https" else 80)
    return (
        scheme == "http"
        and host in {"127.0.0.1", "localhost", "::1"}
        and resolved_port == launch.PORT
        and parsed.path in {"", "/"}
    )


def format_server_label(base_url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = parsed.port
    except ValueError:
        return sanitize_terminal_text(base_url)
    if is_local_server(base_url):
        if port in {None, launch.PORT}:
            return "local"
        return f"local:{port}"
    return sanitize_terminal_text(parsed.netloc or base_url)


def effective_cleanup(state: GuiState) -> dict[str, object] | None:
    """Resolve the active cleanup config, preserving granular preset settings."""
    has_granular = any(
        val is not None
        for val in (
            state.consent_mode,
            state.block_ads,
            state.block_trackers,
            state.block_chats,
            state.block_newsletters,
        )
    )
    if not has_granular:
        if not state.cleanup:
            return None
        return {
            "consent_mode": "reject",
            "block_ads": True,
            "block_trackers": True,
            "block_chats": True,
            "block_newsletters": True,
        }
    consent = state.consent_mode if state.consent_mode is not None else ("reject" if state.cleanup else "none")
    ads = state.block_ads if state.block_ads is not None else state.cleanup
    trackers = state.block_trackers if state.block_trackers is not None else state.cleanup
    chats = state.block_chats if state.block_chats is not None else state.cleanup
    newsletters = state.block_newsletters if state.block_newsletters is not None else state.cleanup
    if consent == "none" and not any((ads, trackers, chats, newsletters)):
        return None
    return {
        "consent_mode": consent,
        "block_ads": bool(ads),
        "block_trackers": bool(trackers),
        "block_chats": bool(chats),
        "block_newsletters": bool(newsletters),
    }


def _cleanup_badge(state: GuiState) -> tuple[str, str]:
    cfg = effective_cleanup(state)
    if cfg is None:
        return "clean:off", "2"
    if (
        cfg["consent_mode"] == "reject"
        and cfg["block_ads"] is True
        and cfg["block_trackers"] is True
        and cfg["block_chats"] is True
        and cfg["block_newsletters"] is True
    ):
        return "clean:on", "1;32"
    return "clean:custom", "1;36"


def _last_saved_path(state: GuiState) -> str:
    if state.saved:
        return state.saved
    if state.captures:
        return state.captures[0].path
    return ""


def _format_scale(scale: float) -> str:
    if float(scale).is_integer():
        return f"{int(scale)}x"
    return f"{scale:g}x"


def validate_capture_state(state: GuiState) -> str:
    """Return an error string if the current option combination is invalid."""
    allowed = state.available_formats or FORMATS
    if state.output not in allowed:
        return f"{state.output.upper()} output is not supported by this server."
    if state.output == "pdf" and state.engine != "chromium":
        return "PDF output requires Chromium (press Ctrl+B to switch engine)."
    if state.selector.strip() and state.output not in IMAGE_FORMATS:
        return "Element selector only works with PNG, JPEG, WebP, or AVIF."
    return ""


def capture_payload(state: GuiState, url: str) -> dict[str, object]:
    width, height = state.viewport
    viewport_doc: dict[str, object] = {"width": width, "height": height}
    if state.scale != 1 or state.device != "desktop":
        viewport_doc["device_scale_factor"] = int(state.scale) if float(state.scale).is_integer() else state.scale

    selector = state.selector.strip()
    use_selector = bool(selector) and state.output in IMAGE_FORMATS
    full_page = False if use_selector else state.full_page

    payload: dict[str, object] = {
        "url": url,
        "output": state.output,
        "full_page": full_page,
        "viewport": viewport_doc,
    }
    if state.engine != "chromium":
        payload["engine"] = state.engine

    environment: dict[str, object] = {}
    if state.device != "desktop":
        environment["device"] = state.device
    if state.color_scheme != "system":
        environment["color_scheme"] = state.color_scheme
    if environment:
        payload["environment"] = environment

    cleanup_doc = effective_cleanup(state)
    if cleanup_doc is not None:
        payload["cleanup"] = cleanup_doc

    if use_selector:
        payload["selector"] = selector

    image_doc: dict[str, object] = {}
    if state.output in QUALITY_FORMATS and state.quality is not None:
        image_doc["quality"] = state.quality
    if state.transparent and state.output in TRANSPARENT_IMAGE_FORMATS:
        image_doc["transparent_background"] = True
    if image_doc:
        payload["image"] = image_doc

    if state.output in VIDEO_FORMATS:
        # A full-page clip is encoded by scrolling the page from top to
        # bottom when video.scroll is enabled.
        video_doc: dict[str, object] = {
            "duration_ms": state.video_duration_ms,
            "scroll": state.video_scroll,
        }
        if state.video_fps != 60:
            video_doc["fps"] = state.video_fps
        if state.transparent and full_page and state.output in {"gif", "webm"}:
            video_doc["transparent_background"] = True
        payload["video"] = video_doc

    if state.output == "pdf":
        payload["pdf"] = {
            "paper_size": state.pdf_paper_size,
            "orientation": state.pdf_orientation,
        }

    wait_for: dict[str, object] = {}
    if state.wait_delay_ms > 0:
        wait_for["delay_ms"] = state.wait_delay_ms
    if state.wait_selector.strip():
        wait_for["selector"] = state.wait_selector.strip()
    if wait_for:
        payload["wait_for"] = wait_for

    if state.profile != "preview":
        payload["profile"] = state.profile
    if not state.stealth:
        payload["stealth"] = False
    if state.diagnostics:
        payload["diagnostics"] = {"bundle": True}

    return payload


def _cycle_tuple(values: Sequence[object], current: object, step: int = 1) -> object:
    try:
        index = list(values).index(current)
    except ValueError:
        return values[0]
    return values[(index + step) % len(values)]


def cycle_output(output: str, step: int = 1, formats: Sequence[str] = FORMATS) -> str:
    choices = formats if formats else FORMATS
    return str(_cycle_tuple(choices, output, step))


def viewport_choices(custom_viewport: tuple[int, int] | None = None) -> list[tuple[str, int, int, str]]:
    choices = list(VIEWPORT_PRESETS)
    if custom_viewport is not None:
        cw, ch = custom_viewport
        if not any(w == cw and h == ch and dev == "desktop" for _, w, h, dev in choices):
            choices.insert(0, ("Custom", cw, ch, "desktop"))
    return choices


def viewport_preset_label(
    viewport: tuple[int, int],
    device: str = "desktop",
    custom_viewport: tuple[int, int] | None = None,
) -> str:
    for label, w, h, dev in viewport_choices(custom_viewport):
        if (w, h) == viewport and dev == device:
            return label
    return f"{viewport[0]}x{viewport[1]}"


def cycle_viewport(state: GuiState, step: int = 1) -> tuple[tuple[int, int], str]:
    choices = viewport_choices(state.custom_viewport)
    current_index = -1
    for idx, (_, w, h, dev) in enumerate(choices):
        if (w, h) == state.viewport and dev == state.device:
            current_index = idx
            break
    next_index = 0 if current_index < 0 else (current_index + step) % len(choices)
    _, w, h, dev = choices[next_index]
    return (w, h), dev


def cycle_color_scheme(scheme: str, step: int = 1) -> str:
    return str(_cycle_tuple(COLOR_SCHEMES, scheme, step))


def cycle_engine(engine: str, step: int = 1) -> str:
    return str(_cycle_tuple(BROWSER_ENGINES, engine, step))


def cycle_video_duration(ms: int, step: int = 1) -> int:
    return int(_cycle_tuple(VIDEO_DURATIONS_MS, ms, step))  # type: ignore[arg-type]


def cycle_wait_delay(ms: int, step: int = 1) -> int:
    return int(_cycle_tuple(WAIT_DELAYS_MS, ms, step))  # type: ignore[arg-type]


def format_seconds_short(ms: int) -> str:
    if ms <= 0:
        return "0s"
    if ms % 1000 == 0:
        return f"{ms // 1000}s"
    return f"{ms / 1000:.1f}s"


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def _prev_word_boundary(text: str, cursor: int) -> int:
    pos = max(0, min(len(text), cursor))
    while pos > 0 and text[pos - 1] in _WORD_SEPARATORS:
        pos -= 1
    while pos > 0 and text[pos - 1] not in _WORD_SEPARATORS:
        pos -= 1
    return pos


def _next_word_boundary(text: str, cursor: int) -> int:
    pos = max(0, min(len(text), cursor))
    length = len(text)
    while pos < length and text[pos] not in _WORD_SEPARATORS:
        pos += 1
    while pos < length and text[pos] in _WORD_SEPARATORS:
        pos += 1
    return pos


def _sanitize_pasted(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in cleaned.split("\n"):
        stripped = "".join(ch for ch in line if ch.isprintable()).strip()
        if stripped:
            return stripped
    return ""


def _edit_text(
    text: str,
    cursor: int,
    key: str,
    *,
    max_len: int = MAX_URL_CHARS,
) -> tuple[str, int, bool]:
    """Apply a single text-editing key to ``(text, cursor)``."""
    cursor = max(0, min(len(text), cursor))
    if key == "left":
        return text, max(0, cursor - 1), True
    if key == "right":
        return text, min(len(text), cursor + 1), True
    if key == "ctrl-left":
        return text, _prev_word_boundary(text, cursor), True
    if key == "ctrl-right":
        return text, _next_word_boundary(text, cursor), True
    if key in {"home", "ctrl-a"}:
        return text, 0, True
    if key in {"end", "ctrl-e"}:
        return text, len(text), True
    if key == "backspace":
        if cursor == 0:
            return text, 0, True
        return text[: cursor - 1] + text[cursor:], cursor - 1, True
    if key == "delete":
        if cursor >= len(text):
            return text, cursor, True
        return text[:cursor] + text[cursor + 1 :], cursor, True
    if key == "ctrl-w":
        new_pos = _prev_word_boundary(text, cursor)
        return text[:new_pos] + text[cursor:], new_pos, True
    if key == "ctrl-u":
        return "", 0, True
    if key.startswith("paste:"):
        inserted = _sanitize_pasted(key[6:])
        if not inserted:
            return text, cursor, True
        budget = max(0, max_len - len(text))
        if budget <= 0:
            return text, cursor, True
        chunk = inserted[:budget]
        return text[:cursor] + chunk + text[cursor:], cursor + len(chunk), True
    if len(key) == 1 and key.isprintable():
        if len(text) >= max_len:
            return text, cursor, True
        return text[:cursor] + key + text[cursor:], cursor + 1, True
    return text, cursor, False


def _option_value_text(state: GuiState, field_key: str) -> str:
    if field_key == "scale":
        return _format_scale(state.scale)
    if field_key == "quality":
        suffix = "" if state.output in QUALITY_FORMATS else " (jpeg/webp/avif)"
        return f"{state.quality}{suffix}"
    if field_key == "transparent":
        suffix = "" if state.output in TRANSPARENT_IMAGE_FORMATS else " (png/webp/avif)"
        return f"{'on' if state.transparent else 'off'}{suffix}"
    if field_key == "profile":
        return state.profile
    if field_key == "wait_selector":
        return state.wait_selector or "(none)"
    if field_key == "selector":
        return state.selector or "(none)"
    if field_key == "video_scroll":
        return "on" if state.video_scroll else "off"
    if field_key == "video_fps":
        return f"{state.video_fps} fps"
    if field_key == "pdf_paper_size":
        return state.pdf_paper_size
    if field_key == "pdf_orientation":
        return state.pdf_orientation
    if field_key == "stealth":
        return "on" if state.stealth else "off"
    if field_key == "diagnostics":
        return "on (.zip)" if state.diagnostics else "off"
    if field_key == "output_dir":
        return state.output_dir or "(current dir)"
    if field_key == "gpu_mode":
        if not state.local_server:
            return f"{state.gpu_mode} (remote)"
        if state.gpu_mode == "auto":
            hw = "hw active" if state.gpu_hardware else "software fallback"
            return f"auto ({hw})" if state.gpu_known else "auto"
        return "off"
    return ""


def _cycle_numeric(values: Sequence[int], current: float, step: int) -> int:
    if current in values:
        return int(_cycle_tuple(values, current, step))  # type: ignore[arg-type]
    if step >= 0:
        for val in values:
            if val > current:
                return val
        return values[0]
    for val in reversed(values):
        if val < current:
            return val
    return values[-1]


def _cycle_option(state: GuiState, field_key: str, step: int) -> GuiState:
    if field_key == "scale":
        return _replace(state, scale=_cycle_numeric(SCALES, state.scale, step), error="", notice="")
    if field_key == "quality":
        return _replace(state, quality=_cycle_numeric(QUALITIES, state.quality, step), error="", notice="")
    if field_key == "transparent":
        return _replace(state, transparent=not state.transparent, error="", notice="")
    if field_key == "profile":
        return _replace(state, profile=str(_cycle_tuple(PROFILES, state.profile, step)), error="", notice="")
    if field_key == "video_scroll":
        return _replace(state, video_scroll=not state.video_scroll, error="", notice="")
    if field_key == "video_fps":
        return _replace(state, video_fps=_cycle_numeric(VIDEO_FPS_CHOICES, state.video_fps, step), error="", notice="")
    if field_key == "pdf_paper_size":
        return _replace(state, pdf_paper_size=str(_cycle_tuple(PDF_PAPER_SIZES, state.pdf_paper_size, step)), error="", notice="")
    if field_key == "pdf_orientation":
        return _replace(state, pdf_orientation=str(_cycle_tuple(PDF_ORIENTATIONS, state.pdf_orientation, step)), error="", notice="")
    if field_key == "stealth":
        return _replace(state, stealth=not state.stealth, error="", notice="")
    if field_key == "diagnostics":
        return _replace(state, diagnostics=not state.diagnostics, error="", notice="")
    return state


def _apply_editing_key(state: GuiState, key: str) -> tuple[GuiState, str]:
    if key == "quit":
        return state, "quit"
    if key == "esc":
        return _replace(state, editing_field="", edit_buffer="", edit_cursor=0), "edit"
    if key == "ctrl-v":
        return state, "paste"
    if key == "enter":
        field = state.editing_field
        value = state.edit_buffer.strip()
        if field == "preset_name":
            if not value:
                return _replace(state, error="Enter a preset name."), "edit"
            return _replace(state, error="", edit_buffer=value[:MAX_PRESET_NAME_CHARS]), "save_preset"
        if field == "wait_selector":
            return _replace(state, wait_selector=value, editing_field="", edit_buffer="", edit_cursor=0, error="", notice=""), "edit"
        if field == "selector":
            changes: dict[str, object] = {
                "selector": value,
                "editing_field": "",
                "edit_buffer": "",
                "edit_cursor": 0,
                "error": "",
                "notice": "",
            }
            if value:
                changes["full_page"] = False
            return _replace(state, **changes), "edit"
        if field == "output_dir":
            return _replace(state, output_dir=value, editing_field="", edit_buffer="", edit_cursor=0, error="", notice=""), "edit"
        return _replace(state, editing_field="", edit_buffer="", edit_cursor=0), "edit"
    limit = MAX_PRESET_NAME_CHARS if state.editing_field == "preset_name" else MAX_FIELD_CHARS
    new_buf, new_cur, handled = _edit_text(state.edit_buffer, state.edit_cursor, key, max_len=limit)
    if handled:
        return _replace(state, edit_buffer=new_buf, edit_cursor=new_cur, error=""), "edit"
    return state, "edit"


def _toggle_drawer(state: GuiState, target: str) -> tuple[GuiState, str]:
    if state.drawer == target:
        return _replace(state, drawer="", drawer_index=0, editing_field="", edit_buffer="", edit_cursor=0, error=""), "edit"
    next_state = _replace(
        state,
        drawer=target,
        drawer_index=0,
        editing_field="",
        edit_buffer="",
        edit_cursor=0,
        error="",
        notice="",
    )
    if target == "presets":
        return next_state, "load_presets"
    return next_state, "edit"


def _apply_options_drawer_key(state: GuiState, key: str) -> tuple[GuiState, str]:
    count = len(OPTIONS_FIELDS)
    idx = state.drawer_index % count
    field_key, _, kind = OPTIONS_FIELDS[idx]
    if key in {"esc", "ctrl-g"}:
        return _replace(state, drawer="", drawer_index=0, error=""), "edit"
    if key == "up":
        return _replace(state, drawer_index=(idx - 1) % count, error=""), "edit"
    if key == "down":
        return _replace(state, drawer_index=(idx + 1) % count, error=""), "edit"
    if key in {"left", "right", " "}:
        step = -1 if key == "left" else 1
        if kind == "cycle":
            return _cycle_option(state, field_key, step), "edit"
        if kind == "gpu":
            if not state.local_server:
                return _replace(state, error="GPU mode can only be toggled on a local server."), "edit"
            return state, "toggle_gpu"
        return state, "edit"
    if key == "enter":
        if kind == "text":
            current = str(getattr(state, field_key, ""))
            return _replace(
                state,
                editing_field=field_key,
                edit_buffer=current,
                edit_cursor=len(current),
                error="",
            ), "edit"
        if kind == "cycle":
            return _cycle_option(state, field_key, 1), "edit"
        if kind == "gpu":
            if not state.local_server:
                return _replace(state, error="GPU mode can only be toggled on a local server."), "edit"
            return state, "toggle_gpu"
    return state, "edit"


def _apply_presets_drawer_key(state: GuiState, key: str) -> tuple[GuiState, str]:
    count = len(state.presets)
    idx = state.drawer_index % count if count else 0
    if key in {"esc", "ctrl-f"}:
        return _replace(state, drawer="", drawer_index=0, error=""), "edit"
    if key == "up" and count:
        return _replace(state, drawer_index=(idx - 1) % count, error=""), "edit"
    if key == "down" and count:
        return _replace(state, drawer_index=(idx + 1) % count, error=""), "edit"
    if key == "enter" and count:
        preset = state.presets[idx]
        applied, ignored = apply_preset_settings_with_warnings(state, preset.settings)
        notice = f"Applied preset '{preset.name}'."
        if ignored:
            notice = f"Applied '{preset.name}' (ignored: {', '.join(ignored)})."
        return _replace(
            applied,
            drawer="",
            drawer_index=0,
            error="",
            notice=notice,
        ), "edit"
    if key in {"s", "S"}:
        if not state.presets_supported:
            return _replace(state, error="Presets are only available for a local server."), "edit"
        return _replace(
            state,
            editing_field="preset_name",
            edit_buffer="",
            edit_cursor=0,
            error="",
        ), "edit"
    if key in {"d", "D", "delete"} and count:
        if not state.presets_supported:
            return _replace(state, error="Presets are only available for a local server."), "edit"
        return state, "delete_preset"
    return state, "edit"


def _apply_history_drawer_key(state: GuiState, key: str) -> tuple[GuiState, str]:
    count = len(state.captures)
    idx = state.drawer_index % count if count else 0
    if key in {"esc", "ctrl-l"}:
        return _replace(state, drawer="", drawer_index=0, error=""), "edit"
    if key == "up" and count:
        return _replace(state, drawer_index=(idx - 1) % count, error="", notice=""), "edit"
    if key == "down" and count:
        return _replace(state, drawer_index=(idx + 1) % count, error="", notice=""), "edit"
    if key == "enter" and count:
        return state, "open_selected"
    if key in {"y", "Y"} and count:
        return state, "copy_selected"
    if key in {"r", "R"} and count:
        rec = state.captures[idx]
        return _replace(
            state,
            url=rec.url,
            cursor=len(rec.url),
            output=rec.output,
            full_page=rec.full_page,
            viewport=rec.viewport,
            device=rec.device,
            color_scheme=rec.color_scheme,
            engine=rec.engine,
            cleanup=rec.cleanup,
            consent_mode=rec.consent_mode,
            block_ads=rec.block_ads,
            block_trackers=rec.block_trackers,
            block_chats=rec.block_chats,
            block_newsletters=rec.block_newsletters,
            scale=rec.scale,
            quality=rec.quality,
            transparent=rec.transparent,
            profile=rec.profile,
            wait_delay_ms=rec.wait_delay_ms,
            wait_selector=rec.wait_selector,
            selector=rec.selector,
            video_duration_ms=rec.video_duration_ms,
            video_scroll=rec.video_scroll,
            video_fps=rec.video_fps,
            pdf_paper_size=rec.pdf_paper_size,
            pdf_orientation=rec.pdf_orientation,
            stealth=rec.stealth,
            diagnostics=rec.diagnostics,
            drawer="",
            drawer_index=0,
            history_index=-1,
            error="",
            notice="Reloaded capture settings.",
        ), "edit"
    return state, "edit"


def _apply_help_drawer_key(state: GuiState, key: str) -> tuple[GuiState, str]:
    if key in {"esc", "f1", "ctrl-slash"}:
        return _replace(state, drawer="", drawer_index=0, error=""), "edit"
    if key == "up":
        return _replace(state, drawer_index=max(0, state.drawer_index - 1)), "edit"
    if key == "down":
        return _replace(state, drawer_index=min(max(0, len(HELP_ENTRIES) - 1), state.drawer_index + 1)), "edit"
    return state, "edit"


def apply_key(state: GuiState, key: str) -> tuple[GuiState, str]:
    """Apply one key. Returns ``(new_state, action)``."""
    if key == "quit":
        return state, "quit"
    if state.busy:
        if key == "esc":
            return _replace(state, cancelling=True), "edit"
        return state, "edit"
    if state.editing_field:
        return _apply_editing_key(state, key)

    # Drawer switching / toggling works from main screen or another drawer.
    if key == "ctrl-g":
        return _toggle_drawer(state, "options")
    if key == "ctrl-f":
        return _toggle_drawer(state, "presets")
    if key == "ctrl-l":
        return _toggle_drawer(state, "history")
    if key in {"f1", "ctrl-slash"}:
        return _toggle_drawer(state, "help")

    if state.drawer == "options":
        return _apply_options_drawer_key(state, key)
    if state.drawer == "presets":
        return _apply_presets_drawer_key(state, key)
    if state.drawer == "history":
        return _apply_history_drawer_key(state, key)
    if state.drawer == "help":
        return _apply_help_drawer_key(state, key)

    # Main screen (URL bar focused)
    if key == "esc":
        return state, "quit"
    if key == "tab":
        return _replace(state, output=cycle_output(state.output, 1, state.available_formats), error="", notice=""), "edit"
    if key == "shift-tab":
        return _replace(state, output=cycle_output(state.output, -1, state.available_formats), error="", notice=""), "edit"
    if key == "ctrl-p":
        new_full = not state.full_page
        changes: dict[str, object] = {"full_page": new_full, "error": "", "notice": ""}
        if new_full and state.selector.strip():
            changes["selector"] = ""
            changes["notice"] = "Cleared element selector for full page."
        return _replace(state, **changes), "edit"
    if key == "ctrl-r":
        new_vp, new_dev = cycle_viewport(state, 1)
        return _replace(state, viewport=new_vp, device=new_dev, error="", notice=""), "edit"
    if key == "ctrl-t":
        return _replace(state, color_scheme=cycle_color_scheme(state.color_scheme), error="", notice=""), "edit"
    if key == "ctrl-b":
        return _replace(state, engine=cycle_engine(state.engine), error="", notice=""), "edit"
    if key == "ctrl-x":
        next_clean = effective_cleanup(state) is None
        return _replace(
            state,
            cleanup=next_clean,
            consent_mode="reject" if next_clean else "none",
            block_ads=next_clean,
            block_trackers=next_clean,
            block_chats=next_clean,
            block_newsletters=next_clean,
            error="",
            notice="",
        ), "edit"
    if key == "ctrl-d":
        if state.output in VIDEO_FORMATS:
            return _replace(state, video_duration_ms=cycle_video_duration(state.video_duration_ms), error="", notice=""), "edit"
        return _replace(state, wait_delay_ms=cycle_wait_delay(state.wait_delay_ms), error="", notice=""), "edit"
    if key == "ctrl-o":
        if not _last_saved_path(state):
            return _replace(state, error="No saved capture yet.", notice=""), "edit"
        return state, "open_last"
    if key == "ctrl-y":
        if not _last_saved_path(state):
            return _replace(state, error="No saved capture yet.", notice=""), "edit"
        return state, "copy_last"
    if key == "ctrl-v":
        return state, "paste"
    if key == "up":
        if not state.url_history:
            return state, "edit"
        if state.history_index < 0:
            draft = state.url
            next_idx = 0
        else:
            draft = state.history_draft
            next_idx = min(len(state.url_history) - 1, state.history_index + 1)
        recalled = sanitize_terminal_text(state.url_history[next_idx])
        return _replace(
            state,
            url=recalled,
            cursor=len(recalled),
            history_index=next_idx,
            history_draft=draft,
            error="",
            notice="",
        ), "edit"
    if key == "down":
        if state.history_index < 0:
            return state, "edit"
        if state.history_index == 0:
            draft = sanitize_terminal_text(state.history_draft)
            return _replace(
                state,
                url=draft,
                cursor=len(draft),
                history_index=-1,
                history_draft="",
                error="",
                notice="",
            ), "edit"
        next_idx = state.history_index - 1
        recalled = state.url_history[next_idx]
        return _replace(
            state,
            url=recalled,
            cursor=len(recalled),
            history_index=next_idx,
            error="",
            notice="",
        ), "edit"
    if key == "enter":
        try:
            normalize_url(state.url)
        except ValueError as exc:
            return _replace(state, error=str(exc), saved="", notice=""), "edit"
        validation_error = validate_capture_state(state)
        if validation_error:
            return _replace(state, error=validation_error, notice=""), "edit"
        return _replace(state, error="", saved="", notice="", busy=True, progress=0, cancelling=False), "capture"

    new_url, new_cursor, handled = _edit_text(state.url, state.cursor, key, max_len=MAX_URL_CHARS)
    if handled:
        changed_text = new_url != state.url
        changes = {
            "url": new_url,
            "cursor": new_cursor,
            "error": "",
            "notice": "",
        }
        if changed_text:
            changes["saved"] = ""
            changes["history_index"] = -1
            changes["history_draft"] = ""
        return _replace(state, **changes), "edit"
    return state, "edit"


def keys_from_bytes(pending: bytearray, data: bytes) -> list[str]:
    """Turn terminal bytes into key names, including arrows, F1, and bracketed paste.

    A lone Escape stays pending so it is not confused with an escape sequence.
    ``flush_bare_escape`` turns that byte into Esc after a short wait.
    """
    pending.extend(data)
    keys: list[str] = []
    while pending:
        byte = pending[0]
        if byte == 0x1B:
            if not _consume_escape(pending, keys):
                break
            continue
        if byte < 0x20 or byte == 0x7F:
            del pending[0]
            name = _control_key(byte)
            if name:
                keys.append(name)
            continue
        text, size = _one_utf8(pending)
        if size == 0:
            break
        if size < 0:
            del pending[0]
            continue
        del pending[:size]
        if text.isprintable():
            keys.append(text)
    return keys


def flush_bare_escape(pending: bytearray) -> list[str]:
    """Treat a lone Escape as Esc once no follow-up byte arrived."""
    if len(pending) == 1 and pending[0] == 0x1B:
        del pending[:]
        return ["esc"]
    return []


def _input_timeout(pending: bytearray, *, capturing: bool) -> float | None:
    if len(pending) == 1 and pending[0] == 0x1B:
        return 0.05
    if capturing:
        return 0.08
    return None


_CSI_MAP: dict[bytes, str] = {
    b"A": "up",
    b"B": "down",
    b"C": "right",
    b"D": "left",
    b"H": "home",
    b"F": "end",
    b"Z": "shift-tab",
    b"1~": "home",
    b"7~": "home",
    b"4~": "end",
    b"8~": "end",
    b"3~": "delete",
    b"11~": "f1",
    b"1;1P": "f1",
    b"1;5C": "ctrl-right",
    b"5C": "ctrl-right",
    b"1;3C": "ctrl-right",
    b"1;5D": "ctrl-left",
    b"5D": "ctrl-left",
    b"1;3D": "ctrl-left",
    b"47;5u": "ctrl-slash",
    b"27;5;47~": "ctrl-slash",
}

_SS3_MAP: dict[int, str] = {
    ord("A"): "up",
    ord("B"): "down",
    ord("C"): "right",
    ord("D"): "left",
    ord("H"): "home",
    ord("F"): "end",
    ord("P"): "f1",
}


def _consume_escape(pending: bytearray, keys: list[str]) -> bool:
    """Consume one escape sequence. False means more bytes are required."""
    if len(pending) == 1:
        return False
    second = pending[1]
    if second == 0x4F:  # SS3: \x1bO<x>
        if len(pending) < 3:
            return False
        mapped = _SS3_MAP.get(pending[2])
        if mapped:
            keys.append(mapped)
        del pending[:3]
        return True
    if second != 0x5B:  # Not CSI
        keys.append("esc")
        del pending[0]
        return True

    # Bracketed paste: \x1b[200~ ... \x1b[201~
    if pending.startswith(_PASTE_START):
        start_len = len(_PASTE_START)
        end_idx = pending.find(_PASTE_END, start_len)
        if end_idx < 0:
            # Stay in paste mode until the closing marker arrives, while keeping
            # the buffered paste bounded and preserving the last 5 bytes in case
            # \x1b[201~ is split across reads.
            tail_keep = len(_PASTE_END) - 1
            trunc_idx = pending.find(_PASTE_TRUNCATED_MARK, start_len)
            if trunc_idx >= 0:
                tail = bytes(pending[trunc_idx + 1 :])
                if len(tail) > tail_keep:
                    del pending[trunc_idx + 1 : len(pending) - tail_keep]
                return False
            if len(pending) > start_len + MAX_PASTE_BYTES + tail_keep:
                kept = bytes(pending[start_len : start_len + MAX_PASTE_BYTES]).replace(b"\x00", b"")
                tail = bytes(pending[-tail_keep:])
                pending[:] = _PASTE_START + kept + _PASTE_TRUNCATED_MARK + tail
            return False
        trunc_idx = pending.find(_PASTE_TRUNCATED_MARK, start_len, end_idx)
        body_end = trunc_idx if trunc_idx >= 0 else end_idx
        raw = bytes(pending[start_len:body_end]).replace(b"\x00", b"")
        del pending[: end_idx + len(_PASTE_END)]
        text = raw.decode("utf-8", errors="replace")
        keys.append("paste:" + text)
        return True

    end = 2
    while end < len(pending) and not 0x40 <= pending[end] <= 0x7E:
        end += 1
        if end > 16:
            del pending[:end]
            return True
    if end >= len(pending):
        return False
    body = bytes(pending[2 : end + 1])
    if body == b"200~":
        # Wait for closing \x1b[201~ while keeping \x1b[200~ at the head of pending.
        return False
    del pending[: end + 1]
    mapped = _CSI_MAP.get(body)
    if mapped:
        keys.append(mapped)
    return True


_CONTROL_KEY_MAP: dict[int, str] = {
    0x01: "ctrl-a",
    0x02: "ctrl-b",
    0x03: "quit",
    0x04: "ctrl-d",
    0x05: "ctrl-e",
    0x06: "ctrl-f",
    0x07: "ctrl-g",
    0x08: "backspace",
    0x09: "tab",
    0x0A: "enter",
    0x0C: "ctrl-l",
    0x0D: "enter",
    0x0F: "ctrl-o",
    0x10: "ctrl-p",
    0x12: "ctrl-r",
    0x14: "ctrl-t",
    0x15: "ctrl-u",
    0x16: "ctrl-v",
    0x17: "ctrl-w",
    0x18: "ctrl-x",
    0x19: "ctrl-y",
    0x1F: "ctrl-slash",
    0x7F: "backspace",
}


def _control_key(byte: int) -> str | None:
    return _CONTROL_KEY_MAP.get(byte)


def _one_utf8(pending: bytearray) -> tuple[str, int]:
    """Decode one UTF-8 character. Size 0 waits; size -1 drops a bad byte."""
    lead = pending[0]
    if lead < 0x80:
        return chr(lead), 1
    if 0xC2 <= lead <= 0xDF:
        need = 2
    elif 0xE0 <= lead <= 0xEF:
        need = 3
    elif 0xF0 <= lead <= 0xF4:
        need = 4
    else:
        return "", -1
    if len(pending) < need:
        return "", 0
    for index in range(1, need):
        if not 0x80 <= pending[index] <= 0xBF:
            return "", -1
    try:
        text = bytes(pending[:need]).decode("utf-8")
    except UnicodeDecodeError:
        return "", -1
    return text, need


def error_text(status: int, body: bytes) -> str:
    message = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(payload.get("detail"), str):
            message = str(payload["detail"])
    except (UnicodeError, json.JSONDecodeError, AttributeError):
        message = ""
    if not message:
        message = body.decode("utf-8", errors="replace")
    message = " ".join(sanitize_terminal_text(message).split())
    if not message:
        return f"The capture failed ({status})."
    if len(message) > 280:
        message = message[:279].rstrip() + "…"
    return message


def output_extension(fmt: str, *, diagnostics: bool = False) -> str:
    if diagnostics:
        return "zip"
    return FORMAT_EXTENSIONS.get(fmt, fmt)


def _content_type_matches_text_output(fmt: str, content_type: str | None) -> bool:
    if not content_type:
        return True
    mime = content_type.split(";", 1)[0].strip().lower()
    if not mime:
        return True
    if fmt == "html":
        return mime in {"text/html", "application/xhtml+xml"}
    if fmt == "markdown":
        return mime in {"text/markdown", "text/x-markdown", "text/plain"}
    if fmt == "metadata":
        return mime == "application/json" or mime.endswith("+json")
    return True


def looks_like_output(
    fmt: str,
    body: bytes,
    *,
    diagnostics: bool = False,
    content_type: str | None = None,
) -> bool:
    if diagnostics or fmt == "zip":
        return bool(body) and body.startswith((b"PK\x03\x04", b"PK\x05\x06"))
    if fmt == "markdown":
        if not _content_type_matches_text_output(fmt, content_type):
            return False
        if b"\x00" in body:
            return False
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
    if not body:
        return False
    if fmt == "png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if fmt == "jpeg":
        return body.startswith(b"\xff\xd8\xff")
    if fmt == "webp":
        return len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP"
    if fmt == "avif":
        return (
            len(body) >= 12
            and body[4:8] == b"ftyp"
            and any(brand in body[8:32] for brand in (b"avif", b"avis", b"mif1", b"miaf"))
        )
    if fmt == "pdf":
        return body.startswith(b"%PDF-")
    if fmt == "gif":
        return body.startswith(b"GIF8")
    if fmt == "mp4":
        return len(body) >= 12 and body[4:8] == b"ftyp"
    if fmt == "webm":
        return body.startswith(b"\x1a\x45\xdf\xa3")
    if fmt == "html":
        if not _content_type_matches_text_output(fmt, content_type):
            return False
        if b"\x00" in body:
            return False
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return bool(text.strip())
    if fmt == "metadata":
        if not _content_type_matches_text_output(fmt, content_type):
            return False
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(parsed, dict)
    return False


def output_path(
    directory: Path,
    url: str,
    fmt: str,
    moment: datetime,
    *,
    diagnostics: bool = False,
) -> Path:
    parsed = urllib.parse.urlparse(url)
    host = _HOST.sub("-", parsed.hostname or "page").strip("-") or "page"
    stamp = moment.astimezone().strftime("%Y%m%d-%H%M%S")
    ext = output_extension(fmt, diagnostics=diagnostics)
    path = directory / f"vipercapture-{host}-{stamp}.{ext}"
    suffix = 2
    while path.exists():
        path = directory / f"vipercapture-{host}-{stamp}-{suffix}.{ext}"
        suffix += 1
    return path


def progress_bar(tick: int, width: int = 20) -> str:
    width = max(8, width)
    filled = tick % (width + 1)
    return "█" * filled + "░" * (width - filled)


def page_label(full_page: bool) -> str:
    if full_page:
        return "full page"
    return "viewport"


def visible(text: str) -> str:
    return _ANSI.sub("", text)


def history_file_path() -> Path:
    return Path.home() / ".vipercapture" / "gui_history.json"


def load_url_history(path: Path | None = None, *, limit: int = HISTORY_LIMIT) -> tuple[str, ...]:
    target = history_file_path() if path is None else path
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, list):
        return ()
    seen: set[str] = set()
    items: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cleaned = sanitize_terminal_text(entry).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        items.append(cleaned)
        if len(items) >= limit:
            break
    return tuple(items)


def record_url_history(
    history: Sequence[str],
    url: str,
    *,
    limit: int = HISTORY_LIMIT,
) -> tuple[str, ...]:
    cleaned = sanitize_terminal_text(url).strip()
    if not cleaned:
        return tuple(history)[:limit]
    result = [cleaned]
    for item in history:
        if item != cleaned:
            result.append(item)
        if len(result) >= limit:
            break
    return tuple(result)


def save_url_history(
    urls: Sequence[str],
    path: Path | None = None,
    *,
    limit: int = HISTORY_LIMIT,
) -> None:
    target = history_file_path() if path is None else path
    items = list(urls)[:limit]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(target.parent, 0o700)
            except OSError:
                pass
        fd, name = tempfile.mkstemp(prefix=".gui-history-", suffix=".tmp", dir=target.parent)
        tmp = Path(name)
        try:
            if os.name != "nt" and hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(items, handle, indent=2)
            os.replace(tmp, target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    except OSError:
        return


def _no_window_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def read_clipboard() -> str | None:
    """Read plain text from the OS clipboard using native commands."""
    commands: list[list[str]] = []
    if os.name == "nt":
        commands.append(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw",
            ]
        )
    elif sys.platform == "darwin":
        commands.append(["pbpaste"])
    else:
        if shutil.which("wl-paste"):
            commands.append(["wl-paste", "--no-newline"])
        if shutil.which("xclip"):
            commands.append(["xclip", "-o", "-selection", "clipboard"])
        if shutil.which("xsel"):
            commands.append(["xsel", "--clipboard", "--output"])
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=2.0,
                check=False,
                **_no_window_kwargs(),  # type: ignore[arg-type]
            )
            if proc.returncode == 0:
                return proc.stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def copy_to_clipboard(text: str) -> bool:
    """Copy plain text to the OS clipboard using native commands."""
    commands: list[tuple[list[str], bytes]] = []
    encoded = text.encode("utf-8")
    if os.name == "nt":
        if text.isascii():
            commands.append((["clip"], encoded))
        commands.append(
            (
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())",
                ],
                encoded,
            )
        )
    elif sys.platform == "darwin":
        commands.append((["pbcopy"], encoded))
    else:
        if shutil.which("wl-copy"):
            commands.append((["wl-copy"], encoded))
        if shutil.which("xclip"):
            commands.append((["xclip", "-selection", "clipboard"], encoded))
        if shutil.which("xsel"):
            commands.append((["xsel", "--clipboard", "--input"], encoded))
    for cmd, data in commands:
        try:
            proc = subprocess.run(
                cmd,
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
                **_no_window_kwargs(),  # type: ignore[arg-type]
            )
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def open_in_viewer(path: str | Path) -> bool:
    """Open a saved file in the OS default viewer without blocking."""
    target = Path(path)
    if not target.exists():
        return False
    try:
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
            return True
        cmd = ["open", str(target)] if sys.platform == "darwin" else ["xdg-open", str(target)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def state_to_preset_settings(state: GuiState) -> dict[str, object]:
    """Serialize ``GuiState`` into Web UI-compatible preset settings."""
    cleanup_cfg = effective_cleanup(state)
    consent = str(cleanup_cfg["consent_mode"]) if cleanup_cfg else "none"
    block_ads = bool(cleanup_cfg["block_ads"]) if cleanup_cfg else False
    block_trackers = bool(cleanup_cfg["block_trackers"]) if cleanup_cfg else False
    block_chats = bool(cleanup_cfg["block_chats"]) if cleanup_cfg else False
    block_newsletters = bool(cleanup_cfg["block_newsletters"]) if cleanup_cfg else False
    return {
        "output": state.output,
        "width": state.viewport[0],
        "height": state.viewport[1],
        "density": int(state.scale) if float(state.scale).is_integer() else state.scale,
        "fullPage": state.full_page,
        "selector": state.selector,
        "quality": state.quality,
        "transparent": state.transparent,
        "diagnostics": state.diagnostics,
        "videoDuration": max(1, round(state.video_duration_ms / 1000)),
        "videoFps": state.video_fps,
        "videoScroll": state.video_scroll,
        "waitDelay": round(state.wait_delay_ms / 1000, 1),
        "waitSelector": state.wait_selector,
        "consent": consent,
        "blockAds": block_ads,
        "blockTrackers": block_trackers,
        "blockChats": block_chats,
        "blockNewsletters": block_newsletters,
        "device": state.device,
        "colorScheme": state.color_scheme,
        "paperSize": state.pdf_paper_size,
        "orientation": state.pdf_orientation,
        "engine": state.engine,
        "profile": state.profile,
        "stealth": state.stealth,
    }


def apply_preset_settings_with_warnings(
    state: GuiState,
    settings: Mapping[str, object],
) -> tuple[GuiState, tuple[str, ...]]:
    """Apply preset settings onto ``state`` and return any unsupported behavior-changing keys."""
    changes: dict[str, object] = {}
    ignored: list[str] = []

    out = settings.get("output")
    allowed_formats = state.available_formats or FORMATS
    if isinstance(out, str) and out in FORMATS:
        if out in allowed_formats:
            changes["output"] = out
        else:
            ignored.append(f"output({out})")

    w = settings.get("width")
    h = settings.get("height")
    if isinstance(w, (int, float)) and not isinstance(w, bool) and isinstance(h, (int, float)) and not isinstance(h, bool):
        wi, hi = int(w), int(h)
        if 1 <= wi <= 16_384 and 1 <= hi <= 16_384:
            changes["viewport"] = (wi, hi)

    density = settings.get("density")
    if isinstance(density, (int, float)) and not isinstance(density, bool) and 0.1 <= float(density) <= 8.0:
        d_val = float(density)
        changes["scale"] = int(d_val) if d_val.is_integer() else d_val

    if isinstance(settings.get("fullPage"), bool):
        changes["full_page"] = settings["fullPage"]

    if isinstance(settings.get("selector"), str):
        sel = sanitize_terminal_text(settings["selector"]).strip()
        changes["selector"] = sel
        if sel:
            changes["full_page"] = False

    quality = settings.get("quality")
    if isinstance(quality, (int, float)) and not isinstance(quality, bool) and 1 <= int(quality) <= 100:
        changes["quality"] = int(quality)

    if isinstance(settings.get("transparent"), bool):
        changes["transparent"] = settings["transparent"]

    if isinstance(settings.get("diagnostics"), bool):
        changes["diagnostics"] = settings["diagnostics"]

    vdur = settings.get("videoDuration")
    if isinstance(vdur, (int, float)) and not isinstance(vdur, bool) and 1 <= float(vdur) <= 30:
        changes["video_duration_ms"] = int(round(float(vdur) * 1000))

    vfps = settings.get("videoFps")
    if isinstance(vfps, (int, float)) and not isinstance(vfps, bool) and 1 <= int(vfps) <= 60:
        changes["video_fps"] = int(vfps)

    if isinstance(settings.get("videoScroll"), bool):
        changes["video_scroll"] = settings["videoScroll"]

    wdelay = settings.get("waitDelay")
    if isinstance(wdelay, (int, float)) and not isinstance(wdelay, bool) and 0 <= float(wdelay) <= 15:
        changes["wait_delay_ms"] = int(round(float(wdelay) * 1000))

    if isinstance(settings.get("waitSelector"), str):
        changes["wait_selector"] = sanitize_terminal_text(settings["waitSelector"]).strip()

    consent_raw = settings.get("consent")
    ads_raw = settings.get("blockAds")
    trackers_raw = settings.get("blockTrackers")
    chats_raw = settings.get("blockChats")
    newsletters_raw = settings.get("blockNewsletters")
    has_cleanup_keys = consent_raw is not None or any(
        isinstance(flag, bool) for flag in (ads_raw, trackers_raw, chats_raw, newsletters_raw)
    )
    if has_cleanup_keys:
        consent_val = (
            consent_raw
            if isinstance(consent_raw, str) and consent_raw in {"none", "reject", "accept", "hide"}
            else "none"
        )
        ads_val = bool(ads_raw) if isinstance(ads_raw, bool) else False
        trackers_val = bool(trackers_raw) if isinstance(trackers_raw, bool) else False
        chats_val = bool(chats_raw) if isinstance(chats_raw, bool) else False
        newsletters_val = bool(newsletters_raw) if isinstance(newsletters_raw, bool) else False
        changes["consent_mode"] = consent_val
        changes["block_ads"] = ads_val
        changes["block_trackers"] = trackers_val
        changes["block_chats"] = chats_val
        changes["block_newsletters"] = newsletters_val
        changes["cleanup"] = consent_val != "none" or any((ads_val, trackers_val, chats_val, newsletters_val))

    device = settings.get("device")
    if isinstance(device, str) and device in {"desktop", "iphone_14", "pixel_7", "ipad"}:
        changes["device"] = device

    scheme = settings.get("colorScheme")
    if isinstance(scheme, str) and scheme in COLOR_SCHEMES:
        changes["color_scheme"] = scheme
    elif isinstance(scheme, str) and scheme == "no-preference":
        ignored.append("colorScheme")

    paper = settings.get("paperSize")
    if isinstance(paper, str) and paper in PDF_PAPER_SIZES:
        changes["pdf_paper_size"] = paper
    elif isinstance(paper, str) and paper in {"A0", "A1", "A2", "A3", "A5", "A6"}:
        ignored.append("paperSize")

    orient = settings.get("orientation")
    if isinstance(orient, str) and orient in PDF_ORIENTATIONS:
        changes["pdf_orientation"] = orient

    engine = settings.get("engine")
    if isinstance(engine, str) and engine in BROWSER_ENGINES:
        changes["engine"] = engine

    profile = settings.get("profile")
    if isinstance(profile, str) and profile in PROFILES:
        changes["profile"] = profile

    if isinstance(settings.get("stealth"), bool):
        changes["stealth"] = settings["stealth"]

    # Detect behavior-changing Web UI preset options that the TUI does not model.
    if settings.get("preserveViewportWidth") is True:
        ignored.append("preserveViewportWidth")
    if isinstance(settings.get("headers"), str) and settings["headers"].strip():
        ignored.append("headers")
    if isinstance(settings.get("customCss"), str) and settings["customCss"].strip():
        ignored.append("customCss")
    if settings.get("clipEnabled") is True:
        ignored.append("clip")
    if isinstance(settings.get("locale"), str) and settings["locale"].strip():
        ignored.append("locale")
    if isinstance(settings.get("timezone"), str) and settings["timezone"].strip():
        ignored.append("timezone")
    if isinstance(settings.get("waitText"), str) and settings["waitText"].strip():
        ignored.append("waitText")
    if isinstance(settings.get("waitEvent"), str) and settings["waitEvent"] not in {"", "load"}:
        ignored.append("waitEvent")
    if isinstance(settings.get("reducedMotion"), str) and settings["reducedMotion"] not in {"", "system", "no-preference"}:
        ignored.append("reducedMotion")
    if isinstance(settings.get("failStatuses"), str) and settings["failStatuses"].strip():
        ignored.append("failStatuses")
    if isinstance(settings.get("lazyLoad"), str) and settings["lazyLoad"] not in {"", "adaptive"}:
        ignored.append("lazyLoad")
    if isinstance(settings.get("extractMode"), str) and settings["extractMode"] not in {"", "document"}:
        ignored.append("extractMode")
    if isinstance(settings.get("pdfMode"), str) and settings["pdfMode"] not in {"", "print"}:
        ignored.append("pdfMode")
    def _number_differs(value: object, default: int | float, *, tol: float = 0.0) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return True
        if tol > 0:
            try:
                return abs(float(value) - float(default)) > tol
            except OverflowError:
                return True
        return value != default

    if _number_differs(settings.get("waitTimeout"), 15):
        ignored.append("waitTimeout")
    if _number_differs(settings.get("pdfMargin"), 0.4, tol=1e-6):
        ignored.append("pdfMargin")
    if _number_differs(settings.get("videoBitrate"), 20):
        ignored.append("videoBitrate")

    return _replace(state, **changes), tuple(ignored)


def apply_preset_settings(state: GuiState, settings: Mapping[str, object]) -> GuiState:
    """Apply preset settings (Web UI or GUI format) onto ``state``."""
    updated, _ignored = apply_preset_settings_with_warnings(state, settings)
    return updated


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: Mapping[str, object] | None = None,
    timeout: float = 3.0,
    opener: object | None = None,
) -> tuple[int, dict[str, object] | None, str]:
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    if opener is None:
        if is_local_server(url):
            open_fn: object = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        else:
            open_fn = urllib.request.urlopen
    else:
        open_fn = opener
    try:
        with open_fn(req, timeout=timeout) as response:  # type: ignore[operator]
            status = getattr(response, "status", None)
            if status is None:
                status = getattr(response, "code", 200)
            body = response.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        raw = exc.read(64 * 1024)
        return exc.code, None, error_text(exc.code, raw)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        return 0, None, str(exc) or "Could not reach server."
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None, "Server returned invalid JSON."
    if not isinstance(parsed, dict):
        return status, None, "Server returned unexpected JSON."
    if status >= 400:
        return status, parsed, error_text(status, body)
    return status, parsed, ""


def fetch_app_config(
    base_url: str,
    *,
    timeout: float = 2.0,
    opener: object | None = None,
) -> dict[str, object] | None:
    status, data, err = _json_request(f"{base_url.rstrip('/')}/app-config", timeout=timeout, opener=opener)
    if err or status >= 400 or data is None:
        return None
    return data


def apply_app_config(state: GuiState, config: Mapping[str, object] | None) -> GuiState:
    if not isinstance(config, Mapping):
        return state
    changes: dict[str, object] = {}
    if isinstance(config.get("presets"), bool):
        changes["presets_supported"] = bool(config["presets"]) and state.local_server
    raw_formats = config.get("output_formats")
    if isinstance(raw_formats, list):
        allowed_set = {item for item in raw_formats if isinstance(item, str)}
        supported = tuple(fmt for fmt in FORMATS if fmt in allowed_set)
        if supported:
            changes["available_formats"] = supported
            if state.output not in supported:
                changes["output"] = supported[0]
    gpu = config.get("gpu")
    if isinstance(gpu, Mapping):
        mode = gpu.get("mode")
        if isinstance(mode, str) and mode in {"off", "auto"}:
            changes["gpu_mode"] = mode
        if isinstance(gpu.get("hardware_active"), bool):
            changes["gpu_hardware"] = gpu["hardware_active"]
        changes["gpu_known"] = True
    return _replace(state, **changes) if changes else state


def set_gpu_mode(
    base_url: str,
    mode: str,
    *,
    timeout: float = 20.0,
    opener: object | None = None,
) -> tuple[dict[str, object] | None, str]:
    status, data, err = _json_request(
        f"{base_url.rstrip('/')}/local/gpu-mode",
        method="POST",
        payload={"mode": mode},
        timeout=timeout,
        opener=opener,
    )
    if err or status >= 400 or data is None:
        return None, err or "Could not change GPU mode."
    gpu = data.get("gpu")
    if isinstance(gpu, dict):
        return gpu, ""
    return None, "Unexpected GPU response."


def _parse_presets_body(data: Mapping[str, object] | None) -> tuple[PresetEntry, ...]:
    if not isinstance(data, Mapping):
        return ()
    raw_list = data.get("presets")
    if not isinstance(raw_list, list):
        return ()
    entries: list[PresetEntry] = []
    for item in raw_list:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        settings = item.get("settings")
        if isinstance(name, str) and name.strip() and isinstance(settings, dict):
            entries.append(PresetEntry(name=name, settings=dict(settings)))
    return tuple(entries)


def fetch_presets(
    base_url: str,
    *,
    timeout: float = 3.0,
    opener: object | None = None,
) -> tuple[tuple[PresetEntry, ...], str]:
    status, data, err = _json_request(
        f"{base_url.rstrip('/')}/local/presets",
        method="GET",
        timeout=timeout,
        opener=opener,
    )
    if err or status >= 400:
        return (), err or "Could not load presets."
    return _parse_presets_body(data), ""


def save_preset_remote(
    base_url: str,
    name: str,
    settings: Mapping[str, object],
    *,
    timeout: float = 3.0,
    opener: object | None = None,
) -> tuple[tuple[PresetEntry, ...], str]:
    status, data, err = _json_request(
        f"{base_url.rstrip('/')}/local/presets",
        method="POST",
        payload={"name": name, "settings": dict(settings)},
        timeout=timeout,
        opener=opener,
    )
    if err or status >= 400:
        return (), err or "Could not save preset."
    return _parse_presets_body(data), ""


def delete_preset_remote(
    base_url: str,
    name: str,
    *,
    timeout: float = 3.0,
    opener: object | None = None,
) -> tuple[tuple[PresetEntry, ...], str]:
    encoded = urllib.parse.quote(name, safe="")
    status, data, err = _json_request(
        f"{base_url.rstrip('/')}/local/presets/{encoded}",
        method="DELETE",
        timeout=timeout,
        opener=opener,
    )
    if err or status >= 400:
        return (), err or "Could not delete preset."
    return _parse_presets_body(data), ""


def _char_width(char: str) -> int:
    if not char:
        return 0
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _display_width(text: str) -> int:
    return sum(_char_width(ch) for ch in text)


def _clip(text: str, width: int) -> str:
    """Keep the trailing end of text within ``width`` terminal display cells."""
    if width < 1:
        return ""
    if _display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    budget = width - 1
    kept: list[str] = []
    used = 0
    for ch in reversed(text):
        w = _char_width(ch)
        if used + w > budget:
            break
        kept.append(ch)
        used += w
    return "…" + "".join(reversed(kept))


def _clip_right(text: str, width: int) -> str:
    """Keep the leading prefix of text within ``width`` terminal display cells."""
    if width < 1:
        return ""
    if _display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    budget = width - 1
    kept: list[str] = []
    used = 0
    for ch in text:
        w = _char_width(ch)
        if used + w > budget:
            break
        kept.append(ch)
        used += w
    return "".join(kept) + "…"


def _window_around_cursor(text: str, cursor: int, width: int) -> tuple[str, str, str]:
    """Return ``(before, caret_char, after)`` fitting within ``width`` display cells.

    Handles horizontal scrolling with ``…`` markers and wide/CJK characters
    so the cursor is always visible and total cell width never exceeds ``width``.
    """
    if width < 1:
        return "", "", ""
    cursor = max(0, min(len(text), cursor))
    at_end = cursor == len(text)
    caret_char = " " if at_end else text[cursor]
    caret_w = _char_width(caret_char)
    if caret_w > width:
        return "", " ", ""

    # Fast path: entire text + caret fits without clipping.
    total_w = _display_width(text) + (1 if at_end else 0)
    if total_w <= width:
        before = text[:cursor]
        after = "" if at_end else text[cursor + 1 :]
        return before, caret_char, after

    left_full = text[:cursor]
    right_full = "" if at_end else text[cursor + 1 :]
    remaining = width - caret_w
    if remaining <= 0:
        return "", caret_char, ""

    # When cursor is at the very end, show as much trailing text as fits.
    if not right_full:
        return _clip(left_full, remaining), caret_char, ""
    if not left_full:
        return "", caret_char, _clip_right(right_full, remaining)

    left_w = _display_width(left_full)
    right_w = _display_width(right_full)

    # Prefer showing more context before the cursor (~65%), expanding into unused budget.
    target_left = max(1, (remaining * 2) // 3)
    target_right = remaining - target_left
    if left_w <= target_left:
        left_budget = left_w
        right_budget = remaining - left_budget
    elif right_w <= target_right:
        right_budget = right_w
        left_budget = remaining - right_budget
    else:
        left_budget = target_left
        right_budget = target_right

    before = _clip(left_full, left_budget) if left_budget > 0 else ""
    after = _clip_right(right_full, right_budget) if right_budget > 0 else ""
    return before, caret_char, after


def _ljust_cells(text: str, width: int) -> str:
    clipped = _clip_right(text, width)
    pad = max(0, width - _display_width(clipped))
    return clipped + " " * pad


def _split_word_cells(word: str, width: int) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    used = 0
    for ch in word:
        w = _char_width(ch)
        if w > width:
            continue
        if current and used + w > width:
            pieces.append("".join(current))
            current = [ch]
            used = w
        else:
            current.append(ch)
            used += w
    if current:
        pieces.append("".join(current))
    return pieces or [""]


def _wrap(text: str, width: int) -> list[str]:
    if width < 8:
        return [_clip_right(text, max(1, width))]
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    current_width = 0
    for word in words:
        word_width = _display_width(word)
        if word_width > width:
            if current:
                lines.append(current)
                current = ""
                current_width = 0
            chunks = _split_word_cells(word, width)
            lines.extend(chunks[:-1])
            current = chunks[-1]
            current_width = _display_width(current)
            continue
        if not current:
            current = word
            current_width = word_width
        elif current_width + 1 + word_width <= width:
            current = f"{current} {word}"
            current_width += 1 + word_width
        else:
            lines.append(current)
            current = word
            current_width = word_width
    if current:
        lines.append(current)
    return lines


def _format_window_items(active: str, formats: Sequence[str] = FORMATS) -> tuple[str, ...]:
    """Return a compact slice of ``formats`` centered around ``active``."""
    choices = tuple(formats) if formats else FORMATS
    try:
        idx = choices.index(active)
    except ValueError:
        idx = 0
    # Show up to 5 formats around the active one for a sleek bar.
    total = len(choices)
    if total <= 5:
        return choices
    start = max(0, min(total - 5, idx - 2))
    return choices[start : start + 5]


def _viewport_summary(state: GuiState) -> str:
    dims = f"{state.viewport[0]}x{state.viewport[1]}"
    if state.device != "desktop":
        label = viewport_preset_label(state.viewport, state.device, state.custom_viewport)
        if label != dims:
            return f"{label} {dims}"
    return dims


def _secondary_tokens(state: GuiState) -> list[tuple[str, str]]:
    """Return ``(text, style)`` tokens for the live status/options bar."""
    tokens: list[tuple[str, str]] = []
    tokens.append((f"theme:{state.color_scheme}", "1;36" if state.color_scheme != "system" else "2"))
    tokens.append((state.engine, "1;33" if state.engine != "chromium" else "2"))
    tokens.append(_cleanup_badge(state))
    if state.output in VIDEO_FORMATS:
        dur = format_seconds_short(state.video_duration_ms)
        tokens.append((f"dur:{dur}", "1;36" if state.video_duration_ms != VIDEO_DURATION_MS else "2"))
    else:
        delay = format_seconds_short(state.wait_delay_ms)
        tokens.append((f"delay:{delay}", "1;36" if state.wait_delay_ms > 0 else "2"))
    if state.scale != 1:
        tokens.append((_format_scale(state.scale), "1;35"))
    if state.transparent and state.output in TRANSPARENT_IMAGE_FORMATS:
        tokens.append(("alpha", "1;35"))
    if state.profile != "preview":
        tokens.append((state.profile, "1;35"))
    if state.selector.strip():
        tokens.append(("sel", "1;35"))
    if state.diagnostics:
        tokens.append(("zip", "1;35"))
    if not state.stealth:
        tokens.append(("stealth:off", "1;33"))
    if state.gpu_known or state.local_server:
        gpu_text = "GPU" if state.gpu_hardware else "CPU"
        tokens.append((gpu_text, "1;32" if state.gpu_hardware else "2"))
    tokens.append((state.server_label, "1;36" if not state.local_server else "2"))
    return tokens


def _secondary_summary_plain(state: GuiState, inner: int) -> str:
    tokens = _secondary_tokens(state)
    sep = "  "
    parts: list[str] = []
    used = 0
    for text, _style in tokens:
        add = len(text) + (len(sep) if parts else 0)
        if parts and used + add > inner:
            break
        parts.append(text)
        used += add
    return _clip_right(sep.join(parts), inner)


def _secondary_summary_colored(state: GuiState, inner: int) -> str:
    tokens = _secondary_tokens(state)
    sep = "  "
    parts: list[str] = []
    used = 0
    for text, style in tokens:
        w = _display_width(text)
        add = w + (len(sep) if parts else 0)
        if parts and used + add > inner:
            break
        if w > inner:
            clipped = _clip_right(text, inner)
            parts.append(f"\x1b[{style}m{clipped}\x1b[0m")
            used = _display_width(clipped)
            break
        if parts:
            parts.append(sep)
            used += len(sep)
        parts.append(f"\x1b[{style}m{text}\x1b[0m")
        used += w
    return "".join(parts) + " " * max(0, inner - used)


def _saved_status_text(state: GuiState) -> str:
    meta: list[str] = []
    if state.last_size_bytes > 0:
        meta.append(format_bytes(state.last_size_bytes))
    if state.last_duration_ms > 0:
        meta.append(format_duration(state.last_duration_ms))
    suffix = f" ({', '.join(meta)})" if meta else ""
    return f"Saved {state.saved}{suffix}"


def panel_lines(state: GuiState, width: int, *, max_extra_lines: int | None = None) -> list[str]:
    """The input panel, without ANSI, so tests can read the words."""
    inner = max(12, width - 4)
    if state.url:
        before, caret, after = _window_around_cursor(state.url, state.cursor, inner)
        link = (before + ("" if caret == " " and state.cursor == len(state.url) else caret) + after)
        link = _clip_right(link, inner)
    else:
        link = _clip_right("example.com", inner)
    mode = page_label(state.full_page)
    dims = _viewport_summary(state)
    choice = "  ".join(_format_window_items(state.output, state.available_formats))
    summary = f"{choice}    {mode}    {dims}"
    secondary = _secondary_summary_plain(state, inner)
    lines = [link, _clip_right(summary, inner), secondary]

    hint = validate_capture_state(state)
    if state.busy:
        if state.cancelling:
            label = "Cancelling"
        elif state.url:
            label = "Capturing"
        else:
            label = "Starting"
        lines.append(_clip(f"{label}  {progress_bar(state.progress, min(20, inner - 14))}", inner))
    elif state.error:
        limit = 3 if max_extra_lines is None else max(1, min(3, max_extra_lines))
        lines.extend(_wrap(sanitize_terminal_text(state.error), inner)[:limit])
    elif hint:
        limit = 2 if max_extra_lines is None else max(1, min(2, max_extra_lines))
        lines.extend(_wrap(f"Note: {hint}", inner)[:limit])
    elif state.saved:
        limit = 2 if max_extra_lines is None else max(1, min(2, max_extra_lines))
        text = sanitize_terminal_text(_saved_status_text(state))
        if state.notice:
            text = f"{text} · {sanitize_terminal_text(state.notice)}"
        lines.extend(_wrap(text, inner)[:limit])
    elif state.notice:
        limit = 2 if max_extra_lines is None else max(1, min(2, max_extra_lines))
        lines.extend(_wrap(sanitize_terminal_text(state.notice), inner)[:limit])
    return lines


def footer(state: GuiState, *, max_width: int | None = None) -> str:
    if state.editing_field:
        candidates: tuple[str, ...] = (
            "enter save    esc cancel    ctrl+v paste",
            "enter save  esc cancel",
            "enter  esc",
        )
    elif state.drawer == "options":
        candidates = (
            "↑/↓ select    ←/→/space change    enter edit    esc close",
            "↑/↓ nav  ←/→ change  enter edit  esc close",
            "↑/↓  ←/→  enter  esc",
        )
    elif state.drawer == "presets":
        candidates = (
            "↑/↓ select    enter apply    s save    d delete    esc close",
            "↑/↓ nav  enter apply  s save  d del  esc",
            "enter  s  d  esc",
        )
    elif state.drawer == "history":
        candidates = (
            "↑/↓ select    enter open    r reload    y copy    esc close",
            "↑/↓ nav  enter open  r reload  y copy  esc",
            "enter  r  y  esc",
        )
    elif state.drawer == "help":
        candidates = (
            "↑/↓ scroll    esc / f1 close",
            "esc / f1 close",
            "esc",
        )
    elif state.busy and state.cancelling:
        candidates = (
            "waiting for the capture to stop    ctrl+c quit",
            "stopping...  ctrl+c quit",
            "ctrl+c quit",
        )
    elif state.busy:
        candidates = ("esc cancel",)
    elif state.saved:
        candidates = (
            "enter capture again    ctrl+o open    ctrl+y copy    f1 help    esc quit",
            "enter again  ^o open  ^y copy  f1 help  esc",
            "enter  ^o  ^y  esc",
        )
    else:
        candidates = (
            "tab format    ctrl+p page    ctrl+r size    ctrl+g opts    enter capture    f1 help    esc quit",
            "tab format  ctrl+p page  ^g opts  enter capture  f1 help  esc",
            "tab fmt  ^p page  ^g opts  enter go  esc quit",
            "tab  ^p  enter  esc",
            "tab ^p enter esc",
        )
    if max_width is None:
        return candidates[0]
    for text in candidates:
        if _display_width(text) <= max_width:
            return text
    return _clip_right(candidates[-1], max(1, max_width))


def _screen_bounds(size: launch.WindowSize) -> tuple[int, int]:
    return max(20, size.cols), max(8, size.rows)


def _logo_placement(
    size: launch.WindowSize,
    protocol: str | None,
    png: bytes | None,
) -> tuple[bytes, int, int]:
    """Place the square mark in a cell box that stays square on screen."""
    if not protocol or not png:
        return b"", 0, 0
    logo_cols, logo_rows = launch.logo_cells(size)
    if logo_cols < 1 or logo_rows < 1:
        return b"", 0, 0
    logo = launch.place_logo(png, protocol, cols=logo_cols, rows=logo_rows, size=size)
    if not logo:
        return b"", 0, 0
    return logo, logo_cols, logo_rows


def _png_dimensions(png: bytes) -> tuple[int, int] | None:
    if len(png) < 24 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if png[12:16] != b"IHDR":
        return None
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    if width < 1 or height < 1:
        return None
    return width, height


def _preview_cells(
    size: launch.WindowSize,
    png: bytes,
    *,
    max_cols: int,
    max_rows: int,
) -> tuple[int, int]:
    if max_cols < 4 or max_rows < 2:
        return 0, 0
    dims = _png_dimensions(png)
    if dims is None:
        return min(max_cols, max_rows * 2), max_rows
    img_w, img_h = dims
    cell_w, cell_h = launch._cell_pixels(size)
    box_px_w = max_cols * cell_w
    box_px_h = max_rows * cell_h
    scale = min(box_px_w / img_w, box_px_h / img_h)
    cols = max(4, min(max_cols, round((img_w * scale) / cell_w)))
    rows = max(2, min(max_rows, round((img_h * scale) / cell_h)))
    return cols, rows


def _read_preview_png(record: CaptureRecord | None) -> bytes | None:
    if record is None or not record.path.lower().endswith(".png"):
        return None
    try:
        path = Path(record.path)
        if not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return data


def _wordmark(cols: int, *, image: bool, rows: int | None = None) -> tuple[str, ...]:
    if image:
        return ()
    if cols < 64 or (rows is not None and rows < 15):
        return ("ViperCapture",)
    return tuple(styled_ascii_logo())


def _render_edit_field_ansi(text: str, cursor: int, width: int, placeholder: str = "") -> str:
    clean_text = sanitize_terminal_text(text)
    if not clean_text:
        shown = _clip_right(placeholder, max(1, width - 1)) if placeholder else ""
        caret = "\x1b[7m \x1b[0m"
        pad = " " * max(0, width - 1 - _display_width(shown))
        return caret + (f"\x1b[2m{shown}\x1b[0m" if shown else "") + pad
    clamped_cursor = max(0, min(len(clean_text), cursor))
    before, caret_char, after = _window_around_cursor(clean_text, clamped_cursor, width)
    used = _display_width(before) + _char_width(caret_char) + _display_width(after)
    pad = " " * max(0, width - used)
    return f"{before}\x1b[7m{caret_char}\x1b[0m{after}{pad}"


def _box_lines(title: str, body_lines: list[str], inner: int) -> list[str]:
    bar = "\x1b[34m│\x1b[0m "
    title_text = f" {title} "
    if _display_width(title_text) <= inner:
        rem = inner + 2 - _display_width(title_text)
        left_dashes = "─"
        right_dashes = "─" * max(0, rem - 1)
        top = f"\x1b[34m┌{left_dashes}\x1b[1;37m{title_text}\x1b[0;34m{right_dashes}┐\x1b[0m"
    else:
        top = "\x1b[34m┌" + "─" * (inner + 2) + "┐\x1b[0m"
    bottom = "\x1b[34m└" + "─" * (inner + 2) + "┘\x1b[0m"
    framed = [top]
    for line in body_lines:
        vis_w = _display_width(visible(line))
        pad = " " * max(0, inner - vis_w)
        framed.append(bar + line + pad + " \x1b[34m│\x1b[0m")
    framed.append(bottom)
    return framed


def _scroll_window(total: int, selected: int, max_visible: int) -> tuple[int, int]:
    if total <= max_visible:
        return 0, total
    start = max(0, min(total - max_visible, selected - max_visible // 2))
    return start, start + max_visible


def _drawer_panel_options(state: GuiState, inner: int, max_body_rows: int) -> list[str]:
    body: list[str] = []
    extra_rows = 1 if (state.error or state.notice) else 0
    list_rows = max(1, max_body_rows - extra_rows)
    count = len(OPTIONS_FIELDS)
    selected = state.drawer_index % count
    start, end = _scroll_window(count, selected, list_rows)
    label_w = max(4, min(18, (inner - 3) // 2))
    val_w = max(1, inner - label_w - 3)
    for idx in range(start, end):
        field_key, label, _kind = OPTIONS_FIELDS[idx]
        is_sel = idx == selected
        prefix = "▸ " if is_sel else "  "
        lbl = _ljust_cells(label, label_w)
        if is_sel and state.editing_field == field_key:
            val_part = _render_edit_field_ansi(state.edit_buffer, state.edit_cursor, val_w)
            body.append(f"\x1b[1;34m{prefix}{lbl}\x1b[0m {val_part}")
        else:
            val_text = _ljust_cells(sanitize_terminal_text(_option_value_text(state, field_key)), val_w)
            if is_sel:
                body.append(f"\x1b[1;34m{prefix}{lbl}\x1b[0m \x1b[1;37m{val_text}\x1b[0m")
            else:
                body.append(f"{prefix}\x1b[2m{lbl}\x1b[0m {val_text}")
    if state.error:
        body.append(f"\x1b[31m{_ljust_cells(sanitize_terminal_text(state.error), inner)}\x1b[0m")
    elif state.notice:
        body.append(f"\x1b[36m{_ljust_cells(sanitize_terminal_text(state.notice), inner)}\x1b[0m")
    return _box_lines("Advanced Options", body, inner)


def _preset_summary(settings: Mapping[str, object]) -> str:
    bits: list[str] = []
    out = settings.get("output")
    if isinstance(out, str):
        clean_out = sanitize_terminal_text(out).strip()
        if clean_out:
            bits.append(clean_out)
    w, h = settings.get("width"), settings.get("height")
    if isinstance(w, (int, float)) and not isinstance(w, bool) and isinstance(h, (int, float)) and not isinstance(h, bool):
        bits.append(f"{int(w)}x{int(h)}")
    scheme = settings.get("colorScheme")
    if isinstance(scheme, str) and scheme != "system":
        clean_scheme = sanitize_terminal_text(scheme).strip()
        if clean_scheme:
            bits.append(clean_scheme)
    if settings.get("fullPage") is True:
        bits.append("full")
    return sanitize_terminal_text(" · ".join(bits))


def _drawer_panel_presets(state: GuiState, inner: int, max_body_rows: int) -> list[str]:
    body: list[str] = []
    if state.editing_field == "preset_name":
        prompt_lbl = "Save name: "
        field_w = max(4, inner - len(prompt_lbl))
        rendered = _render_edit_field_ansi(
            state.edit_buffer,
            state.edit_cursor,
            field_w,
            placeholder="e.g. Blog dark",
        )
        body.append(f"\x1b[1;34m{prompt_lbl}\x1b[0m{rendered}")
    if not state.presets_supported:
        body.append(f"\x1b[2m{_ljust_cells('Presets are only available for a local server.', inner)}\x1b[0m")
    elif not state.presets:
        body.append(f"\x1b[2m{_ljust_cells('No saved presets yet. Press s to save current settings.', inner)}\x1b[0m")
    else:
        overhead = len(body) + (1 if (state.error or state.notice) else 0)
        list_rows = max(1, max_body_rows - overhead)
        count = len(state.presets)
        selected = state.drawer_index % count
        start, end = _scroll_window(count, selected, list_rows)
        for idx in range(start, end):
            preset = state.presets[idx]
            is_sel = idx == selected
            prefix = "▸ " if is_sel else "  "
            clean_name = sanitize_terminal_text(preset.name)
            summary = _preset_summary(preset.settings)
            line_text = f"{clean_name}  ({summary})" if summary else clean_name
            clipped = _ljust_cells(line_text, max(1, inner - 2))
            if is_sel:
                body.append(f"\x1b[1;34m{prefix}\x1b[1;37m{clipped}\x1b[0m")
            else:
                body.append(f"{prefix}{clipped}")
    if state.error:
        body.append(f"\x1b[31m{_ljust_cells(sanitize_terminal_text(state.error), inner)}\x1b[0m")
    elif state.notice:
        body.append(f"\x1b[36m{_ljust_cells(sanitize_terminal_text(state.notice), inner)}\x1b[0m")
    return _box_lines("Saved Presets", body, inner)


def _drawer_panel_history(
    state: GuiState,
    inner: int,
    max_body_rows: int,
    *,
    preview_rows: int = 0,
) -> tuple[list[str], int]:
    """Return ``(boxed_lines, preview_row_offset_within_box)``."""
    body: list[str] = []
    preview_offset = -1
    if not state.captures:
        body.append(f"\x1b[2m{_ljust_cells('No captures in this session yet.', inner)}\x1b[0m")
        return _box_lines("Capture History", body, inner), -1

    count = len(state.captures)
    selected = state.drawer_index % count
    rec = state.captures[selected]
    detail_rows = 2 + (1 if (state.error or state.notice) else 0)
    reserve_preview = preview_rows + 1 if preview_rows > 0 else 0
    list_rows = max(1, min(6, max_body_rows - detail_rows - reserve_preview))
    start, end = _scroll_window(count, selected, list_rows)
    for idx in range(start, end):
        item = state.captures[idx]
        is_sel = idx == selected
        prefix = "▸ " if is_sel else "  "
        host = urllib.parse.urlparse(item.url).hostname or item.url
        ext = output_extension(item.output, diagnostics=item.diagnostics)
        meta = f"{ext} {format_bytes(item.size_bytes)} {format_duration(item.duration_ms)}"
        host_w = max(6, inner - 2 - len(meta) - 2)
        row_text = f"{_ljust_cells(host, host_w)}  {meta}"
        row_text = _ljust_cells(row_text, max(1, inner - 2))
        if is_sel:
            body.append(f"\x1b[1;34m{prefix}\x1b[1;37m{row_text}\x1b[0m")
        else:
            body.append(f"{prefix}{row_text}")

    # Selected item detail lines
    meta_line = (
        f"{rec.output} · {rec.viewport[0]}x{rec.viewport[1]} · {rec.engine} · "
        f"{format_bytes(rec.size_bytes)} · {format_duration(rec.duration_ms)}"
    )
    body.append(f"\x1b[2m{_ljust_cells(meta_line, inner)}\x1b[0m")
    body.append(f"\x1b[2m{_ljust_cells(rec.path, inner)}\x1b[0m")
    if preview_rows > 0:
        preview_offset = len(body) + 1  # +1 for top border line
        for _ in range(preview_rows):
            body.append(" " * inner)
    if state.error:
        body.append(f"\x1b[31m{_ljust_cells(sanitize_terminal_text(state.error), inner)}\x1b[0m")
    elif state.notice:
        body.append(f"\x1b[36m{_ljust_cells(sanitize_terminal_text(state.notice), inner)}\x1b[0m")
    return _box_lines("Capture History", body, inner), preview_offset


def _drawer_panel_help(state: GuiState, inner: int, max_body_rows: int) -> list[str]:
    body: list[str] = []
    two_col = inner >= 60
    if two_col:
        half = (inner - 2) // 2
        key_w = 18
        desc_w = max(4, half - key_w - 1)
        rows_data: list[str] = []
        mid = (len(HELP_ENTRIES) + 1) // 2
        for i in range(mid):
            left_k, left_d = HELP_ENTRIES[i]
            left_cell = f"\x1b[1;34m{_ljust_cells(left_k, key_w)}\x1b[0m {_ljust_cells(left_d, desc_w)}"
            if i + mid < len(HELP_ENTRIES):
                right_k, right_d = HELP_ENTRIES[i + mid]
                right_cell = f"\x1b[1;34m{_ljust_cells(right_k, key_w)}\x1b[0m {_ljust_cells(right_d, desc_w)}"
            else:
                right_cell = " " * half
            rows_data.append(f"{left_cell}  {right_cell}")
    else:
        key_w = min(18, max(10, inner // 2))
        desc_w = max(4, inner - key_w - 1)
        rows_data = [
            f"\x1b[1;34m{_ljust_cells(k, key_w)}\x1b[0m {_ljust_cells(d, desc_w)}"
            for k, d in HELP_ENTRIES
        ]
    max_start = max(0, len(rows_data) - max(1, max_body_rows))
    start = min(state.drawer_index, max_start)
    body.extend(rows_data[start : start + max(1, max_body_rows)])
    return _box_lines("Keybinds Help", body, inner)


def _frame_block(
    state: GuiState,
    cols: int,
    *,
    image: bool,
    rows: int | None = None,
    preview_rows: int = 0,
) -> list[str]:
    panel_width = min(76, cols - 4)
    inner = max(12, panel_width - 4)
    footer_line = f"\x1b[2m{footer(state, max_width=max(8, cols - 2))}\x1b[0m"

    if state.drawer:
        max_body = 16 if rows is None else max(1, rows - 5)
        if state.drawer == "options":
            colored = _drawer_panel_options(state, inner, max_body)
        elif state.drawer == "presets":
            colored = _drawer_panel_presets(state, inner, max_body)
        elif state.drawer == "history":
            colored, _ = _drawer_panel_history(state, inner, max_body, preview_rows=preview_rows)
        else:
            colored = _drawer_panel_help(state, inner, max_body)
        if rows is not None and len(colored) + 2 > rows:
            return [*colored, footer_line]
        return [*colored, "", footer_line]

    max_extra: int | None = None
    if rows is not None and rows < 13:
        max_extra = max(1, rows - 7)
    plain = panel_lines(state, panel_width, max_extra_lines=max_extra)
    colored = _color_panel(state, panel_width, plain)
    mark = _wordmark(cols, image=image, rows=rows)
    if rows is not None and len(mark) > 1 and (len(mark) + len(colored) + 3) > rows:
        mark = ("ViperCapture",)
    full_block = [*mark, "", *colored, "", footer_line]
    if rows is not None and len(full_block) > rows:
        compact_block = [*mark, *colored, footer_line]
        if len(compact_block) <= rows:
            return compact_block
        if rows <= len(colored) + 1:
            return [*colored, footer_line]
        return compact_block
    return full_block


def _frame_origin(rows: int, block_len: int, logo_rows: int) -> int:
    return max(1, (rows - block_len - logo_rows) // 2)


def _render_tiny_frame(size: launch.WindowSize, state: GuiState, *, clear: bool) -> bytes:
    """Minimal frame when the physical terminal is smaller than 20x8."""
    cols = max(1, size.cols)
    rows = max(1, size.rows)
    parts: list[bytes] = []
    if clear:
        parts.append(b"\x1b[2J\x1b[H")
    else:
        for erase_row in range(1, rows + 1):
            parts.append(f"\x1b[{erase_row};1H\x1b[2K".encode("ascii"))
    if state.drawer:
        lines = [_clip_right(state.drawer.title(), cols)]
        if rows >= 2:
            if state.drawer == "options":
                key, label, _ = OPTIONS_FIELDS[state.drawer_index % len(OPTIONS_FIELDS)]
                lines.append(_clip_right(f"{label}: {_option_value_text(state, key)}", cols))
            elif state.drawer == "presets":
                if state.presets:
                    lines.append(_clip_right(state.presets[state.drawer_index % len(state.presets)].name, cols))
                else:
                    lines.append(_clip_right("No presets", cols))
            elif state.drawer == "history":
                if state.captures:
                    lines.append(_clip_right(state.captures[state.drawer_index % len(state.captures)].url, cols))
                else:
                    lines.append(_clip_right("No captures", cols))
            else:
                k, d = HELP_ENTRIES[state.drawer_index % len(HELP_ENTRIES)]
                lines.append(_clip_right(f"{k}: {d}", cols))
        if rows >= 3:
            lines.append(_clip_right(footer(state, max_width=cols), cols))
    else:
        lines = [_clip_right("ViperCapture", cols)]
        if rows >= 2:
            lines.append(_clip(state.url or "example.com", cols))
        if rows >= 3:
            if state.busy:
                lines.append(_clip_right("Cancelling" if state.cancelling else "Capturing", cols))
            elif state.error:
                lines.append(_clip_right(state.error, cols))
            elif state.saved:
                lines.append(_clip_right(f"Saved {state.saved}", cols))
            else:
                lines.append(_clip_right(f"{state.output} {page_label(state.full_page)}", cols))
        if rows >= 4:
            lines.append(_clip_right(footer(state, max_width=cols), cols))
    for row_index, raw_text in enumerate(lines[:rows], start=1):
        text = _clip_right(sanitize_terminal_text(raw_text), cols)
        width = _display_width(text)
        col = max(1, (cols - width) // 2 + 1)
        parts.append(f"\x1b[{row_index};{col}H".encode("ascii"))
        parts.append(text.encode("utf-8"))
    return b"".join(parts)


def render_frame(
    size: launch.WindowSize,
    state: GuiState,
    *,
    protocol: str | None = None,
    png: bytes | None = None,
    clear: bool = True,
    clear_images: bool = False,
    logo: bytes | None = None,
    logo_cols: int | None = None,
    logo_rows: int | None = None,
    origin: int | None = None,
    preview_png: bytes | None = None,
) -> bytes:
    if size.cols < 20 or size.rows < 8:
        return _render_tiny_frame(size, state, clear=clear)
    cols, rows = _screen_bounds(size)

    # Inline thumbnail preview inside History drawer on image-capable terminals.
    preview_bytes = b""
    preview_cols = 0
    preview_rows = 0
    preview_offset = -1
    if state.drawer == "history" and protocol and state.captures and cols >= 40 and rows >= 18:
        rec = state.captures[state.drawer_index % len(state.captures)]
        img = preview_png if preview_png is not None else _read_preview_png(rec)
        if img and protocol == "sixel":
            dims = _png_dimensions(img)
            if dims is None or dims[0] * dims[1] > MAX_SIXEL_PREVIEW_PIXELS:
                img = None
        if img:
            panel_width = min(76, cols - 4)
            inner = max(12, panel_width - 4)
            max_p_rows = min(10, max(4, rows - 14))
            preview_cols, preview_rows = _preview_cells(size, img, max_cols=inner - 2, max_rows=max_p_rows)
            if preview_cols > 0 and preview_rows > 0:
                preview_bytes = launch.place_logo(
                    img, protocol, cols=preview_cols, rows=preview_rows, size=size
                )
                if not preview_bytes:
                    preview_cols = preview_rows = 0

    if state.drawer:
        logo_bytes = b""
        logo_cols = 0
        logo_rows = 0
    else:
        if logo is None:
            logo, auto_cols, auto_rows = _logo_placement(size, protocol, png)
            if logo_cols is None:
                logo_cols = auto_cols
            if logo_rows is None:
                logo_rows = auto_rows
        if logo_rows is None:
            logo_rows = 0
        if logo_cols is None:
            logo_cols = 0
        logo_bytes = logo or b""

    available_rows = max(1, rows - (logo_rows + 1 if logo_rows else 0))
    if state.drawer == "history" and preview_rows > 0:
        panel_width = min(76, cols - 4)
        inner = max(12, panel_width - 4)
        max_body = max(1, available_rows - 5)
        colored_hist, preview_offset = _drawer_panel_history(
            state, inner, max_body, preview_rows=preview_rows
        )
        footer_line = f"\x1b[2m{footer(state, max_width=max(8, cols - 2))}\x1b[0m"
        block = (
            [*colored_hist, footer_line]
            if len(colored_hist) + 2 > available_rows
            else [*colored_hist, "", footer_line]
        )
    else:
        block = _frame_block(state, cols, image=logo_rows > 0, rows=available_rows)
    if origin is None:
        origin = _frame_origin(rows, len(block), logo_rows)
    parts: list[bytes] = []
    if clear:
        if clear_images and protocol == "kitty":
            parts.append(b"\x1b_Ga=d,d=A,q=2\x1b\\")
        parts.append(b"\x1b[2J\x1b[H")
    row = max(1, origin)
    windows_image = b""
    if logo_rows:
        if clear and logo_bytes:
            col = max(1, (cols - logo_cols) // 2 + 1)
            placement = f"\x1b[{row};{col}H".encode("ascii") + logo_bytes
            if os.name == "nt" and protocol == "iterm2":
                # ConPTY forwards the cursor position for an inline image only
                # while the cursor is visible. Draw it after the text updates.
                windows_image = b"\x1b[?25h" + placement + b"\x1b[?25l"
            else:
                parts.append(placement)
        row += logo_rows + 1
    if not clear:
        for erase_row in range(min(row, rows), rows + 1):
            parts.append(f"\x1b[{erase_row};1H\x1b[2K".encode("ascii"))
    block_start_row = row
    for line in block:
        if row > rows:
            break
        shown = visible(line)
        col = max(1, (cols - _display_width(shown)) // 2 + 1)
        parts.append(f"\x1b[{row};{col}H".encode("ascii"))
        parts.append(line.encode("utf-8"))
        row += 1
    if preview_bytes and preview_offset >= 0:
        preview_row = block_start_row + preview_offset
        if preview_row + preview_rows - 1 <= rows:
            preview_col = max(1, (cols - preview_cols) // 2 + 1)
            placement = f"\x1b[{preview_row};{preview_col}H".encode("ascii") + preview_bytes
            if os.name == "nt" and protocol == "iterm2":
                windows_image += b"\x1b[?25h" + placement + b"\x1b[?25l"
            else:
                parts.append(placement)
    parts.append(windows_image)
    return b"".join(parts)


def _color_panel(state: GuiState, width: int, plain: list[str]) -> list[str]:
    inner = max(12, width - 4)
    bar = "\x1b[34m│\x1b[0m "
    rule = "\x1b[34m┌" + "─" * (inner + 2) + "┐\x1b[0m"
    bottom = "\x1b[34m└" + "─" * (inner + 2) + "┘\x1b[0m"
    hint = validate_capture_state(state)
    lines = [rule]
    for index, text in enumerate(plain):
        if index == 1:
            body = _colored_summary(state, inner)
        elif index == 2:
            body = _secondary_summary_colored(state, inner)
        elif index == 0 and not state.busy:
            body = _render_edit_field_ansi(
                state.url,
                state.cursor,
                inner,
                placeholder="example.com",
            )
        elif state.error and index >= 3 and not state.busy:
            body = f"\x1b[31m{_ljust_cells(text, inner)}\x1b[0m"
        elif hint and index >= 3 and not state.busy and not state.error:
            body = f"\x1b[33m{_ljust_cells(text, inner)}\x1b[0m"
        elif state.saved and index >= 3 and not state.busy:
            body = f"\x1b[32m{_ljust_cells(text, inner)}\x1b[0m"
        elif state.notice and index >= 3 and not state.busy:
            body = f"\x1b[36m{_ljust_cells(text, inner)}\x1b[0m"
        else:
            body = _ljust_cells(text, inner)
        lines.append(bar + body + " \x1b[34m│\x1b[0m")
    lines.append(bottom)
    return lines


def _colored_formats(state: GuiState, separator: str) -> tuple[str, int]:
    pieces: list[str] = []
    visible_len = 0
    window = _format_window_items(state.output, state.available_formats)
    for index, name in enumerate(window):
        if index:
            pieces.append(separator)
            visible_len += len(separator)
        if name == state.output:
            pieces.append(f"\x1b[1;34m{name}\x1b[0m")
        else:
            pieces.append(f"\x1b[2m{name}\x1b[0m")
        visible_len += len(name)
    return "".join(pieces), visible_len


def _colored_summary(state: GuiState, inner: int) -> str:
    mode = page_label(state.full_page)
    dims = _viewport_summary(state)
    raw_dims = f"{state.viewport[0]}x{state.viewport[1]}"
    for fmt_sep, tail_sep in (("  ", "    "), (" ", "  ")):
        colored_fmts, fmt_len = _colored_formats(state, fmt_sep)
        for dim_text in (dims, raw_dims):
            tail = f"{tail_sep}{mode}{tail_sep}{dim_text}"
            total = fmt_len + len(tail)
            if total <= inner:
                return colored_fmts + tail + " " * (inner - total)
    short_mode = "full" if state.full_page else "view"
    active = f"\x1b[1;34m{state.output}\x1b[0m"
    for tail in (f"  {mode}  {dims}", f"  {mode}  {raw_dims}", f"  {short_mode}  {raw_dims}", f"  {raw_dims}", ""):
        total = len(state.output) + len(tail)
        if total <= inner:
            return active + tail + " " * (inner - total)
    clipped = _clip(state.output, inner)
    return f"\x1b[1;34m{clipped}\x1b[0m" + " " * max(0, inner - len(clipped))


class _Cancelled(Exception):
    """The user pressed Esc while the render request was open."""


def read_capture(
    response: object,
    limit: int = MAX_CAPTURE_BYTES,
    cancel: "CancelFlag | None" = None,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read = getattr(response, "read")
    while True:
        if cancel is not None and cancel.requested:
            raise _Cancelled()
        try:
            block = read(64 * 1024)
        except (TimeoutError, OSError, http.client.HTTPException):
            if cancel is not None and cancel.requested:
                raise _Cancelled() from None
            raise
        if not block:
            break
        total += len(block)
        if total > limit:
            raise ValueError("The capture is larger than 200 MB.")
        chunks.append(block)
    return b"".join(chunks)


class CancelFlag:
    def __init__(self) -> None:
        self.requested = False
        self._lock = threading.Lock()
        self._close: Callable[[], None] | None = None

    def bind(self, close: Callable[[], None]) -> None:
        with self._lock:
            self._close = close
            requested = self.requested
        if requested:
            _safe_close(close)

    def abort(self) -> None:
        with self._lock:
            self.requested = True
            close = self._close
        if close is not None:
            _safe_close(close)


def _safe_close(close: Callable[[], None]) -> None:
    try:
        close()
    except OSError:
        return


def capture_credential(
    *,
    started_server: bool,
    environ: Mapping[str, str] | None = None,
    endpoint: str | None = None,
) -> str:
    """Bearer token for POST /v1/render.

    Admin token fallback is restricted to the default local server (or a server
    started by this session). Explicit remote servers only receive
    ``VIPERCAPTURE_API_KEY`` and only over HTTPS.
    """
    env = os.environ if environ is None else environ
    project_key = _header_token(env.get("VIPERCAPTURE_API_KEY", ""))
    admin = _header_token(env.get("VIPERCAPTURE_ADMIN_TOKEN", ""))
    if started_server and admin:
        return admin
    if endpoint is not None:
        try:
            parsed = urllib.parse.urlsplit(endpoint)
        except ValueError:
            return ""
        scheme = parsed.scheme.lower()
        local = is_local_server(endpoint)
        default_local = local and (parsed.port or (443 if scheme == "https" else 80)) == launch.PORT and scheme == "http"
        if not local:
            if scheme != "https":
                return ""
            return project_key
        if project_key:
            return project_key
        return admin if default_local else ""
    if project_key:
        return project_key
    return admin


def _header_token(value: str) -> str:
    token = value.strip()
    if any(char in token for char in "\r\n\x00"):
        return ""
    return token


def _cancelled_state(state: GuiState) -> GuiState:
    return _replace(state, busy=False, cancelling=False, saved="", error="Capture cancelled.")


def _response_header(response: object, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    raw: object = None
    if headers is not None and hasattr(headers, "get"):
        raw = headers.get(name)
        if raw is None:
            raw = headers.get(name.lower())
    elif hasattr(response, "getheader"):
        raw = response.getheader(name)
    return raw if isinstance(raw, str) else None


def _response_encode_ms(response: object) -> int:
    raw = _response_header(response, "X-ViperCapture-Encode-Ms")
    if isinstance(raw, str):
        try:
            value = int(float(raw.strip()))
            if value > 0:
                return value
        except ValueError:
            pass
    return 0


def perform_capture(
    state: GuiState,
    *,
    directory: Path,
    endpoint: str = launch.URL + "v1/render",
    timeout: float | None = None,
    moment: datetime | None = None,
    opener: object | None = None,
    cancel: CancelFlag | None = None,
    started_server: bool = False,
    authorization: str | None = None,
) -> GuiState:
    """POST one render and save the file. Network errors stay on the panel."""
    try:
        url = normalize_url(state.url)
    except ValueError as exc:
        return _replace(state, busy=False, error=str(exc))
    validation_error = validate_capture_state(state)
    if validation_error:
        return _replace(state, busy=False, cancelling=False, error=validation_error)
    payload = capture_payload(state, url)
    if timeout is None:
        timeout = 180 if state.output in VIDEO_FORMATS else 90
    if authorization is None:
        authorization = capture_credential(started_server=started_server, endpoint=endpoint)
    headers = {"Content-Type": "application/json", "Accept": "*/*"}
    if authorization:
        headers["Authorization"] = f"Bearer {authorization}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")

    def open_url(req: urllib.request.Request, timeout: float) -> object:
        if opener is None:
            return _open_capture(req, timeout, cancel)
        return opener(req, timeout=timeout)  # type: ignore[operator]

    started_at = time.monotonic()
    encode_ms = 0
    content_type: str | None = None
    try:
        with open_url(request, timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = getattr(response, "code", 200)
            encode_ms = _response_encode_ms(response)
            content_type = _response_header(response, "Content-Type")
            body = read_capture(response, cancel=cancel)
    except _Cancelled:
        return _cancelled_state(state)
    except urllib.error.HTTPError as exc:
        if cancel is not None and cancel.requested:
            return _cancelled_state(state)
        detail = error_text(exc.code, exc.read(64 * 1024))
        return _replace(state, busy=False, cancelling=False, error=detail)
    except urllib.error.URLError:
        if cancel is not None and cancel.requested:
            return _cancelled_state(state)
        return _replace(
            state,
            busy=False,
            cancelling=False,
            error="The API server did not answer. Check that ViperCapture is running.",
        )
    except (TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
        if cancel is not None and cancel.requested:
            return _cancelled_state(state)
        return _replace(state, busy=False, cancelling=False, error=sanitize_terminal_text(str(exc)))
    wall_ms = max(1, round((time.monotonic() - started_at) * 1000))
    duration_ms = encode_ms if encode_ms > 0 else wall_ms
    if (cancel is not None and cancel.requested) or state.cancelling:
        return _cancelled_state(state)
    if not (200 <= status < 300):
        return _replace(state, busy=False, cancelling=False, error=error_text(status, body))
    expected_label = "zip" if state.diagnostics else state.output
    if not looks_like_output(
        state.output,
        body,
        diagnostics=state.diagnostics,
        content_type=content_type,
    ):
        return _replace(
            state,
            busy=False,
            cancelling=False,
            error=f"The API did not return a {expected_label} file.",
        )
    when = moment or datetime.now(timezone.utc)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = output_path(directory, url, state.output, when, diagnostics=state.diagnostics)
        _save_exclusive(directory, path, body)
    except OSError as exc:
        return _replace(state, busy=False, cancelling=False, error=sanitize_terminal_text(str(exc)))
    cleanup_cfg = effective_cleanup(state)
    record = CaptureRecord(
        url=url,
        output=state.output,
        path=str(path),
        size_bytes=len(body),
        duration_ms=duration_ms,
        timestamp=when.astimezone().strftime("%H:%M:%S"),
        full_page=state.full_page,
        viewport=state.viewport,
        device=state.device,
        color_scheme=state.color_scheme,
        engine=state.engine,
        cleanup=cleanup_cfg is not None,
        consent_mode=str(cleanup_cfg["consent_mode"]) if cleanup_cfg else "none",
        block_ads=bool(cleanup_cfg["block_ads"]) if cleanup_cfg else False,
        block_trackers=bool(cleanup_cfg["block_trackers"]) if cleanup_cfg else False,
        block_chats=bool(cleanup_cfg["block_chats"]) if cleanup_cfg else False,
        block_newsletters=bool(cleanup_cfg["block_newsletters"]) if cleanup_cfg else False,
        scale=state.scale,
        quality=state.quality,
        transparent=state.transparent,
        profile=state.profile,
        wait_delay_ms=state.wait_delay_ms,
        wait_selector=state.wait_selector,
        selector=state.selector,
        video_duration_ms=state.video_duration_ms,
        video_scroll=state.video_scroll,
        video_fps=state.video_fps,
        pdf_paper_size=state.pdf_paper_size,
        pdf_orientation=state.pdf_orientation,
        stealth=state.stealth,
        diagnostics=state.diagnostics,
    )
    captures = (record, *state.captures)[:HISTORY_LIMIT]
    url_history = record_url_history(state.url_history, url)
    return _replace(
        state,
        busy=False,
        cancelling=False,
        error="",
        notice="",
        saved=str(path),
        last_size_bytes=len(body),
        last_duration_ms=duration_ms,
        captures=captures,
        url_history=url_history,
        history_index=-1,
        history_draft="",
    )


def _save_exclusive(directory: Path, path: Path, body: bytes) -> None:
    """Write the capture to a new file, then rename it into place."""
    fd, name = tempfile.mkstemp(prefix=".vipercapture-", suffix=".part", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _outgoing_headers(request: urllib.request.Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.header_items():
        if key.lower() in {"host", "content-length"}:
            continue
        headers[key] = value
    return headers


def _close_connection(connection: http.client.HTTPConnection) -> None:
    sock = getattr(connection, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        connection.close()
    except OSError:
        pass
    if sock is not None and hasattr(sock, "_real_close"):
        try:
            sock._real_close()
        except OSError:
            pass


class _CaptureResponse:
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
    ) -> None:
        self._response = response
        self._connection = connection
        self.status = response.status
        self.code = response.status
        self.headers = response.headers

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._response.getheader(name, default)

    def read(self, size: int) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        try:
            self._response.close()
        except OSError:
            pass
        _close_connection(self._connection)

    def __enter__(self) -> "_CaptureResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        self.close()
        return False


def _proxy_for_url(url: str, scheme: str, hostname: str) -> str | None:
    if is_local_server(url):
        return None
    proxies = urllib.request.getproxies()
    if not proxies:
        return None
    if urllib.request.proxy_bypass_environment(hostname, proxies) or urllib.request.proxy_bypass(hostname):
        return None
    proxy_url = proxies.get(scheme) or proxies.get("all")
    if not proxy_url:
        return None
    return proxy_url


def _open_capture(
    request: urllib.request.Request,
    timeout: float,
    cancel: CancelFlag | None,
) -> _CaptureResponse:
    parsed = urllib.parse.urlsplit(request.full_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("The API address is not http or https.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    headers = _outgoing_headers(request)
    request_target = path
    proxy_raw = _proxy_for_url(request.full_url, parsed.scheme, parsed.hostname)
    if proxy_raw:
        proxy_url = proxy_raw if "://" in proxy_raw else f"http://{proxy_raw}"
        proxy_parsed = urllib.parse.urlsplit(proxy_url)
        if proxy_parsed.scheme not in {"http", "https"} or proxy_parsed.hostname is None:
            raise ValueError("The configured proxy URL is invalid.")
        proxy_port = proxy_parsed.port or (443 if proxy_parsed.scheme == "https" else 80)
        proxy_headers: dict[str, str] = {}
        if proxy_parsed.username is not None:
            user = urllib.parse.unquote(proxy_parsed.username)
            password = urllib.parse.unquote(proxy_parsed.password or "")
            creds = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            proxy_headers["Proxy-Authorization"] = f"Basic {creds}"
        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                proxy_parsed.hostname,
                proxy_port,
                timeout=timeout,
            )
            connection.set_tunnel(parsed.hostname, port, headers=proxy_headers or None)
        else:
            if proxy_parsed.scheme == "https":
                connection = http.client.HTTPSConnection(
                    proxy_parsed.hostname,
                    proxy_port,
                    timeout=timeout,
                )
            else:
                connection = http.client.HTTPConnection(
                    proxy_parsed.hostname,
                    proxy_port,
                    timeout=timeout,
                )
            request_target = request.full_url
            headers.update(proxy_headers)
    elif parsed.scheme == "https":
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            port,
            timeout=timeout,
        )
    else:
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
    try:
        connection.request(
            request.get_method(),
            request_target,
            body=request.data,
            headers=headers,
        )
        if cancel is not None:
            cancel.bind(lambda: _close_connection(connection))
        if cancel is not None and cancel.requested:
            raise _Cancelled()
        response = connection.getresponse()
    except _Cancelled:
        _close_connection(connection)
        raise
    except Exception:
        _close_connection(connection)
        if cancel is not None and cancel.requested:
            raise _Cancelled() from None
        raise
    return _CaptureResponse(response, connection)


def server_command() -> list[str]:
    return [
        sys.executable,
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


def _window_size() -> launch.WindowSize:
    if os.name != "nt":
        try:
            fd = sys.stdout.fileno()
            return launch.read_window_size(fd)
        except (OSError, ValueError, AttributeError, ModuleNotFoundError):
            pass
    for stream in (sys.stdout, sys.stdin):
        try:
            fd = stream.fileno()
            size = os.get_terminal_size(fd)
            return launch.WindowSize(size.columns, size.lines)
        except (OSError, ValueError, AttributeError):
            pass
    size = shutil.get_terminal_size((80, 24))
    return launch.WindowSize(size.columns, size.lines)


class _Drawer:
    """Redraw the menu in place. Images are sent again only when layout/selection changes."""

    def __init__(self) -> None:
        self._key: tuple[int, int, int, int, str | None] | None = None
        self._origin: int | None = None
        self._last_drawer: str | None = None
        self._last_drawer_sig: tuple[object, ...] | None = None
        self._last_preview: str | None = None
        self._has_image = False
        self._logo = b""
        self._logo_cols = 0
        self._logo_rows = 0

    def draw(self, state: GuiState) -> None:
        size = _window_size()
        protocol = launch.choose_graphics_protocol(alternate_screen=True)
        png = launch._logo_png() if protocol else None
        key = (size.cols, size.rows, size.xpixels, size.ypixels, protocol)
        size_changed = key != self._key
        if size_changed:
            self._logo, self._logo_cols, self._logo_rows = _logo_placement(size, protocol, png)
            self._key = key
        cols, rows = _screen_bounds(size)
        logo_rows = 0 if state.drawer else self._logo_rows
        available_rows = max(1, rows - (logo_rows + 1 if logo_rows else 0))
        if state.drawer:
            origin: int | None = None
        else:
            block_len = len(_frame_block(state, cols, image=logo_rows > 0, rows=available_rows))
            origin = _frame_origin(rows, block_len, logo_rows)

        preview_target = ""
        if state.drawer == "history" and state.captures:
            preview_target = state.captures[state.drawer_index % len(state.captures)].path
        drawer_sig = (
            state.drawer,
            bool(state.error),
            bool(state.notice),
            state.editing_field,
            len(state.presets),
            len(state.captures),
        )
        drawer_changed = state.drawer != self._last_drawer or drawer_sig != self._last_drawer_sig
        preview_changed = preview_target != self._last_preview
        full = size_changed or origin != self._origin or drawer_changed or preview_changed
        self._origin = origin
        self._last_drawer = state.drawer
        self._last_drawer_sig = drawer_sig
        self._last_preview = preview_target
        frame = render_frame(
            size,
            state,
            protocol=protocol,
            clear=full,
            clear_images=full and self._has_image,
            logo=self._logo if (full and not state.drawer) else b"",
            logo_cols=self._logo_cols,
            logo_rows=logo_rows,
            origin=origin,
        )
        if full:
            self._has_image = bool(
                protocol and (
                    (not state.drawer and logo_rows > 0 and self._logo)
                    or (state.drawer == "history" and preview_target)
                )
            )
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()


_STD_OUTPUT_HANDLE = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def _enable_windows_vt() -> int | None:
    """Turn on ANSI processing for the classic Windows console."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    enabled = mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
    if not kernel32.SetConsoleMode(handle, enabled):
        return None
    return mode.value


def _restore_windows_vt(previous: int | None) -> None:
    if previous is None:
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(_STD_OUTPUT_HANDLE), previous)


def _enter() -> None:
    sys.stdout.buffer.write(launch.ALT_ENTER + BRACKETED_PASTE_ON)
    sys.stdout.buffer.flush()


def _leave(protocol: str | None) -> None:
    clear = b"\x1b_Ga=d,d=A,q=2\x1b\\" if protocol == "kitty" else b""
    sys.stdout.buffer.write(BRACKETED_PASTE_OFF + clear + launch.ALT_LEAVE)
    sys.stdout.buffer.flush()


def _start_server() -> tuple[subprocess.Popen[object], object, str]:
    log_path = Path.home() / ".vipercapture" / "requests.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(log_fd, 0o600)
    log_handle = open(log_fd, "w", encoding="utf-8", buffering=1)
    server_env, shutdown_token = launch._new_server_environment()
    kwargs: dict[str, object] = {
        "cwd": str(launch.ROOT),
        "env": server_env,
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    try:
        server = subprocess.Popen(server_command(), **kwargs)
        launch._record_server_process(server.pid, shutdown_token, launch.PORT)
    except Exception:
        log_handle.close()
        raise
    return server, log_handle, shutdown_token


def _wait_until_ready(
    state: GuiState,
    deadline: float,
    server: subprocess.Popen[object],
    drawer: _Drawer,
    *,
    shutdown_token: str = "",
) -> GuiState:
    tick = 0
    while time.monotonic() < deadline:
        if launch.application_shutdown_requested():
            raise KeyboardInterrupt
        if launch.port_open():
            if shutdown_token:
                launch._record_endpoint_server_process(server.pid, shutdown_token, launch.PORT)
            return state
        if server.poll() is not None:
            break
        tick += 1
        state = _replace(state, busy=True, progress=tick, saved="", error="")
        drawer.draw(state)
        time.sleep(0.05)
    return _replace(
        state,
        busy=False,
        error="The local API did not start. Look in ~/.vipercapture/requests.log.",
    )


def _run_keys(
    state: GuiState,
    directory: Path,
    drawer: _Drawer,
    *,
    started_server: bool,
    base_url: str = "",
    config_loaded: bool = False,
) -> int:
    resolved_base = base_url or server_base_url(None)
    if not config_loaded:
        config = fetch_app_config(resolved_base, timeout=0.6)
        if config is not None:
            state = apply_app_config(state, config)
    if os.name == "nt":
        return _run_windows_keys(
            state, directory, drawer, started_server=started_server, base_url=resolved_base
        )
    return _run_posix_keys(
        state, directory, drawer, started_server=started_server, base_url=resolved_base
    )


def _start_capture(
    state: GuiState,
    directory: Path,
    cancel: CancelFlag,
    box: dict[str, GuiState],
    *,
    started_server: bool,
    base_url: str = "",
) -> threading.Thread:
    target_dir = Path(state.output_dir).expanduser() if state.output_dir.strip() else directory
    endpoint = f"{(base_url or server_base_url(None)).rstrip('/')}/v1/render"

    def work() -> None:
        try:
            result = perform_capture(
                state,
                directory=target_dir,
                endpoint=endpoint,
                cancel=cancel,
                started_server=started_server,
            )
            if result.saved and result.url_history:
                save_url_history(result.url_history)
            box["state"] = result
        except Exception as exc:
            box["state"] = _replace(state, busy=False, cancelling=False, error=str(exc))

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def _handle_key(
    state: GuiState,
    key: str,
    thread: threading.Thread | None,
    cancel: CancelFlag,
    box: dict[str, GuiState],
    directory: Path,
    *,
    started_server: bool,
    base_url: str = "",
) -> tuple[GuiState, threading.Thread | None, CancelFlag, dict[str, GuiState], bool]:
    resolved_base = base_url or server_base_url(None)
    if key in {"esc", "quit"} and state.busy:
        cancel.abort()
    state, action = apply_key(state, key)
    if action == "quit":
        return state, thread, cancel, box, True
    if action == "capture" and thread is None:
        cancel = CancelFlag()
        box = {}
        thread = _start_capture(
            state,
            directory,
            cancel,
            box,
            started_server=started_server,
            base_url=resolved_base,
        )
    elif action == "paste":
        pasted = read_clipboard()
        if pasted:
            state, _ = apply_key(state, "paste:" + pasted)
        else:
            state = _replace(state, notice="Clipboard is empty or unavailable.")
    elif action == "open_last":
        target = _last_saved_path(state)
        if target and open_in_viewer(target):
            state = _replace(state, notice=f"Opened {Path(target).name}.", error="")
        else:
            state = _replace(state, error="Could not open the saved file.")
    elif action == "copy_last":
        target = _last_saved_path(state)
        if target and copy_to_clipboard(target):
            state = _replace(state, notice="Copied path to clipboard.", error="")
        else:
            state = _replace(state, error="Could not access the clipboard.")
    elif action == "open_selected" and state.captures:
        rec = state.captures[state.drawer_index % len(state.captures)]
        if open_in_viewer(rec.path):
            state = _replace(state, notice=f"Opened {Path(rec.path).name}.", error="")
        else:
            state = _replace(state, error="Could not open the selected file.")
    elif action == "copy_selected" and state.captures:
        rec = state.captures[state.drawer_index % len(state.captures)]
        if copy_to_clipboard(rec.path):
            state = _replace(state, notice="Copied path to clipboard.", error="")
        else:
            state = _replace(state, error="Could not access the clipboard.")
    elif action == "load_presets":
        if state.presets_supported:
            presets, err = fetch_presets(resolved_base)
            if err:
                state = _replace(state, error=err)
            else:
                state = _replace(state, presets=presets, drawer_index=0, error="")
    elif action == "save_preset":
        name = state.edit_buffer.strip()
        presets, err = save_preset_remote(resolved_base, name, state_to_preset_settings(state))
        if err:
            state = _replace(state, error=err)
        else:
            state = _replace(
                state,
                presets=presets,
                editing_field="",
                edit_buffer="",
                edit_cursor=0,
                drawer_index=0,
                error="",
                notice=f"Saved preset '{name}'.",
            )
    elif action == "delete_preset" and state.presets:
        target = state.presets[state.drawer_index % len(state.presets)].name
        presets, err = delete_preset_remote(resolved_base, target)
        if err:
            state = _replace(state, error=err)
        else:
            new_idx = min(state.drawer_index, max(0, len(presets) - 1))
            state = _replace(
                state,
                presets=presets,
                drawer_index=new_idx,
                error="",
                notice=f"Deleted preset '{target}'.",
            )
    elif action == "toggle_gpu":
        next_mode = "off" if state.gpu_mode == "auto" else "auto"
        gpu, err = set_gpu_mode(resolved_base, next_mode)
        if err:
            state = _replace(state, error=err)
        elif gpu is not None:
            hw = bool(gpu.get("hardware_active", False))
            mode = str(gpu.get("mode", next_mode))
            label = "GPU active" if hw else ("GPU requested (software fallback)" if mode == "auto" else "CPU mode")
            state = _replace(
                state,
                gpu_mode=mode,
                gpu_hardware=hw,
                gpu_known=True,
                error="",
                notice=f"GPU mode: {label}.",
            )
    return state, thread, cancel, box, False


def _run_posix_keys(
    state: GuiState,
    directory: Path,
    drawer: _Drawer,
    *,
    started_server: bool,
    base_url: str = "",
) -> int:
    import select
    import signal
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    wake_r = wake_w = -1
    thread: threading.Thread | None = None
    cancel = CancelFlag()
    box: dict[str, GuiState] = {}
    try:
        tty.setcbreak(fd)
        # Disable IEXTEN/IXON so Ctrl+V, Ctrl+O, Ctrl+W reach the app directly.
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~getattr(termios, "IXON", 0)
        attrs[3] &= ~getattr(termios, "IEXTEN", 0)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        pending = bytearray()
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        signal.signal(signal.SIGWINCH, lambda _signum, _frame: None)
        dirty = True
        last_size = _window_size()
        while True:
            if launch.application_shutdown_requested():
                return 0
            current_size = _window_size()
            if current_size != last_size:
                last_size = current_size
                dirty = True
            if dirty:
                drawer.draw(state)
                dirty = False
            ready, _, _ = select.select(
                [fd, wake_r],
                [],
                [],
                _input_timeout(pending, capturing=thread is not None),
            )
            if wake_r in ready:
                try:
                    os.read(wake_r, 64)
                except OSError:
                    pass
                dirty = True
            keys: list[str] = []
            if fd in ready:
                data = os.read(fd, 256)
                if not data:
                    return 0
                keys = keys_from_bytes(pending, data)
            elif not ready:
                keys = flush_bare_escape(pending)
            for key in keys:
                state, thread, cancel, box, quit_menu = _handle_key(
                    state,
                    key,
                    thread,
                    cancel,
                    box,
                    directory,
                    started_server=started_server,
                    base_url=base_url,
                )
                if quit_menu:
                    return 0
            if keys:
                dirty = True
            if thread is not None and not thread.is_alive():
                state = box.get("state", state)
                thread = None
                dirty = True
            elif thread is not None and not ready:
                state = _replace(state, progress=state.progress + 1)
                dirty = True
    finally:
        if wake_w >= 0:
            signal.set_wakeup_fd(-1)
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            os.close(wake_r)
            os.close(wake_w)
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
    return 0


def _windows_shift_pressed() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
    except Exception:
        return False


def _is_paste_prefix(buf: bytearray) -> bool:
    if not buf:
        return False
    if buf.startswith(_PASTE_START):
        return True
    return len(buf) < len(_PASTE_START) and _PASTE_START.startswith(bytes(buf))


def _drain_windows_escape(
    msvcrt_mod: object,
    pending: bytearray | None = None,
    *,
    paste_grace: float = 0.25,
) -> list[str]:
    """Drain a VT escape sequence or bracketed paste on Windows Terminal.

    When ``pending`` is supplied by the caller, an in-progress bracketed paste
    (including a split opening ``\\x1b[200~`` marker) stays in ``pending`` across
    timeouts instead of being dropped or flushed. A bare Escape receives the
    same bounded grace period before flushing as ``esc`` so a delayed paste
    prefix never exits the GUI.
    """
    buf = pending if pending is not None else bytearray()
    if not buf:
        buf.extend(b"\x1b")
    deadline = time.monotonic() + (paste_grace if _is_paste_prefix(buf) else 0.025)
    while True:
        if msvcrt_mod.kbhit():  # type: ignore[attr-defined]
            ch = msvcrt_mod.getwch()  # type: ignore[attr-defined]
            buf.extend(ch.encode("utf-8", errors="replace"))
            keys = keys_from_bytes(buf, b"")
            if keys:
                return keys
            if not buf:
                return []
            if _is_paste_prefix(buf):
                deadline = time.monotonic() + paste_grace
            else:
                deadline = time.monotonic() + 0.02
            continue
        if time.monotonic() >= deadline:
            break
        time.sleep(0.002)
    if _is_paste_prefix(buf) and len(buf) > 1:
        # Keep paste mode active across delayed chunks (even if \x1b[200~ was split).
        return []
    return flush_bare_escape(buf)


def _run_windows_keys(
    state: GuiState,
    directory: Path,
    drawer: _Drawer,
    *,
    started_server: bool,
    base_url: str = "",
) -> int:
    import msvcrt

    thread: threading.Thread | None = None
    cancel = CancelFlag()
    box: dict[str, GuiState] = {}
    pending = bytearray()
    dirty = True
    last_size = _window_size()
    while True:
        if launch.application_shutdown_requested():
            return 0
        current_size = _window_size()
        if current_size != last_size:
            last_size = current_size
            dirty = True
        if dirty:
            drawer.draw(state)
            dirty = False
        if msvcrt.kbhit():
            if pending:
                names = _drain_windows_escape(msvcrt, pending)
            else:
                char = msvcrt.getwch()
                names = []
                if char in {"\x00", "\xe0"}:
                    ext = msvcrt.getwch()
                    mapped = _windows_extended_key(ext)
                    if mapped is not None:
                        names.append(mapped)
                elif char == "\x1b":
                    names.extend(_drain_windows_escape(msvcrt, pending))
                elif char == "\t" and _windows_shift_pressed():
                    names.append("shift-tab")
                else:
                    mapped = _windows_key(char)
                    if mapped is not None:
                        names.append(mapped)
            for name in names:
                state, thread, cancel, box, quit_menu = _handle_key(
                    state,
                    name,
                    thread,
                    cancel,
                    box,
                    directory,
                    started_server=started_server,
                    base_url=base_url,
                )
                if quit_menu:
                    return 0
                dirty = True
        elif thread is not None:
            time.sleep(0.08)
            if thread.is_alive():
                state = _replace(state, progress=state.progress + 1)
            else:
                state = box.get("state", state)
                thread = None
            dirty = True
        else:
            time.sleep(0.05)
    return 0


_WINDOWS_EXTENDED_MAP: dict[str, str] = {
    "H": "up",        # 0x48 Up arrow
    "P": "down",      # 0x50 Down arrow
    "K": "left",      # 0x4B Left arrow
    "M": "right",     # 0x4D Right arrow
    "G": "home",      # 0x47 Home
    "O": "end",       # 0x4F End
    "S": "delete",    # 0x53 Delete
    "s": "ctrl-left",   # 0x73 Ctrl+Left
    "t": "ctrl-right",  # 0x74 Ctrl+Right
    ";": "f1",        # 0x3B F1
    "\x0f": "shift-tab",  # 0x0F Shift+Tab
}


def _windows_extended_key(code: str) -> str | None:
    """Decode the second character after a Windows ``\\x00`` or ``\\xe0`` prefix."""
    return _WINDOWS_EXTENDED_MAP.get(code)


def _windows_key(char: str) -> str | None:
    if not char:
        return None
    if char == "\x1b":
        return "esc"
    code = ord(char)
    if code < 0x20 or code == 0x7F:
        return _CONTROL_KEY_MAP.get(code)
    if len(char) == 1 and char.isprintable():
        return char
    return None


def _apply_startup_preset(state: GuiState, base_url: str, preset_name: str) -> GuiState:
    if not state.presets_supported:
        return _replace(state, error="Presets are only available for a local server.")
    presets, err = fetch_presets(base_url)
    if err:
        return _replace(state, error=f"Preset '{preset_name}': {err}")
    match = next((p for p in presets if p.name == preset_name), None)
    if match is None:
        lowered = preset_name.lower()
        match = next((p for p in presets if p.name.lower() == lowered), None)
    if match is None:
        return _replace(state, presets=presets, error=f"Preset '{preset_name}' was not found.")
    applied, ignored = apply_preset_settings_with_warnings(state, match.settings)
    notice = f"Loaded preset '{match.name}'."
    if ignored:
        notice = f"Loaded '{match.name}' (ignored: {', '.join(ignored)})."
    return _replace(applied, presets=presets, notice=notice)


def run_capture_gui(
    viewport: tuple[int, int],
    directory: Path | None = None,
    *,
    server: str | None = None,
    preset: str | None = None,
    initial_url: str = "",
    custom_viewport: bool = False,
) -> int:
    """Take over this terminal until the user presses Esc."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("vipercapture --gui needs a terminal.", file=sys.stderr)
        return 1
    if directory is None:
        directory = Path.cwd()
    base_url = server_base_url(server)
    local = is_local_server(base_url)
    launch._clear_application_shutdown_request()
    if server is None:
        # ensure_deps compares the requirements hash itself. A stamp left behind
        # by an update is not enough.
        launch.ensure_deps()
        launch.ensure_playwright()
    url_history = load_url_history()
    clean_initial_url = sanitize_terminal_text(initial_url)
    state = GuiState(
        url=clean_initial_url,
        cursor=len(clean_initial_url),
        viewport=viewport,
        custom_viewport=viewport if custom_viewport else None,
        output_dir=str(directory),
        server_label=format_server_label(base_url),
        local_server=local,
        presets_supported=local,
        url_history=url_history,
    )
    started: subprocess.Popen[object] | None = None
    worker_pid: int | None = None
    log_handle: object | None = None
    protocol = launch.choose_graphics_protocol(alternate_screen=True)
    entered = False
    drawer = _Drawer()
    vt_mode = _enable_windows_vt()
    try:
        _enter()
        entered = True
        if server is None and not launch.port_open():
            started_info = _start_server()
            if len(started_info) == 3:
                started, log_handle, shutdown_token = started_info
            else:
                started, log_handle = started_info  # type: ignore[misc]
                shutdown_token = ""
            state = _wait_until_ready(
                state,
                time.monotonic() + 30,
                started,
                drawer,
                shutdown_token=shutdown_token,
            )
            if state.error:
                drawer.draw(state)
                _pause_for_quit()
                return 1
            if shutdown_token:
                worker_pid = launch._record_endpoint_server_process(
                    started.pid, shutdown_token, launch.PORT
                )
            state = _replace(state, busy=False, progress=0)
        config = fetch_app_config(base_url, timeout=0.6)
        if config is None:
            config = fetch_app_config(base_url, timeout=0.6)
        if config is not None:
            state = apply_app_config(state, config)
        if preset:
            state = _apply_startup_preset(state, base_url, preset)
        return _run_keys(
            state,
            directory,
            drawer,
            started_server=started is not None,
            base_url=base_url,
            config_loaded=True,
        )
    except KeyboardInterrupt:
        return 0
    finally:
        if entered:
            _leave(protocol)
        _restore_windows_vt(vt_mode)
        if started is not None:
            launch.stop_server(started, announce=False, worker_pid=worker_pid)
        if log_handle is not None:
            log_handle.close()  # type: ignore[union-attr]


def _pause_for_quit() -> None:
    """Block on Esc after a startup error so the message stays readable."""
    if os.name == "nt":
        import msvcrt

        while True:
            if launch.application_shutdown_requested():
                return
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            char = msvcrt.getwch()
            if char in {"\x1b", "\x03", "\r"}:
                return
        return
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    pending = bytearray()
    try:
        while True:
            if launch.application_shutdown_requested():
                return
            ready, _, _ = select.select(
                [fd],
                [],
                [],
                _input_timeout(pending, capturing=False),
            )
            if not ready:
                keys = flush_bare_escape(pending)
            else:
                keys = keys_from_bytes(pending, os.read(fd, 64))
            if any(key in {"esc", "quit", "enter"} for key in keys):
                return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
