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
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess
    from typing import Callable, Mapping

_LAZY_MODULES = frozenset({
    "base64",
    "hashlib",
    "json",
    "platform",
    "re",
    "secrets",
    "shlex",
    "shutil",
    "signal",
    "socket",
    "struct",
    "subprocess",
    "webbrowser",
    "zlib",
})


def _mod(name: str):
    existing = globals().get(name)
    if existing is not None:
        return existing
    loaded = __import__(name)
    globals()[name] = loaded
    return loaded


def __getattr__(name: str):
    if name in _LAZY_MODULES:
        return _mod(name)
    if name == "version":
        from importlib.metadata import version as _meta_version

        globals()["version"] = _meta_version
        return _meta_version
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _windows_tcp_listener_pids(port: int, *, host: str | None = None) -> set[int] | None:
    """Query listening TCP socket owner PIDs on Windows via iphlpapi (<1ms)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        iphlpapi = ctypes.WinDLL("iphlpapi")
        get_tcp_table = iphlpapi.GetExtendedTcpTable
        get_tcp_table.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
            wintypes.ULONG,
            wintypes.DWORD,
            wintypes.ULONG,
        ]
        get_tcp_table.restype = wintypes.DWORD

        AF_INET = 2
        AF_INET6 = 23
        TCP_TABLE_OWNER_PID_LISTENER = 3
        ERROR_INSUFFICIENT_BUFFER = 122

        class MIB_TCPROW_OWNER_PID(ctypes.Structure):
            _fields_ = [
                ("dwState", wintypes.DWORD),
                ("dwLocalAddr", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD),
                ("dwRemotePort", wintypes.DWORD),
                ("dwOwningPid", wintypes.DWORD),
            ]

        class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
            _fields_ = [
                ("ucLocalAddr", ctypes.c_ubyte * 16),
                ("dwLocalScopeId", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD),
                ("ucRemoteAddr", ctypes.c_ubyte * 16),
                ("dwRemoteScopeId", wintypes.DWORD),
                ("dwRemotePort", wintypes.DWORD),
                ("dwState", wintypes.DWORD),
                ("dwOwningPid", wintypes.DWORD),
            ]

        host_lower = host.lower() if host is not None else None
        families: list[tuple[int, type[ctypes.Structure]]] = []
        if host_lower in {None, "127.0.0.1", "localhost"}:
            families.append((AF_INET, MIB_TCPROW_OWNER_PID))
        if host_lower in {None, "::1", "localhost", "::"}:
            families.append((AF_INET6, MIB_TCP6ROW_OWNER_PID))

        pids: set[int] = set()
        ipv6_any = bytes(16)
        ipv6_loopback = bytes(15) + b"\x01"
        ipv6_mapped_loopback = b"\x00" * 10 + b"\xff\xff\x7f\x00\x00\x01"
        for af, row_cls in families:
            size = wintypes.DWORD(0)
            rc = get_tcp_table(None, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_LISTENER, 0)
            if rc not in {0, ERROR_INSUFFICIENT_BUFFER} or not size.value:
                if rc not in {0, ERROR_INSUFFICIENT_BUFFER}:
                    return None
                continue
            buf = ctypes.create_string_buffer(size.value)
            rc = get_tcp_table(buf, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_LISTENER, 0)
            if rc == ERROR_INSUFFICIENT_BUFFER and size.value:
                buf = ctypes.create_string_buffer(size.value)
                rc = get_tcp_table(buf, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_LISTENER, 0)
            if rc != 0:
                return None
            count = wintypes.DWORD.from_buffer(buf, 0).value
            row_size = ctypes.sizeof(row_cls)
            offset = 4
            for idx in range(count):
                row = row_cls.from_buffer(buf, offset + idx * row_size)
                local_port = ((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF)
                if local_port != port:
                    continue
                if host_lower is not None:
                    if af == AF_INET:
                        # 0.0.0.0 or 127.0.0.1 in network byte order
                        if row.dwLocalAddr not in {0, 0x0100007F}:
                            continue
                    else:
                        addr_bytes = bytes(row.ucLocalAddr)
                        if host_lower == "::":
                            if addr_bytes not in {ipv6_any, ipv6_mapped_loopback}:
                                continue
                        elif addr_bytes not in {ipv6_any, ipv6_loopback}:
                            continue
                if row.dwOwningPid > 0:
                    pids.add(int(row.dwOwningPid))
        return pids
    except Exception:
        return None


def port_open(host: str = HOST, port: int = PORT, *, timeout: float = 0.2) -> bool:
    host_lower = host.lower()
    if sys.platform == "win32" and host_lower in _LOOPBACK_HOSTS:
        listeners = _windows_tcp_listener_pids(port, host=host_lower)
        if listeners is not None:
            if listeners:
                return True
            if host_lower != "127.0.0.1":
                return False
            # A dual-stack IPv6 wildcard (::) or IPv4-mapped loopback socket
            # accepts 127.0.0.1 connections while appearing only in the IPv6
            # TCP table. Probe via socket only when such a listener exists.
            v6_dual_candidates = _windows_tcp_listener_pids(port, host="::")
            if v6_dual_candidates is not None and not v6_dual_candidates:
                return False
    socket = _mod("socket")
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run(*cmd: str | Path, label: str = "") -> None:
    """Run a subprocess and exit hard if it fails."""
    subprocess = _mod("subprocess")
    result = subprocess.run([str(c) for c in cmd])
    if result.returncode != 0:
        tag = f" ({label})" if label else ""
        print(f"\n  ERROR{tag}: command exited with code {result.returncode}")
        print(f"  Command: {' '.join(str(c) for c in cmd)}")
        wait_and_exit(1)


# The capture command reports failure and exits. The interactive launcher
# still waits for Enter when a person is sitting at the terminal.
INTERACTIVE_FAILURES = True


def wait_and_exit(code: int = 0) -> None:
    if INTERACTIVE_FAILURES and sys.stdin.isatty():
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


def _parse_version_tuple(val: str | None) -> tuple[int, ...] | None:
    if not val:
        return None
    matches = _mod("re").findall(r"\d+", str(val))
    if matches:
        return tuple(int(x) for x in matches[:3])
    return None


def _windows_terminal_supports_sixel(environ: Mapping[str, str]) -> bool:
    """Return an explicit Windows Terminal Sixel opt-in.

    Windows Terminal exports ``WT_SESSION`` but no supported capability or
    version variable.  Looking at an installed ``wt.exe`` is also not proof
    that it owns this session (and cannot work in WSL), so automatic detection
    would risk emitting raw Sixel escapes.  The opt-in is intentionally valid
    in native Windows and WSL alike.
    """
    value = environ.get("VIPERCAPTURE_SIXEL", "").strip().lower()
    return bool(environ.get("WT_SESSION") or environ.get("WT_PROFILE_ID")) and value in {
        "1",
        "true",
        "yes",
        "on",
    }


def _is_sixel_terminal(environ: Mapping[str, str]) -> bool:
    """Terminals whose normal build displays Sixel."""
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
    if _windows_terminal_supports_sixel(environ):
        return True
    return False


def _strip_lua_comments(text: str) -> str:
    re = _mod("re")
    text = re.sub(r"--\[\[.*?\]\]", "", text, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", text)


def _lua_assigns_true(text: str, name: str) -> bool:
    matches = _mod("re").findall(rf"\b{name}\s*=\s*(true|false)\b", _strip_lua_comments(text))
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
    alternate_screen: bool = False,
) -> str | None:
    """Pick the image protocol supported by the terminal and its console host."""
    if platform_name is None:
        platform_name = sys.platform
    if is_tty is None:
        is_tty = sys.stdout.isatty()
    if environ is None:
        environ = os.environ
    if not is_tty:
        return None
    term = environ.get("TERM", "")
    if environ.get("TMUX") or term.startswith(("screen", "tmux")):
        return None
    if alternate_screen and platform_name == "win32" and _is_wezterm(environ):
        # ConPTY can drop or misplace Kitty images in the alternate screen.
        return "iterm2"
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
    encoded = _mod("base64").standard_b64encode(png)
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
    encoded = _mod("base64").standard_b64encode(png)
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
    struct = _mod("struct")
    zlib = _mod("zlib")
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
    struct = _mod("struct")
    zlib = _mod("zlib")
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


class WindowSize:
    __slots__ = ("cols", "rows", "xpixels", "ypixels")

    def __init__(self, cols: int, rows: int, xpixels: int = 0, ypixels: int = 0) -> None:
        object.__setattr__(self, "cols", cols)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "xpixels", xpixels)
        object.__setattr__(self, "ypixels", ypixels)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("WindowSize is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WindowSize):
            return NotImplemented
        return (self.cols, self.rows, self.xpixels, self.ypixels) == (
            other.cols, other.rows, other.xpixels, other.ypixels
        )

    def __hash__(self) -> int:
        return hash((self.cols, self.rows, self.xpixels, self.ypixels))

    def __repr__(self) -> str:
        return f"WindowSize(cols={self.cols!r}, rows={self.rows!r}, xpixels={self.xpixels!r}, ypixels={self.ypixels!r})"


class StatusLayout:
    __slots__ = ("logo_col", "logo_row", "logo_cols", "logo_rows", "text_row", "lines")

    def __init__(
        self,
        logo_col: int,
        logo_row: int,
        logo_cols: int,
        logo_rows: int,
        text_row: int,
        lines: tuple[str, ...],
    ) -> None:
        object.__setattr__(self, "logo_col", logo_col)
        object.__setattr__(self, "logo_row", logo_row)
        object.__setattr__(self, "logo_cols", logo_cols)
        object.__setattr__(self, "logo_rows", logo_rows)
        object.__setattr__(self, "text_row", text_row)
        object.__setattr__(self, "lines", lines)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("StatusLayout is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, StatusLayout):
            return NotImplemented
        return (self.logo_col, self.logo_row, self.logo_cols, self.logo_rows, self.text_row, self.lines) == (
            other.logo_col, other.logo_row, other.logo_cols, other.logo_rows, other.text_row, other.lines
        )

    def __hash__(self) -> int:
        return hash((self.logo_col, self.logo_row, self.logo_cols, self.logo_rows, self.text_row, self.lines))

    def __repr__(self) -> str:
        return (
            f"StatusLayout(logo_col={self.logo_col!r}, logo_row={self.logo_row!r}, "
            f"logo_cols={self.logo_cols!r}, logo_rows={self.logo_rows!r}, "
            f"text_row={self.text_row!r}, lines={self.lines!r})"
        )


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
    encoded = _mod("base64").standard_b64encode(png)
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
    shutil = _mod("shutil")
    subprocess = _mod("subprocess")
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
    shutil = _mod("shutil")
    subprocess = _mod("subprocess")
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


def stop_server(
    server: subprocess.Popen[object],
    *,
    announce: bool = True,
    host: str = HOST,
    port: int = PORT,
    worker_pid: int | None = None,
) -> None:
    if server.poll() is not None and (worker_pid is None or not _is_process_alive(worker_pid)):
        if not _is_process_alive(server.pid):
            _clear_server_record(server.pid)
        if worker_pid is not None and worker_pid != server.pid and not _is_process_alive(worker_pid):
            _clear_server_record(worker_pid)
        return
    if announce:
        print("\n  Stopping server...")
    resolved_worker = worker_pid
    if resolved_worker is None:
        endpoint_pid = _is_vipercapture_endpoint(host=host, port=port)
        if endpoint_pid is not None and (
            endpoint_pid == server.pid or _is_descendant_pid(endpoint_pid, server.pid)
        ):
            resolved_worker = endpoint_pid
    target_pid = resolved_worker if resolved_worker is not None else server.pid
    identity = _capture_process_identity(target_pid)
    requested = bool(
        identity is not None
        and identity.kind == "server"
        and _request_server_shutdown(identity, host=host, port=port)
    )
    if not requested:
        if resolved_worker is not None and resolved_worker != server.pid:
            if identity is not None and identity.kind == "server":
                _terminate_process(identity, host=host, port=port)
        if server.poll() is None:
            server.terminate()
    try:
        server.wait(timeout=shutdown_grace_seconds())
    except _mod("subprocess").TimeoutExpired:
        if resolved_worker is not None and resolved_worker != server.pid and _is_process_alive(resolved_worker):
            worker_identity = _capture_process_identity(resolved_worker)
            if worker_identity is not None and worker_identity.kind == "server":
                _terminate_process(worker_identity, host=host, port=port)
        if server.poll() is None:
            server.kill()
            server.wait(timeout=5)
    finally:
        # Ensure a Windows venvlauncher child worker never outlives its launcher.
        if (
            resolved_worker is not None
            and resolved_worker != server.pid
            and _is_process_alive(resolved_worker)
        ):
            worker_identity = identity if (identity is not None and identity.pid == resolved_worker) else _capture_process_identity(resolved_worker)
            if worker_identity is not None and worker_identity.kind == "server":
                _terminate_process(worker_identity, host=host, port=port)
        if not _is_process_alive(server.pid):
            _clear_server_record(server.pid)
        if resolved_worker is not None and resolved_worker != server.pid and not _is_process_alive(resolved_worker):
            _clear_server_record(resolved_worker)
    if announce:
        print("  Server stopped.")


def _windows_process_snapshot() -> list[tuple[int, int, str]] | None:
    """Return [(pid, ppid, exe_name), ...] on Windows via CreateToolhelp32Snapshot."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_snapshot = kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create_snapshot.restype = wintypes.HANDLE
        process_first = kernel32.Process32FirstW
        process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        process_first.restype = wintypes.BOOL
        process_next = kernel32.Process32NextW
        process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        process_next.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        hSnapshot = create_snapshot(TH32CS_SNAPPROCESS, 0)
        if not hSnapshot or hSnapshot == INVALID_HANDLE_VALUE:
            return None
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            entries: list[tuple[int, int, str]] = []
            success = process_first(hSnapshot, ctypes.byref(pe))
            while success:
                entries.append((int(pe.th32ProcessID), int(pe.th32ParentProcessID), pe.szExeFile))
                success = process_next(hSnapshot, ctypes.byref(pe))
            return entries
        finally:
            close_handle(hSnapshot)
    except Exception:
        return None


