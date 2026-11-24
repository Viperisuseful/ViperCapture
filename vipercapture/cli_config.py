"""Optional user configuration and version helpers for the ViperCapture CLI."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

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
        if isinstance(item, dict):
            continue
        key = str(raw_key).strip().replace("-", "_")
        if key == "viewport":
            if isinstance(item, list) and len(item) == 2 and all(type(v) is int for v in item):
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
            values[key] = item
        elif key in _FLOAT_KEYS:
            if type(item) not in {int, float}:
                raise ValueError(f"config.toml {raw_key} must be a number")
            values[key] = float(item)
    return values


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
    try:
        parsed = tomllib.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Invalid TOML in {target}: {exc}") from exc
    if not isinstance(parsed, dict):
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
