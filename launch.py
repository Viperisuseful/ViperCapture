#!/usr/bin/env python3
"""
ViperCapture launcher
---------------------
Run this file directly with Python.
Handles venv setup, dependency install, browser install,
server startup, and opening your browser automatically.
``vipercapture update`` replaces an installed copy from GitHub and keeps its
virtualenv. That command uses the Python standard library on every OS.

Prefers uv (https://docs.astral.sh/uv/) when it is on PATH.
Set VIPERCAPTURE_USE_UV=0 to force the stdlib venv + pip path.
Without uv, the launcher falls back to pip.

On subsequent runs, dependency checks are skipped unless
    requirements.txt has changed (hash-stamped in .venv/).
"""

from __future__ import annotations
import base64
import hashlib
from importlib.metadata import version
import os
import platform
import re
import shutil
import sys
import subprocess
import socket
import struct
import time
import webbrowser
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

ROOT             = Path(__file__).parent.resolve()
VENV_PYTHON      = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
HOST             = "127.0.0.1"
PORT             = 8000
URL              = f"http://{HOST}:{PORT}/"
DEPS_STAMP       = ROOT / ".venv" / ".deps_stamp"
PLAYWRIGHT_STAMP = ROOT / ".venv" / ".playwright_stamp"
# PyCA 49.0.0 dropped Intel macOS wheels. Source builds need Rust 1.83+.
# https://cryptography.io/en/latest/installation/#building-cryptography-on-macos
MIN_RUSTC        = (1, 83, 0)
INTEL_MACOS_CRYPTOGRAPHY_DOCS = (
    "https://cryptography.io/en/latest/installation/#building-cryptography-on-macos"
)
INTEL_MACOS_CRYPTOGRAPHY_CHANGELOG = (
    "https://cryptography.io/en/latest/changelog/#v49-0-0"
)


# ── Helpers ───────────────────────────────────────────────────

def port_open() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


def run(*cmd: str | Path, label: str = "") -> None:
    """Run a subprocess and exit hard if it fails."""
    result = subprocess.run([str(c) for c in cmd])
    if result.returncode != 0:
        tag = f" ({label})" if label else ""
        print(f"\n  ERROR{tag}: command exited with code {result.returncode}")
        print(f"  Command: {' '.join(str(c) for c in cmd)}")
        wait_and_exit(1)


def wait_and_exit(code: int = 0) -> None:
    if sys.stdin.isatty():
        input("\n  Press Enter to close...")
    sys.exit(code)


def listening_line(url: str = URL) -> str:
    base = url.rstrip("/")
    return f"  ViperCapture is up on {base} and API is listening on {base}/v1"


STOP_LINE = "  Press Esc twice or Ctrl+C to close this (or kill the terminal)"
LOGO_PNG = ROOT / "static" / "vipercapture-mark.png"
LOGO_ROWS = 6


def ready_banner(url: str = URL) -> str:
    return f"\n{listening_line(url)}\n{STOP_LINE}\n"


def _is_kitty(environ: Mapping[str, str]) -> bool:
    term = environ.get("TERM", "")
    return bool(
        environ.get("KITTY_WINDOW_ID")
        or term == "xterm-kitty"
        or term.startswith("xterm-kitty")
    )


def _is_ghostty(environ: Mapping[str, str]) -> bool:
    term = environ.get("TERM", "")
    program = environ.get("TERM_PROGRAM", "").lower()
    return bool(
        program == "ghostty"
        or term == "xterm-ghostty"
        or term.startswith("xterm-ghostty")
        or environ.get("GHOSTTY_RESOURCES_DIR")
    )


def _is_iterm2(environ: Mapping[str, str]) -> bool:
    program = environ.get("TERM_PROGRAM", "").lower()
    return program in {"iterm.app", "iterm2"} or bool(environ.get("ITERM_SESSION_ID"))


def _is_wezterm(environ: Mapping[str, str]) -> bool:
    program = environ.get("TERM_PROGRAM", "").lower()
    return program == "wezterm" or bool(environ.get("WEZTERM_PANE") or environ.get("WEZTERM_EXECUTABLE"))


def _is_sixel_terminal(environ: Mapping[str, str]) -> bool:
    """Terminals whose normal build displays Sixel, including Windows Terminal."""
    term = environ.get("TERM", "").lower()
    program = environ.get("TERM_PROGRAM", "").lower()
    if term == "foot" or term.startswith("foot-") or program == "foot":
        return True
    if term == "mlterm" or term.startswith("mlterm"):
        return True
    if term == "contour" or term.startswith("contour-"):
        return True
    if "sixel" in term:
        return True
    return bool(environ.get("WT_SESSION") or environ.get("WT_PROFILE_ID"))


def _strip_lua_comments(text: str) -> str:
    text = re.sub(r"--\[\[.*?\]\]", "", text, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", text)


def _lua_assigns_true(text: str, name: str) -> bool:
    matches = re.findall(rf"\b{name}\s*=\s*(true|false)\b", _strip_lua_comments(text))
    return bool(matches) and matches[-1] == "true"


def wezterm_config_candidates(environ: Mapping[str, str], home: Path | None = None) -> list[Path]:
    """Config files in the order WezTerm searches. An explicit path is used alone."""
    explicit = environ.get("WEZTERM_CONFIG_FILE")
    if explicit:
        return [Path(explicit)]
    root = Path.home() if home is None else home
    candidates: list[Path] = []
    xdg = environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "wezterm" / "wezterm.lua")
    candidates.append(root / ".config" / "wezterm" / "wezterm.lua")
    candidates.append(root / ".wezterm.lua")
    return candidates