def _windows_parent_map() -> dict[int, int]:
    """Return {pid: ppid} on Windows via CreateToolhelp32Snapshot."""
    entries = _windows_process_snapshot()
    if not entries:
        return {}
    return {pid: ppid for pid, ppid, _name in entries}


def _windows_pid_commandline(pid: int) -> tuple[bool, str | None]:
    """Read a Windows process command line directly via NtQueryInformationProcess (<0.2ms).

    Returns ``(handled, cmdline)``. ``handled=False`` signals that the OS does not
    support ProcessCommandLineInformation (Windows < 8.1) and the caller may fall back.
    """
    if sys.platform != "win32" or pid <= 0:
        return True, None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ProcessCommandLineInformation = 60
        STATUS_INVALID_INFO_CLASS = -1073741821  # 0xC0000003
        STATUS_NOT_IMPLEMENTED = -1073741822     # 0xC0000002

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        ntdll = ctypes.WinDLL("ntdll")
        query_info = ntdll.NtQueryInformationProcess
        query_info.argtypes = [
            wintypes.HANDLE,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        query_info.restype = wintypes.LONG

        handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True, None
        try:
            needed = wintypes.ULONG(0)
            buf_size = 1024
            for _ in range(4):
                buf = ctypes.create_string_buffer(buf_size)
                status = query_info(
                    handle,
                    ProcessCommandLineInformation,
                    buf,
                    buf_size,
                    ctypes.byref(needed),
                )
                if status == 0:
                    us = UNICODE_STRING.from_buffer(buf)
                    if us.Length == 0 or not us.Buffer:
                        return True, None
                    text = ctypes.wstring_at(us.Buffer, us.Length // 2).strip()
                    return True, text or None
                if status in {STATUS_INVALID_INFO_CLASS, STATUS_NOT_IMPLEMENTED}:
                    return False, None
                if needed.value > buf_size:
                    buf_size = int(needed.value) + 64
                    continue
                return True, None
        finally:
            close_handle(handle)
        return True, None
    except Exception:
        return False, None


def _get_parent_pid(pid: int) -> int | None:
    if pid <= 0:
        return None
    if sys.platform == "win32":
        ppid = _windows_parent_map().get(pid)
        return ppid if ppid and ppid > 4 else None
    return None


def _is_descendant_pid(child_pid: int, ancestor_pid: int) -> bool:
    if child_pid <= 0 or ancestor_pid <= 0 or child_pid == ancestor_pid:
        return False
    if sys.platform == "win32":
        parents = _windows_parent_map()
        seen: set[int] = {child_pid}
        cur = child_pid
        while cur in parents and parents[cur] > 4:
            cur = parents[cur]
            if cur == ancestor_pid:
                return True
            if cur in seen:
                break
            seen.add(cur)
    return False


def _get_process_ancestors() -> set[int]:
    """Return the set of PIDs covering current process and all its ancestors."""
    ancestors = {os.getpid()}
    if sys.platform == "win32":
        parents = _windows_parent_map()
        cur = os.getpid()
        while cur in parents and parents[cur] > 4 and parents[cur] not in ancestors:
            cur = parents[cur]
            ancestors.add(cur)
    elif sys.platform == "linux":
        try:
            cur = os.getpid()
            while cur > 1:
                # Read /proc/<cur>/stat on Linux
                stat_file = Path(f"/proc/{cur}/stat")
                if stat_file.is_file():
                    stat_text = stat_file.read_text(encoding="utf-8", errors="ignore")
                    rparen = stat_text.rfind(")")
                    if rparen != -1:
                        parts = stat_text[rparen + 1 :].split()
                        ppid = int(parts[1])
                        if ppid in ancestors or ppid <= 1:
                            break
                        ancestors.add(ppid)
                        cur = ppid
                        continue
                out = _mod("subprocess").check_output(
                    ["ps", "-p", str(cur), "-o", "ppid="],
                    text=True,
                    errors="ignore",
                    timeout=2,
                )
                ppid = int(out.strip())
                if ppid in ancestors or ppid <= 1:
                    break
                ancestors.add(ppid)
                cur = ppid
        except Exception:
            pass
    else:
        # macOS and other POSIX systems do not expose /proc. Walk the complete
        # chain with ps rather than calling os.getppid(), which only describes
        # the current Python process and therefore cannot advance the walk.
        subprocess = _mod("subprocess")
        try:
            cur = os.getpid()
            while cur > 1:
                out = subprocess.check_output(
                    ["ps", "-p", str(cur), "-o", "ppid="],
                    text=True,
                    errors="ignore",
                    timeout=2,
                )
                ppid = int(out.strip())
                if ppid in ancestors or ppid <= 1:
                    break
                ancestors.add(ppid)
                cur = ppid
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return ancestors


def _get_process_start_time(pid: int) -> int | str | None:
    """Return an opaque identifier for the process start time to guard against PID reuse."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                try:
                    creation = wintypes.FILETIME()
                    exit_time = wintypes.FILETIME()
                    kernel_t = wintypes.FILETIME()
                    user_t = wintypes.FILETIME()
                    if kernel32.GetProcessTimes(
                        h,
                        ctypes.byref(creation),
                        ctypes.byref(exit_time),
                        ctypes.byref(kernel_t),
                        ctypes.byref(user_t),
                    ):
                        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                finally:
                    kernel32.CloseHandle(h)
        except Exception:
            pass
    elif sys.platform == "linux":
        try:
            stat_file = Path(f"/proc/{pid}/stat")
            if stat_file.is_file():
                text = stat_file.read_text(encoding="utf-8", errors="ignore")
                rparen = text.rfind(")")
                if rparen != -1:
                    parts = text[rparen + 1 :].split()
                    if len(parts) >= 20:
                        return parts[19]  # starttime
        except OSError:
            pass
    elif sys.platform == "darwin":
        subprocess = _mod("subprocess")
        try:
            value = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "lstart="],
                text=True,
                errors="ignore",
                timeout=2,
            ).strip()
            return value or None
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _split_process_command(cmdline: str) -> list[str]:
    """Split a process command line without treating labels as executables."""
    if not cmdline.strip():
        return []
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            argc = ctypes.c_int()
            command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
            command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
            command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
            local_free = ctypes.windll.kernel32.LocalFree
            local_free.argtypes = [wintypes.HLOCAL]
            local_free.restype = wintypes.HLOCAL
            argv = command_line_to_argv(cmdline, ctypes.byref(argc))
            if argv:
                try:
                    return [argv[index] for index in range(argc.value)]
                finally:
                    local_free(argv)
        except Exception:
            pass
    try:
        return _mod("shlex").split(cmdline, posix=True)
    except ValueError:
        return []


def _normalized_absolute_path(value: str) -> str | None:
    """Normalize an absolute argv path without resolving venv symlinks."""
    value = value.strip().strip('"')
    if not value or not Path(value).is_absolute():
        return None
    normalized = os.path.normpath(os.path.abspath(value))
    return os.path.normcase(normalized).casefold() if sys.platform == "win32" else normalized


def _same_process_path(value: str, expected: Path) -> bool:
    actual = _normalized_absolute_path(value)
    wanted = _normalized_absolute_path(str(expected))
    return actual is not None and wanted is not None and actual == wanted


def _vipercapture_process_kind(
    cmdline: str,
    root_dir: Path = ROOT,
    venv_dir: Path | None = None,
) -> str | None:
    """Classify only argv forms launched by this exact installation."""
    argv = _split_process_command(cmdline)
    if not argv:
        return None

    root_dir = root_dir.absolute()
    venv_dir = (root_dir / ".venv") if venv_dir is None else venv_dir.absolute()
    launch_target = root_dir / "launch.py"
    gui_target = root_dir / "gui.py"

    script_index = 1
    script_kind: str | None = None
    if len(argv) > script_index and _same_process_path(argv[script_index], launch_target):
        script_kind = "launcher"
    elif len(argv) > script_index and _same_process_path(argv[script_index], gui_target):
        script_kind = "gui"
    if script_kind is not None:
        trailing = argv[script_index + 1:]
        if trailing and trailing[0].casefold() == "kill":
            return None
        if script_kind == "launcher" and any(arg.casefold() == "--gui" for arg in trailing):
            return "gui"
        return script_kind

    # ViperCapture starts uvicorn with the installation's venv interpreter and
    # this exact module sequence. Text in an unrelated --label is not enough.
    python_candidates = (
        venv_dir / "bin" / "python",
        venv_dir / "bin" / "python3",
        venv_dir / "Scripts" / "python.exe",
    )
    if (
        len(argv) >= 4
        and any(_same_process_path(argv[0], candidate) for candidate in python_candidates)
        and argv[1:4] == ["-m", "uvicorn", "vipercapture.main:app"]
    ):
        return "server"
    return None


def _matches_vipercapture_command(
    cmdline: str,
    root_dir: Path = ROOT,
    venv_dir: Path | None = None,
) -> bool:
    """True only for a ViperCapture argv belonging to this installation."""
    return _vipercapture_process_kind(cmdline, root_dir, venv_dir) is not None


def _installation_id(root_dir: Path = ROOT) -> str:
    normalized = os.path.normcase(os.path.abspath(root_dir))
    return _mod("hashlib").sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()


class ProcessIdentity:
    __slots__ = ("pid", "start_time", "command", "kind")

    def __init__(self, pid: int, start_time: int | str, command: str, kind: str) -> None:
        object.__setattr__(self, "pid", pid)
        object.__setattr__(self, "start_time", start_time)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "kind", kind)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ProcessIdentity is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProcessIdentity):
            return NotImplemented
        return (self.pid, self.start_time, self.command, self.kind) == (
            other.pid, other.start_time, other.command, other.kind
        )

    def __hash__(self) -> int:
        return hash((self.pid, self.start_time, self.command, self.kind))

    def __repr__(self) -> str:
        return (
            f"ProcessIdentity(pid={self.pid!r}, start_time={self.start_time!r}, "
            f"command={self.command!r}, kind={self.kind!r})"
        )


def _local_http_request(
    method: str,
    path: str,
    *,
    host: str,
    port: int,
    timeout: float,
    headers: dict[str, str],
    body: bytes | None = None,
) -> tuple[int, bytes]:
    """Send one direct local HTTP request without importing urllib/proxy stacks."""
    import http.client

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read(16 * 1024)
    finally:
        conn.close()


def _is_vipercapture_endpoint(
    host: str = HOST,
    port: int = PORT,
    timeout: float = 1.0,
    *,
    expected_pids: set[int] | None = None,
    root_dir: Path = ROOT,
) -> int | None:
    """Return the one PID claimed by a verified ViperCapture endpoint."""
    json = _mod("json")
    try:
        status, raw = _local_http_request(
            "GET",
            "/__vipercapture__/identity",
            host=host,
            port=port,
            timeout=timeout,
            headers={"User-Agent": "ViperCapture-Probe"},
        )
        if status != 200:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError, Exception):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if type(pid) is not int or pid <= 0:
        return None
    if not (
        data.get("service") == "vipercapture"
        and data.get("install_id") == _installation_id(root_dir)
        and (expected_pids is None or pid in expected_pids)
    ):
        return None
    return pid


def _has_matching_server_record(pid: int, root_dir: Path = ROOT) -> bool:
    """True when this installation wrote a live runtime record matching pid and start time."""
    if pid <= 0:
        return False
    started = _get_process_start_time(pid)
    if started is None:
        return False
    try:
        record = _mod("json").loads(_server_record_path(pid).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    return bool(
        isinstance(record, dict)
        and record.get("service") == "vipercapture"
        and record.get("install_id") == _installation_id(root_dir)
        and record.get("pid") == pid
        and record.get("start_time") == started
    )


def _classify_pid_kind(pid: int, command: str, root_dir: Path = ROOT) -> str | None:
    """Classify a process command, including Windows venvlauncher child uvicorn workers."""
    kind = _vipercapture_process_kind(command, root_dir=root_dir)
    if kind is not None:
        return kind
    if sys.platform == "win32":
        argv = _split_process_command(command)
        if len(argv) >= 4 and argv[1:4] == ["-m", "uvicorn", "vipercapture.main:app"]:
            parent_pid = _get_parent_pid(pid)
            if parent_pid is not None:
                parent_start = _get_process_start_time(parent_pid)
                child_start = _get_process_start_time(pid)
                start_order_ok = (
                    parent_start is None
                    or child_start is None
                    or (
                        isinstance(parent_start, int)
                        and isinstance(child_start, int)
                        and parent_start <= child_start
                    )
                )
                if start_order_ok:
                    parent_command = _get_pid_commandline(parent_pid)
                    if (
                        parent_command
                        and _vipercapture_process_kind(parent_command, root_dir=root_dir) == "server"
                    ):
                        return "server"
            if _has_matching_server_record(pid, root_dir=root_dir):
                return "server"
    return None


def _capture_process_identity(pid: int, root_dir: Path = ROOT) -> ProcessIdentity | None:
    """Atomically-enough bind a PID to its start time and exact command identity."""
    if pid <= 0:
        return None
    started_before = _get_process_start_time(pid)
    if started_before is None:
        return None
    command = _get_pid_commandline(pid)
    if not command:
        return None
    kind = _classify_pid_kind(pid, command, root_dir=root_dir)
    if kind is None:
        return None
    started_after = _get_process_start_time(pid)
    if started_after is None or started_after != started_before:
        return None
    return ProcessIdentity(pid, started_before, command, kind)


def _process_identity_matches(identity: ProcessIdentity, root_dir: Path = ROOT) -> bool:
    """Revalidate start time and exact command before acting on a PID."""
    started_before = _get_process_start_time(identity.pid)
    if started_before != identity.start_time:
        return False
    command = _get_pid_commandline(identity.pid)
    if not command:
        return False
    if _split_process_command(command) != _split_process_command(identity.command):
        return False
    if _classify_pid_kind(identity.pid, command, root_dir=root_dir) != identity.kind:
        return False
    return _get_process_start_time(identity.pid) == identity.start_time


def _find_vipercapture_candidate_pids(
    port: int = PORT,
    root_dir: Path = ROOT,
    host: str = HOST,
) -> set[int]:
    """Find candidate PIDs; callers must bind and revalidate their identities."""
    pids: set[int] = set()
    exclude_pids = _get_process_ancestors()

    # 1. Port lookup for active listening TCP server
    listening_pids: set[int] = set()
    if sys.platform == "win32":
        native_listeners = _windows_tcp_listener_pids(port)
        if native_listeners is not None:
            for pid in native_listeners:
                if pid > 0 and pid not in exclude_pids:
                    listening_pids.add(pid)
        else:
            subprocess = _mod("subprocess")
            try:
                out = subprocess.check_output(
                    ["netstat", "-ano", "-p", "tcp"],
                    text=True,
                    errors="ignore",
                    timeout=5,
                )
                for line in out.splitlines():
                    line = line.strip()
                    parts = line.split()
                    if len(parts) >= 5 and parts[0].upper() == "TCP" and "LISTENING" in line.upper():
                        try:
                            local_port = int(parts[1].rsplit(":", 1)[-1])
                            pid = int(parts[-1])
                            if local_port == port and pid > 0 and pid not in exclude_pids:
                                listening_pids.add(pid)
                        except ValueError:
                            pass
            except (OSError, subprocess.SubprocessError):
                pass
    elif sys.platform == "darwin":
        subprocess = _mod("subprocess")
        try:
            out = subprocess.check_output(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                text=True,
                errors="ignore",
                timeout=5,
            )
            for line in out.splitlines():
                try:
                    pid = int(line.strip())
                    if pid > 0 and pid not in exclude_pids:
                        listening_pids.add(pid)
                except ValueError:
                    pass
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        subprocess = _mod("subprocess")
        re = _mod("re")
        ss_ok = False
        try:
            out = subprocess.check_output(
                ["ss", "-lptn", f"sport = :{port}"],
                text=True,
                errors="ignore",
                timeout=5,
            )
            ss_ok = True
            for match in re.finditer(r"pid=(\d+)", out):
                pid = int(match.group(1))
                if pid > 0 and pid not in exclude_pids:
                    listening_pids.add(pid)
        except (OSError, subprocess.SubprocessError):
            pass
        if not listening_pids and (not ss_ok or port_open(host=host, port=port)):
            try:
                out = subprocess.check_output(
                    ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                    text=True,
                    errors="ignore",
                    timeout=5,
                )
                for line in out.splitlines():
                    try:
                        pid = int(line.strip())
                        if pid > 0 and pid not in exclude_pids:
                            listening_pids.add(pid)
                    except ValueError:
                        pass
            except (OSError, subprocess.SubprocessError):
                pass

    # Verify that the port owner is actually ViperCapture before accepting
    if listening_pids:
        endpoint_pid = _is_vipercapture_endpoint(
            host=host,
            port=port,
            expected_pids=listening_pids,
            root_dir=root_dir,
        )
        if endpoint_pid is not None:
            pids.add(endpoint_pid)
        # Every other listener must independently pass exact command matching.
        for pid in listening_pids - {endpoint_pid}:
            cmd = _get_pid_commandline(pid)
            if cmd and _classify_pid_kind(pid, cmd, root_dir=root_dir) is not None:
                pids.add(pid)

    # 2. Process inspection by command line for this specific installation
    if sys.platform == "win32":
        snapshot = _windows_process_snapshot()
        if snapshot is not None:
            for proc_id, _ppid, exe_name in snapshot:
                if proc_id in exclude_pids or proc_id in pids:
                    continue
                lower_name = exe_name.lower()
                if not (
                    lower_name in {"python.exe", "pythonw.exe", "py.exe", "vipercapture.exe"}
                    or lower_name.startswith("python3")
                ):
                    continue
                cmd = _get_pid_commandline(proc_id)
                if not cmd:
                    continue
                cmd_lower = cmd.lower()
                if (
                    "vipercapture" in cmd_lower
                    or "launch.py" in cmd_lower
                    or "gui.py" in cmd_lower
                ) and _classify_pid_kind(proc_id, cmd, root_dir=root_dir) is not None:
                    pids.add(proc_id)
        else:
            subprocess = _mod("subprocess")
            try:
                ps_script = (
                    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                    "Get-CimInstance Win32_Process | ForEach-Object { "
                    "$cmd = $_.CommandLine; "
                    "if ($cmd -and ($cmd -match 'vipercapture' -or $cmd -match 'launch\\.py' -or $cmd -match 'gui\\.py')) { "
                    'Write-Output "$($_.ProcessId)|$($cmd)" '
                    "} }"
                )
                out = subprocess.check_output(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                for line in out.splitlines():
                    line = line.strip()
                    if not line or "|" not in line:
                        continue
                    parts = line.split("|", 1)
                    try:
                        proc_id = int(parts[0])
                        cmd = parts[1] if len(parts) > 1 else ""
                        if proc_id not in exclude_pids and _classify_pid_kind(proc_id, cmd, root_dir=root_dir) is not None:
                            pids.add(proc_id)
                    except ValueError:
                        pass
            except (OSError, subprocess.SubprocessError):
                pass

    elif sys.platform == "darwin":
        subprocess = _mod("subprocess")
        try:
            out = subprocess.check_output(
                ["ps", "-axo", "pid,command"],
                text=True,
                errors="ignore",
                timeout=5,
            )
            for line in out.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) >= 2:
                    try:
                        proc_id = int(parts[0])
                        cmd = parts[1]
                        if proc_id not in exclude_pids and _matches_vipercapture_command(cmd, root_dir=root_dir):
                            pids.add(proc_id)
                    except ValueError:
                        pass
        except (OSError, subprocess.SubprocessError):
            pass

    else:
        proc_dir = Path("/proc")
        if proc_dir.is_dir():
            shlex = _mod("shlex")
            try:
                for entry in proc_dir.iterdir():
                    if entry.name.isdigit():
                        proc_id = int(entry.name)
                        if proc_id in exclude_pids:
                            continue
                        try:
                            cmdline_bytes = (entry / "cmdline").read_bytes()
                            if not cmdline_bytes:
                                continue
                            lowered_bytes = cmdline_bytes.lower()
                            if (
                                b"vipercapture" not in lowered_bytes
                                and b"launch.py" not in lowered_bytes
                                and b"gui.py" not in lowered_bytes
                            ):
                                continue
                            argv = [
                                item
                                for item in cmdline_bytes.decode("utf-8", errors="replace").split("\x00")
                                if item
                            ]
                            cmdline = shlex.join(argv)
                            if _matches_vipercapture_command(cmdline, root_dir=root_dir):
                                pids.add(proc_id)
                        except (OSError, UnicodeError):
                            continue
            except OSError:
                pass

    pids.difference_update(exclude_pids)
    return pids


def find_vipercapture_processes(
    port: int = PORT,
    root_dir: Path = ROOT,
    host: str = HOST,
) -> dict[int, ProcessIdentity]:
    """Return identity-bound ViperCapture targets safe to revalidate."""
    identities: dict[int, ProcessIdentity] = {}
    for pid in _find_vipercapture_candidate_pids(port=port, root_dir=root_dir, host=host):
        identity = _capture_process_identity(pid, root_dir=root_dir)
        if identity is not None:
            identities[pid] = identity
    return identities


def find_vipercapture_pids(
    port: int = PORT,
    root_dir: Path = ROOT,
    host: str = HOST,
) -> set[int]:
    """Compatibility wrapper returning PIDs from identity-bound discovery."""
    return set(find_vipercapture_processes(port=port, root_dir=root_dir, host=host))


def _get_pid_commandline(pid: int) -> str | None:
    """Best-effort command line lookup for a single PID."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        handled, cmdline = _windows_pid_commandline(pid)
        if handled:
            return cmdline
        subprocess = _mod("subprocess")
        try:
            ps_script = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine"
            )
            out = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            return out.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    elif sys.platform == "darwin":
        subprocess = _mod("subprocess")
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="],
                text=True,
                errors="ignore",
                timeout=5,
            )
            return out.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    else:
        try:
            cmd_file = Path(f"/proc/{pid}/cmdline")
            if cmd_file.is_file():
                argv = [
                    item
                    for item in cmd_file.read_bytes().decode("utf-8", errors="replace").split("\x00")
                    if item
                ]
                return _mod("shlex").join(argv) or None
        except OSError:
            pass
    return None


