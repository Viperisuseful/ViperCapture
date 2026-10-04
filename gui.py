"""Full-screen capture menu for ``vipercapture --gui``.

The menu stays in this terminal. It does not open a log window, a browser,
or a second tab. Image-capable terminals get the logo; every other terminal
gets an ASCII wordmark.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import launch

FORMATS = ("png", "gif", "mp4")
DEFAULT_VIEWPORT = (1920, 1080)
MAX_CAPTURE_BYTES = 200 * 1024 * 1024
VIDEO_DURATION_MS = 4_000
ASCII_LOGO = (
    "█ █ █ █▀█ █▀▀ █▀█   █▀▀ █▀█ █▀█ ▀█▀ █ █ █▀█ █▀▀",
    "▀▄▀ █ █▀▀ █▀▀ █▀▄   █   █▀█ █▀▀  █  █ █ █▀▄ █▀▀",
    " ▀  ▀ ▀   ▀▀▀ ▀ ▀   ▀▀▀ ▀ ▀ ▀   ▀▀▀ ▀▀▀ ▀ ▀ ▀▀▀",
    "ViperCapture",
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_HOST = re.compile(r"[^A-Za-z0-9.-]+")


@dataclass(frozen=True)
class GuiState:
    url: str = ""
    output: str = "png"
    full_page: bool = False
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    error: str = ""
    saved: str = ""
    busy: bool = False
    progress: int = 0
    cancelling: bool = False


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


def capture_payload(state: GuiState, url: str) -> dict[str, object]:
    width, height = state.viewport
    payload: dict[str, object] = {
        "url": url,
        "output": state.output,
        "full_page": state.full_page,
        "viewport": {"width": width, "height": height},
    }
    if state.output in {"gif", "mp4"}:
        # A full-page clip is encoded by scrolling the page from top to
        # bottom. scroll stays off so a viewport clip records the screen
        # without moving.
        payload["video"] = {"duration_ms": VIDEO_DURATION_MS, "scroll": False}
    return payload


def cycle_output(output: str) -> str:
    try:
        index = FORMATS.index(output)
    except ValueError:
        return FORMATS[0]
    return FORMATS[(index + 1) % len(FORMATS)]


def apply_key(state: GuiState, key: str) -> tuple[GuiState, str]:
    """Apply one key. The action is edit, capture, or quit."""
    if state.busy:
        if key in {"esc", "quit"}:
            return _replace(state, cancelling=True), "edit"
        return state, "edit"
    if key in {"esc", "quit"}:
        return state, "quit"
    if key == "tab":
        return _replace(state, output=cycle_output(state.output), error=""), "edit"
    if key == "ctrl-p":
        return _replace(state, full_page=not state.full_page, error=""), "edit"
    if key == "enter":
        try:
            normalize_url(state.url)
        except ValueError as exc:
            return _replace(state, error=str(exc), saved=""), "edit"
        return _replace(state, error="", saved="", busy=True, progress=0, cancelling=False), "capture"
    if key == "backspace":
        return _replace(state, url=state.url[:-1], error="", saved=""), "edit"
    if key == "ctrl-u":
        return _replace(state, url="", error="", saved=""), "edit"
    if len(key) == 1 and key.isprintable() and len(state.url) < 2048:
        return _replace(state, url=state.url + key, error="", saved=""), "edit"
    return state, "edit"


def keys_from_bytes(pending: bytearray, data: bytes) -> list[str]:
    """Turn terminal bytes into key names. Arrow sequences are ignored."""
    pending.extend(data)
    keys: list[str] = []
    while pending:
        byte = pending[0]
        if byte == 0x1B:
            if len(pending) == 1:
                break
            if pending[1] != 0x5B:
                keys.append("esc")
                del pending[0]
                continue
            end = 2
            while end < len(pending) and not 0x40 <= pending[end] <= 0x7E:
                end += 1
                if end > 16:
                    del pending[:end]
                    end = -1
                    break
            if end < 0:
                continue
            if end >= len(pending):
                break
            del pending[: end + 1]
            continue
        del pending[0]
        if byte == 0x09:
            keys.append("tab")
        elif byte in {0x0D, 0x0A}:
            keys.append("enter")
        elif byte in {0x7F, 0x08}:
            keys.append("backspace")
        elif byte == 0x10:
            keys.append("ctrl-p")
        elif byte == 0x15:
            keys.append("ctrl-u")
        elif byte == 0x03:
            keys.append("quit")
        elif 0x20 <= byte <= 0x7E:
            keys.append(chr(byte))
    return keys


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
    message = " ".join(message.split())
    if not message:
        return f"The capture failed ({status})."
    if len(message) > 280:
        message = message[:279].rstrip() + "…"
    return message


def looks_like_output(fmt: str, body: bytes) -> bool:
    if fmt == "png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if fmt == "gif":
        return body.startswith(b"GIF8")
    if fmt == "mp4":
        return len(body) >= 12 and body[4:8] == b"ftyp"
    return False


def output_path(directory: Path, url: str, fmt: str, moment: datetime) -> Path:
    parsed = urllib.parse.urlparse(url)
    host = _HOST.sub("-", parsed.hostname or "page").strip("-") or "page"
    stamp = moment.astimezone().strftime("%Y%m%d-%H%M%S")
    path = directory / f"vipercapture-{host}-{stamp}.{fmt}"
    suffix = 2
    while path.exists():
        path = directory / f"vipercapture-{host}-{stamp}-{suffix}.{fmt}"
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


def _replace(state: GuiState, **changes: object) -> GuiState:
    data = {
        "url": state.url,
        "output": state.output,
        "full_page": state.full_page,
        "viewport": state.viewport,
        "error": state.error,
        "saved": state.saved,
        "busy": state.busy,
        "progress": state.progress,
        "cancelling": state.cancelling,
    }
    data.update(changes)
    return GuiState(**data)  # type: ignore[arg-type]


def _clip(text: str, width: int) -> str:
    if width < 1:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return "…" + text[-(width - 1) :]


def _wrap(text: str, width: int) -> list[str]:
    if width < 8:
        return [text[:width]]
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        while len(word) > width:
            piece = word[:width]
            word = word[width:]
            if current:
                lines.append(current)
                current = ""
            lines.append(piece)
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def panel_lines(state: GuiState, width: int) -> list[str]:
    """The input panel, without ANSI, so tests can read the words."""
    inner = max(12, width - 4)
    if state.url:
        link = state.url
    else:
        link = "example.com"
    mode = page_label(state.full_page)
    width_label = f"{state.viewport[0]}x{state.viewport[1]}"
    choice = "   ".join(FORMATS)
    summary = f"{choice}    {mode}    {width_label}"
    lines = [_clip(link, inner), _clip(summary, inner)]
    if state.busy:
        if state.cancelling:
            label = "Cancelling"
        elif state.url:
            label = "Capturing"
        else:
            label = "Starting"
        lines.append(_clip(f"{label}  {progress_bar(state.progress, min(20, inner - 14))}", inner))
    elif state.error:
        lines.extend(_wrap(state.error, inner)[:3])
    elif state.saved:
        lines.extend(_wrap(f"Saved {state.saved}", inner)[:2])
    return lines


def footer(state: GuiState) -> str:
    if state.busy and state.cancelling:
        return "waiting for the capture to stop    ctrl+c quit"
    if state.busy:
        return "esc cancel"
    if state.saved:
        return "enter capture again    esc quit"
    return "tab format    ctrl+p page    enter capture    esc quit"


def _screen_bounds(size: launch.WindowSize) -> tuple[int, int]:
    return max(20, size.cols), max(8, size.rows)


def _logo_placement(
    size: launch.WindowSize,
    protocol: str | None,
    png: bytes | None,
) -> tuple[bytes, int]:
    _cols, rows = _screen_bounds(size)
    if not protocol or not png:
        return b"", 0
    logo_rows = min(6, max(1, rows // 5))
    logo = launch.place_logo(png, protocol, cols=24, rows=logo_rows, size=size)
    if not logo:
        return b"", 0
    return logo, logo_rows


def _wordmark(cols: int, *, image: bool) -> tuple[str, ...]:
    if image:
        return ()
    if cols < 52:
        return ("ViperCapture",)
    return ASCII_LOGO


def _frame_block(state: GuiState, cols: int, *, image: bool) -> list[str]:
    panel_width = min(72, cols - 4)
    plain = panel_lines(state, panel_width)
    colored = _color_panel(state, panel_width, plain)
    return [*_wordmark(cols, image=image), "", *colored, "", f"\x1b[2m{footer(state)}\x1b[0m"]


def _frame_origin(rows: int, block_len: int, logo_rows: int) -> int:
    return max(1, (rows - block_len - logo_rows) // 2)


def render_frame(
    size: launch.WindowSize,
    state: GuiState,
    *,
    protocol: str | None = None,
    png: bytes | None = None,
    clear: bool = True,
    logo: bytes | None = None,
    logo_rows: int | None = None,
    origin: int | None = None,
) -> bytes:
    cols, rows = _screen_bounds(size)
    if logo is None:
        logo, auto_rows = _logo_placement(size, protocol, png)
        if logo_rows is None:
            logo_rows = auto_rows
    if logo_rows is None:
        logo_rows = 0
    logo_bytes = logo or b""
    block = _frame_block(state, cols, image=logo_rows > 0)
    if origin is None:
        origin = _frame_origin(rows, len(block), logo_rows)
    parts: list[bytes] = []
    if clear:
        parts.append(b"\x1b[2J\x1b[H")
    row = max(1, origin)
    if logo_rows:
        if clear and logo_bytes:
            col = max(1, (cols - 24) // 2)
            parts.append(f"\x1b[{row};{col}H".encode("ascii"))
            parts.append(logo_bytes)
        row += logo_rows + 1
    if not clear:
        for erase_row in range(min(row, rows), rows + 1):
            parts.append(f"\x1b[{erase_row};1H\x1b[2K".encode("ascii"))
    for line in block:
        if row > rows:
            break
        shown = visible(line)
        col = max(1, (cols - len(shown)) // 2 + 1)
        parts.append(f"\x1b[{row};{col}H".encode("ascii"))
        parts.append(line.encode("utf-8"))
        row += 1
    return b"".join(parts)


def _color_panel(state: GuiState, width: int, plain: list[str]) -> list[str]:
    inner = max(12, width - 4)
    bar = "\x1b[34m│\x1b[0m "
    rule = "\x1b[34m┌" + "─" * (inner + 2) + "┐\x1b[0m"
    bottom = "\x1b[34m└" + "─" * (inner + 2) + "┘\x1b[0m"
    lines = [rule]
    for index, text in enumerate(plain):
        if index == 1:
            body = _colored_summary(state, inner)
        elif index == 0 and not state.busy:
            shown = text[: inner - 1]
            pad = " " * (inner - 1 - len(shown))
            caret = "\x1b[7m \x1b[0m"
            if state.url:
                body = shown + caret + pad
            else:
                body = caret + f"\x1b[2m{shown}\x1b[0m" + pad
        elif state.error and index >= 2 and not state.busy:
            body = f"\x1b[31m{text.ljust(inner)}\x1b[0m"
        else:
            body = text.ljust(inner)
        lines.append(bar + body + " \x1b[34m│\x1b[0m")
    lines.append(bottom)
    return lines


def _colored_summary(state: GuiState, inner: int) -> str:
    pieces: list[str] = []
    visible_len = 0
    for index, name in enumerate(FORMATS):
        if index:
            pieces.append("   ")
            visible_len += 3
        if name == state.output:
            pieces.append(f"\x1b[1;34m{name}\x1b[0m")
        else:
            pieces.append(f"\x1b[2m{name}\x1b[0m")
        visible_len += len(name)
    tail = f"    {page_label(state.full_page)}    {state.viewport[0]}x{state.viewport[1]}"
    pieces.append(tail)
    visible_len += len(tail)
    pad = max(0, inner - visible_len)
    return "".join(pieces) + " " * pad


def read_capture(response: object, limit: int = MAX_CAPTURE_BYTES) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read = getattr(response, "read")
    while True:
        block = read(64 * 1024)
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


def perform_capture(
    state: GuiState,
    *,
    directory: Path,
    endpoint: str = launch.URL + "v1/render",
    timeout: float | None = None,
    moment: datetime | None = None,
    opener: object | None = None,
    cancel: CancelFlag | None = None,
) -> GuiState:
    """POST one render and save the file. Network errors stay on the panel."""
    try:
        url = normalize_url(state.url)
    except ValueError as exc:
        return _replace(state, busy=False, error=str(exc))
    payload = capture_payload(state, url)
    if timeout is None:
        timeout = 180 if state.output in {"gif", "mp4"} else 90
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "*/*"},
        method="POST",
    )
    open_url = urllib.request.urlopen if opener is None else opener
    try:
        with open_url(request, timeout=timeout) as response:  # type: ignore[operator]
            body = read_capture(response)
    except urllib.error.HTTPError as exc:
        if cancel is not None and cancel.requested:
            return _replace(state, busy=False, cancelling=False, saved="", error="Capture cancelled.")
        detail = error_text(exc.code, exc.read(64 * 1024))
        return _replace(state, busy=False, cancelling=False, error=detail)
    except urllib.error.URLError:
        if cancel is not None and cancel.requested:
            return _replace(state, busy=False, cancelling=False, saved="", error="Capture cancelled.")
        return _replace(
            state,
            busy=False,
            cancelling=False,
            error="The local API did not answer. Stop vipercapture and start it again.",
        )
    except (TimeoutError, OSError, ValueError) as exc:
        if cancel is not None and cancel.requested:
            return _replace(state, busy=False, cancelling=False, saved="", error="Capture cancelled.")
        return _replace(state, busy=False, cancelling=False, error=str(exc))
    if (cancel is not None and cancel.requested) or state.cancelling:
        return _replace(state, busy=False, cancelling=False, saved="", error="Capture cancelled.")
    if not body or not looks_like_output(state.output, body):
        return _replace(
            state,
            busy=False,
            cancelling=False,
            error=f"The API did not return a {state.output} file.",
        )
    when = moment or datetime.now(timezone.utc)
    path = output_path(directory, url, state.output, when)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(body)
    temporary.replace(path)
    return _replace(state, busy=False, cancelling=False, error="", saved=str(path))


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
    ]


def _window_size() -> launch.WindowSize:
    try:
        return launch.read_window_size(sys.stdout.fileno())
    except OSError:
        size = shutil.get_terminal_size((80, 24))
        return launch.WindowSize(size.columns, size.lines)


class _Drawer:
    """Redraw the menu in place. The logo is sent again only when its position changes."""

    def __init__(self) -> None:
        self._key: tuple[int, int, str | None] | None = None
        self._origin: int | None = None
        self._logo = b""
        self._logo_rows = 0

    def draw(self, state: GuiState) -> None:
        size = _window_size()
        protocol = launch.choose_graphics_protocol()
        png = launch._logo_png() if protocol else None
        key = (size.cols, size.rows, protocol)
        size_changed = key != self._key
        if size_changed:
            self._logo, self._logo_rows = _logo_placement(size, protocol, png)
            self._key = key
        cols, rows = _screen_bounds(size)
        block_len = len(_frame_block(state, cols, image=self._logo_rows > 0))
        origin = _frame_origin(rows, block_len, self._logo_rows)
        full = size_changed or origin != self._origin
        self._origin = origin
        frame = render_frame(
            size,
            state,
            clear=full,
            logo=self._logo if full else b"",
            logo_rows=self._logo_rows,
            origin=origin,
        )
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()


def _enter() -> None:
    sys.stdout.buffer.write(launch.ALT_ENTER)
    sys.stdout.buffer.flush()


def _leave(protocol: str | None) -> None:
    clear = b"\x1b_Ga=d,d=A,q=2\x1b\\" if protocol == "kitty" else b""
    sys.stdout.buffer.write(clear + launch.ALT_LEAVE)
    sys.stdout.buffer.flush()


def _start_server() -> tuple[subprocess.Popen[object], object]:
    log_path = Path.home() / ".vipercapture" / "requests.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(log_fd, 0o600)
    log_handle = open(log_fd, "w", encoding="utf-8", buffering=1)
    kwargs: dict[str, object] = {
        "cwd": str(launch.ROOT),
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
    except Exception:
        log_handle.close()
        raise
    return server, log_handle


def _wait_until_ready(
    state: GuiState,
    deadline: float,
    server: subprocess.Popen[object],
    drawer: _Drawer,
) -> GuiState:
    tick = 0
    while time.monotonic() < deadline:
        if launch.port_open():
            return state
        if server.poll() is not None:
            break
        tick += 1
        state = _replace(state, busy=True, progress=tick, saved="", error="")
        drawer.draw(state)
        time.sleep(0.1)
    return _replace(
        state,
        busy=False,
        error="The local API did not start. Look in ~/.vipercapture/requests.log.",
    )


def _run_keys(state: GuiState, directory: Path, drawer: _Drawer) -> int:
    if os.name == "nt":
        return _run_windows_keys(state, directory, drawer)
    return _run_posix_keys(state, directory, drawer)


def _start_capture(state: GuiState, directory: Path, cancel: CancelFlag, box: dict[str, GuiState]) -> threading.Thread:
    def work() -> None:
        try:
            box["state"] = perform_capture(state, directory=directory, cancel=cancel)
        except Exception as exc:
            box["state"] = _replace(state, busy=False, cancelling=False, error=str(exc))

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def _run_posix_keys(state: GuiState, directory: Path, drawer: _Drawer) -> int:
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
        pending = bytearray()
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        signal.signal(signal.SIGWINCH, lambda _signum, _frame: None)
        dirty = True
        while True:
            if dirty:
                drawer.draw(state)
                dirty = False
            ready, _, _ = select.select([fd, wake_r], [], [], 0.08 if thread is not None else None)
            if wake_r in ready:
                try:
                    os.read(wake_r, 64)
                except OSError:
                    pass
                dirty = True
            if fd in ready:
                data = os.read(fd, 64)
                if not data:
                    return 0
                for key in keys_from_bytes(pending, data):
                    if key == "esc" and state.busy:
                        cancel.requested = True
                    state, action = apply_key(state, key)
                    if action == "quit":
                        return 0
                    if action == "capture" and thread is None:
                        cancel = CancelFlag()
                        box = {}
                        thread = _start_capture(state, directory, cancel, box)
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


def _run_windows_keys(state: GuiState, directory: Path, drawer: _Drawer) -> int:
    import msvcrt

    thread: threading.Thread | None = None
    cancel = CancelFlag()
    box: dict[str, GuiState] = {}
    dirty = True
    while True:
        if dirty:
            drawer.draw(state)
            dirty = False
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in {"\x00", "\xe0"}:
                msvcrt.getwch()
            else:
                name = _windows_key(char)
                if name == "esc" and state.busy:
                    cancel.requested = True
                if name is not None:
                    state, action = apply_key(state, name)
                    if action == "quit":
                        return 0
                    if action == "capture" and thread is None:
                        cancel = CancelFlag()
                        box = {}
                        thread = _start_capture(state, directory, cancel, box)
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
            time.sleep(0.08)
    return 0


def _windows_key(char: str) -> str | None:
    if char == "\t":
        return "tab"
    if char in {"\r", "\n"}:
        return "enter"
    if char in {"\x7f", "\x08"}:
        return "backspace"
    if char == "\x10":
        return "ctrl-p"
    if char == "\x15":
        return "ctrl-u"
    if char == "\x1b":
        return "esc"
    if char == "\x03":
        return "quit"
    if len(char) == 1 and char.isprintable():
        return char
    return None


def run_capture_gui(viewport: tuple[int, int], directory: Path | None = None) -> int:
    """Take over this terminal until the user presses Esc."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("vipercapture --gui needs a terminal.", file=sys.stderr)
        return 1
    if directory is None:
        directory = Path.cwd()
    if not launch.DEPS_STAMP.exists() or not launch.PLAYWRIGHT_STAMP.exists():
        launch.ensure_deps()
        launch.ensure_playwright()
    state = GuiState(viewport=viewport)
    started: subprocess.Popen[object] | None = None
    log_handle: object | None = None
    protocol = launch.choose_graphics_protocol()
    entered = False
    drawer = _Drawer()
    try:
        _enter()
        entered = True
        if not launch.port_open():
            started, log_handle = _start_server()
            state = _wait_until_ready(state, time.monotonic() + 30, started, drawer)
            if state.error:
                drawer.draw(state)
                _pause_for_quit()
                return 1
            state = _replace(state, busy=False, progress=0)
        return _run_keys(state, directory, drawer)
    except KeyboardInterrupt:
        return 0
    finally:
        if entered:
            _leave(protocol)
        if started is not None:
            launch.stop_server(started, announce=False)
        if log_handle is not None:
            log_handle.close()  # type: ignore[union-attr]


def _pause_for_quit() -> None:
    """Block on Esc after a startup error so the message stays readable."""
    if os.name == "nt":
        import msvcrt

        while True:
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
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            for key in keys_from_bytes(pending, os.read(fd, 64)):
                if key in {"esc", "quit", "enter"}:
                    return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
