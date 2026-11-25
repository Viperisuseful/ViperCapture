"""Top-level CLI subcommands for ViperCapture (parity with vipercapture/main.py).

Every command shares the HTTP, auth, auto-start, and JSON-error conventions of
``vipercapture.capture_cli``: exit 0 is success, 2 is bad CLI arguments, and 1
is a runtime/API/verification error. Errors are emitted as one JSON line on stderr.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import hashlib
import io
import itertools
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import launch
from vipercapture import capture_cli
from vipercapture.capture_cli import (
    CaptureError,
    CaptureParser,
    _config_credential_for_server,
    _header_token,
    _json_line,
    _media_type,
    _read_json,
    _read_json_value,
    _read_text,
    add_render_option_groups,
    api_root,
    bearer_token,
    build_payload,
    derive_batch_idempotency_key,
    display_path,
    display_server,
    encode_multipart_form,
    ensure_server,
    fail,
    filename_from_headers,
    is_local_default,
    load_local_env,
    normalize_url,
    open_raw_response,
    open_response,
    open_response_with_retries,
    payload_to_preset_settings,
    public_payload,
    read_response_bytes,
    resolve_api_url,
    safe_filename,
    validate_payload,
    write_bytes_atomic,
    write_line,
    write_response,
)
from vipercapture.cli_config import default_server_from_config, load_cli_config, read_version

SUBCOMMANDS = (
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
)

FAST_NO_VENV_SUBCOMMANDS = frozenset({"version", "logs", "completion"})
_OPEN_JOBS = frozenset({"queued", "running"})
_ADMIN_SCOPES = ("render", "jobs", "schedules", "profiles", "baselines")
_MAX_VERIFY_MEMBER_BYTES = 512 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_SIG_BYTES = 4 * 1024
_MAX_DIFF_REPORT_BYTES = 64 * 1024
_BASELINE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _default_server() -> str:
    return default_server_from_config()


def _add_common_client_flags(
    parser: argparse.ArgumentParser,
    *,
    include_no_start: bool = True,
    include_api_key: bool = True,
    include_json: bool = True,
    include_timeout: bool = False,
) -> None:
    group = parser.add_argument_group("client")
    group.add_argument(
        "--server",
        default=_default_server(),
        help="API base URL (defaults to config.toml or local server).",
    )
    if include_api_key:
        group.add_argument(
            "--api-key",
            help="Bearer token. Otherwise VIPERCAPTURE_API_KEY or VIPERCAPTURE_ADMIN_TOKEN.",
        )
    if include_no_start:
        group.add_argument(
            "--no-start",
            action="store_true",
            help="Do not auto-start the local server.",
        )
    if include_timeout:
        group.add_argument(
            "--request-timeout",
            type=float,
            default=60.0,
            help="HTTP timeout in seconds.",
        )
    if include_json:
        group.add_argument("--json", action="store_true", help="Print JSON output.")


def _auth_headers(
    args: argparse.Namespace,
    *,
    started_server: bool = False,
    content_type: str | None = None,
    token_override: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "*/*"}
    if content_type:
        headers["Content-Type"] = content_type
    token = token_override
    if token is None:
        token = bearer_token(args, started_server=started_server)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update(extra)
    return headers


def _is_local_target(args: argparse.Namespace) -> bool:
    server = getattr(args, "server", None)
    return server is None or is_local_default(str(server))


def _load_local_env_for_target(args: argparse.Namespace) -> None:
    if _is_local_target(args):
        load_local_env()


def _admin_token(args: argparse.Namespace) -> str:
    explicit = getattr(args, "admin_token", None) or getattr(args, "api_key", None)
    if explicit is not None:
        return _header_token(explicit)
    if not getattr(args, "no_config", False):
        try:
            cfg = load_cli_config()
        except ValueError:
            cfg = {}
        cfg_admin = _config_credential_for_server(cfg, "admin_token", getattr(args, "server", None))
        if cfg_admin:
            return cfg_admin
    if _is_local_target(args):
        env_admin = _header_token(os.environ.get("VIPERCAPTURE_ADMIN_TOKEN", ""))
        if env_admin:
            return env_admin
    return ""


def _signing_token(args: argparse.Namespace) -> str:
    explicit_sign = getattr(args, "signing_token", None)
    if explicit_sign is not None and explicit_sign.strip():
        return _header_token(explicit_sign)
    explicit_api = getattr(args, "api_key", None)
    if explicit_api is not None and explicit_api.strip():
        return _header_token(explicit_api)
    if not getattr(args, "no_config", False):
        try:
            cfg = load_cli_config()
        except ValueError:
            cfg = {}
        cfg_sign = _config_credential_for_server(cfg, "signing_token", getattr(args, "server", None))
        if cfg_sign:
            return cfg_sign
    if _is_local_target(args):
        for env_name in ("VIPERCAPTURE_SIGNING_ADMIN_TOKEN", "VIPERCAPTURE_SIGNING_SECRET"):
            val = _header_token(os.environ.get(env_name, ""))
            if val:
                return val
    return bearer_token(args, started_server=False)


def _emit_json(data: object) -> None:
    write_line(sys.stdout, json.dumps(data, ensure_ascii=False, indent=2))


def _ensure_render_namespace(args: argparse.Namespace) -> argparse.Namespace:
    defaults = {
        "target": None,
        "targets": [],
        "url": None,
        "urls_file": None,
        "output": None,
        "output_dir": None,
        "output_template": None,
        "open_output": False,
        "copy_output": False,
        "webhook": None,
        "timezone": None,
        "no_wait": False,
        "async_job": False,
        "poll_ms": 500,
        "retries": 0,
        "retry_delay_ms": 500,
        "no_config": False,
        "dry_run": False,
        "quiet": False,
        "verbose": False,
        "stop_server": False,
        "request_id": None,
        "idempotency_key": None,
        "request_timeout": None,
    }
    for key, value in defaults.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    return args


def _has_render_flags(args: argparse.Namespace) -> bool:
    check_fields = (
        "url", "target", "html", "html_file", "markdown", "markdown_file",
        "base_url", "from_json", "preset", "format", "engine", "full_page", "viewport",
        "scale", "device", "selector", "clip", "preserve_viewport_width",
        "lazy_load", "profile", "cache", "cache_key", "named_viewport",
        "slice_height", "slice_overlap", "wait_selector", "wait_text",
        "wait_event", "wait_state", "wait_images", "delay_ms", "timeout_ms",
        "steps", "click", "fill", "hover", "press", "press_on", "select",
        "hide", "scroll", "scroll_to", "pause", "js", "js_file",
        "action_delay", "action_timeout", "action_wait", "action_wait_text",
        "color_scheme", "reduced_motion", "media", "locale", "timezone",
        "css", "css_file", "javascript", "user_agent", "header", "cookie",
        "cookie_file", "block_url", "block_resource", "geolocation",
        "geo_accuracy", "proxy", "proxy_username", "proxy_password",
        "proxy_bypass", "bypass_csp", "ignore_https_errors", "consent",
        "block_ads", "block_trackers", "block_chats", "block_newsletters",
        "assert_includes", "assert_excludes", "assert_request_failure",
        "fail_on", "stealth", "captcha", "captcha_solver", "captcha_timeout_ms",
        "proceed_on_captcha", "quality", "image_width", "image_height",
        "transparent", "optimize_for_speed", "thumbnail", "pdf_mode", "paper",
        "orientation", "pdf_background", "pdf_tagged", "pdf_margin",
        "pdf_header", "pdf_header_file", "pdf_footer", "pdf_footer_file",
        "page_ranges", "video_duration_ms", "fps", "bitrate_mbps",
        "video_scroll", "scroll_step", "scroll_delay_ms", "video_transparent",
        "extract", "shadow_dom", "element", "side", "diagnostics", "har",
        "trace", "warc", "include_console", "include_network", "deterministic",
        "timestamp_ms", "seed", "wait_for_fonts", "certify", "profile_id",
        "save_profile", "webhook",
    )
    return any(
        getattr(args, field, None) is not None and getattr(args, field, None) != []
        for field in check_fields
    )


def _read_binary_file(path_str: str, label: str = "file") -> tuple[str, bytes]:
    path = Path(path_str)
    if not path.is_file():
        raise ValueError(f"{label} not found: {path_str}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Could not read {label} {path_str}: {exc}") from exc
    if not data:
        raise ValueError(f"{label} is empty: {path_str}")
    return path.name, data


# ── version ───────────────────────────────────────────────────


def cmd_version(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture version",
        description="Print the installed ViperCapture version.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(argv)
    ver = read_version()
    if args.json:
        _emit_json({"ok": True, "version": ver, "python": sys.version.split()[0]})
    else:
        write_line(sys.stdout, f"ViperCapture {ver}")
    return 0


# ── status ────────────────────────────────────────────────────


def _probe_json_allow_error_body(url: str, headers: dict[str, str], timeout: float) -> tuple[int | None, dict[str, object] | None, str | None]:
    try:
        resp = open_response(url, method="GET", payload=None, headers=headers, timeout=timeout)
        try:
            return getattr(resp, "status", 200), _read_json(resp), None
        finally:
            resp.close()
    except CaptureError as exc:
        if exc.status is not None and isinstance(exc.body, dict):
            return exc.status, exc.body, None
        return exc.status, None, str(exc)


def cmd_status(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture status",
        description="Inspect ViperCapture server health, readiness, version, GPU, and async jobs.",
    )
    _add_common_client_flags(parser, include_no_start=False, include_api_key=False)
    args = parser.parse_args(argv)
    try:
        root = api_root(args.server)
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    parsed = urlsplit(root)
    host = parsed.hostname or launch.HOST
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    is_loopback = host.lower() in {"127.0.0.1", "localhost", "::1"}
    local_port_open: bool | None = None
    local_pid: int | None = None
    if is_loopback and parsed.scheme == "http":
        local_port_open = launch.port_open(host=host, port=port)
        if local_port_open:
            local_pid = launch._is_vipercapture_endpoint(host=host, port=port)

    # /health, /ready, and /app-config are public routes; never send ambient credentials.
    headers = {"Accept": "application/json"}
    health_status, health_doc, health_err = _probe_json_allow_error_body(root + "/health", headers, 5.0)
    running = health_status == 200 and isinstance(health_doc, dict)
    ready_status: int | None = None
    ready_doc: dict[str, object] | None = None
    cfg_status: int | None = None
    config_doc: dict[str, object] | None = None
    if running:
        ready_status, ready_doc, _ = _probe_json_allow_error_body(root + "/ready", headers, 5.0)
        cfg_status, config_doc, _ = _probe_json_allow_error_body(root + "/app-config", headers, 5.0)

    is_ready = bool(ready_status == 200 and isinstance(ready_doc, dict) and ready_doc.get("ready"))
    cli_ver = read_version()
    server_ver: str | None = None
    if cfg_status == 200 and isinstance(config_doc, dict) and isinstance(config_doc.get("version"), str) and config_doc["version"].strip():
        server_ver = config_doc["version"].strip()
    elif running:
        oa_status, oa_doc, _ = _probe_json_allow_error_body(root + "/openapi.json", headers, 5.0)
        if oa_status == 200 and isinstance(oa_doc, dict) and isinstance(oa_doc.get("info"), dict):
            oa_ver = oa_doc["info"].get("version")
            if isinstance(oa_ver, str) and oa_ver.strip():
                server_ver = oa_ver.strip()
    summary: dict[str, object] = {
        "ok": running,
        "server": display_server(root),
        "cli_version": cli_ver,
        "server_version": server_ver,
        "running": running,
        "healthy": running,
        "ready": is_ready,
        "pid": local_pid,
        "port_open": local_port_open,
        "health": health_doc,
        "readiness": ready_doc,
        "config": config_doc,
    }
    if not running and health_err:
        summary["error"] = health_err

    if args.json:
        _emit_json(summary)
        return 0 if running else 1

    write_line(sys.stdout, f"Server:      {display_server(root)}")
    write_line(sys.stdout, f"CLI Version: {cli_ver}")
    if server_ver:
        write_line(sys.stdout, f"Server Ver:  {server_ver}")
    if not running:
        write_line(sys.stdout, "Status:   stopped (unreachable)")
        return 1
    pid_note = f" (PID {local_pid})" if local_pid is not None else ""
    write_line(sys.stdout, f"Status:   running{pid_note}")
    write_line(sys.stdout, f"Ready:    {'yes' if is_ready else 'warming up'}")
    if isinstance(ready_doc, dict) and ready_doc.get("role"):
        write_line(sys.stdout, f"Role:     {ready_doc['role']}")
    if isinstance(config_doc, dict):
        gpu = config_doc.get("gpu")
        if isinstance(gpu, dict):
            hw = "hardware active" if gpu.get("hardware_active") else "software"
            write_line(sys.stdout, f"GPU:      {gpu.get('mode', 'off')} ({hw})")
        engines = config_doc.get("browser_engines")
        if isinstance(engines, list):
            write_line(sys.stdout, f"Engines:  {', '.join(str(e) for e in engines)}")
        async_cfg = config_doc.get("async_jobs")
        if isinstance(async_cfg, dict):
            enabled = "enabled" if async_cfg.get("enabled") else "disabled"
            workers = async_cfg.get("workers", 0)
            limit = async_cfg.get("queue_limit", 0)
            write_line(sys.stdout, f"Async:    {enabled} (workers={workers}, queue_limit={limit})")
        write_line(
            sys.stdout,
            f"Control:  {'enabled' if config_doc.get('control_plane') else 'disabled'}",
        )
    return 0


# ── logs ──────────────────────────────────────────────────────


def default_log_path() -> Path:
    override = os.environ.get("VIPERCAPTURE_LOG_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vipercapture" / "requests.log"


def cmd_logs(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture logs",
        description="Read or follow the local ViperCapture request log.",
    )
    parser.add_argument("-n", "--lines", type=int, default=50, help="Number of trailing lines to show (0 for all).")
    parser.add_argument("-f", "--follow", action="store_true", help="Follow new log lines until Ctrl+C.")
    parser.add_argument("--path", action="store_true", help="Print the log file path and exit.")
    args = parser.parse_args(argv)
    if args.lines < 0:
        return fail("--lines must be 0 or greater.", exit_code=2)

    log_path = default_log_path()
    if args.path:
        write_line(sys.stdout, str(log_path))
        return 0

    if not log_path.is_file():
        if not args.follow:
            return fail(f"Log file not found: {log_path}", exit_code=1)
        try:
            while not log_path.is_file():
                time.sleep(0.15)
        except KeyboardInterrupt:
            return 0

    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
            tail = lines if args.lines == 0 else lines[-args.lines:]
            for line in tail:
                sys.stdout.write(line)
            sys.stdout.flush()
            if not args.follow:
                return 0
            position = handle.tell()
            while True:
                try:
                    size = log_path.stat().st_size
                except OSError:
                    size = position
                if size < position:
                    handle.seek(0)
                    position = 0
                chunk = handle.readline()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    position = handle.tell()
                else:
                    time.sleep(0.15)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        return fail(f"Could not read log file: {exc}")


# ── gpu ───────────────────────────────────────────────────────


def cmd_gpu(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture gpu",
        description="Show or switch local GPU rendering mode (off, auto, status).",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=("status", "off", "auto"),
        help="GPU action: status (default), off, or auto.",
    )
    _add_common_client_flags(parser)
    args = parser.parse_args(argv)
    if args.mode != "status":
        _load_local_env_for_target(args)
    try:
        root = api_root(args.server)
    except ValueError as exc:
        return fail(str(exc), exit_code=2)
    try:
        started = ensure_server(args)
        if args.mode == "status":
            resp = open_response(
                root + "/app-config",
                method="GET",
                payload=None,
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            gpu = doc.get("gpu") if isinstance(doc.get("gpu"), dict) else {}
        else:
            resp = open_response(
                root + "/local/gpu-mode",
                method="POST",
                payload={"mode": args.mode},
                headers=_auth_headers(args, started_server=started, content_type="application/json"),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            gpu = doc.get("gpu") if isinstance(doc.get("gpu"), dict) else doc
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)

    if args.json:
        _emit_json({"ok": True, "gpu": gpu})
    else:
        hw = "yes" if gpu.get("hardware_active") else "no"
        mutable = "yes" if gpu.get("mutable") else "no"
        write_line(sys.stdout, f"GPU mode: {gpu.get('mode', 'unknown')} (hardware active: {hw}, mutable: {mutable})")
    return 0


# ── preset ────────────────────────────────────────────────────


def cmd_preset(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture preset",
        description="Manage saved local capture presets via /local/presets.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="List saved presets.")
    _add_common_client_flags(p_list, include_api_key=False)

    p_show = sub.add_parser("show", help="Show a saved preset.")
    p_show.add_argument("name", help="Preset name.")
    _add_common_client_flags(p_show, include_api_key=False)

    p_save = sub.add_parser("save", help="Save a preset from capture flags or --from-json.")
    p_save.add_argument("name", help="Preset name (1-40 characters).")
    add_render_option_groups(p_save)
    _add_common_client_flags(p_save, include_api_key=False)

    p_del = sub.add_parser("delete", help="Delete a saved preset.")
    p_del.add_argument("name", help="Preset name.")
    _add_common_client_flags(p_del, include_api_key=False)

    args = parser.parse_args(argv)
    load_local_env()
    try:
        root = api_root(args.server)
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    try:
        started = ensure_server(args)
        if args.action == "list":
            resp = open_response(
                root + "/local/presets",
                method="GET",
                payload=None,
                headers=_auth_headers(args, started_server=started),
                timeout=15.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            presets = doc.get("presets") if isinstance(doc.get("presets"), list) else []
            if args.json:
                safe_list = [
                    {
                        "name": item.get("name"),
                        "settings": public_payload(item["settings"]) if isinstance(item.get("settings"), dict) else {},
                    }
                    for item in presets
                    if isinstance(item, dict)
                ]
                _emit_json({"ok": True, "count": len(safe_list), "presets": safe_list})
            elif not presets:
                write_line(sys.stdout, "No presets saved.")
            else:
                for item in presets:
                    if isinstance(item, dict) and item.get("name"):
                        write_line(sys.stdout, str(item["name"]))
            return 0

        if args.action == "show":
            resp = open_response(
                root + "/local/presets",
                method="GET",
                payload=None,
                headers=_auth_headers(args, started_server=started),
                timeout=15.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            presets = doc.get("presets") if isinstance(doc.get("presets"), list) else []
            found = next((item for item in presets if isinstance(item, dict) and item.get("name") == args.name), None)
            if found is None:
                return fail(f"Preset {args.name!r} was not found.", code="preset_not_found", exit_code=1)
            safe_settings = public_payload(found["settings"]) if isinstance(found.get("settings"), dict) else {}
            if args.json:
                _emit_json({"ok": True, "name": found["name"], "settings": safe_settings})
            else:
                write_line(sys.stdout, json.dumps(safe_settings, ensure_ascii=False, indent=2))
            return 0

        if args.action == "save":
            name = args.name.strip()
            if not name or len(name) > 40:
                return fail("Preset name must be 1-40 characters.", exit_code=2)
            _ensure_render_namespace(args)
            try:
                settings = payload_to_preset_settings(build_payload(args, require_source=False))
            except ValueError as exc:
                return fail(str(exc), exit_code=2)
            resp = open_response(
                root + "/local/presets",
                method="POST",
                payload={"name": name, "settings": settings},
                headers=_auth_headers(args, started_server=started, content_type="application/json"),
                timeout=15.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, "name": name, "settings": public_payload(settings), "count": len(doc.get("presets", [])) if isinstance(doc.get("presets"), list) else 1})
            else:
                write_line(sys.stdout, f"Saved preset: {name}")
            return 0

        if args.action == "delete":
            encoded = quote(args.name, safe="")
            resp = open_response(
                root + f"/local/presets/{encoded}",
                method="DELETE",
                payload=None,
                headers=_auth_headers(args, started_server=started),
                timeout=15.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, "deleted": args.name, "remaining": len(doc.get("presets", [])) if isinstance(doc.get("presets"), list) else 0})
            else:
                write_line(sys.stdout, f"Deleted preset: {args.name}")
            return 0
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)
    return 2


# ── diff & baseline helpers ───────────────────────────────────


def _read_zip_member_bounded(
    archive: zipfile.ZipFile,
    name: str,
    max_bytes: int,
    *,
    error_code: str = "bundle_invalid",
) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > max_bytes:
        raise CaptureError(f"Archive entry {name!r} exceeds maximum allowed size ({max_bytes} bytes).", code=error_code)
    with archive.open(info, "r") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise CaptureError(f"Archive entry {name!r} exceeds maximum allowed size ({max_bytes} bytes).", code=error_code)
    return data


def _parse_diff_zip_report(bundle_bytes: bytes) -> dict[str, object]:
    try:
        with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as archive:
            raw = _read_zip_member_bounded(
                archive,
                "report.json",
                _MAX_DIFF_REPORT_BYTES,
                error_code="diff_report_too_large",
            )
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _emit_diff_result(
    *,
    bundle: bytes,
    headers,
    output_arg: str | None,
    as_json: bool,
    fail_on_diff: bool,
    default_filename: str = "visual-diff.zip",
) -> int:
    report = _parse_diff_zip_report(bundle)
    if "passed" not in report:
        passed_hdr = str(headers.get("X-ViperCapture-Diff-Passed", "")).lower()
        if passed_hdr in {"true", "false"}:
            report["passed"] = passed_hdr == "true"
    if "difference_ratio" not in report:
        ratio_hdr = headers.get("X-ViperCapture-Difference-Ratio")
        if ratio_hdr is not None:
            try:
                report["difference_ratio"] = float(ratio_hdr)
            except ValueError:
                pass

    saved_path: Path | None = None
    if output_arg:
        saved_path = Path(output_arg)
        write_bytes_atomic(saved_path, bundle)
    elif not as_json:
        saved_path = Path.cwd() / default_filename
        write_bytes_atomic(saved_path, bundle)

    passed = bool(report.get("passed", False))
    if as_json:
        result: dict[str, object] = {
            "ok": True,
            "passed": passed,
            "report": report,
            "bytes": len(bundle),
        }
        if saved_path is not None:
            result["path"] = display_path(saved_path)
            result["absolute"] = str(saved_path.resolve())
        _emit_json(result)
    else:
        ratio = report.get("difference_ratio", 0.0)
        changed = report.get("changed_pixels", "?")
        total = report.get("total_pixels", "?")
        verdict = "PASS" if passed else "DIFF"
        line = f"{verdict}: ratio={ratio} ({changed}/{total} pixels changed)"
        if saved_path is not None:
            line += f" -> {display_path(saved_path)}"
        write_line(sys.stdout, line)

    if fail_on_diff and not passed:
        return 1
    return 0


def cmd_diff(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture diff",
        description="Compare two images via POST /v1/diff and save the visual-diff ZIP.",
    )
    parser.add_argument("baseline", help="Baseline image file path.")
    parser.add_argument("current", help="Current image file path.")
    parser.add_argument("--output", help="Path to write the visual-diff ZIP (default: visual-diff.zip unless --json).")
    parser.add_argument("--threshold", type=int, default=0, help="Per-channel pixel difference threshold (0-255).")
    parser.add_argument("--max-diff-ratio", type=float, default=0.0, help="Maximum allowed changed-pixel ratio (0-1).")
    parser.add_argument("--fail-on-diff", action="store_true", help="Exit 1 when the diff does not pass the threshold.")
    _add_common_client_flags(parser, include_timeout=True)
    args = parser.parse_args(argv)
    if not (0 <= args.threshold <= 255):
        return fail("--threshold must be between 0 and 255.", exit_code=2)
    if not (0.0 <= args.max_diff_ratio <= 1.0):
        return fail("--max-diff-ratio must be between 0 and 1.", exit_code=2)

    load_local_env()
    try:
        root = api_root(args.server)
        base_name, base_bytes = _read_binary_file(args.baseline, "baseline image")
        curr_name, curr_bytes = _read_binary_file(args.current, "current image")
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    content_type, body = encode_multipart_form(
        fields={
            "pixel_threshold": str(args.threshold),
            "max_difference_ratio": str(args.max_diff_ratio),
        },
        files={
            "baseline": (base_name, base_bytes, None),
            "current": (curr_name, curr_bytes, None),
        },
    )
    try:
        started = ensure_server(args)
        resp = open_raw_response(
            root + "/v1/diff",
            method="POST",
            body=body,
            headers=_auth_headers(args, started_server=started, content_type=content_type),
            timeout=args.request_timeout,
        )
        try:
            bundle = read_response_bytes(resp)
            headers = resp.headers
        finally:
            resp.close()
        return _emit_diff_result(
            bundle=bundle,
            headers=headers,
            output_arg=args.output,
            as_json=args.json,
            fail_on_diff=args.fail_on_diff,
        )
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)


# ── baseline ──────────────────────────────────────────────────


def _validate_baseline_name(name: str) -> str:
    if name in {".", ".."} or not _BASELINE_NAME_RE.fullmatch(name):
        raise ValueError("Baseline names must be 1-128 characters using letters, numbers, dot, dash, and underscore.")
    return name


def _resolve_image_or_capture(
    args: argparse.Namespace,
    image_or_url: str | None,
    *,
    root: str,
    started_server: bool,
) -> tuple[str, bytes]:
    """Return (filename, bytes) either from a local image file or by rendering via /v1/render."""
    _ensure_render_namespace(args)
    treat_as_url = False
    if image_or_url:
        if re.match(r"^https?://", image_or_url, flags=re.IGNORECASE):
            treat_as_url = True
            args.target = image_or_url
        elif not Path(image_or_url).is_file() and _has_render_flags(args):
            treat_as_url = True
            args.target = image_or_url
        elif not Path(image_or_url).is_file():
            raise ValueError(f"Image file not found: {image_or_url}")
    if image_or_url and not treat_as_url:
        if _has_render_flags(args):
            raise ValueError("Pass either an image file or capture options, not both.")
        return _read_binary_file(image_or_url, "image")

    if not _has_render_flags(args):
        raise ValueError("Provide an image file path or capture options (--url / --html / --markdown / --from-json).")
    payload = build_payload(args)
    validate_payload(payload)
    resp = open_response(
        root + "/v1/render",
        method="POST",
        payload=payload,
        headers=_auth_headers(args, started_server=started_server, content_type="application/json"),
        timeout=getattr(args, "request_timeout", 120.0) or 120.0,
    )
    try:
        data = read_response_bytes(resp)
        fname = filename_from_headers(resp.headers, "capture")
    finally:
        resp.close()
    return fname, data


def cmd_baseline(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture baseline",
        description="Manage project visual baselines (/v1/baselines).",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_save = sub.add_parser("save", help="Upload or capture a named baseline.")
    p_save.add_argument("name", help="Baseline name.")
    p_save.add_argument("image", nargs="?", help="Image file path (or URL when capturing directly).")
    p_save.add_argument("--url", help="Capture this URL and store it as the baseline.")
    add_render_option_groups(p_save)
    _add_common_client_flags(p_save, include_timeout=True)

    p_list = sub.add_parser("list", help="List saved baselines.")
    _add_common_client_flags(p_list)

    p_del = sub.add_parser("delete", help="Delete a saved baseline.")
    p_del.add_argument("name", help="Baseline name.")
    _add_common_client_flags(p_del)

    p_cmp = sub.add_parser("compare", help="Compare an image or live capture against a saved baseline.")
    p_cmp.add_argument("name", help="Baseline name.")
    p_cmp.add_argument("image", nargs="?", help="Current image file path (or URL to capture).")
    p_cmp.add_argument("--url", help="Capture this URL and compare against the baseline.")
    p_cmp.add_argument("--output", help="Path to write the visual-diff ZIP.")
    p_cmp.add_argument("--threshold", type=int, default=0, help="Pixel threshold (0-255).")
    p_cmp.add_argument("--max-diff-ratio", type=float, default=0.0, help="Maximum allowed changed-pixel ratio (0-1).")
    p_cmp.add_argument("--fail-on-diff", action="store_true", help="Exit 1 when the diff fails the threshold.")
    add_render_option_groups(p_cmp)
    _add_common_client_flags(p_cmp, include_timeout=True)

    args = parser.parse_args(argv)
    load_local_env()
    try:
        root = api_root(args.server)
        if args.action in {"save", "delete", "compare"}:
            _validate_baseline_name(args.name)
        if args.action == "compare":
            if not (0 <= args.threshold <= 255):
                raise ValueError("--threshold must be between 0 and 255.")
            if not (0.0 <= args.max_diff_ratio <= 1.0):
                raise ValueError("--max-diff-ratio must be between 0 and 1.")
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    try:
        started = ensure_server(args)
        if args.action == "list":
            resp = open_response(
                root + "/v1/baselines",
                method="GET",
                payload=None,
                headers=_auth_headers(args, started_server=started),
                timeout=30.0,
            )
            try:
                items = _read_json_value(resp)
            finally:
                resp.close()
            baselines = items if isinstance(items, list) else []
            if args.json:
                _emit_json({"ok": True, "count": len(baselines), "baselines": baselines})
            elif not baselines:
                write_line(sys.stdout, "No baselines found.")
            else:
                for item in baselines:
                    if isinstance(item, dict):
                        name = item.get("name", "")
                        sha = str(item.get("sha256", ""))[:12]
                        size = item.get("bytes", "")
                        write_line(sys.stdout, f"{name}\t{size} bytes\t{sha}")
            return 0

        if args.action == "delete":
            encoded = quote(args.name, safe="")
            resp = open_response(
                root + f"/v1/baselines/{encoded}",
                method="DELETE",
                payload=None,
                headers=_auth_headers(args, started_server=started),
                timeout=30.0,
            )
            resp.close()
            if args.json:
                _emit_json({"ok": True, "deleted": args.name})
            else:
                write_line(sys.stdout, f"Deleted baseline: {args.name}")
            return 0

        if args.action == "save":
            try:
                fname, data = _resolve_image_or_capture(args, args.image, root=root, started_server=started)
            except ValueError as exc:
                return fail(str(exc), exit_code=2)
            content_type, body = encode_multipart_form(files={"image": (fname, data, None)})
            encoded = quote(args.name, safe="")
            resp = open_raw_response(
                root + f"/v1/baselines/{encoded}",
                method="PUT",
                body=body,
                headers=_auth_headers(args, started_server=started, content_type=content_type),
                timeout=args.request_timeout,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, **doc})
            else:
                write_line(sys.stdout, f"Saved baseline: {doc.get('name', args.name)}")
            return 0

        if args.action == "compare":
            try:
                fname, data = _resolve_image_or_capture(args, args.image, root=root, started_server=started)
            except ValueError as exc:
                return fail(str(exc), exit_code=2)
            content_type, body = encode_multipart_form(
                fields={
                    "pixel_threshold": str(args.threshold),
                    "max_difference_ratio": str(args.max_diff_ratio),
                },
                files={"current": (fname, data, None)},
            )
            encoded = quote(args.name, safe="")
            resp = open_raw_response(
                root + f"/v1/baselines/{encoded}/compare",
                method="POST",
                body=body,
                headers=_auth_headers(args, started_server=started, content_type=content_type),
                timeout=args.request_timeout,
            )
            try:
                bundle = read_response_bytes(resp)
                headers = resp.headers
            finally:
                resp.close()
            return _emit_diff_result(
                bundle=bundle,
                headers=headers,
                output_arg=args.output,
                as_json=args.json,
                fail_on_diff=args.fail_on_diff,
                default_filename=f"{safe_filename(args.name)}-diff.zip",
            )
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)
    return 2


# ── bulk ──────────────────────────────────────────────────────


def _parse_bulk_input(
    urls_file: str | None,
    cli_urls: list[str] | None,
    base_render: dict[str, object],
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []

    def _merge_render(entry_render: dict[str, object]) -> dict[str, object]:
        merged = capture_cli._deep_merge(base_render, entry_render) if base_render else copy.deepcopy(entry_render)
        if "output" not in merged:
            merged["output"] = "png"
        return merged

    def _coerce_item(raw_entry: object, index: int) -> dict[str, object]:
        if isinstance(raw_entry, str):
            url = normalize_url(raw_entry)
            render = _merge_render({"url": url})
            return {"render": render}
        if isinstance(raw_entry, dict):
            if "render" in raw_entry and isinstance(raw_entry["render"], dict):
                item: dict[str, object] = {"render": _merge_render(raw_entry["render"])}
                if raw_entry.get("id") is not None:
                    item["id"] = str(raw_entry["id"])
                if raw_entry.get("idempotency_key") is not None:
                    item["idempotency_key"] = str(raw_entry["idempotency_key"])
                return item
            render = _merge_render(raw_entry)
            if isinstance(render.get("url"), str):
                render["url"] = normalize_url(render["url"])
            return {"render": render}
        raise ValueError(f"Bulk item #{index} must be a URL string or JSON object.")

    if urls_file:
        text = _read_text(urls_file).removeprefix("\ufeff").strip()
        if not text:
            raise ValueError("Bulk input file is empty.")
        loaded_json = False
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                loaded_json = True
                if isinstance(parsed, dict):
                    if isinstance(parsed.get("items"), list):
                        for idx, entry in enumerate(parsed["items"], start=1):
                            items.append(_coerce_item(entry, idx))
                    else:
                        items.append(_coerce_item(parsed, 1))
                elif isinstance(parsed, list):
                    for idx, entry in enumerate(parsed, start=1):
                        items.append(_coerce_item(entry, idx))
                else:
                    raise ValueError("Bulk JSON input must be an array or an object with 'items'.")
        if not loaded_json:
            for idx, raw_line in enumerate(text.splitlines(), start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("{"):
                    try:
                        parsed_line = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Invalid JSONL on line {idx}: {exc}") from exc
                    items.append(_coerce_item(parsed_line, idx))
                else:
                    items.append(_coerce_item(line, idx))

    for idx, url in enumerate(cli_urls or [], start=len(items) + 1):
        items.append(_coerce_item(url, idx))

    if not items:
        raise ValueError("Pass a URLs/JSON/JSONL file or at least one --url.")
    return items


def _poll_and_download_job(
    *,
    root: str,
    job: dict[str, object],
    output_dir: Path,
    item_label: str,
    headers: dict[str, str],
    poll_ms: int,
    deadline: float,
) -> dict[str, object]:
    current = dict(job)
    while True:
        status = str(current.get("status") or "")
        if status == "succeeded":
            result_url = current.get("result_url")
            if not isinstance(result_url, str) or not result_url:
                raise CaptureError("The async job did not include a result URL.")
            remaining = max(1.0, deadline - time.monotonic())
            resp = open_response(
                resolve_api_url(root, result_url),
                method="GET",
                payload=None,
                headers=headers,
                timeout=remaining,
            )
            try:
                fname = filename_from_headers(resp.headers, item_label)
                suffix = Path(fname).suffix
                target = output_dir / f"{safe_filename(item_label)}{suffix}"
                total = write_response(resp, target)
                media = _media_type(resp.headers)
            finally:
                resp.close()
            return {
                "ok": True,
                "id": current.get("id"),
                "status": "succeeded",
                "path": display_path(target),
                "absolute": str(target.resolve()),
                "bytes": total,
                "media_type": media,
            }
        if status not in _OPEN_JOBS:
            err = current.get("error")
            msg = str(err.get("message")) if isinstance(err, dict) and err.get("message") else f"Job {status}"
            return {
                "ok": False,
                "id": current.get("id"),
                "status": status,
                "error": msg,
            }
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "id": current.get("id"),
                "status": "timeout",
                "error": "Timed out waiting for async job.",
            }
        status_url = current.get("status_url")
        if not isinstance(status_url, str) or not status_url:
            return {
                "ok": False,
                "id": current.get("id"),
                "status": "error",
                "error": "The async job did not include a status URL.",
            }
        time.sleep(min(max(poll_ms, 1) / 1000.0, max(0.01, deadline - time.monotonic())))
        resp = open_response(
            resolve_api_url(root, status_url),
            method="GET",
            payload=None,
            headers=headers,
            timeout=min(30.0, max(1.0, deadline - time.monotonic())),
        )
        try:
            current = _read_json(resp)
        finally:
            resp.close()


def cmd_bulk(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture bulk",
        description="Submit batch async render jobs to POST /v1/jobs/bulk.",
    )
    parser.add_argument("urls_file", nargs="?", help="File with URLs per line, JSONL, JSON array, bulk JSON envelope, or - for stdin.")
    parser.add_argument("--url", dest="bulk_urls", action="append", help="URL to include in the batch. Repeatable.")
    parser.add_argument("--output-dir", help="Directory to download results into when --wait is used.")
    parser.add_argument("--idempotency-key", help="Idempotency-Key header for the batch.")
    parser.add_argument("--wait", action="store_true", help="Wait for all accepted jobs and download their results.")
    parser.add_argument("--concurrency", type=int, default=4, help="Max concurrent job pollers/downloaders with --wait.")
    parser.add_argument("--poll-ms", type=int, default=500, help="Async poll interval in milliseconds.")
    add_render_option_groups(parser, include_wait_flag=False)
    _add_common_client_flags(parser, include_timeout=True)
    parser.set_defaults(request_timeout=300.0)
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        return fail("--concurrency must be 1 or greater.", exit_code=2)
    if args.poll_ms < 1:
        return fail("--poll-ms must be 1 or greater.", exit_code=2)

    load_local_env()
    try:
        root = api_root(args.server)
        _ensure_render_namespace(args)
        base_render = build_payload(args, require_source=False) if _has_render_flags(args) else {}
        for source_key in ("url", "html", "markdown"):
            base_render.pop(source_key, None)
        items = _parse_bulk_input(args.urls_file, args.bulk_urls, base_render)
        for item in items:
            validate_payload(item["render"])  # type: ignore[arg-type]
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    output_dir: Path | None = None
    if args.wait:
        output_dir = Path(args.output_dir) if args.output_dir else Path.cwd()
        if output_dir.exists() and not output_dir.is_dir():
            return fail(f"--output-dir is not a directory: {output_dir}", exit_code=2)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return fail(f"Cannot create --output-dir {output_dir}: {exc}", exit_code=2)

    try:
        started = ensure_server(args)
        all_results: list[dict[str, object]] = []
        accepted_total = 0
        failed_total = 0
        chunks = [items[i : i + 100] for i in range(0, len(items), 100)]
        for batch_idx, chunk in enumerate(chunks, start=1):
            extra: dict[str, str] = {}
            if args.idempotency_key:
                extra["Idempotency-Key"] = (
                    args.idempotency_key
                    if len(chunks) == 1
                    else derive_batch_idempotency_key(args.idempotency_key, f"batch-{batch_idx}")
                )
            resp = open_response(
                root + "/v1/jobs/bulk",
                method="POST",
                payload={"items": chunk},
                headers=_auth_headers(args, started_server=started, content_type="application/json", extra=extra),
                timeout=min(60.0, args.request_timeout),
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            accepted_total += int(doc.get("accepted") or 0)
            failed_total += int(doc.get("failed") or 0)
            offset = (batch_idx - 1) * 100
            for entry in doc.get("results") or []:
                if isinstance(entry, dict):
                    copied = dict(entry)
                    copied["index"] = offset + int(copied.get("index", 0))
                    all_results.append(copied)

        if not args.wait:
            out_doc = {
                "ok": failed_total == 0,
                "count": len(all_results),
                "accepted": accepted_total,
                "failed": failed_total,
                "results": all_results,
            }
            if args.json:
                _emit_json(out_doc)
            else:
                write_line(
                    sys.stdout,
                    f"Submitted {len(all_results)} job(s): {accepted_total} accepted, {failed_total} failed.",
                )
                for entry in all_results:
                    job = entry.get("job")
                    if isinstance(job, dict) and job.get("id"):
                        write_line(sys.stdout, f"  [{entry.get('index')}] {job['id']} ({job.get('status')})")
                    else:
                        err = entry.get("error")
                        msg = err.get("message") if isinstance(err, dict) else "failed"
                        write_line(sys.stdout, f"  [{entry.get('index')}] FAILED: {msg}")
            return 0 if failed_total == 0 else 1

        assert output_dir is not None
        deadline = time.monotonic() + float(args.request_timeout)
        get_headers = _auth_headers(args, started_server=started)
        downloaded: list[dict[str, object]] = []
        download_failures = failed_total

        tasks: list[tuple[int, dict[str, object], str]] = []
        used_stems: set[str] = set()
        for entry in all_results:
            if not entry.get("accepted") or not isinstance(entry.get("job"), dict):
                downloaded.append({
                    "index": entry.get("index"),
                    "ok": False,
                    "error": entry.get("error"),
                })
                continue
            idx = int(entry.get("index", 0))
            item_id = entry.get("id")
            if isinstance(item_id, str) and item_id:
                base_stem = safe_filename(item_id)
            else:
                render_obj = items[idx]["render"] if idx < len(items) else {}
                url_val = render_obj.get("url") if isinstance(render_obj, dict) else None
                host = urlsplit(str(url_val)).hostname if url_val else "item"
                base_stem = safe_filename(f"{safe_filename(host or 'item')}-{idx + 1}")
            stem = base_stem
            if stem.lower() in used_stems:
                for suffix_num in itertools.count(1):
                    suffix = f"-{idx + 1}" if suffix_num == 1 else f"-{idx + 1}-{suffix_num}"
                    prefix_limit = max(1, 80 - len(suffix))
                    prefix = base_stem[:prefix_limit].rstrip(" ._-") or "item"
                    candidate = safe_filename(f"{prefix}{suffix}")
                    if candidate.lower() not in used_stems:
                        stem = candidate
                        break
            used_stems.add(stem.lower())
            tasks.append((idx, entry["job"], stem))

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.concurrency, max(1, len(tasks)))) as pool:
            future_map = {
                pool.submit(
                    _poll_and_download_job,
                    root=root,
                    job=job_doc,
                    output_dir=output_dir,
                    item_label=label,
                    headers=get_headers,
                    poll_ms=args.poll_ms,
                    deadline=deadline,
                ): idx
                for idx, job_doc, label in tasks
            }
            for future in concurrent.futures.as_completed(future_map):
                idx = future_map[future]
                try:
                    res = future.result()
                except CaptureError as exc:
                    res = {"ok": False, "error": str(exc), "status": exc.status}
                except OSError as exc:
                    res = {"ok": False, "error": str(exc), "status": None}
                res["index"] = idx
                if not res.get("ok"):
                    download_failures += 1
                downloaded.append(res)

        downloaded.sort(key=lambda item: int(item.get("index", 0)))
        succeeded_count = sum(1 for item in downloaded if item.get("ok"))
        summary = {
            "ok": download_failures == 0,
            "count": len(downloaded),
            "succeeded": succeeded_count,
            "failed": download_failures,
            "results": downloaded,
        }
        if args.json:
            _emit_json(summary)
        else:
            for item in downloaded:
                if item.get("ok") and item.get("path"):
                    write_line(sys.stdout, str(item["path"]))
                else:
                    write_line(sys.stderr, _json_line({"ok": False, "index": item.get("index"), "error": item.get("error")}))
        return 0 if download_failures == 0 else 1
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)


# ── job ───────────────────────────────────────────────────────


def _poll_job_until_done(
    *,
    root: str,
    job_id: str,
    headers: dict[str, str],
    poll_ms: int,
    timeout: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    encoded = quote(job_id, safe="")
    while True:
        resp = open_response(
            root + f"/v1/jobs/{encoded}",
            method="GET",
            payload=None,
            headers=headers,
            timeout=min(30.0, max(1.0, deadline - time.monotonic())),
        )
        try:
            job = _read_json(resp)
        finally:
            resp.close()
        if str(job.get("status") or "") not in _OPEN_JOBS:
            return job
        if time.monotonic() >= deadline:
            raise CaptureError("Timed out waiting for async job.")
        time.sleep(min(max(poll_ms, 1) / 1000.0, max(0.01, deadline - time.monotonic())))


def cmd_job(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture job",
        description="Inspect, cancel, or download async render jobs (/v1/jobs).",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_status = sub.add_parser("status", help="Get async job status.")
    p_status.add_argument("job_id", help="Job UUID.")
    p_status.add_argument("--wait", action="store_true", help="Poll until the job finishes.")
    p_status.add_argument("--poll-ms", type=int, default=500, help="Poll interval in milliseconds.")
    _add_common_client_flags(p_status, include_timeout=True)

    p_cancel = sub.add_parser("cancel", help="Cancel a queued or running async job.")
    p_cancel.add_argument("job_id", help="Job UUID.")
    _add_common_client_flags(p_cancel, include_timeout=True)

    p_dl = sub.add_parser("download", help="Download a succeeded async job result.")
    p_dl.add_argument("job_id", help="Job UUID.")
    p_dl.add_argument("--output", help="Output file path, or - for stdout.")
    p_dl.add_argument("--wait", action="store_true", help="Wait for the job to succeed before downloading.")
    p_dl.add_argument("--poll-ms", type=int, default=500, help="Poll interval in milliseconds.")
    _add_common_client_flags(p_dl, include_timeout=True)

    args = parser.parse_args(argv)
    load_local_env()
    try:
        root = api_root(args.server)
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    encoded = quote(args.job_id.strip(), safe="")
    try:
        started = ensure_server(args)
        headers = _auth_headers(args, started_server=started)
        if args.action == "status":
            if args.wait:
                job = _poll_job_until_done(
                    root=root,
                    job_id=args.job_id.strip(),
                    headers=headers,
                    poll_ms=args.poll_ms,
                    timeout=args.request_timeout,
                )
            else:
                resp = open_response(
                    root + f"/v1/jobs/{encoded}",
                    method="GET",
                    payload=None,
                    headers=headers,
                    timeout=args.request_timeout,
                )
                try:
                    job = _read_json(resp)
                finally:
                    resp.close()
            if args.json:
                _emit_json({"ok": True, **job})
            else:
                write_line(sys.stdout, f"{job.get('id', args.job_id)}\t{job.get('status', 'unknown')}")
            return 0

        if args.action == "cancel":
            resp = open_response(
                root + f"/v1/jobs/{encoded}",
                method="DELETE",
                payload=None,
                headers=headers,
                timeout=args.request_timeout,
            )
            try:
                job = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, **job})
            else:
                write_line(sys.stdout, f"Cancelled job {job.get('id', args.job_id)} (status: {job.get('status')})")
            return 0

        if args.action == "download":
            if args.wait:
                job = _poll_job_until_done(
                    root=root,
                    job_id=args.job_id.strip(),
                    headers=headers,
                    poll_ms=args.poll_ms,
                    timeout=args.request_timeout,
                )
                if job.get("status") != "succeeded":
                    err = job.get("error")
                    msg = str(err.get("message")) if isinstance(err, dict) and err.get("message") else f"Job ended with status {job.get('status')}"
                    return fail(msg, body=job, exit_code=1)
            resp = open_response(
                root + f"/v1/jobs/{encoded}/result",
                method="GET",
                payload=None,
                headers=headers,
                timeout=args.request_timeout,
            )
            try:
                if args.output == "-":
                    target: Path | None = None
                elif args.output:
                    target = Path(args.output)
                else:
                    target = Path.cwd() / filename_from_headers(resp.headers, args.job_id.strip())
                total = write_response(resp, target)
                media = _media_type(resp.headers)
            finally:
                resp.close()
            if target is None:
                if args.json:
                    write_line(sys.stderr, _json_line({"ok": True, "path": "-", "bytes": total, "media_type": media}))
            elif args.json:
                _emit_json({
                    "ok": True,
                    "job_id": args.job_id.strip(),
                    "path": display_path(target),
                    "absolute": str(target.resolve()),
                    "bytes": total,
                    "media_type": media,
                })
            else:
                write_line(sys.stdout, display_path(target))
            return 0
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)
    return 2


# ── schedule ──────────────────────────────────────────────────


def cmd_schedule(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture schedule",
        description="Manage recurring cron capture schedules (/v1/schedules).",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_create = sub.add_parser("create", help="Create a cron schedule.")
    p_create.add_argument("target", nargs="?", help="Target URL for the scheduled capture.")
    p_create.add_argument("--url", help="Target URL for the scheduled capture.")
    p_create.add_argument("--name", required=True, help="Schedule name (1-128 chars).")
    p_create.add_argument("--cron", required=True, help="Five-field cron expression, e.g. '0 * * * *'.")
    p_create.add_argument("--timezone", dest="schedule_timezone", default="UTC", help="IANA timezone for the cron schedule (default: UTC).")
    enable_grp = p_create.add_mutually_exclusive_group()
    enable_grp.add_argument("--enabled", dest="schedule_enabled", action="store_const", const=True, default=True, help="Enable schedule (default).")
    enable_grp.add_argument("--disabled", dest="schedule_enabled", action="store_const", const=False, help="Create schedule disabled.")
    add_render_option_groups(p_create, include_timezone=False)
    _add_common_client_flags(p_create)

    p_list = sub.add_parser("list", help="List schedules.")
    p_list.add_argument("--limit", type=int, default=50, help="Max schedules to return (1-100).")
    p_list.add_argument("--after", help="Pagination cursor.")
    _add_common_client_flags(p_list)

    p_get = sub.add_parser("get", help="Get a schedule by ID.")
    p_get.add_argument("schedule_id", help="Schedule UUID.")
    _add_common_client_flags(p_get)

    p_upd = sub.add_parser("update", help="Update a schedule.")
    p_upd.add_argument("schedule_id", help="Schedule UUID.")
    p_upd.add_argument("target", nargs="?", help="Optional replacement target URL.")
    p_upd.add_argument("--url", help="Optional replacement target URL.")
    p_upd.add_argument("--name", help="New schedule name.")
    p_upd.add_argument("--cron", help="New five-field cron expression.")
    p_upd.add_argument("--timezone", dest="schedule_timezone", help="New IANA timezone.")
    upd_enable = p_upd.add_mutually_exclusive_group()
    upd_enable.add_argument("--enabled", dest="schedule_enabled", action="store_const", const=True, default=None, help="Enable schedule.")
    upd_enable.add_argument("--disabled", dest="schedule_enabled", action="store_const", const=False, help="Disable schedule.")
    add_render_option_groups(p_upd, include_timezone=False)
    _add_common_client_flags(p_upd)

    p_del = sub.add_parser("delete", help="Delete a schedule.")
    p_del.add_argument("schedule_id", help="Schedule UUID.")
    _add_common_client_flags(p_del)

    args = parser.parse_args(argv)
    load_local_env()
    try:
        root = api_root(args.server)
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    try:
        started = ensure_server(args)
        if args.action == "create":
            _ensure_render_namespace(args)
            try:
                render_doc = build_payload(args)
                validate_payload(render_doc)
            except ValueError as exc:
                return fail(str(exc), exit_code=2)
            body = {
                "name": args.name,
                "cron": args.cron,
                "timezone": args.schedule_timezone,
                "enabled": bool(args.schedule_enabled),
                "render": render_doc,
            }
            resp = open_response(
                root + "/v1/schedules",
                method="POST",
                payload=body,
                headers=_auth_headers(args, started_server=started, content_type="application/json"),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, **doc})
            else:
                write_line(sys.stdout, f"{doc.get('id')}\t{doc.get('name')}\t{doc.get('cron')} ({doc.get('timezone')})")
            return 0

        if args.action == "list":
            if not (1 <= args.limit <= 100):
                return fail("--limit must be between 1 and 100.", exit_code=2)
            params: dict[str, object] = {"limit": args.limit}
            if args.after:
                params["after"] = args.after
            resp = open_response(
                root + "/v1/schedules?" + urlencode(params),
                method="GET",
                payload=None,
                headers=_auth_headers(args, started_server=started),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, **doc})
            else:
                schedules = doc.get("schedules") if isinstance(doc.get("schedules"), list) else []
                if not schedules:
                    write_line(sys.stdout, "No schedules found.")
                else:
                    for item in schedules:
                        if isinstance(item, dict):
                            state = "enabled" if item.get("enabled") else "disabled"
                            write_line(
                                sys.stdout,
                                f"{item.get('id')}\t{item.get('name')}\t{item.get('cron')}\t{item.get('timezone')}\t{state}",
                            )
            return 0

        if args.action == "get":
            encoded = quote(args.schedule_id.strip(), safe="")
            resp = open_response(
                root + f"/v1/schedules/{encoded}",
                method="GET",
                payload=None,
                headers=_auth_headers(args, started_server=started),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, **doc})
            else:
                write_line(sys.stdout, json.dumps(doc, ensure_ascii=False, indent=2))
            return 0

        if args.action == "update":
            _ensure_render_namespace(args)
            patch: dict[str, object] = {}
            if args.name is not None:
                patch["name"] = args.name
            if args.cron is not None:
                patch["cron"] = args.cron
            if args.schedule_timezone is not None:
                patch["timezone"] = args.schedule_timezone
            if args.schedule_enabled is not None:
                patch["enabled"] = bool(args.schedule_enabled)
            if _has_render_flags(args):
                try:
                    render_doc = build_payload(args)
                    validate_payload(render_doc)
                except ValueError as exc:
                    return fail(str(exc), exit_code=2)
                patch["render"] = render_doc
            if not patch:
                return fail("Specify at least one field to update (--name, --cron, --timezone, --enabled/--disabled, or render options).", exit_code=2)
            encoded = quote(args.schedule_id.strip(), safe="")
            resp = open_response(
                root + f"/v1/schedules/{encoded}",
                method="PATCH",
                payload=patch,
                headers=_auth_headers(args, started_server=started, content_type="application/json"),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, **doc})
            else:
                write_line(sys.stdout, f"Updated schedule: {doc.get('id', args.schedule_id)}")
            return 0

        if args.action == "delete":
            encoded = quote(args.schedule_id.strip(), safe="")
            resp = open_response(
                root + f"/v1/schedules/{encoded}",
                method="DELETE",
                payload=None,
                headers=_auth_headers(args, started_server=started),
                timeout=30.0,
            )
            resp.close()
            if args.json:
                _emit_json({"ok": True, "deleted": args.schedule_id.strip()})
            else:
                write_line(sys.stdout, f"Deleted schedule: {args.schedule_id.strip()}")
            return 0
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)
    return 2


# ── profile ───────────────────────────────────────────────────


def cmd_profile(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture profile",
        description="Manage encrypted browser profiles (/v1/profiles). Secrets are never echoed.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_create = sub.add_parser("create", help="Create a browser profile (empty or from Playwright storage_state JSON).")
    p_create.add_argument("--file", dest="state_file", help="Path to Playwright storage state JSON, or - for stdin.")
    p_create.add_argument("--ttl", type=int, help="Optional TTL in seconds (60 to 31536000).")
    _add_common_client_flags(p_create)

    p_import = sub.add_parser("import", help="Import cookies/session into an encrypted browser profile.")
    p_import.add_argument("file_pos", nargs="?", metavar="FILE", help="Session export file path, or - for stdin.")
    p_import.add_argument("--file", dest="file_opt", help="Session export file path, or - for stdin.")
    p_import.add_argument(
        "--format",
        default="auto",
        choices=("auto", "playwright", "cookies_json", "netscape", "cookie_header"),
        help="Input export format (default: auto).",
    )
    p_import.add_argument("--origin", help="Origin URL (required for cookie_header format).")
    p_import.add_argument("--ttl", type=int, help="Optional TTL in seconds (60 to 31536000).")
    _add_common_client_flags(p_import)

    p_del = sub.add_parser("delete", help="Delete a browser profile.")
    p_del.add_argument("profile_id", help="Profile ID.")
    _add_common_client_flags(p_del)

    args = parser.parse_args(argv)
    load_local_env()
    try:
        root = api_root(args.server)
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    try:
        started = ensure_server(args)
        if args.action == "create":
            storage_state: dict[str, object] = {"cookies": [], "origins": []}
            if args.state_file:
                try:
                    raw_text = _read_text(args.state_file)
                    parsed = json.loads(raw_text)
                except (ValueError, json.JSONDecodeError) as exc:
                    return fail(f"Invalid storage state JSON: {exc}", exit_code=2)
                if not isinstance(parsed, dict):
                    return fail("Storage state file must contain a JSON object.", exit_code=2)
                if isinstance(parsed.get("storage_state"), dict):
                    storage_state = parsed["storage_state"]
                else:
                    storage_state = parsed
            body: dict[str, object] = {"storage_state": storage_state}
            if args.ttl is not None:
                body["ttl_seconds"] = args.ttl
            resp = open_response(
                root + "/v1/profiles",
                method="POST",
                payload=body,
                headers=_auth_headers(args, started_server=started, content_type="application/json"),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            safe_doc = {"ok": True, "id": doc.get("id"), "expires_in": doc.get("expires_in")}
            if args.json:
                _emit_json(safe_doc)
            else:
                write_line(sys.stdout, str(doc.get("id", "")))
            return 0

        if args.action == "import":
            if args.file_pos and args.file_opt:
                return fail("Pass the session file once, either positionally or with --file.", exit_code=2)
            source_path = args.file_opt or args.file_pos
            if not source_path:
                return fail("Pass a session file with --file PATH (or - for stdin).", exit_code=2)
            try:
                content = _read_text(source_path)
            except ValueError as exc:
                return fail(str(exc), exit_code=2)
            body = {
                "content": content,
                "format": args.format,
            }
            if args.origin:
                body["origin"] = args.origin
            if args.ttl is not None:
                body["ttl_seconds"] = args.ttl
            resp = open_response(
                root + "/v1/profiles/import",
                method="POST",
                payload=body,
                headers=_auth_headers(args, started_server=started, content_type="application/json"),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            safe_doc = {
                "ok": True,
                "id": doc.get("id"),
                "expires_in": doc.get("expires_in"),
                "cookies": doc.get("cookies"),
                "origins": doc.get("origins"),
            }
            if args.json:
                _emit_json(safe_doc)
            else:
                write_line(sys.stdout, str(doc.get("id", "")))
            return 0

        if args.action == "delete":
            encoded = quote(args.profile_id.strip(), safe="")
            resp = open_response(
                root + f"/v1/profiles/{encoded}",
                method="DELETE",
                payload=None,
                headers=_auth_headers(args, started_server=started),
                timeout=30.0,
            )
            resp.close()
            if args.json:
                _emit_json({"ok": True, "deleted": args.profile_id.strip()})
            else:
                write_line(sys.stdout, f"Deleted profile: {args.profile_id.strip()}")
            return 0
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)
    return 2


# ── sign ──────────────────────────────────────────────────────


def cmd_sign(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture sign",
        description="Create an HMAC-signed render URL via POST /v1/signed-url.",
    )
    parser.add_argument("target", nargs="?", help="Target URL to sign.")
    parser.add_argument("--url", help="Target URL to sign.")
    parser.add_argument("--ttl", type=int, default=3600, help="Signed URL TTL in seconds (default: 3600).")
    parser.add_argument("--signing-token", help="Signing admin token (or VIPERCAPTURE_SIGNING_ADMIN_TOKEN).")
    add_render_option_groups(parser, include_webhook=False)
    _add_common_client_flags(parser)
    args = parser.parse_args(argv)
    if args.ttl < 1:
        return fail("--ttl must be 1 or greater.", exit_code=2)

    _load_local_env_for_target(args)
    try:
        root = api_root(args.server)
        _ensure_render_namespace(args)
        payload = build_payload(args)
        validate_payload(payload)
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    token = _signing_token(args)
    try:
        started = ensure_server(args)
        query = urlencode({"ttl_seconds": args.ttl})
        resp = open_response(
            root + f"/v1/signed-url?{query}",
            method="POST",
            payload=payload,
            headers=_auth_headers(
                args,
                started_server=started,
                content_type="application/json",
                token_override=token,
            ),
            timeout=30.0,
        )
        try:
            doc = _read_json(resp)
        finally:
            resp.close()
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)

    if args.json:
        _emit_json({"ok": True, "url": doc.get("url"), "expires": doc.get("expires")})
    else:
        write_line(sys.stdout, str(doc.get("url", "")))
    return 0


# ── verify ────────────────────────────────────────────────────


def _decode_b64_bytes(text: str) -> bytes:
    cleaned = text.strip()
    padding = "=" * (-len(cleaned) % 4)
    try:
        return base64.urlsafe_b64decode(cleaned + padding)
    except Exception:
        return base64.b64decode(cleaned + padding)


def _load_ed25519_public_key(raw_spec: str, *, allow_file: bool = True):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    text = raw_spec.strip()
    if allow_file:
        candidate_path = Path(text)
        if candidate_path.is_file():
            text = candidate_path.read_text(encoding="utf-8-sig").strip()

        if "-----BEGIN" in text:
            loaded = serialization.load_pem_public_key(text.encode("utf-8"))
            if not isinstance(loaded, Ed25519PublicKey):
                raise ValueError("Public key PEM is not an Ed25519 key.")
            raw_pub = loaded.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            return loaded, base64.urlsafe_b64encode(raw_pub).decode("ascii").rstrip("=")

        if text.startswith("{"):
            parsed = json.loads(text)
            if isinstance(parsed, dict) and isinstance(parsed.get("public_key"), str):
                text = parsed["public_key"].strip()

    key_bytes = _decode_b64_bytes(text)
    if len(key_bytes) != 32:
        raise ValueError("Ed25519 public key must decode to 32 raw bytes (or be PEM-encoded).")
    pub = Ed25519PublicKey.from_public_bytes(key_bytes)
    canonical_b64 = base64.urlsafe_b64encode(key_bytes).decode("ascii").rstrip("=")
    return pub, canonical_b64


def verify_certified_bundle(
    bundle_path: Path,
    *,
    public_key_spec: str,
    allow_key_file: bool = True,
) -> dict[str, object]:
    """Verify a --certify ZIP bundle's Ed25519 signature, manifest, and SHA-256 hash."""
    from cryptography.exceptions import InvalidSignature

    if not bundle_path.is_file():
        raise CaptureError(f"Bundle not found: {bundle_path}", code="bundle_not_found")
    try:
        public_key, expected_key_b64 = _load_ed25519_public_key(
            public_key_spec,
            allow_file=allow_key_file,
        )
    except Exception as exc:
        raise CaptureError(f"Invalid public key: {exc}", code="invalid_public_key") from exc

    try:
        with zipfile.ZipFile(bundle_path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise CaptureError("Bundle contains duplicate archive entries.", code="bundle_invalid")
            if "manifest.json" not in names or "manifest.sig" not in names:
                raise CaptureError(
                    "Bundle is missing manifest.json or manifest.sig.",
                    code="bundle_invalid",
                )
            manifest_raw = _read_zip_member_bounded(archive, "manifest.json", _MAX_MANIFEST_BYTES)
            sig_raw = _read_zip_member_bounded(archive, "manifest.sig", _MAX_SIG_BYTES)

            try:
                manifest = json.loads(manifest_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CaptureError("manifest.json is not valid UTF-8 JSON.", code="manifest_invalid") from exc
            if not isinstance(manifest, dict):
                raise CaptureError("manifest.json must contain a JSON object.", code="manifest_invalid")
            if manifest.get("schema_version") != 1:
                raise CaptureError("Unsupported certification schema_version.", code="manifest_invalid")
            if manifest.get("algorithm") != "Ed25519":
                raise CaptureError("Unsupported certification algorithm.", code="manifest_invalid")

            canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
            manifest_stripped = manifest_raw.rstrip(b"\r\n")
            if manifest_stripped != canonical:
                raise CaptureError("manifest.json is not in canonical certified JSON format.", code="manifest_tampered")

            manifest_pub = manifest.get("public_key")
            if not isinstance(manifest_pub, str) or manifest_pub.strip() != expected_key_b64:
                raise CaptureError(
                    "Bundle public_key does not match the trusted certification public key.",
                    code="public_key_mismatch",
                )

            try:
                sig_bytes = _decode_b64_bytes(sig_raw.decode("ascii"))
            except Exception as exc:
                raise CaptureError("manifest.sig is not valid base64.", code="signature_invalid") from exc
            if len(sig_bytes) != 64:
                raise CaptureError("Ed25519 signature must be 64 bytes.", code="signature_invalid")
            try:
                public_key.verify(sig_bytes, canonical)
            except InvalidSignature as exc:
                raise CaptureError("Ed25519 signature verification failed.", code="signature_invalid") from exc

            artifact = manifest.get("artifact")
            if not isinstance(artifact, dict):
                raise CaptureError("manifest.json is missing artifact metadata.", code="manifest_invalid")
            filename = artifact.get("filename")
            expected_sha = artifact.get("sha256")
            expected_bytes = artifact.get("bytes")
            if (
                not isinstance(filename, str)
                or not filename
                or "/" in filename
                or "\\" in filename
                or filename in {".", "..", "manifest.json", "manifest.sig"}
            ):
                raise CaptureError("manifest.json contains an invalid artifact filename.", code="manifest_invalid")
            if filename not in names:
                raise CaptureError(f"Certified artifact {filename!r} is missing from bundle.", code="artifact_missing")
            expected_entries = {"manifest.json", "manifest.sig", filename}
            extra_entries = sorted(set(names) - expected_entries)
            if extra_entries:
                raise CaptureError(
                    f"Bundle contains unexpected uncertified entries: {', '.join(extra_entries)}",
                    code="bundle_tampered",
                )
            if type(expected_bytes) is not int or expected_bytes < 0 or expected_bytes > _MAX_VERIFY_MEMBER_BYTES:
                raise CaptureError("Certified artifact byte length is invalid.", code="manifest_invalid")

            artifact_info = archive.getinfo(filename)
            if artifact_info.file_size > _MAX_VERIFY_MEMBER_BYTES:
                raise CaptureError("Bundle entry exceeds maximum verification size.", code="bundle_invalid")

            digest = hashlib.sha256()
            actual_bytes = 0
            with archive.open(artifact_info, "r") as handle:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    actual_bytes += len(chunk)
                    if actual_bytes > _MAX_VERIFY_MEMBER_BYTES:
                        raise CaptureError("Bundle entry exceeds maximum verification size.", code="bundle_invalid")
                    digest.update(chunk)
            actual_sha = digest.hexdigest()
            if actual_bytes != expected_bytes:
                raise CaptureError("Certified artifact byte length mismatch.", code="artifact_tampered")
            if not isinstance(expected_sha, str) or actual_sha != expected_sha.lower():
                raise CaptureError("Certified artifact SHA-256 digest mismatch.", code="artifact_tampered")

            return {
                "ok": True,
                "verified": True,
                "algorithm": "Ed25519",
                "schema_version": 1,
                "created_at_unix_ms": manifest.get("created_at_unix_ms"),
                "public_key": expected_key_b64,
                "artifact": {
                    "filename": filename,
                    "media_type": artifact.get("media_type"),
                    "bytes": actual_bytes,
                    "sha256": actual_sha,
                },
            }
    except zipfile.BadZipFile as exc:
        raise CaptureError(f"Not a valid ZIP archive: {bundle_path}", code="bundle_invalid") from exc


def cmd_verify(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture verify",
        description="Verify an Ed25519-certified ViperCapture bundle (--certify).",
    )
    parser.add_argument("bundle", help="Path to the certified bundle ZIP.")
    parser.add_argument(
        "--public-key",
        help="Ed25519 public key (file path, PEM text, or base64). If omitted, fetches /v1/certification/public-key.",
    )
    _add_common_client_flags(parser, include_no_start=False)
    args = parser.parse_args(argv)
    _load_local_env_for_target(args)

    key_spec = args.public_key
    allow_key_file = True
    if not key_spec:
        allow_key_file = False
        try:
            root = api_root(args.server)
        except ValueError as exc:
            return fail(str(exc), exit_code=2)
        try:
            resp = open_response(
                root + "/v1/certification/public-key",
                method="GET",
                payload=None,
                headers=_auth_headers(args, started_server=False, extra={"Accept": "application/json"}),
                timeout=15.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
        except CaptureError as exc:
            return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)
        fetched = doc.get("public_key")
        if not isinstance(fetched, str) or not fetched.strip():
            return fail("Server did not return a certification public_key.", code="invalid_public_key", exit_code=1)
        key_spec = fetched.strip()

    try:
        result = verify_certified_bundle(
            Path(args.bundle),
            public_key_spec=key_spec,
            allow_key_file=allow_key_file,
        )
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)

    if args.json:
        _emit_json(result)
    else:
        art = result["artifact"]
        assert isinstance(art, dict)
        write_line(
            sys.stdout,
            f"VERIFIED: {art['filename']} ({art['bytes']} bytes, sha256={art['sha256']})",
        )
    return 0


# ── admin ─────────────────────────────────────────────────────


def _add_admin_flags(parser: argparse.ArgumentParser) -> None:
    _add_common_client_flags(parser, include_no_start=True, include_api_key=True)
    parser.add_argument(
        "--admin-token",
        help="Administrator bearer token (otherwise VIPERCAPTURE_ADMIN_TOKEN).",
    )


def cmd_admin(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture admin",
        description="Manage ViperCapture control plane projects, API keys, audit log, and operator status.",
    )
    sub = parser.add_subparsers(dest="section", required=True)

    p_proj = sub.add_parser("projects", help="Manage projects (/v1/admin/projects).")
    proj_sub = p_proj.add_subparsers(dest="proj_action", required=True)
    p_proj_list = proj_sub.add_parser("list", help="List projects.")
    _add_admin_flags(p_proj_list)

    p_proj_create = proj_sub.add_parser("create", help="Create a project.")
    p_proj_create.add_argument("name_pos", nargs="?", metavar="NAME", help="Project name.")
    p_proj_create.add_argument("--name", dest="name_opt", help="Project name.")
    p_proj_create.add_argument("--rpm", type=int, default=60, help="Requests per minute (default: 60).")
    p_proj_create.add_argument("--concurrency", type=int, default=2, help="Max concurrent renders (default: 2).")
    _add_admin_flags(p_proj_create)

    p_keys = sub.add_parser("keys", help="Manage project API keys (/v1/admin/projects/{id}/keys, /v1/admin/keys/{id}).")
    keys_sub = p_keys.add_subparsers(dest="key_action", required=True)
    p_key_create = keys_sub.add_parser("create", help="Create a project API key.")
    p_key_create.add_argument("project_id", help="Project ID.")
    p_key_create.add_argument("--name", default="default", help="Key name (default: default).")
    p_key_create.add_argument(
        "--scope",
        dest="scopes",
        action="append",
        choices=_ADMIN_SCOPES,
        help="Allowed scope (repeatable; defaults to all scopes).",
    )
    _add_admin_flags(p_key_create)

    p_key_revoke = keys_sub.add_parser("revoke", help="Revoke an API key.")
    p_key_revoke.add_argument("key_id", help="API key ID.")
    _add_admin_flags(p_key_revoke)

    p_audit = sub.add_parser("audit", help="Read recent control-plane audit events (/v1/admin/audit).")
    p_audit.add_argument("--limit", type=int, default=100, help="Max events to return (1-1000).")
    _add_admin_flags(p_audit)

    p_status = sub.add_parser("status", help="Read operator status (/v1/admin/status).")
    _add_admin_flags(p_status)

    args = parser.parse_args(argv)
    _load_local_env_for_target(args)
    try:
        root = api_root(args.server)
    except ValueError as exc:
        return fail(str(exc), exit_code=2)

    token = _admin_token(args)
    try:
        started = ensure_server(args)
        if args.section == "projects" and args.proj_action == "list":
            resp = open_response(
                root + "/v1/admin/projects",
                method="GET",
                payload=None,
                headers=_auth_headers(args, started_server=started, token_override=token),
                timeout=30.0,
            )
            try:
                items = _read_json_value(resp)
            finally:
                resp.close()
            projects = items if isinstance(items, list) else []
            if args.json:
                _emit_json({"ok": True, "count": len(projects), "projects": projects})
            elif not projects:
                write_line(sys.stdout, "No projects found.")
            else:
                for proj in projects:
                    if isinstance(proj, dict):
                        write_line(
                            sys.stdout,
                            f"{proj.get('id')}\t{proj.get('name')}\trpm={proj.get('requests_per_minute')}\tconcurrency={proj.get('concurrency')}",
                        )
            return 0

        if args.section == "projects" and args.proj_action == "create":
            name = (args.name_opt or args.name_pos or "").strip()
            if not name:
                return fail("Project name is required (pass NAME or --name NAME).", exit_code=2)
            resp = open_response(
                root + "/v1/admin/projects",
                method="POST",
                payload={
                    "name": name,
                    "requests_per_minute": args.rpm,
                    "concurrency": args.concurrency,
                },
                headers=_auth_headers(args, started_server=started, content_type="application/json", token_override=token),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, **doc})
            else:
                write_line(sys.stdout, f"{doc.get('id')}\t{doc.get('name')}")
            return 0

        if args.section == "keys" and args.key_action == "create":
            scopes = list(dict.fromkeys(args.scopes)) if args.scopes else list(_ADMIN_SCOPES)
            encoded = quote(args.project_id.strip(), safe="")
            resp = open_response(
                root + f"/v1/admin/projects/{encoded}/keys",
                method="POST",
                payload={"name": args.name, "scopes": scopes},
                headers=_auth_headers(args, started_server=started, content_type="application/json", token_override=token),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, **doc})
            else:
                write_line(sys.stdout, f"id: {doc.get('id')}")
                write_line(sys.stdout, f"api_key: {doc.get('api_key')}")
                write_line(sys.stdout, f"scopes: {', '.join(doc.get('scopes', []))}")
            return 0

        if args.section == "keys" and args.key_action == "revoke":
            encoded = quote(args.key_id.strip(), safe="")
            resp = open_response(
                root + f"/v1/admin/keys/{encoded}",
                method="DELETE",
                payload=None,
                headers=_auth_headers(args, started_server=started, token_override=token),
                timeout=30.0,
            )
            resp.close()
            if args.json:
                _emit_json({"ok": True, "revoked": args.key_id.strip()})
            else:
                write_line(sys.stdout, f"Revoked API key: {args.key_id.strip()}")
            return 0

        if args.section == "audit":
            if not (1 <= args.limit <= 1000):
                return fail("--limit must be between 1 and 1000.", exit_code=2)
            resp = open_response(
                root + f"/v1/admin/audit?limit={args.limit}",
                method="GET",
                payload=None,
                headers=_auth_headers(args, started_server=started, token_override=token),
                timeout=30.0,
            )
            try:
                items = _read_json_value(resp)
            finally:
                resp.close()
            events = items if isinstance(items, list) else []
            if args.json:
                _emit_json({"ok": True, "count": len(events), "events": events})
            elif not events:
                write_line(sys.stdout, "No audit events.")
            else:
                for ev in events:
                    if isinstance(ev, dict):
                        write_line(
                            sys.stdout,
                            f"{ev.get('created_at')}\t{ev.get('actor')}\t{ev.get('action')}\t{ev.get('resource') or ''}",
                        )
            return 0

        if args.section == "status":
            resp = open_response(
                root + "/v1/admin/status",
                method="GET",
                payload=None,
                headers=_auth_headers(args, started_server=started, token_override=token),
                timeout=30.0,
            )
            try:
                doc = _read_json(resp)
            finally:
                resp.close()
            if args.json:
                _emit_json({"ok": True, **doc})
            else:
                write_line(sys.stdout, json.dumps(doc, ensure_ascii=False, indent=2))
            return 0
    except CaptureError as exc:
        return fail(str(exc), status=exc.status, code=exc.code, body=exc.body, exit_code=exc.exit_code)
    return 2