def _shutdown_request_path(pid: int | None = None) -> Path:
    target_pid = os.getpid() if pid is None else pid
    return Path.home() / ".vipercapture" / "runtime" / f"shutdown-{target_pid}"


def _request_application_shutdown(pid: int) -> bool:
    """Ask an interactive ViperCapture process to unwind through its cleanup."""
    try:
        path = _shutdown_request_path(pid)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text("stop\n", encoding="ascii")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def application_shutdown_requested(*, consume: bool = True) -> bool:
    """Return whether this process received a cross-platform shutdown request."""
    path = _shutdown_request_path()
    try:
        requested = path.is_file()
        if requested and consume:
            path.unlink(missing_ok=True)
        return requested
    except OSError:
        return False


def _clear_application_shutdown_request(pid: int | None = None) -> None:
    try:
        _shutdown_request_path(pid).unlink(missing_ok=True)
    except OSError:
        pass


def shutdown_grace_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Grace period before force-kill; configurable without allowing extremes."""
    env = os.environ if environ is None else environ
    try:
        value = float(env.get("VIPERCAPTURE_KILL_GRACE_SECONDS", "30"))
    except ValueError:
        value = 30.0
    return min(300.0, max(5.0, value))


def _server_record_path(pid: int) -> Path:
    return Path.home() / ".vipercapture" / "runtime" / f"server-{pid}.json"


def _load_local_env_into(env: dict[str, str], root_dir: Path | None = None) -> None:
    """Load machine-only KEY=VALUE entries from .env.local without overriding existing env."""
    path = (root_dir if root_dir is not None else ROOT) / ".env.local"
    try:
        if not path.is_file():
            return
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return
    re_mod = _mod("re")
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if separator and re_mod.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            env.setdefault(key, value.strip().strip("\"'"))


def _new_server_environment() -> tuple[dict[str, str], str]:
    token = _mod("secrets").token_urlsafe(32)
    env = dict(os.environ)
    _load_local_env_into(env)
    env["VIPERCAPTURE_SHUTDOWN_TOKEN"] = token
    return env, token


def _prune_stale_server_records(root_dir: Path = ROOT) -> None:
    """Best-effort cleanup of runtime records belonging to exited processes of this install."""
    runtime_dir = Path.home() / ".vipercapture" / "runtime"
    try:
        if not runtime_dir.is_dir():
            return
        candidates = list(runtime_dir.glob("server-*.json"))
    except OSError:
        return
    if not candidates:
        return
    json = _mod("json")
    install_id = _installation_id(root_dir)
    for path in candidates:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(record, dict) or record.get("install_id") != install_id:
            continue
        pid = record.get("pid")
        start_time = record.get("start_time")
        if type(pid) is not int or pid <= 0:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        alive = _is_process_alive(pid)
        if not alive:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        current_start = _get_process_start_time(pid)
        if current_start is not None and current_start != start_time:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _record_server_process(pid: int, token: str, port: int = PORT) -> bool:
    _prune_stale_server_records()
    started = _get_process_start_time(pid)
    if started is None:
        return False
    record = {
        "service": "vipercapture",
        "install_id": _installation_id(),
        "pid": pid,
        "port": port,
        "start_time": started,
        "token": token,
    }
    try:
        path = _server_record_path(pid)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(_mod("json").dumps(record), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
        return True
    except OSError:
        return False


def _record_endpoint_server_process(
    launcher_pid: int,
    token: str,
    port: int = PORT,
    *,
    host: str = HOST,
) -> int | None:
    """Record the listening worker PID when a Windows venvlauncher spawns a child."""
    endpoint_pid = _is_vipercapture_endpoint(host=host, port=port)
    if endpoint_pid is None:
        return None
    if endpoint_pid != launcher_pid and _is_descendant_pid(endpoint_pid, launcher_pid):
        _record_server_process(endpoint_pid, token, port)
        return endpoint_pid
    return endpoint_pid if endpoint_pid == launcher_pid else None


def _clear_server_record(pid: int) -> None:
    try:
        _server_record_path(pid).unlink(missing_ok=True)
    except OSError:
        pass


def _request_server_shutdown(
    identity: ProcessIdentity,
    *,
    host: str = HOST,
    port: int = PORT,
    timeout: float = 2.0,
) -> bool:
    """Use the per-process secret to ask Uvicorn to run application cleanup."""
    json = _mod("json")
    try:
        record = json.loads(_server_record_path(identity.pid).read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return False
        if not (
            record.get("service") == "vipercapture"
            and record.get("install_id") == _installation_id()
            and record.get("pid") == identity.pid
            and record.get("port") == port
            and record.get("start_time") == identity.start_time
            and isinstance(record.get("token"), str)
            and record["token"]
        ):
            return False
        req_headers = {
            "Content-Type": "application/json",
            "X-ViperCapture-Shutdown-Token": record["token"],
        }
        status, raw = _local_http_request(
            "POST",
            "/__vipercapture__/shutdown",
            host=host,
            port=port,
            timeout=timeout,
            headers=req_headers,
            body=b"{}",
        )
        if status != 200:
            return False
        payload = json.loads(raw.decode("utf-8"))
        return bool(
            isinstance(payload, dict)
            and payload.get("stopping") is True
            and payload.get("pid") == identity.pid
        )
    except (OSError, ValueError, UnicodeError, Exception):
        return False


def _get_posix_process_state(pid: int) -> str | None:
    """Return the POSIX process state, including ``Z`` for an unreaped child."""
    if pid <= 0 or sys.platform == "win32":
        return None
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # The command is parenthesized and may itself contain spaces or ``)``.
            closing_paren = stat.rfind(")")
            fields = stat[closing_paren + 1 :].split() if closing_paren >= 0 else []
            if fields:
                return fields[0]
        except (OSError, UnicodeError):
            pass
    subprocess = _mod("subprocess")
    try:
        output = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "stat="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return output.split()[0] if output else None


def _is_process_alive(pid: int) -> bool:
    """True if process exists and is alive."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                err = kernel32.GetLastError()
                return err == 5  # ERROR_ACCESS_DENIED means process exists
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code)):
                kernel32.CloseHandle(h)
                return exit_code.value == 259  # STILL_ACTIVE
            kernel32.CloseHandle(h)
            return False
        except Exception:
            return False
    else:
        # kill(pid, 0) reports zombies as existing. They have already exited and
        # cannot respond to another signal; their parent only needs to reap them.
        state = _get_posix_process_state(pid)
        if state and state.upper().startswith("Z"):
            return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False


