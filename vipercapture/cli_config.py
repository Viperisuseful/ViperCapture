"""Optional user configuration and version helpers for the ViperCapture CLI."""

from __future__ import annotations

import ast
import math
import os
import re
from pathlib import Path

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback (e.g. Ubuntu CI)
    try:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

import launch

DEFAULT_CONFIG_PATH = Path.home() / ".vipercapture" / "config.toml"

_STR_KEYS = frozenset({
    "server",
    "api_key",
    "admin_token",
    "signing_token",
    "format",
    "engine",
    "device",
    "color_scheme",
    "reduced_motion",
    "media",
    "locale",
    "timezone",
    "lazy_load",
    "profile",
    "consent",
    "pdf_mode",
    "paper",
    "orientation",
    "extract",
    "preset",
    "output_dir",
    "output_template",
    "user_agent",
    "proxy",
    "proxy_username",
    "proxy_bypass",
    "profile_id",
})
_BOOL_KEYS = frozenset({
    "full_page",
    "preserve_viewport_width",
    "cache",
    "wait_images",
    "javascript",
    "bypass_csp",
    "ignore_https_errors",
    "block_ads",
    "block_trackers",
    "block_chats",
    "block_newsletters",
    "stealth",
    "proceed_on_captcha",
    "transparent",
    "optimize_for_speed",
    "pdf_background",
    "pdf_tagged",
    "shadow_dom",
    "no_start",
    "quiet",
    "verbose",
    "json",
    "stop_server",
})
_INT_KEYS = frozenset({
    "delay_ms",
    "timeout_ms",
    "quality",
    "image_width",
    "image_height",
    "video_duration_ms",
    "fps",
    "bitrate_mbps",
    "poll_ms",
    "retries",
    "retry_delay_ms",
})
_FLOAT_KEYS = frozenset({
    "scale",
    "request_timeout",
})


def read_version(root: Path | None = None) -> str:
    base = launch.ROOT if root is None else root
    version_file = base / "VERSION"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
    return text or "0.0.0"


def config_path() -> Path:
    override = os.environ.get("VIPERCAPTURE_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vipercapture" / "config.toml"


def _normalize_section(raw: dict[str, object]) -> dict[str, object]:
    values: dict[str, object] = {}
    for raw_key, item in raw.items():
        key = str(raw_key).strip().replace("-", "_")
        is_known = key == "viewport" or key in _STR_KEYS or key in _BOOL_KEYS or key in _INT_KEYS or key in _FLOAT_KEYS
        if isinstance(item, dict) and not is_known:
            continue
        if key == "viewport":
            if (
                isinstance(item, list)
                and len(item) == 2
                and all(type(v) is int and 1 <= v <= 2_147_483_647 for v in item)
            ):
                values["viewport"] = [int(item[0]), int(item[1])]
            else:
                raise ValueError("config.toml viewport must be a two-integer array [width, height]")
            continue
        if key in _STR_KEYS:
            if not isinstance(item, str):
                raise ValueError(f"config.toml {raw_key} must be a string")
            values[key] = item
        elif key in _BOOL_KEYS:
            if type(item) is not bool:
                raise ValueError(f"config.toml {raw_key} must be a boolean")
            values[key] = item
        elif key in _INT_KEYS:
            if type(item) is not int:
                raise ValueError(f"config.toml {raw_key} must be an integer")
            if item < -2_147_483_647 or item > 2_147_483_647:
                raise ValueError(f"config.toml {raw_key} is out of integer range")
            values[key] = item
        elif key in _FLOAT_KEYS:
            if type(item) not in {int, float}:
                raise ValueError(f"config.toml {raw_key} must be a number")
            try:
                converted = float(item)
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"config.toml {raw_key} is out of numeric range") from exc
            if not math.isfinite(converted):
                raise ValueError(f"config.toml {raw_key} must be a finite number")
            values[key] = converted
    return values