# ── completion ────────────────────────────────────────────────


_TOP_COMMANDS = (
    "capture", "version", "status", "logs", "gpu", "preset", "diff",
    "baseline", "bulk", "job", "schedule", "profile", "sign", "verify",
    "admin", "completion", "update", "kill",
)
_TOP_FLAGS = ("--one-window", "--gui", "--viewport", "--host", "--port", "--version", "--help")
_SUBCOMMAND_TREE: dict[str, tuple[str, ...]] = {
    "gpu": ("status", "off", "auto"),
    "preset": ("list", "show", "save", "delete"),
    "baseline": ("save", "list", "delete", "compare"),
    "job": ("status", "cancel", "download"),
    "schedule": ("create", "list", "get", "update", "delete"),
    "profile": ("create", "import", "delete"),
    "admin": ("projects", "keys", "audit", "status"),
    "completion": ("bash", "zsh", "fish", "powershell"),
    "update": ("--check", "--help"),
}
_CAPTURE_MAJOR_FLAGS = (
    "--url", "--urls-file", "--html", "--html-file", "--markdown", "--markdown-file",
    "--base-url", "--from-json", "--preset", "--output", "--output-dir", "--output-template",
    "--format", "--engine", "--full-page", "--no-full-page", "--viewport", "--scale",
    "--device", "--selector", "--clip", "--lazy-load", "--profile", "--cache", "--cache-key",
    "--wait", "--wait-text", "--wait-event", "--wait-images", "--delay-ms", "--timeout-ms",
    "--click", "--hover", "--hide", "--fill", "--press", "--press-on", "--select", "--scroll",
    "--scroll-to", "--js", "--js-file", "--action-wait", "--action-wait-text", "--pause-ms",
    "--action", "--color-scheme", "--reduced-motion", "--media", "--locale", "--timezone",
    "--css", "--css-file", "--javascript", "--no-javascript", "--user-agent", "--header",
    "--cookie", "--cookie-file", "--block-url", "--block-resource", "--proxy", "--consent",
    "--block-ads", "--block-trackers", "--block-chats", "--block-newsletters", "--stealth",
    "--quality", "--image-width", "--image-height", "--transparent", "--pdf-mode", "--paper",
    "--orientation", "--video-duration-ms", "--fps", "--bitrate-mbps", "--diagnostics",
    "--har", "--trace", "--warc", "--deterministic", "--certify", "--profile-id",
    "--save-profile", "--webhook", "--server", "--api-key", "--async", "--no-wait",
    "--poll-ms", "--retries", "--retry-delay-ms", "--open", "--copy", "--no-start",
    "--stop-server", "--no-config", "--dry-run", "--json", "--quiet", "--verbose", "--help",
)