def wezterm_config_enables_kitty_graphics(
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> bool:
    """True when the WezTerm config the user would actually load sets the flag."""
    env = os.environ if environ is None else environ
    for path in wezterm_config_candidates(env, home):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return _lua_assigns_true(text, "enable_kitty_graphics")
    return False


def choose_graphics_protocol(
    *,
    platform_name: str | None = None,
    is_tty: bool | None = None,
    environ: Mapping[str, str] | None = None,
    kitty_graphics: bool | None = None,
) -> str | None:
    """Pick kitty, iterm2, or sixel. The operating system does not disable images.

    ``platform_name`` does not turn images off. Windows Terminal and WezTerm
    on Windows use the same rules as every other operating system.
    """
    _ = platform_name
    if is_tty is None:
        is_tty = sys.stdout.isatty()
    if environ is None:
        environ = os.environ
    if not is_tty:
        return None
    term = environ.get("TERM", "")
    if environ.get("TMUX") or term.startswith(("screen", "tmux")):
        return None
    if _is_kitty(environ) or _is_ghostty(environ):
        return "kitty"
    if _is_iterm2(environ):
        return "iterm2"
    if _is_wezterm(environ):
        enabled = (
            wezterm_config_enables_kitty_graphics(environ)
            if kitty_graphics is None
            else kitty_graphics
        )
        return "kitty" if enabled else "iterm2"
    if _is_sixel_terminal(environ):
        return "sixel"
    return None


def terminal_graphics_supported(
    *,
    platform_name: str | None = None,
    is_tty: bool | None = None,
    environ: Mapping[str, str] | None = None,
    kitty_graphics: bool | None = None,
) -> bool:
    """True when this terminal can draw the logo, including on Windows."""
    return choose_graphics_protocol(
        platform_name=platform_name,
        is_tty=is_tty,
        environ=environ,
        kitty_graphics=kitty_graphics,
    ) is not None


def kitty_graphics_sequence(png: bytes, *, rows: int = LOGO_ROWS) -> bytes:
    """Place a PNG with the Kitty graphics protocol, which Ghostty also implements."""
    encoded = base64.standard_b64encode(png)
    parts: list[bytes] = []
    for start in range(0, len(encoded), 4096):
        chunk = encoded[start : start + 4096]
        more = 0 if start + 4096 >= len(encoded) else 1
        if start == 0:
            control = f"a=T,f=100,t=d,q=2,r={rows},m={more}"
        else:
            control = f"m={more}"
        parts.append(f"\x1b_G{control};".encode("ascii") + chunk + b"\x1b\\")
    # The placement leaves the cursor on the next row, indented by the image width.
    parts.append(b"\r")
    return b"".join(parts)


def iterm2_image_sequence(
    png: bytes,
    *,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """iTerm2 inline image. WezTerm displays this protocol without extra config."""
    encoded = base64.standard_b64encode(png)
    fields = ["inline=1", "preserveAspectRatio=1", f"size={len(png)}"]
    if width:
        fields.append(f"width={width}")
    if height:
        fields.append(f"height={height}")
    header = ";".join(fields).encode("ascii")
    return b"\x1b]1337;File=" + header + b":" + encoded + b"\a"


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left


def png_rgba(png: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit RGB or RGBA PNG. Other PNG kinds raise ValueError."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a png")
    offset = 8
    width = height = 0
    bit_depth = color_type = interlace = -1
    idat = bytearray()
    while offset + 8 <= len(png):
        length = int.from_bytes(png[offset : offset + 4], "big")
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if compression != 0 or filter_method != 0:
                raise ValueError("unsupported png")
        elif kind == b"IDAT":
            idat.extend(data)
        elif kind == b"IEND":
            break
    if (
        width < 1
        or height < 1
        or not idat
        or bit_depth != 8
        or interlace != 0
        or color_type not in {2, 6}
    ):
        raise ValueError("unsupported png")
    channels = 4 if color_type == 6 else 3
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    expected = height * (stride + 1)
    if len(raw) < expected:
        raise ValueError("truncated png")
    rows: list[bytearray] = []
    pos = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[pos]
        pos += 1
        row = bytearray(raw[pos : pos + stride])
        pos += stride
        if filter_type == 1:
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                row[index] = (row[index] + left) & 255
        elif filter_type == 2:
            for index in range(stride):
                row[index] = (row[index] + previous[index]) & 255
        elif filter_type == 3:
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                row[index] = (row[index] + ((left + previous[index]) // 2)) & 255
        elif filter_type == 4:
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                up = previous[index]
                upper_left = previous[index - channels] if index >= channels else 0
                row[index] = (row[index] + _paeth(left, up, upper_left)) & 255
        elif filter_type != 0:
            raise ValueError("bad png filter")
        previous = row
        rows.append(row)
    if color_type == 6:
        return width, height, b"".join(rows)
    rgba = bytearray()
    for row in rows:
        for index in range(0, len(row), 3):
            rgba.extend(row[index : index + 3])
            rgba.append(255)
    return width, height, bytes(rgba)


def _scale_rgba(pixels: bytes, source_width: int, source_height: int, width: int, height: int) -> bytes:
    if source_width == width and source_height == height:
        return pixels
    scaled = bytearray(width * height * 4)
    for y in range(height):
        source_y = min(source_height - 1, y * source_height // height)
        for x in range(width):
            source_x = min(source_width - 1, x * source_width // width)
            start = (source_y * source_width + source_x) * 4
            dest = (y * width + x) * 4
            scaled[dest : dest + 4] = pixels[start : start + 4]
    return bytes(scaled)


def _sixel_level(value: int, bits: int) -> int:
    bucket = value >> (8 - bits)
    return (bucket * 255 + (1 << (bits - 1)) - 1) // ((1 << bits) - 1)


def encode_sixel(width: int, height: int, rgba: bytes) -> bytes:
    """Encode RGBA pixels as a Sixel image. Transparent pixels stay transparent."""
    if width < 1 or height < 1 or len(rgba) < width * height * 4:
        raise ValueError("bad raster")
    bits = 5
    palette: dict[tuple[int, int, int], int] = {}
    indexes = bytearray(width * height)
    while True:
        palette.clear()
        indexes = bytearray(width * height)
        for pixel in range(width * height):
            offset = pixel * 4
            if rgba[offset + 3] < 128:
                continue
            color = (
                _sixel_level(rgba[offset], bits),
                _sixel_level(rgba[offset + 1], bits),
                _sixel_level(rgba[offset + 2], bits),
            )
            index = palette.get(color)
            if index is None:
                index = len(palette) + 1
                palette[color] = index
            indexes[pixel] = index
        if len(palette) <= 256 or bits == 3:
            break
        bits -= 1
    parts = [f'\x1bPq"1;1;{width};{height}'.encode("ascii")]
    for color, index in palette.items():
        red, green, blue = ((component * 100 + 127) // 255 for component in color)
        parts.append(f"#{index};2;{red};{green};{blue}".encode("ascii"))

    def rle(chars: list[int]) -> bytes:
        encoded: list[str] = []
        cursor = 0
        while cursor < len(chars):
            end = cursor + 1
            while end < len(chars) and chars[end] == chars[cursor]:
                end += 1
            count = end - cursor
            symbol = chr(chars[cursor])
            encoded.append(f"!{count}{symbol}" if count >= 3 else symbol * count)
            cursor = end
        return "".join(encoded).encode("ascii")

    for band in range(0, height, 6):
        for index in range(1, len(palette) + 1):
            chars: list[int] = []
            used = False
            for x in range(width):
                value = 0
                for bit in range(6):
                    y = band + bit
                    if y < height and indexes[y * width + x] == index:
                        value |= 1 << bit
                        used = True
                chars.append(63 + value)
            if used:
                parts.append(f"#{index}".encode("ascii"))
                parts.append(rle(chars))
                parts.append(b"$")
        parts.append(b"-")
    parts.append(b"\x1b\\")
    return b"".join(parts)


def _cell_pixels(size: WindowSize | None) -> tuple[int, int]:
    if size is not None and size.cols > 0 and size.rows > 0 and size.xpixels > 0 and size.ypixels > 0:
        return max(1, size.xpixels // size.cols), max(1, size.ypixels // size.rows)
    return 8, 16


def place_logo(
    png: bytes,
    protocol: str,
    *,
    cols: int | None = None,
    rows: int = LOGO_ROWS,
    size: WindowSize | None = None,
) -> bytes:
    """Draw the logo at the cursor with the protocol this terminal understands."""
    try:
        if protocol == "kitty":
            if cols:
                return kitty_graphics_at(png, cols=cols, rows=rows)
            return kitty_graphics_sequence(png, rows=rows)
        if protocol == "iterm2":
            return iterm2_image_sequence(png, width=cols, height=rows)
        if protocol == "sixel":
            source_width, source_height, pixels = png_rgba(png)
            cell_width, cell_height = _cell_pixels(size)
            if cols:
                target_width = max(1, cols * cell_width)
                target_height = max(1, rows * cell_height)
            else:
                target_height = max(1, rows * cell_height)
                target_width = target_height
            scaled = _scale_rgba(pixels, source_width, source_height, target_width, target_height)
            return encode_sixel(target_width, target_height, scaled)
    except (ValueError, zlib.error, struct.error):
        return b""
    return b""


def show_terminal_logo() -> None:
    protocol = choose_graphics_protocol()
    if protocol is None:
        return
    png = _logo_png()
    if png is None:
        return
    sys.stdout.buffer.write(place_logo(png, protocol, rows=LOGO_ROWS))
    sys.stdout.buffer.flush()


def _logo_png() -> bytes | None:
    try:
        png = LOGO_PNG.read_bytes()
    except OSError:
        return None
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return png


@dataclass(frozen=True)
class WindowSize:
    cols: int
    rows: int
    xpixels: int = 0
    ypixels: int = 0


@dataclass(frozen=True)
class StatusLayout:
    logo_col: int
    logo_row: int
    logo_cols: int
    logo_rows: int
    text_row: int
    lines: tuple[str, ...]


def status_lines(url: str = URL, note: str = "") -> tuple[str, ...]:
    base = url.rstrip("/")
    lines = (
        f"ViperCapture is up on {base}",
        f"API is listening on {base}/v1",
        "",
        "Press Esc twice or Ctrl+C to close this (or kill the terminal)",
    )
    if note:
        return lines + ("", note)
    return lines


def center_line(text: str, width: int) -> str:
    if width <= 0 or len(text) >= width:
        return text[:width] if width > 0 else text
    return " " * ((width - len(text)) // 2) + text


def wrap_center(text: str, width: int) -> list[str]:
    if width <= 0:
        return [text]
    if not text:
        return [""]
    return [center_line(text[index : index + width], width) for index in range(0, len(text), width)]


def logo_cells(size: WindowSize) -> tuple[int, int]:
    """Cell size for a square logo that tracks the window's pixel size."""
    if size.cols < 24 or size.rows < 12:
        return (0, 0)
    if size.xpixels > 0 and size.ypixels > 0:
        cell_w = size.xpixels / size.cols
        cell_h = size.ypixels / size.rows
        target = max(64.0, min(size.xpixels, size.ypixels) * 0.28)
    else:
        cell_w, cell_h = 1.0, 2.0
        target = float(max(8, min(size.cols, size.rows * 2) // 3))
    cols = max(4, round(target / cell_w))
    rows = max(2, round(target / cell_h))
    max_rows = max(2, size.rows - 8)
    max_cols = max(4, size.cols - 4)
    if rows > max_rows:
        cols = max(4, round(cols * (max_rows / rows)))
        rows = max_rows
    if cols > max_cols:
        rows = max(2, round(rows * (max_cols / cols)))
        cols = max_cols
    return cols, rows


def status_layout(
    size: WindowSize,
    url: str = URL,
    *,
    graphics: bool = True,
    note: str = "",
) -> StatusLayout:
    lines = tuple(
        piece for line in status_lines(url, note) for piece in wrap_center(line, max(1, size.cols))
    )
    logo_cols, logo_rows = logo_cells(size) if graphics else (0, 0)
    gap = 1 if logo_rows else 0
    block = logo_rows + gap + len(lines)
    top = max(1, (size.rows - block) // 2 + 1)
    return StatusLayout(
        logo_col=max(1, (size.cols - logo_cols) // 2 + 1),
        logo_row=top,
        logo_cols=logo_cols,
        logo_rows=logo_rows,
        text_row=top + logo_rows + gap,
        lines=lines,
    )


def kitty_graphics_at(png: bytes, *, cols: int, rows: int) -> bytes:
    """Transmit and place a PNG at the cursor, scaled to a cell rectangle."""
    encoded = base64.standard_b64encode(png)
    parts = [b"\x1b_Ga=d,d=A,q=2\x1b\\"]
    for start in range(0, len(encoded), 4096):
        chunk = encoded[start : start + 4096]
        more = 0 if start + 4096 >= len(encoded) else 1
        if start == 0:
            control = f"a=T,f=100,t=d,q=2,i=1,c={cols},r={rows},m={more}"
        else:
            control = f"m={more}"
        parts.append(f"\x1b_G{control};".encode("ascii") + chunk + b"\x1b\\")
    return b"".join(parts)


def render_status_frame(
    size: WindowSize,
    url: str = URL,
    png: bytes | None = None,
    note: str = "",
    protocol: str = "kitty",
) -> bytes:
    """Full-screen frame. The caller is already on the alternate screen."""
    layout = status_layout(size, url, graphics=png is not None, note=note)
    parts = [b"\x1b[2J\x1b[H"]
    if png is not None and layout.logo_rows and protocol:
        parts.append(f"\x1b[{layout.logo_row};{layout.logo_col}H".encode("ascii"))
        parts.append(
            place_logo(
                png,
                protocol,
                cols=layout.logo_cols,
                rows=layout.logo_rows,
                size=size,
            )
        )
    for offset, line in enumerate(layout.lines):
        row = layout.text_row + offset
        if row > size.rows:
            break
        text = line.encode("utf-8")
        if offset == len(layout.lines) - 1:
            text = b"\x1b[2m" + text + b"\x1b[0m"
        parts.append(f"\x1b[{row};1H".encode("ascii") + text)
    return b"".join(parts)


def read_window_size(fd: int) -> WindowSize:
    import fcntl
    import struct
    import termios

    packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    rows, cols, xpixels, ypixels = struct.unpack("HHHH", packed)
    return WindowSize(cols=cols or 80, rows=rows or 24, xpixels=xpixels, ypixels=ypixels)


def terminal_kind(environ: Mapping[str, str] | None = None) -> str | None:
    """Which terminal can open a companion window for request logs."""
    env = os.environ if environ is None else environ
    term = env.get("TERM", "")
    program = env.get("TERM_PROGRAM", "").lower()
    if program == "ghostty" or "ghostty" in term or env.get("GHOSTTY_RESOURCES_DIR") or env.get("GHOSTTY_BIN_DIR"):
        return "ghostty"
    if env.get("KITTY_WINDOW_ID") or term == "xterm-kitty" or term.startswith("xterm-kitty"):
        return "kitty"
    return None


def tail_can_follow_pid() -> bool:
    """GNU tail accepts ``--pid``. BSD tail, including macOS, does not."""
    tail = shutil.which("tail")
    if tail is None:
        return False
    try:
        result = subprocess.run(
            [tail, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "GNU coreutils" in f"{result.stdout}{result.stderr}"


def tail_command(log_path: Path, pid: int, *, follow_pid: bool) -> list[str]:
    """Follow the log and exit when this process does.

    GNU tail can do that with ``--pid``. BSD tail needs a small watcher.
    """
    if follow_pid:
        return ["tail", "-n", "+1", "--pid", str(pid), "-F", str(log_path)]
    script = (
        'tail -n +1 -F "$1" & child=$!; '
        'while kill -0 "$2" 2>/dev/null; do sleep 1; done; '
        'kill "$child" 2>/dev/null; wait "$child" 2>/dev/null'
    )
    return ["sh", "-c", script, "vipercapture-logs", str(log_path), str(pid)]


def request_window_argv(
    kind: str,
    log_path: Path,
    pid: int,
    *,
    follow_pid: bool = True,
    platform_name: str | None = None,
) -> list[str]:
    """Open a request log in the current terminal.

    Linux Ghostty uses ``ghostty +new-window``. macOS Ghostty does not, so
    it is opened with ``open -na Ghostty.app``. Kitty prefers a new tab,
    then an OS window.
    """
    if platform_name is None:
        platform_name = sys.platform
    title = "ViperCapture requests"
    tail = tail_command(log_path, pid, follow_pid=follow_pid)
    if kind == "ghostty" and platform_name == "darwin":
        return ["open", "-na", "Ghostty.app", "--args", f"--title={title}", "-e", *tail]
    if kind == "ghostty":
        return ["ghostty", "+new-window", f"--title={title}", "-e", *tail]
    if kind == "kitty-tab":
        return ["kitten", "@", "launch", "--type=tab", f"--title={title}", *tail]
    if kind == "kitty-window":
        return ["kitty", "--detach", "--title", title, *tail]
    return []


def open_request_window(log_path: Path, pid: int, environ: Mapping[str, str] | None = None) -> bool:
    kind = terminal_kind(environ)
    if kind is None:
        return False
    attempts = ["ghostty"] if kind == "ghostty" else ["kitty-tab", "kitty-window"]
    for attempt in attempts:
        argv = request_window_argv(
            attempt,
            log_path,
            pid,
            follow_pid=tail_can_follow_pid(),
        )
        if not argv or shutil.which(argv[0]) is None:
            continue
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            continue
        try:
            code = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # The terminal client is still running because the window is open.
            return True
        if code == 0:
            return True
    return False


ALT_ENTER = b"\x1b[?1049h\x1b[?25l"
ALT_LEAVE = b"\x1b[?25h\x1b[?1049l"


class StatusScreen:
    """Centered full-screen status. Leaving it restores the terminal scrollback."""

    def __init__(self, url: str = URL) -> None:
        self.url = url
        self.note = ""
        self.protocol = choose_graphics_protocol()
        self.png = _logo_png() if self.protocol else None
        self.active = False

    def enter(self) -> None:
        sys.stdout.buffer.write(ALT_ENTER)
        sys.stdout.buffer.flush()
        self.active = True
        self.draw()

    def leave(self) -> None:
        if not self.active:
            return
        clear_images = b"\x1b_Ga=d,d=A,q=2\x1b\\" if self.protocol == "kitty" else b""
        sys.stdout.buffer.write(clear_images + ALT_LEAVE)
        sys.stdout.buffer.flush()
        self.active = False

    def draw(self) -> None:
        if not self.active:
            return
        try:
            size = read_window_size(sys.stdout.fileno())
        except OSError:
            size = WindowSize(80, 24)
        sys.stdout.buffer.write(
            render_status_frame(size, self.url, self.png, self.note, self.protocol or "kitty")
        )
        sys.stdout.buffer.flush()


class StopKeyBuffer:
    """Count bare Esc presses. Arrow and other CSI sequences do not count."""

    def __init__(self) -> None:
        self.presses = 0
        self.pending_esc = False
        self.in_csi = False

    def feed(self, data: bytes) -> bool:
        for byte in data:
            if self._feed_byte(byte):
                return True
        return False

    def flush(self) -> bool:
        """Count a lone Esc once no further sequence bytes arrived."""
        if self.pending_esc and not self.in_csi:
            self.pending_esc = False
            self.presses += 1
            return self.presses >= 2
        return False

    def _feed_byte(self, byte: int) -> bool:
        if self.in_csi:
            if 0x40 <= byte <= 0x7E:
                self.in_csi = False
            return False
        if self.pending_esc:
            self.pending_esc = False
            if byte == 0x1B:
                self.presses += 1
                self.pending_esc = True
                return self.presses >= 2
            if byte == 0x5B:
                self.in_csi = True
                return False
            self.presses += 1
            return self.presses >= 2
        if byte == 0x1B:
            self.pending_esc = True
        return False


def stop_server(server: subprocess.Popen[object], *, announce: bool = True) -> None:
    if server.poll() is not None:
        return
    if announce:
        print("\n  Stopping server...")
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)
    if announce:
        print("  Server stopped.")


def _wait_posix_keys(
    server: subprocess.Popen[object],
    on_resize: Callable[[], None] | None = None,
) -> None:
    import select
    import signal
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    keys = StopKeyBuffer()
    wake_r, wake_w = (-1, -1)
    watched = [fd]
    if on_resize is not None:
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        signal.signal(signal.SIGWINCH, lambda _signum, _frame: None)
        watched.append(wake_r)
    try:
        while server.poll() is None:
            try:
                readable, _, _ = select.select(watched, [], [], 0.2)
            except InterruptedError:
                if on_resize is not None:
                    on_resize()
                continue
            if wake_r in readable:
                while True:
                    try:
                        if not os.read(wake_r, 256):
                            break
                    except BlockingIOError:
                        break
                if on_resize is not None:
                    on_resize()
            if fd not in readable:
                if keys.flush():
                    return
                continue
            data = os.read(fd, 64)
            if not data:
                server.wait()
                return
            if keys.feed(data):
                return
            if keys.pending_esc:
                more, _, _ = select.select([fd], [], [], 0.2)
                if not more and keys.flush():
                    return
    finally:
        if wake_w >= 0:
            signal.set_wakeup_fd(-1)
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            os.close(wake_r)
            os.close(wake_w)
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _wait_windows_keys(server: subprocess.Popen[object]) -> None:
    import msvcrt

    keys = StopKeyBuffer()
    while server.poll() is None:
        if not msvcrt.kbhit():
            if keys.flush():
                return
            time.sleep(0.05)
            continue
        char = msvcrt.getwch() if hasattr(msvcrt, "getwch") else msvcrt.getch()
        if isinstance(char, str):
            data = char.encode("utf-8", "ignore")
        else:
            data = char
        if keys.feed(data) or (not msvcrt.kbhit() and keys.flush()):
            return


def wait_for_shutdown(
    server: subprocess.Popen[object],
    *,
    on_resize: Callable[[], None] | None = None,
    announce: bool = True,
) -> bool:
    """Block until Esc twice, Ctrl+C, or the server exits.

    Returns True when the server process quit on its own.
    """
    try:
        if sys.stdin.isatty() and sys.platform != "win32":
            _wait_posix_keys(server, on_resize)
        elif sys.stdin.isatty() and sys.platform == "win32":
            _wait_windows_keys(server)
        else:
            server.wait()
    except KeyboardInterrupt:
        pass
    exited = server.poll() is not None
    stop_server(server, announce=announce)
    return exited


def present_running_server(server: subprocess.Popen[object], log_path: Path) -> None:
    """Full-screen status here, request log in another window of this terminal."""
    screen: StatusScreen | None = None
    # Alternate-screen and image escapes belong on a terminal. Redirected
    # stdout (vipercapture > startup.log) keeps the normal banner.
    if sys.stdin.isatty() and sys.stdout.isatty() and sys.platform != "win32":
        screen = StatusScreen(URL)
    if screen is None:
        print(ready_banner())
        if not open_request_window(log_path, os.getpid()):
            print(f"  Requests: {log_path}")
        wait_for_shutdown(server)
        return
    try:
        screen.enter()
        if not open_request_window(log_path, os.getpid()):
            screen.note = f"Requests: {log_path}"
            screen.draw()
        exited = wait_for_shutdown(server, on_resize=screen.draw, announce=False)
    finally:
        screen.leave()
    if exited:
        print("\n  Server exited.")
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            tail = ""
        if tail.strip():
            print(tail.rstrip())
        return
    print("\n  Server stopped.")


def _req_hash() -> str:
    """MD5 of requirements.txt — used to detect changes between runs."""
    req = ROOT / "requirements.txt"
    return hashlib.md5(req.read_bytes()).hexdigest() if req.exists() else ""


def find_uv() -> str | None:
    """Return the uv executable when it should be used, otherwise None."""
    flag = os.environ.get("VIPERCAPTURE_USE_UV", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None
    return shutil.which("uv")


def venv_command(python: str, venv_dir: Path, uv: str | None) -> list[str]:
    if uv:
        return [uv, "venv", "--python", python, str(venv_dir)]
    return [python, "-m", "venv", str(venv_dir)]


def deps_commands(
    python: str,
    requirements: Path,
    uv: str | None,
    *,
    intel_macos: bool = False,
) -> list[tuple[list[str], str]]:
    source_build = ["--no-binary", "cryptography"] if intel_macos else []
    if uv:
        return [(
            [
                uv,
                "pip",
                "install",
                "--python",
                python,
                "-r",
                str(requirements),
                *source_build,
            ],
            "uv pip install",
        )]
    return [
        ([python, "-m", "pip", "install", "--upgrade", "pip", "-q"], "pip upgrade"),
        (
            [python, "-m", "pip", "install", "-r", str(requirements), *source_build],
            "pip install",
        ),
    ]


def is_intel_macos(
    sys_platform: str = sys.platform,
    machine: str | None = None,
) -> bool:
    return sys_platform == "darwin" and (machine or platform.machine()) == "x86_64"


def parse_rustc_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"rustc\s+(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _run_command(
    cmd: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]],
    timeout: int = 15,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _command_stdout(
    cmd: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> str | None:
    result = _run_command(cmd, run)
    if result is None or result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _command_succeeded(
    cmd: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]],
    *,
    needles: tuple[str, ...] = (),
) -> bool:
    result = _run_command(cmd, run)
    if result is None or result.returncode != 0:
        return False
    if not needles:
        return True
    text = f"{result.stdout or ''}{result.stderr or ''}".lower()
    return any(needle.lower() in text for needle in needles)


def _working_compiler(
    which: Callable[[str], str | None],
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> str | None:
    """Return a compiler that actually runs. Apple CLT stubs exist as /usr/bin/cc."""
    candidates: list[str] = []
    for name in ("cc", "clang"):
        found = which(name)
        if found and found not in candidates:
            candidates.append(found)
    developer_dir = _command_stdout(["xcode-select", "-p"], run)
    if developer_dir:
        for rel in ("usr/bin/clang", "usr/bin/cc"):
            nested = str(Path(developer_dir) / rel)
            if nested not in candidates:
                candidates.append(nested)
    for compiler in candidates:
        if _command_succeeded(
            [compiler, "-v"], run, needles=("clang", "gcc", "Apple LLVM")
        ):
            return compiler
    return None


def _has_openssl_libs(prefix: Path) -> bool:
    lib = prefix / "lib"
    if not lib.is_dir():
        return False
    names = {path.name for path in lib.iterdir()}

    def present(stem: str) -> bool:
        return any(
            name == f"lib{stem}.dylib"
            or name == f"lib{stem}.a"
            or name == f"lib{stem}.so"
            or name.startswith(f"lib{stem}.")
            and name.endswith(".dylib")
            for name in names
        )

    return present("crypto") and present("ssl")


def openssl_headers_are_usable(opensslv_h: str) -> bool:
    """Accept OpenSSL 3+; reject LibreSSL and older OpenSSL."""
    if re.search(r"^\s*#\s*define\s+LIBRESSL_VERSION_NUMBER\b", opensslv_h, re.M):
        return False
    major = re.search(
        r"^\s*#\s*define\s+OPENSSL_VERSION_MAJOR\s+(\d+)", opensslv_h, re.M
    )
    if major:
        return int(major.group(1)) >= 3
    number = re.search(
        r"^\s*#\s*define\s+OPENSSL_VERSION_NUMBER\s+(0x[0-9a-fA-F]+)",
        opensslv_h,
        re.M,
    )
    if number:
        return int(number.group(1), 16) >= 0x30000000
    return False


def _openssl_prefix_if_present(prefix: str) -> str | None:
    if not prefix:
        return None
    root = Path(prefix)
    header = root / "include" / "openssl" / "ssl.h"
    version_header = root / "include" / "openssl" / "opensslv.h"
    if not header.is_file() or not version_header.is_file():
        return None
    if not _has_openssl_libs(root):
        return None
    try:
        opensslv = version_header.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not openssl_headers_are_usable(opensslv):
        return None
    return prefix


def probe_intel_macos_cryptography_toolchain(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    environ: dict[str, str] | None = None,
) -> dict[str, object]:
    """Discover a working compiler, rustc+cargo, and non-Apple OpenSSL 3 prefix."""
    env = os.environ if environ is None else environ
    compiler = _working_compiler(which, run)
    rustc = which("rustc")
    cargo = which("cargo")
    rust_version = None
    if rustc:
        rust_version = parse_rustc_version(
            _command_stdout([rustc, "--version"], run) or ""
        )
    cargo_ok = bool(
        cargo and _command_succeeded([cargo, "--version"], run, needles=("cargo",))
    )
    openssl_dir = _openssl_prefix_if_present(env.get("OPENSSL_DIR", "").strip())
    brew = which("brew")
    if openssl_dir is None and brew:
        for formula in ("openssl@3", "openssl"):
            prefix = _command_stdout([brew, "--prefix", formula], run)
            openssl_dir = _openssl_prefix_if_present(prefix or "")
            if openssl_dir:
                break
    if openssl_dir is None:
        openssl_dir = _openssl_prefix_if_present("/opt/local")
    if openssl_dir is None:
        pkg_config = which("pkg-config") or which("pkgconf")
        if pkg_config:
            prefix = _command_stdout(
                [pkg_config, "--variable=prefix", "libcrypto"], run
            )
            openssl_dir = _openssl_prefix_if_present(prefix or "")
    return {
        "compiler": compiler,
        "rustc": rustc,
        "rust_version": rust_version,
        "cargo": cargo if cargo_ok else None,
        "openssl_dir": openssl_dir,
    }


def intel_macos_cryptography_missing(toolchain: dict[str, object]) -> list[str]:
    missing: list[str] = []
    if not toolchain.get("compiler"):
        missing.append("Xcode command line tools (clang)")
    rust_version = toolchain.get("rust_version")
    rust_ok = (
        isinstance(rust_version, tuple)
        and rust_version >= MIN_RUSTC
        and toolchain.get("cargo")
    )
    if not rust_ok:
        missing.append(
            f"Rust {MIN_RUSTC[0]}.{MIN_RUSTC[1]}.{MIN_RUSTC[2]}+ (rustc and cargo)"
        )
    if not toolchain.get("openssl_dir"):
        missing.append("Homebrew/MacPorts OpenSSL 3 (not Apple LibreSSL)")
    return missing


def apply_intel_macos_cryptography_env(
    environ: dict[str, str], openssl_dir: str
) -> dict[str, str]:
    """Point the cryptography sdist at a real OpenSSL prefix (PyCA OPENSSL_DIR)."""
    environ["OPENSSL_DIR"] = openssl_dir
    pkgconfig = Path(openssl_dir) / "lib" / "pkgconfig"
    if pkgconfig.is_dir():
        current = environ.get("PKG_CONFIG_PATH", "")
        prefix = str(pkgconfig)
        parts = [part for part in current.split(":") if part]
        if prefix not in parts:
            environ["PKG_CONFIG_PATH"] = ":".join([prefix, *parts])
    return environ


def format_intel_macos_cryptography_error(missing: list[str]) -> str:
    needed = ", ".join(missing)
    return (
        "\n  ERROR: Intel macOS has no cryptography >=49 wheel on PyPI "
        "(GHSA-jwv3-5hgf-82ww).\n"
        f"  pyca removed x86_64/universal2 wheels in 49.0.0: "
        f"{INTEL_MACOS_CRYPTOGRAPHY_CHANGELOG}\n"
        f"  Source-build tools missing: {needed}\n"
        "\n  Install the official PyCA macOS build dependencies, then rerun:\n"
        "    xcode-select --install\n"
        "    brew install openssl@3 rust\n"
        "    # or: sudo port install openssl rust\n"
        f"  {INTEL_MACOS_CRYPTOGRAPHY_DOCS}\n"
        "  Do not pin cryptography <=48; those releases are vulnerable.\n"
    )


def prepare_intel_macos_cryptography_build(
    *,
    sys_platform: str = sys.platform,
    machine: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """
    On Intel Macs, refuse to pip-install until a source build can succeed.
    Returns missing-tool messages (empty when ready). Exits from ensure_deps.
    """
    if not is_intel_macos(sys_platform, machine):
        return []
    env = os.environ if environ is None else environ
    toolchain = probe_intel_macos_cryptography_toolchain(
        which=which, run=run, environ=env
    )
    missing = intel_macos_cryptography_missing(toolchain)
    if missing:
        return missing
    openssl_dir = toolchain.get("openssl_dir")
    if isinstance(openssl_dir, str) and openssl_dir:
        apply_intel_macos_cryptography_env(env, openssl_dir)
    print(
        "  Intel macOS: building cryptography from the official sdist "
        "(no PyPI x86_64 wheel for 49+)."
    )
    return []


def _venv_has_pip(python: str) -> bool:
    result = subprocess.run(
        [python, "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def in_project_venv(prefix: str | Path | None = None, venv: str | Path | None = None) -> bool:
    """True when this process is the project venv, not the base interpreter.

    Arch, Omarchy, and other externally managed Pythons make ``.venv/bin/python``
    a symlink to ``/usr/bin/python3``. Resolving both paths makes them look like
    the same program, and ``uv pip install --python`` then targets ``/usr`` and
    refuses to install.
    """
    if prefix is None:
        prefix = sys.prefix
    if venv is None:
        venv = ROOT / ".venv"
    try:
        return Path(prefix).resolve() == Path(venv).resolve()
    except OSError:
        return False


# ── Setup steps ───────────────────────────────────────────────

def ensure_venv() -> None:
    """
    If the venv doesn't exist yet, create it.
    If we're not running from the venv Python, re-launch with it so
    all subsequent imports and subprocess calls use the right Python.
    """
    if not VENV_PYTHON.exists():
        uv = find_uv()
        installer = "uv" if uv else "venv"
        print(f"  [1/3] Creating Python environment with {installer} (first run only)...")
        command = venv_command(sys.executable, ROOT / ".venv", uv)
        run(*command, label=f"{installer} venv")

    if not in_project_venv():
        # Hand off to the venv Python — this process becomes just a waiter.
        # Pass the venv path itself. Do not resolve the symlink first.
        result = subprocess.run([str(VENV_PYTHON), __file__] + sys.argv[1:])
        sys.exit(result.returncode)


def ensure_deps() -> None:
    """
    Install packages from requirements.txt.
    Skipped on subsequent runs unless requirements.txt has changed.
    Prefers uv when available; falls back to pip.
    """
    current_hash = _req_hash()
    if DEPS_STAMP.exists() and DEPS_STAMP.read_text().strip() == current_hash:
        print("  [2/3] Python packages already up to date — skipping.")
        return

    uv = find_uv()
    # Always the venv interpreter path, never sys.executable. On Arch that
    # value is /usr/bin/python3 even when the venv symlink points at it.
    python = str(VENV_PYTHON)
    if uv:
        print("  [2/3] Installing Python packages with uv...")
    else:
        if not _venv_has_pip(python):
            print("\n  ERROR: pip is not available in .venv and uv was not found.")
            print("  Install uv (https://docs.astral.sh/uv/) or delete .venv and rerun.")
            wait_and_exit(1)
        print("  [2/3] Installing Python packages...")

    intel_macos = is_intel_macos()
    missing = prepare_intel_macos_cryptography_build()
    if missing:
        print(format_intel_macos_cryptography_error(missing))
        wait_and_exit(1)

    for command, label in deps_commands(
        python, ROOT / "requirements.txt", uv, intel_macos=intel_macos
    ):
        run(*command, label=label)
    DEPS_STAMP.write_text(current_hash)


def playwright_install_command(
    python: str,
    *,
    platform_name: str = sys.platform,
    has_apt: bool = False,
) -> list[str]:
    """Browser install command.

    ``--with-deps`` only works with apt. On Arch, Omarchy, and other
    non-apt distributions Playwright falls back to Ubuntu and runs
    ``sudo apt-get``, which is not installed.
    """
    command = [python, "-m", "playwright", "install", "--no-shell"]
    if platform_name.startswith("linux") and has_apt:
        command.append("--with-deps")
    return command


def ensure_playwright() -> None:
    """
    Install Playwright's Chromium, Firefox, and WebKit browsers.
    Skipped when the installed browser matches the Playwright package version.
    """
    playwright_stamp = f"{version('playwright')}:chromium,firefox,webkit"
    if (
        PLAYWRIGHT_STAMP.exists()
        and PLAYWRIGHT_STAMP.read_text().strip() == playwright_stamp
    ):
        print("  [3/3] Playwright browsers already installed — skipping.")
        return

    print("  [3/3] Installing Playwright browsers...")
    has_apt = shutil.which("apt-get") is not None
    command = playwright_install_command(sys.executable, has_apt=has_apt)
    if sys.platform.startswith("linux") and not has_apt:
        print("  This distro does not use apt, so system packages are left alone.")
    run(*command, "chromium", "firefox", "webkit", label="playwright install")
    PLAYWRIGHT_STAMP.write_text(playwright_stamp)


# ── Main ──────────────────────────────────────────────────────

LAUNCH_HELP = """\
usage: vipercapture [--one-window]
       vipercapture update [--check]
       vipercapture --gui [--viewport WIDTH HEIGHT]

Start ViperCapture in this terminal. Ghostty and Kitty open request
logs in another window and show a full-screen status view.

  --one-window   Keep the status and request log in this terminal.
                 Use this over SSH, or anywhere a second window is wrong.
  update         Download the latest ViperCapture and replace this install.
                 The virtualenv and saved jobs stay in place. This uses
                 Python only, on Linux, macOS, and Windows.
  update --check Print whether an update is available, then exit.
                 Exit 10 means an update is available.
  --gui          Open a full-screen capture menu in this terminal.
                 Type a website link, press Tab to switch png, gif, and
                 mp4, and press Ctrl+P to switch between the full page and
                 the viewport. Nothing else is opened. --GUI is accepted too.
  --viewport     With --gui, set the page size in pixels. The default
                 is 1920 1080.
  -h, --help     Show this help.

Windows always uses one window. --gui stays in this window on every OS.
"""


@dataclass(frozen=True)
class CliArgs:
    one_window: bool = False
    update: bool = False
    check: bool = False
    gui: bool = False
    viewport: tuple[int, int] | None = None


def _reject_arg(message: str) -> None:
    print(f"{message}\n{LAUNCH_HELP}", end="", file=sys.stderr)
    raise SystemExit(2)


def _viewport_edge(token: str) -> int:
    if not token.isdigit() or len(token) > 5:
        _reject_arg(f"Invalid viewport size: {token}")
    value = int(token)
    if value < 1 or value > 16_384:
        _reject_arg(f"Invalid viewport size: {token}")
    return value


def parse_cli(argv: list[str] | None = None) -> CliArgs:
    """Help exits 0. Unknown arguments exit 2."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "update":
        check = False
        for arg in args[1:]:
            if arg == "--check":
                check = True
                continue
            if arg in {"-h", "--help"}:
                print(LAUNCH_HELP, end="")
                raise SystemExit(0)
            print(f"Unknown argument: {arg}\n{LAUNCH_HELP}", end="", file=sys.stderr)
            raise SystemExit(2)
        return CliArgs(update=True, check=check)
    gui = False
    one_window = False
    viewport: tuple[int, int] | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-h", "--help"}:
            print(LAUNCH_HELP, end="")
            raise SystemExit(0)
        if arg.lower() == "--gui":
            gui = True
            index += 1
            continue
        if arg == "--one-window":
            one_window = True
            index += 1
            continue
        if arg == "--viewport":
            if index + 2 >= len(args):
                _reject_arg("--viewport needs a width and a height.")
            viewport = (_viewport_edge(args[index + 1]), _viewport_edge(args[index + 2]))
            index += 3
            continue
        _reject_arg(f"Unknown argument: {arg}")
    if viewport is not None and not gui:
        _reject_arg("--viewport is only used with --gui.")
    if gui and one_window:
        _reject_arg("--gui already keeps everything in this terminal.")
    return CliArgs(one_window=one_window, gui=gui, viewport=viewport)


def parse_launch_args(argv: list[str] | None = None) -> bool:
    """Return True when --one-window was passed. Help exits 0; unknown args exit 2."""
    return parse_cli(argv).one_window


def use_one_window(one_window: bool, platform_name: str | None = None) -> bool:
    """Windows has no companion terminal, so it always keeps logs in this console."""
    if platform_name is None:
        platform_name = sys.platform
    return one_window or platform_name == "win32"


def main() -> None:
    # Parsed before the venv re-exec so --help and update do not install
    # dependencies or start the server. ensure_venv re-runs this file with
    # the same arguments when the command is a normal launch.
    cli = parse_cli()
    if cli.update:
        import updater
        raise SystemExit(updater.run_update(ROOT, check_only=cli.check))
    if cli.gui:
        ensure_venv()
        import gui
        viewport = cli.viewport if cli.viewport is not None else (1920, 1080)
        raise SystemExit(gui.run_capture_gui(viewport))
    one_window = use_one_window(cli.one_window)
    ensure_venv()    # may re-exec this script under the venv Python

    print()
    print("  ViperCapture")
    print("  ------------")
    print()

    ensure_deps()
    ensure_playwright()

    # Server already running from a previous session?
    if port_open():
        show_terminal_logo()
        print(f"\n{listening_line()}")
        print("  That server is already running. Opening it in your browser.")
        webbrowser.open(URL)
        return

    # ── Start the server ────────────────────────────────────────
    print(f"\n  Starting server at {URL}\n")

    log_path = Path.home() / ".vipercapture" / "requests.log"
    log_handle = None
    popen_kwargs: dict[str, object] = {"cwd": str(ROOT)}
    if not one_window:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        log_handle = open(log_fd, "w", encoding="utf-8", buffering=1)
        popen_kwargs["stdout"] = log_handle
        popen_kwargs["stderr"] = subprocess.STDOUT
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "vipercapture.main:app",
         "--host", HOST, "--port", str(PORT)],
        **popen_kwargs,
    )

    # ── Wait for port (1 check/sec, 30s max) ────────────────────
    # One-window mode inherits uvicorn's output, so progress dots would
    # land in the middle of those lines.
    if one_window:
        print("  Waiting for server to be ready...", flush=True)
    else:
        print("  Waiting for server to be ready...", end="", flush=True)
    ready = False
    for _ in range(30):
        if port_open():
            ready = True
            break
        if server.poll() is not None:
            # Server process already exited — don't wait the full 30s
            break
        time.sleep(1)
        if not one_window:
            print(".", end="", flush=True)
    print()

    if not ready:
        print("\n  ERROR: Server didn't start.")
        details = ""
        if log_handle is not None:
            log_handle.flush()
            try:
                details = log_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                details = ""
        if details:
            print(details)
        else:
            print("  Check the output above for details.")
        if server.poll() is None:
            server.terminate()
        if log_handle is not None:
            log_handle.close()
        wait_and_exit(1)

    # ── Open browser, then present this terminal ───────────────
    webbrowser.open(URL)
    try:
        if one_window:
            show_terminal_logo()
            print(ready_banner())
            if wait_for_shutdown(server):
                print("\n  Server exited.")
        else:
            present_running_server(server, log_path)
    finally:
        if log_handle is not None:
            log_handle.close()


if __name__ == "__main__":
    main()