def _wait_for_process_exit(identity: ProcessIdentity, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_process_alive(identity.pid):
            _clear_application_shutdown_request(identity.pid)
            _clear_server_record(identity.pid)
            return True
        time.sleep(0.02)
    if not _is_process_alive(identity.pid):
        _clear_application_shutdown_request(identity.pid)
        _clear_server_record(identity.pid)
        return True
    return False


def _terminate_process(
    identity: ProcessIdentity,
    *,
    root_dir: Path = ROOT,
    host: str = HOST,
    port: int = PORT,
) -> tuple[bool, str | None]:
    """Terminate one identity-bound target, refusing changed or unverifiable PIDs."""
    if not _is_process_alive(identity.pid):
        return True, None
    if identity.kind not in {"server", "launcher", "gui"}:
        return False, "unrecognized_process_kind"
    if not _process_identity_matches(identity, root_dir=root_dir):
        return False, "process_identity_changed"

    server_handshake = False
    if identity.kind == "server":
        server_handshake = _request_server_shutdown(identity, host=host, port=port)
        if server_handshake:
            if _wait_for_process_exit(identity, shutdown_grace_seconds()):
                return True, None
            if not _process_identity_matches(identity, root_dir=root_dir):
                return False, "process_identity_changed"
    elif _request_application_shutdown(identity.pid):
        if _wait_for_process_exit(identity, 3.0):
            return True, None
        if not _process_identity_matches(identity, root_dir=root_dir):
            return False, "process_identity_changed"

    # Older servers without a runtime record and interactive processes that did
    # not acknowledge their marker still receive an ordinary termination. A
    # server that acknowledged the HTTP handshake already consumed its entire
    # configurable grace window and proceeds directly to verified escalation.
    if not server_handshake:
        if sys.platform == "win32":
            _mod("subprocess").run(
                ["taskkill", "/PID", str(identity.pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.kill(identity.pid, getattr(_mod("signal"), "SIGTERM", 15))
            except PermissionError:
                return False, "access_denied"
            except OSError:
                pass

        grace = shutdown_grace_seconds() if identity.kind == "server" else 3.0
        if _wait_for_process_exit(identity, grace):
            return True, None
        if not _process_identity_matches(identity, root_dir=root_dir):
            return False, "process_identity_changed"

    # Force termination only after the exact command and start time still match.
    if sys.platform == "win32":
        proc = _mod("subprocess").run(
            ["taskkill", "/F", "/PID", str(identity.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        time.sleep(0.1)
        if not _is_process_alive(identity.pid):
            _clear_application_shutdown_request(identity.pid)
            _clear_server_record(identity.pid)
            return True, None
        combined = f"{proc.stdout} {proc.stderr}".lower()
        if "access is denied" in combined or proc.returncode == 5:
            return False, "access_denied"
        err = proc.stderr.strip() or proc.stdout.strip()
        return False, err or f"taskkill failed with code {proc.returncode}"
    try:
        os.kill(identity.pid, getattr(_mod("signal"), "SIGKILL", 9))
    except PermissionError:
        return False, "access_denied"
    except OSError:
        pass
    time.sleep(0.1)
    if not _is_process_alive(identity.pid):
        _clear_application_shutdown_request(identity.pid)
        _clear_server_record(identity.pid)
        return True, None
    return False, "survived_sigkill"


def kill_all_instances(port: int = PORT) -> int:
    """Terminate all running ViperCapture instances with orderly shutdown and verified escalation."""
    print()
    print("  ViperCapture")
    print("  ------------")
    print()

    processes = find_vipercapture_processes(port=port)
    if not processes and not port_open(port=port):
        print("  No running instances of ViperCapture found.")
        return 0

    print("  Stopping running ViperCapture instance(s)...")

    killed: list[int] = []
    permission_errors: list[int] = []
    failed_pids: dict[int, str] = {}

    priority = {"server": 0, "launcher": 1, "gui": 1}
    has_server = any(item.kind == "server" for item in processes.values())
    active_server_pid = _is_vipercapture_endpoint(port=port) if has_server else None
    parent_map = _windows_parent_map() if sys.platform == "win32" else {}

    def _ancestor_depth(pid: int) -> int:
        depth = 0
        seen = {pid}
        cur = pid
        while cur in parent_map and parent_map[cur] > 4:
            cur = parent_map[cur]
            if cur in seen:
                break
            seen.add(cur)
            if cur in processes:
                depth += 1
        return depth

    ordered = sorted(
        processes.values(),
        key=lambda item: (
            priority[item.kind],
            0 if item.pid == active_server_pid else 1,
            -_ancestor_depth(item.pid),
            item.pid,
        ),
    )

    for identity in ordered:
        pid = identity.pid
        success, reason = _terminate_process(identity, port=port)
        if success:
            killed.append(pid)
        elif reason == "access_denied":
            permission_errors.append(pid)
        else:
            failed_pids[pid] = reason or "unknown error"

    _prune_stale_server_records()

    # Wait briefly for port to close if listening server was targeted
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and port_open(port=port):
        time.sleep(0.03)

    if killed:
        pids_str = ", ".join(str(p) for p in killed)
        print(f"  Stopped {len(killed)} ViperCapture process(es): {pids_str}.")
    if permission_errors:
        err_str = ", ".join(str(p) for p in permission_errors)
        print(f"  Access was denied for PID(s): {err_str}.")
        print("  If ViperCapture was started as Administrator or another user, run 'vipercapture kill' from an elevated terminal.")
    if failed_pids:
        for pid, msg in sorted(failed_pids.items()):
            print(f"  Failed to stop PID {pid}: {msg}")

    # Port still open or processes failed -> return failure
    has_failure = bool(permission_errors or failed_pids)
    if port_open(port=port):
        print(f"  Warning: Port {port} is still in use.")
        return 1

    if has_failure:
        return 1

    print("  All ViperCapture instances stopped.")
    return 0


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
            if application_shutdown_requested():
                return
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
        if application_shutdown_requested():
            return
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
    worker_pid: int | None = None,
    host: str = HOST,
    port: int = PORT,
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
    if host != HOST or port != PORT:
        stop_server(server, announce=announce, worker_pid=worker_pid, host=host, port=port)
    else:
        stop_server(server, announce=announce, worker_pid=worker_pid)
    return exited


def present_running_server(
    server: subprocess.Popen[object],
    log_path: Path,
    *,
    worker_pid: int | None = None,
    url: str = URL,
    host: str = HOST,
    port: int = PORT,
) -> None:
    """Full-screen status here, request log in another window of this terminal."""
    screen: StatusScreen | None = None
    custom_bind = host != HOST or port != PORT
    # Alternate-screen and image escapes belong on a terminal. Redirected
    # stdout (vipercapture > startup.log) keeps the normal banner.
    if sys.stdin.isatty() and sys.stdout.isatty() and sys.platform != "win32":
        screen = StatusScreen(url)
    if screen is None:
        print(ready_banner(url) if url != URL else ready_banner())
        if not open_request_window(log_path, os.getpid()):
            print(f"  Requests: {log_path}")
        wait_kwargs: dict[str, object] = {}
        if worker_pid is not None:
            wait_kwargs["worker_pid"] = worker_pid
        if custom_bind:
            wait_kwargs["host"] = host
            wait_kwargs["port"] = port
        wait_for_shutdown(server, **wait_kwargs)  # type: ignore[arg-type]
        return
    try:
        screen.enter()
        if not open_request_window(log_path, os.getpid()):
            screen.note = f"Requests: {log_path}"
            screen.draw()
        wait_kwargs = {"on_resize": screen.draw, "announce": False}
        if worker_pid is not None:
            wait_kwargs["worker_pid"] = worker_pid
        if custom_bind:
            wait_kwargs["host"] = host
            wait_kwargs["port"] = port
        exited = wait_for_shutdown(server, **wait_kwargs)  # type: ignore[arg-type]
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
    return _mod("hashlib").md5(req.read_bytes()).hexdigest() if req.exists() else ""


def find_uv(*, home: Path | None = None) -> str | None:
    """Return the uv executable when it should be used, otherwise None."""
    flag = os.environ.get("VIPERCAPTURE_USE_UV", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None
    found = _mod("shutil").which("uv")
    if found:
        return found
    base = Path.home() if home is None else home
    name = "uv.exe" if os.name == "nt" else "uv"
    for candidate in (base / ".local" / "bin" / name, base / ".cargo" / "bin" / name):
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate)
    return None


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
                "--compile-bytecode",
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
    return sys_platform == "darwin" and (machine or _mod("platform").machine()) == "x86_64"


def parse_rustc_version(text: str) -> tuple[int, int, int] | None:
    match = _mod("re").search(r"rustc\s+(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _run_command(
    cmd: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]],
    timeout: int = 15,
) -> subprocess.CompletedProcess[str] | None:
    subprocess = _mod("subprocess")
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
    re = _mod("re")
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
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, object]:
    """Discover a working compiler, rustc+cargo, and non-Apple OpenSSL 3 prefix."""
    if which is None:
        which = _mod("shutil").which
    if run is None:
        run = _mod("subprocess").run
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
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """
    On Intel Macs, refuse to pip-install until a source build can succeed.
    Returns missing-tool messages (empty when ready). Exits from ensure_deps.
    """
    if not is_intel_macos(sys_platform, machine):
        return []
    if which is None:
        which = _mod("shutil").which
    if run is None:
        run = _mod("subprocess").run
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
    subprocess = _mod("subprocess")
    result = subprocess.run(
        [python, "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _bootstrap_venv_pip(python: str) -> bool:
    """Bootstrap pip inside a venv created without it (for example by uv venv)."""
    subprocess = _mod("subprocess")
    result = subprocess.run(
        [python, "-m", "ensurepip", "--upgrade"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0 and _venv_has_pip(python)


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

def _acquire_venv_lock(lock_path: Path, timeout: float = 120.0) -> int | None:
    """Serialize concurrent .venv creation per installation."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            return fd
        except FileExistsError:
            try:
                holder = int(lock_path.read_text(encoding="ascii").strip())
                if holder > 0 and not _is_process_alive(holder):
                    lock_path.unlink(missing_ok=True)
                    continue
            except (OSError, ValueError):
                try:
                    if time.time() - lock_path.stat().st_mtime > timeout:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)
        except OSError:
            return None


def _release_venv_lock(lock_path: Path, fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_venv() -> None:
    """
    If the venv doesn't exist yet, create it.
    If we're not running from the venv Python, re-launch with it so
    all subsequent imports and subprocess calls use the right Python.
    """
    venv_dir = ROOT / ".venv"
    if not VENV_PYTHON.exists():
        lock_path = ROOT / ".venv.lock"
        lock_fd = _acquire_venv_lock(lock_path)
        try:
            if not VENV_PYTHON.exists():
                existed_before = venv_dir.exists()
                uv = find_uv()
                installer = "uv" if uv else "venv"
                print(f"  [1/3] Creating Python environment with {installer} (first run only)...")
                command = venv_command(sys.executable, venv_dir, uv)
                result = _mod("subprocess").run([str(c) for c in command])
                if result.returncode != 0:
                    if not existed_before:
                        _mod("shutil").rmtree(venv_dir, ignore_errors=True)
                    print(f"\n  ERROR ({installer} venv): command exited with code {result.returncode}")
                    print(f"  Command: {' '.join(str(c) for c in command)}")
                    if VENV_PYTHON.exists():
                        print(f"  Note: incomplete environment files remain in {venv_dir}; delete that folder before rerunning.")
                    if not uv:
                        print(
                            "  Install uv (https://docs.astral.sh/uv/) or ensure Python's "
                            "venv/ensurepip module is installed, then rerun."
                        )
                    wait_and_exit(1)
        finally:
            _release_venv_lock(lock_path, lock_fd)

    if not in_project_venv():
        # Hand off to the venv Python. Pass the venv path itself without
        # resolving symlinks. On POSIX, replace the current process in-place
        # to avoid keeping two Python interpreters alive.
        argv = [str(VENV_PYTHON), __file__, *sys.argv[1:]]
        if os.name != "nt" and "subprocess" not in globals():
            os.execv(str(VENV_PYTHON), argv)
        result = _mod("subprocess").run(argv)
        sys.exit(result.returncode)


def ensure_deps() -> None:
    """
    Install packages from requirements.txt.
    Skipped on subsequent runs unless requirements.txt has changed.
    Prefers uv when available; falls back to pip.
    """
    current_hash = _req_hash()
    if DEPS_STAMP.exists() and DEPS_STAMP.read_text().strip() == current_hash:
        print("  [2/3] Python packages already up to date - skipping.")
        return

    uv = find_uv()
    # Always the venv interpreter path, never sys.executable. On Arch that
    # value is /usr/bin/python3 even when the venv symlink points at it.
    python = str(VENV_PYTHON)
    if uv:
        print("  [2/3] Installing Python packages with uv...")
    else:
        if not _venv_has_pip(python):
            print("  [2/3] Bootstrapping pip in .venv...")
            if not _bootstrap_venv_pip(python):
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


def _playwright_version() -> str:
    try:
        from playwright._repo_version import version as repo_version

        if isinstance(repo_version, str) and repo_version:
            return repo_version
    except Exception:
        pass
    meta_version = globals().get("version")
    if meta_version is None:
        from importlib.metadata import version as meta_version
    return meta_version("playwright")


def ensure_playwright() -> None:
    """
    Install Playwright's Chromium, Firefox, and WebKit browsers.
    Skipped when the installed browser matches the Playwright package version.
    """
    playwright_stamp = f"{_playwright_version()}:chromium,firefox,webkit"
    if (
        PLAYWRIGHT_STAMP.exists()
        and PLAYWRIGHT_STAMP.read_text().strip() == playwright_stamp
    ):
        print("  [3/3] Playwright browsers already installed - skipping.")
        return

    print("  [3/3] Installing Playwright browsers...")
    has_apt = _mod("shutil").which("apt-get") is not None
    command = playwright_install_command(sys.executable, has_apt=has_apt)
    if sys.platform.startswith("linux") and not has_apt:
        print("  This distro does not use apt, so system packages are left alone.")
    run(*command, "chromium", "firefox", "webkit", label="playwright install")
    PLAYWRIGHT_STAMP.write_text(playwright_stamp)


# ── Main ──────────────────────────────────────────────────────

LAUNCH_HELP = """\
usage: vipercapture [--one-window] [--host HOST] [--port PORT]
       vipercapture --version | version [--json]
       vipercapture capture URL [URL ...] --output FILE [options]
       vipercapture status | logs | gpu | preset | diff | baseline
       vipercapture bulk | job | schedule | profile | sign | verify | admin
       vipercapture completion bash|zsh|fish|powershell
       vipercapture update [--check]
       vipercapture kill
       vipercapture --gui [--viewport WIDTH HEIGHT]

Start ViperCapture in this terminal. Ghostty and Kitty open request
logs in another window and show a full-screen status view.

Launcher options:
  --one-window   Keep the status and request log in this terminal.
                 Use this over SSH, or anywhere a second window is wrong.
  --host HOST    Bind address for the server (default: 127.0.0.1).
  --port PORT    TCP port for the server (default: 8000).
  --version, -V  Print the ViperCapture version and exit.
  --gui          Open a full-screen capture menu in this terminal.
                 Type a website link, press Tab to switch png, gif, and
                 mp4, and press Ctrl+P to switch between the full page and
                 the viewport. Nothing else is opened. --GUI is accepted too.
  --viewport     With --gui, set the page size in pixels. The default
                 is 1920 1080.
  -h, --help     Show this help.

Commands (run `vipercapture <command> --help` for details):
  capture        Render page(s) and save file(s). The saved path is printed
                 on stdout. vipercapture capture --help lists a flag for
                 each major API field. The same flags work on Linux, macOS,
                 and Windows.
  version        Print the installed ViperCapture version.
  status         Check server health, readiness, GPU, engines, and async jobs.
  logs           Read or tail ~/.vipercapture/requests.log.
  gpu            Show or switch local GPU mode (status, off, auto).
  preset         Manage saved local presets (list, show, save, delete).
  diff           Compare two images via /v1/diff and save a visual-diff ZIP.
  baseline       Manage visual baselines (save, list, delete, compare).
  bulk           Submit batch async render jobs to /v1/jobs/bulk.
  job            Inspect, cancel, or download async jobs (/v1/jobs).
  schedule       Manage cron schedules (create, list, get, update, delete).
  profile        Manage encrypted browser profiles (create, import, delete).
  sign           Generate an HMAC-signed render URL (/v1/signed-url).
  verify         Verify an Ed25519-certified capture bundle (--certify).
  admin          Manage control-plane projects, API keys, audit, and status.
  completion     Output shell completion for bash, zsh, fish, or powershell.
  update         Download the latest ViperCapture and replace this install.
                 The virtualenv and saved jobs stay in place. This uses
                 Python only, on Linux, macOS, and Windows.
  update --check Print whether an update is available, then exit.
                 Exit 10 means an update is available.
  kill           Stop all running instances of ViperCapture (server, GUI,
                 and background workers) on Windows, macOS, and Linux.

Windows always uses one window. --gui stays in this window on every OS.
"""

_CLI_SUBCOMMANDS = frozenset({
    "version",
    "status",
    "logs",
    "gpu",
    "preset",
    "diff",
    "baseline",
    "bulk",
    "job",
    "schedule",
    "profile",
    "sign",
    "verify",
    "admin",
    "completion",
})


class CliArgs:
    __slots__ = (
        "one_window",
        "update",
        "check",
        "gui",
        "kill",
        "capture",
        "capture_argv",
        "viewport",
        "host",
        "port",
        "version",
        "subcommand",
        "subcommand_argv",
    )

    def __init__(
        self,
        one_window: bool = False,
        update: bool = False,
        check: bool = False,
        gui: bool = False,
        kill: bool = False,
        capture: bool = False,
        capture_argv: tuple[str, ...] = (),
        viewport: tuple[int, int] | None = None,
        host: str | None = None,
        port: int | None = None,
        version: bool = False,
        subcommand: str | None = None,
        subcommand_argv: tuple[str, ...] = (),
    ) -> None:
        object.__setattr__(self, "one_window", one_window)
        object.__setattr__(self, "update", update)
        object.__setattr__(self, "check", check)
        object.__setattr__(self, "gui", gui)
        object.__setattr__(self, "kill", kill)
        object.__setattr__(self, "capture", capture)
        object.__setattr__(self, "capture_argv", capture_argv)
        object.__setattr__(self, "viewport", viewport)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "subcommand", subcommand)
        object.__setattr__(self, "subcommand_argv", subcommand_argv)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("CliArgs is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CliArgs):
            return NotImplemented
        return (
            self.one_window, self.update, self.check, self.gui,
            self.kill, self.capture, self.capture_argv, self.viewport,
            self.host, self.port, self.version, self.subcommand, self.subcommand_argv,
        ) == (
            other.one_window, other.update, other.check, other.gui,
            other.kill, other.capture, other.capture_argv, other.viewport,
            other.host, other.port, other.version, other.subcommand, other.subcommand_argv,
        )

    def __hash__(self) -> int:
        return hash((
            self.one_window, self.update, self.check, self.gui,
            self.kill, self.capture, self.capture_argv, self.viewport,
            self.host, self.port, self.version, self.subcommand, self.subcommand_argv,
        ))

    def __repr__(self) -> str:
        return (
            f"CliArgs(one_window={self.one_window!r}, update={self.update!r}, "
            f"check={self.check!r}, gui={self.gui!r}, kill={self.kill!r}, "
            f"capture={self.capture!r}, capture_argv={self.capture_argv!r}, "
            f"viewport={self.viewport!r}, host={self.host!r}, port={self.port!r}, "
            f"version={self.version!r}, subcommand={self.subcommand!r}, "
            f"subcommand_argv={self.subcommand_argv!r})"
        )


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


def _parse_port(token: str) -> int:
    if not token.isdigit() or len(token) > 5:
        _reject_arg(f"Invalid port: {token}")
    value = int(token)
    if value < 1 or value > 65_535:
        _reject_arg(f"Invalid port: {token}")
    return value


def _parse_host(token: str) -> str:
    value = token.strip()
    if not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        _reject_arg(f"Invalid host: {token}")
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
    if args and args[0] == "kill":
        for arg in args[1:]:
            if arg in {"-h", "--help"}:
                print(LAUNCH_HELP, end="")
                raise SystemExit(0)
            print(f"Unknown argument: {arg}\n{LAUNCH_HELP}", end="", file=sys.stderr)
            raise SystemExit(2)
        return CliArgs(kill=True)
    if args and args[0] == "capture":
        return CliArgs(capture=True, capture_argv=tuple(args[1:]))
    if args and args[0] in _CLI_SUBCOMMANDS:
        return CliArgs(
            version=(args[0] == "version"),
            subcommand=args[0],
            subcommand_argv=tuple(args[1:]),
        )
    if args and args[0] in {"--version", "-V"}:
        return CliArgs(
            version=True,
            subcommand="version",
            subcommand_argv=tuple(args[1:]),
        )
    gui = False
    one_window = False
    viewport: tuple[int, int] | None = None
    host: str | None = None
    port: int | None = None
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
        if arg == "--host":
            if index + 1 >= len(args):
                _reject_arg("--host needs a bind address.")
            host = _parse_host(args[index + 1])
            index += 2
            continue
        if arg.startswith("--host="):
            host = _parse_host(arg.split("=", 1)[1])
            index += 1
            continue
        if arg == "--port":
            if index + 1 >= len(args):
                _reject_arg("--port needs a port number.")
            port = _parse_port(args[index + 1])
            index += 2
            continue
        if arg.startswith("--port="):
            port = _parse_port(arg.split("=", 1)[1])
            index += 1
            continue
        _reject_arg(f"Unknown argument: {arg}")
    if viewport is not None and not gui:
        _reject_arg("--viewport is only used with --gui.")
    if gui and one_window:
        _reject_arg("--gui already keeps everything in this terminal.")
    if gui and (host is not None or port is not None):
        _reject_arg("--host and --port cannot be used with --gui.")
    return CliArgs(one_window=one_window, gui=gui, viewport=viewport, host=host, port=port)


def parse_launch_args(argv: list[str] | None = None) -> bool:
    """Return True when --one-window was passed. Help exits 0; unknown args exit 2."""
    return parse_cli(argv).one_window


def use_one_window(one_window: bool, platform_name: str | None = None) -> bool:
    """Windows has no companion terminal, so it always keeps logs in this console."""
    if platform_name is None:
        platform_name = sys.platform
    return one_window or platform_name == "win32"


def _configure_console_streams() -> None:
    """Prevent UnicodeEncodeError on legacy Windows OEM code pages (for example cp437)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def _can_import_cryptography() -> bool:
    try:
        import cryptography.hazmat.primitives.asymmetric.ed25519  # noqa: F401
        return True
    except ImportError:
        return False


def _is_loopback_bind_host(host: str) -> bool:
    cleaned = host.strip().lower().strip("[]")
    if cleaned in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return _mod("ipaddress").ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _format_bind_url(host: str, port: int) -> tuple[str, str]:
    """Return (connect_host, http_url) for a configured bind host and port."""
    if host == "0.0.0.0":
        connect_host = "127.0.0.1"
    elif host in {"::", "[::]"}:
        connect_host = "::1"
    else:
        connect_host = host.strip("[]")
    url_host = f"[{connect_host}]" if ":" in connect_host else connect_host
    return connect_host, f"http://{url_host}:{port}/"


def main() -> None:
    _configure_console_streams()
    # Parsed before the venv re-exec so --help, --version, fast subcommands,
    # and update do not install dependencies or start the server. ensure_venv
    # re-runs this file with the same arguments when the command needs the venv.
    cli = parse_cli()
    if cli.capture:
        global INTERACTIVE_FAILURES
        INTERACTIVE_FAILURES = False
        import vipercapture.capture_cli as capture_cli
        cap_argv = list(cli.capture_argv)
        skip_venv = capture_cli.wants_help(cap_argv) or (
            "--dry-run" in cap_argv and not VENV_PYTHON.exists()
        )
        if not skip_venv:
            ensure_venv()
        raise SystemExit(capture_cli.main(cap_argv))
    if cli.subcommand is not None or cli.version:
        INTERACTIVE_FAILURES = False
        sub_name = cli.subcommand or "version"
        sub_argv = list(cli.subcommand_argv)
        import vipercapture.capture_cli as capture_cli
        import vipercapture.cli_commands as cli_commands
        if not capture_cli.wants_help(sub_argv):
            if sub_name == "verify":
                if not _can_import_cryptography():
                    ensure_venv()
                    if not _can_import_cryptography():
                        ensure_deps()
            elif sub_name not in cli_commands.FAST_NO_VENV_SUBCOMMANDS and sub_name != "status":
                ensure_venv()
        raise SystemExit(cli_commands.main([sub_name, *sub_argv]))
    if cli.kill:
        raise SystemExit(kill_all_instances())
    if cli.update:
        import updater
        raise SystemExit(updater.run_update(ROOT, check_only=cli.check))
    _clear_application_shutdown_request()
    if cli.gui:
        ensure_venv()
        import gui
        viewport = cli.viewport if cli.viewport is not None else (1920, 1080)
        raise SystemExit(gui.run_capture_gui(viewport))
    one_window = use_one_window(cli.one_window)
    ensure_venv()    # may re-exec this script under the venv Python

    bind_host = cli.host if cli.host is not None else HOST
    bind_port = cli.port if cli.port is not None else PORT
    custom_bind = cli.host is not None or cli.port is not None
    connect_host, bind_url = (
        _format_bind_url(bind_host, bind_port) if custom_bind else (HOST, URL)
    )

    print()
    print("  ViperCapture")
    print("  ------------")
    print()

    # Server already running from a previous session? Open it immediately
    # without hashing requirements or importing Playwright first.
    if port_open(connect_host, bind_port) if custom_bind else port_open():
        show_terminal_logo()
        print(f"\n{listening_line(bind_url)}")
        print("  That server is already running. Opening it in your browser.")
        _mod("webbrowser").open(bind_url)
        return

    ensure_deps()
    ensure_playwright()

    # ── Start the server ────────────────────────────────────────
    print(f"\n  Starting server at {bind_url}\n")

    subprocess = _mod("subprocess")
    log_path = Path.home() / ".vipercapture" / "requests.log"
    log_handle = None
    log_tee_thread = None
    server_env, shutdown_token = _new_server_environment()
    if not _is_loopback_bind_host(bind_host) and not server_env.get("VIPERCAPTURE_ADMIN_TOKEN", "").strip():
        print(
            "  Refusing to bind non-loopback --host without VIPERCAPTURE_ADMIN_TOKEN set "
            "(the render API would be exposed without authentication).\n"
            "  Set VIPERCAPTURE_ADMIN_TOKEN in the environment or .env.local, or bind to 127.0.0.1.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    popen_kwargs: dict[str, object] = {"cwd": str(ROOT), "env": server_env}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    log_handle = open(log_fd, "w", encoding="utf-8", buffering=1)
    if not one_window:
        popen_kwargs["stdout"] = log_handle
        popen_kwargs["stderr"] = subprocess.STDOUT
    else:
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.STDOUT
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "vipercapture.main:app",
         "--host", bind_host, "--port", str(bind_port), "--ws", "none"],
        **popen_kwargs,
    )
    if one_window and getattr(server, "stdout", None) is not None:
        import threading

        def _tee_server_output(pipe, handle) -> None:
            try:
                for chunk in iter(pipe.readline, b""):
                    text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
                    try:
                        handle.write(text)
                        handle.flush()
                    except OSError:
                        pass
                    try:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                    except OSError:
                        pass
            except Exception:
                pass

        log_tee_thread = threading.Thread(
            target=_tee_server_output,
            args=(server.stdout, log_handle),
            daemon=True,
        )
        log_tee_thread.start()
    _record_server_process(server.pid, shutdown_token, bind_port)

    # ── Wait for port (50ms poll, 30s max) ──────────────────────
    # One-window mode inherits uvicorn's output, so progress dots would
    # land in the middle of those lines.
    if one_window:
        print("  Waiting for server to be ready...", flush=True)
    else:
        print("  Waiting for server to be ready...", end="", flush=True)
    ready = False
    worker_pid: int | None = None
    wait_start = time.monotonic()
    deadline = wait_start + 30.0
    next_dot = wait_start + 1.0
    while time.monotonic() < deadline:
        if port_open(connect_host, bind_port) if custom_bind else port_open():
            worker_pid = _record_endpoint_server_process(
                server.pid, shutdown_token, bind_port, host=connect_host
            )
            ready = True
            break
        if server.poll() is not None:
            # Server process already exited — don't wait the full 30s
            break
        now = time.monotonic()
        if not one_window and now >= next_dot:
            print(".", end="", flush=True)
            next_dot = now + 1.0
        time.sleep(0.05)
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
            stop_server(server, announce=False, host=connect_host, port=bind_port, worker_pid=worker_pid)
        if log_handle is not None:
            log_handle.close()
        wait_and_exit(1)

    # ── Open browser, then present this terminal ───────────────
    _mod("webbrowser").open(bind_url)
    try:
        if one_window:
            show_terminal_logo()
            print(ready_banner(bind_url))
            if wait_for_shutdown(server, worker_pid=worker_pid, host=connect_host, port=bind_port):
                print("\n  Server exited.")
        else:
            present_running_server(
                server,
                log_path,
                worker_pid=worker_pid,
                url=bind_url,
                host=connect_host,
                port=bind_port,
            )
    finally:
        if log_handle is not None:
            log_handle.close()


if __name__ == "__main__":
    main()