def _bash_completion() -> str:
    cmds = " ".join(_TOP_COMMANDS)
    top_flags = " ".join(_TOP_FLAGS)
    cap_flags = " ".join(_CAPTURE_MAJOR_FLAGS)
    cases = "\n".join(
        f"        {name}) COMPREPLY=( $(compgen -W \"{' '.join(subs)}\" -- \"$cur\") ); return 0 ;;"
        for name, subs in _SUBCOMMAND_TREE.items()
    )
    return f"""# bash completion for vipercapture
_vipercapture_completions() {{
    local cur prev cmd
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "{cmds} {top_flags}" -- "$cur") )
        return 0
    fi
    cmd="${{COMP_WORDS[1]}}"
    if [[ $COMP_CWORD -eq 2 ]]; then
        case "$cmd" in
{cases}
        capture) COMPREPLY=( $(compgen -W "{cap_flags}" -- "$cur") ); return 0 ;;
        esac
    fi
    case "$cmd" in
        capture|bulk|sign|schedule)
            COMPREPLY=( $(compgen -W "{cap_flags}" -- "$cur") )
            return 0
            ;;
        admin)
            if [[ $COMP_CWORD -eq 3 ]]; then
                case "$prev" in
                    projects) COMPREPLY=( $(compgen -W "list create" -- "$cur") ); return 0 ;;
                    keys) COMPREPLY=( $(compgen -W "create revoke" -- "$cur") ); return 0 ;;
                esac
            fi
            COMPREPLY=( $(compgen -W "--server --admin-token --api-key --json --help" -- "$cur") )
            return 0
            ;;
        *)
            COMPREPLY=( $(compgen -W "--server --api-key --json --help" -- "$cur") )
            return 0
            ;;
    esac
}}
complete -F _vipercapture_completions vipercapture
"""