def _strip_toml_comment(line: str) -> str:
    in_quote: str | None = None
    escaped = False
    for index, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_quote == '"' and ch == "\\":
            escaped = True
            continue
        if ch in {'"', "'"}:
            if in_quote is None:
                in_quote = ch
            elif in_quote == ch:
                in_quote = None
            continue
        if ch == "#" and in_quote is None:
            return line[:index]
    return line


def _parse_toml_scalar(raw: str) -> object:
    value = raw.strip()
    if not value:
        raise ValueError("missing value")
    if value == "true":
        return True
    if value == "false":
        return False
    if value.startswith('"') and value.endswith('"'):
        return ast.literal_eval(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        parts = [part.strip() for part in inner.split(",") if part.strip()]
        return [_parse_toml_scalar(part) for part in parts]
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+|\d+[eE][+-]?\d+|\d+\.\d*[eE][+-]?\d+)", value):
        return float(value)
    raise ValueError(f"unsupported TOML value {value!r}")


def _fallback_toml_loads(text: str) -> dict[str, object]:
    """Minimal TOML subset parser used only on Python <3.11 without tomli."""
    root: dict[str, object] = {}
    current: dict[str, object] = root
    raw_lines = text.splitlines()
    idx = 0
    while idx < len(raw_lines):
        line_no = idx + 1
        line = _strip_toml_comment(raw_lines[idx]).strip()
        idx += 1
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", section):
                raise ValueError(f"line {line_no}: unsupported section [{section}]")
            existing = root.get(section)
            if existing is None:
                existing = {}
                root[section] = existing
            elif not isinstance(existing, dict):
                raise ValueError(f"line {line_no}: section {section} conflicts with key")
            current = existing
            continue
        if "=" not in line:
            raise ValueError(f"line {line_no}: expected key = value")
        key_raw, val_raw = line.split("=", 1)
        key = key_raw.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError(f"line {line_no}: invalid key {key!r}")
        val_text = val_raw.strip()
        # Accumulate multiline arrays (e.g. viewport = [\n  1440,\n  900,\n])
        if val_text.startswith("[") and not val_text.endswith("]"):
            parts = [val_text]
            while idx < len(raw_lines):
                nxt = _strip_toml_comment(raw_lines[idx]).strip()
                idx += 1
                if not nxt:
                    continue
                parts.append(nxt)
                if nxt.endswith("]"):
                    break
            val_text = " ".join(parts)
            if not val_text.endswith("]"):
                raise ValueError(f"line {line_no}: unterminated array")
        try:
            current[key] = _parse_toml_scalar(val_text)
        except (ValueError, SyntaxError) as exc:
            raise ValueError(f"line {line_no}: {exc}") from exc
    return root


def _parse_toml(raw_bytes: bytes, target: Path) -> dict[str, object]:
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Invalid TOML in {target}: {exc}") from exc
    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid TOML in {target}: {exc}") from exc
    else:
        try:
            parsed = _fallback_toml_loads(text)
        except ValueError as exc:
            raise ValueError(f"Invalid TOML in {target}: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


def load_cli_config(path: Path | None = None) -> dict[str, object]:
    """Load optional ~/.vipercapture/config.toml defaults.

    Supports top-level keys (e.g. ``server``, ``api_key``) and a ``[capture]``
    table for capture-specific defaults.
    """
    target = config_path() if path is None else path
    if not target.is_file():
        return {}
    try:
        raw_bytes = target.read_bytes()
    except OSError as exc:
        raise ValueError(f"Could not read config file {target}: {exc}") from exc
    parsed = _parse_toml(raw_bytes, target)
    if not parsed:
        return {}
    merged = _normalize_section(parsed)
    capture_table = parsed.get("capture")
    if capture_table is not None:
        if not isinstance(capture_table, dict):
            raise ValueError(f"Invalid [capture] section in {target}")
        merged.update(_normalize_section(capture_table))
    return merged


def default_server_from_config(path: Path | None = None) -> str:
    try:
        cfg = load_cli_config(path)
    except ValueError:
        return f"http://{launch.HOST}:{launch.PORT}"
    server = cfg.get("server")
    if isinstance(server, str) and server.strip():
        return server.strip()
    return f"http://{launch.HOST}:{launch.PORT}"
