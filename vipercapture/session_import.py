"""Normalize browser cookie exports into Playwright storage state."""

from __future__ import annotations

import json
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from .render_errors import RenderError

MAX_IMPORT_BYTES = 5 * 1024 * 1024
SUPPORTED_FORMATS = {"auto", "playwright", "cookies_json", "netscape", "cookie_header"}


def _same_site(value: object) -> str:
    normalized = str(value or "Lax").lower().replace("_", "-")
    return {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no-restriction": "None",
        "unspecified": "Lax",
    }.get(normalized, "Lax")


def _cookie(item: dict[str, object], *, default_domain: str | None = None) -> dict[str, object]:
    name = str(item.get("name") or "")
    domain = str(item.get("domain") or default_domain or "")
    if not name or not domain:
        raise ValueError("each cookie requires name and domain")
    raw_expires = item.get("expires", item.get("expirationDate", -1))
    expires = float(raw_expires) if raw_expires not in (None, "") else -1
    if expires <= 0:
        expires = -1
    partition_key = item.get("partitionKey")
    if partition_key is not None and (
        not isinstance(partition_key, str) or not partition_key
    ):
        raise ValueError("cookie partitionKey must be a non-empty string")
    cookie = {
        "name": name,
        "value": str(item.get("value") or ""),
        "domain": domain,
        "path": str(item.get("path") or "/"),
        "expires": expires,
        "httpOnly": bool(item.get("httpOnly", False)),
        "secure": bool(item.get("secure", False)),
        "sameSite": _same_site(item.get("sameSite")),
    }
    if partition_key is not None:
        cookie["partitionKey"] = partition_key
    return cookie


def _origin_host(origin: str | None) -> tuple[str, bool]:
    parsed = urlsplit(origin or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("origin is required for Cookie header imports")
    return parsed.hostname, parsed.scheme == "https"


def _parse_json(content: str, format_name: str) -> dict[str, object]:
    document = json.loads(content)
    if format_name == "playwright":
        if not isinstance(document, dict):
            raise ValueError("Playwright storage state must be a JSON object")
        if "cookies" not in document or "origins" not in document:
            raise ValueError("Playwright storage state requires cookie and origin arrays")
        cookies = document["cookies"]
        origins = document["origins"]
        if not isinstance(cookies, list) or not isinstance(origins, list):
            raise ValueError("Playwright storage state requires cookie and origin arrays")
        if any(not isinstance(item, dict) for item in cookies):
            raise ValueError("cookies must be JSON objects")
        return {"cookies": [_cookie(item) for item in cookies], "origins": origins}
    if not isinstance(document, list):
        raise TypeError("browser cookie exports must be a JSON array")
    if any(not isinstance(item, dict) for item in document):
        raise ValueError("cookies must be JSON objects")
    return {"cookies": [_cookie(item) for item in document], "origins": []}


def _parse_netscape(content: str) -> dict[str, object]:
    cookies = []
    for number, raw_line in enumerate(content.splitlines(), 1):
        line = raw_line.strip("\r\n")
        http_only = line.startswith("#HttpOnly_")
        if not line.strip() or (line.startswith("#") and not http_only):
            continue
        if http_only:
            line = line.removeprefix("#HttpOnly_")
        parts = line.split("\t")
        if len(parts) != 7:
            raise ValueError(f"invalid Netscape cookie on line {number}")
        domain, include_subdomains, path, secure, expires, name, value = parts
        if include_subdomains.upper() == "TRUE" and not domain.startswith("."):
            domain = f".{domain}"
        cookies.append(
            _cookie(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "secure": secure.upper() == "TRUE",
                    "httpOnly": http_only,
                    "expires": expires,
                }
            )
        )
    return {"cookies": cookies, "origins": []}


def _parse_cookie_header(content: str, origin: str | None) -> dict[str, object]:
    domain, secure = _origin_host(origin)
    jar = SimpleCookie()
    header = content.strip()
    if header.lower().startswith("cookie:"):
        header = header.partition(":")[2].strip()
    try:
        jar.load(header)
    except CookieError as exc:
        raise ValueError("Cookie header is malformed") from exc
    if not jar:
        raise ValueError("Cookie header did not contain any cookies")
    return {
        "cookies": [
            _cookie(
                {
                    "name": name,
                    "value": morsel.value,
                    "domain": domain,
                    "secure": secure,
                    "expires": -1,
                }
            )
            for name, morsel in jar.items()
        ],
        "origins": [],
    }


def import_storage_state(
    content: str,
    *,
    format_name: str = "auto",
    origin: str | None = None,
) -> dict[str, object]:
    """Parse Playwright, Cookie-Editor JSON, cookies.txt, or a Cookie header."""
    if format_name not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported session import format {format_name!r}")
    if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise RenderError(
            "session_import_too_large",
            "Session import content may not exceed 5 MiB.",
            413,
            False,
        )
    content = content.removeprefix("\ufeff")
    if not content.strip():
        raise ValueError("session import content is empty")
    selected = format_name
    if selected == "auto":
        stripped = content.lstrip()
        selected = (
            "playwright"
            if stripped.startswith("{")
            else "cookies_json"
            if stripped.startswith("[")
            else "netscape"
            if "\t" in content or stripped.startswith("# Netscape")
            else "cookie_header"
        )
    if selected in {"playwright", "cookies_json"}:
        return _parse_json(content, selected)
    if selected == "netscape":
        return _parse_netscape(content)
    return _parse_cookie_header(content, origin)