def _zsh_completion() -> str:
    cmd_desc = "\n".join(f"        '{c}:{c} command'" for c in _TOP_COMMANDS)
    cap_opts = " ".join(_CAPTURE_MAJOR_FLAGS)
    cases = "\n".join(
        f"        {name}) compadd -- {' '.join(subs)} ;;"
        for name, subs in _SUBCOMMAND_TREE.items()
    )
    return f"""#compdef vipercapture
# zsh completion for vipercapture

_vipercapture() {{
    local -a commands
    commands=(
{cmd_desc}
    )
    if (( CURRENT == 2 )); then
        _describe 'command' commands
        compadd -- {' '.join(_TOP_FLAGS)}
        return
    fi
    case "$words[2]" in
{cases}
        capture|bulk|sign)
            compadd -- {cap_opts}
            ;;
        *)
            compadd -- --server --api-key --json --help
            ;;
    esac
}}

compdef _vipercapture vipercapture
"""


def _fish_completion() -> str:
    lines = ["# fish completion for vipercapture", "complete -c vipercapture -f"]
    for cmd in _TOP_COMMANDS:
        lines.append(f"complete -c vipercapture -n '__fish_use_subcommand' -a '{cmd}'")
    for flag in _TOP_FLAGS:
        clean = flag.lstrip("-")
        lines.append(f"complete -c vipercapture -n '__fish_use_subcommand' -l '{clean}'")
    for parent, subs in _SUBCOMMAND_TREE.items():
        for sub_name in subs:
            if sub_name.startswith("--"):
                lines.append(f"complete -c vipercapture -n '__fish_seen_subcommand_from {parent}' -l '{sub_name[2:]}'")
            else:
                lines.append(f"complete -c vipercapture -n '__fish_seen_subcommand_from {parent}' -a '{sub_name}'")
    for flag in _CAPTURE_MAJOR_FLAGS:
        lines.append(f"complete -c vipercapture -n '__fish_seen_subcommand_from capture' -l '{flag[2:]}'")
    return "\n".join(lines) + "\n"


def _powershell_completion() -> str:
    cmds = ", ".join(f"'{c}'" for c in _TOP_COMMANDS)
    top_flags = ", ".join(f"'{f}'" for f in _TOP_FLAGS)
    cap_flags = ", ".join(f"'{f}'" for f in _CAPTURE_MAJOR_FLAGS)
    sub_entries = "\n".join(
        f"        '{name}' = @({', '.join(repr(s) for s in subs)})"
        for name, subs in _SUBCOMMAND_TREE.items()
    )
    return f"""# PowerShell completion for vipercapture
Register-ArgumentCompleter -Native -CommandName vipercapture -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)
    $tokens = $commandAst.CommandElements | ForEach-Object {{ $_.Extent.Text }}
    $topCommands = @({cmds}, {top_flags})
    $subMap = @{{
{sub_entries}
    }}
    $captureFlags = @({cap_flags})
    if ($tokens.Count -le 1 -or ($tokens.Count -eq 2 -and $wordToComplete -ne '')) {{
        $candidates = $topCommands
    }} else {{
        $cmd = $tokens[1]
        if ($subMap.ContainsKey($cmd) -and ($tokens.Count -eq 2 -or ($tokens.Count -eq 3 -and $wordToComplete -ne ''))) {{
            $candidates = $subMap[$cmd]
        }} elseif ($cmd -in @('capture', 'bulk', 'sign', 'schedule')) {{
            $candidates = $captureFlags
        }} else {{
            $candidates = @('--server', '--api-key', '--json', '--help')
        }}
    }}
    $candidates | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }}
}}
"""


def generate_completion(shell: str) -> str:
    if shell == "bash":
        return _bash_completion()
    if shell == "zsh":
        return _zsh_completion()
    if shell == "fish":
        return _fish_completion()
    if shell == "powershell":
        return _powershell_completion()
    raise ValueError(f"Unsupported shell: {shell}")


def cmd_completion(argv: list[str]) -> int:
    parser = CaptureParser(
        prog="vipercapture completion",
        description="Output shell completion scripts for bash, zsh, fish, or powershell.",
    )
    parser.add_argument("shell", choices=("bash", "zsh", "fish", "powershell"), help="Target shell.")
    args = parser.parse_args(argv)
    sys.stdout.write(generate_completion(args.shell))
    sys.stdout.flush()
    return 0


# ── Dispatcher ────────────────────────────────────────────────

_DISPATCH = {
    "version": cmd_version,
    "status": cmd_status,
    "logs": cmd_logs,
    "gpu": cmd_gpu,
    "preset": cmd_preset,
    "diff": cmd_diff,
    "baseline": cmd_baseline,
    "bulk": cmd_bulk,
    "job": cmd_job,
    "schedule": cmd_schedule,
    "profile": cmd_profile,
    "sign": cmd_sign,
    "verify": cmd_verify,
    "admin": cmd_admin,
    "completion": cmd_completion,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] in {"-h", "--help"}:
        write_line(sys.stdout, f"Available subcommands: {', '.join(SUBCOMMANDS)}")
        return 0
    command = raw[0]
    handler = _DISPATCH.get(command)
    if handler is None:
        return fail(f"Unknown command: {command}", exit_code=2)
    try:
        return handler(raw[1:])
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    except KeyboardInterrupt:
        return fail("Cancelled.", exit_code=130)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
