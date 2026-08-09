"""Strict adapters for the commonly used ScreenshotOne and Urlbox options."""

from __future__ import annotations

from collections.abc import Mapping

from render_contract import RenderRequest


def _boolean(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def screenshotone_request(query: Mapping[str, str]) -> RenderRequest:
    allowed = {
        "access_key", "url", "format", "viewport_width", "viewport_height", "device_scale_factor",
        "full_page", "selector", "delay", "block_ads", "block_cookie_banners",
        "dark_mode", "reduced_motion", "cache", "image_quality",
    }
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise ValueError("unsupported ScreenshotOne options: " + ", ".join(unknown))
    if "url" not in query:
        raise ValueError("url is required")
    output = {"jpg": "jpeg"}.get(query.get("format", "png"), query.get("format", "png"))
    selector = query.get("selector")
    full_page = _boolean(query.get("full_page"), selector is None)
    return RenderRequest.model_validate(
        {
            "url": query["url"],
            "output": output,
            "viewport": {
                "width": int(query.get("viewport_width", 1280)),
                "height": int(query.get("viewport_height", 720)),
                "device_scale_factor": float(query.get("device_scale_factor", 1)),
            },
            "full_page": full_page,
            "selector": selector,
            "wait_for": {
                "delay_ms": int(float(query.get("delay", 0)) * 1000)
            },
            "cleanup": {
                "block_ads": _boolean(query.get("block_ads"), False),
                "consent_mode": "hide" if _boolean(query.get("block_cookie_banners"), False) else "none",
            },
            "environment": {
                "color_scheme": "dark" if _boolean(query.get("dark_mode"), False) else None,
                "reduced_motion": "reduce" if _boolean(query.get("reduced_motion"), False) else None,
            },
            "cache": _boolean(query.get("cache"), False),
            "image": {"quality": int(query["image_quality"]) if "image_quality" in query else None},
        }
    )


def urlbox_request(document: Mapping[str, object]) -> RenderRequest:
    mapping = dict(document)
    aliases = {
        "width": ("viewport", "width"),
        "height": ("viewport", "height"),
        "retina": ("viewport", "device_scale_factor"),
        "full_page": (None, "full_page"),
        "selector": (None, "selector"),
        "format": (None, "output"),
        "url": (None, "url"),
        "css": (None, "custom_css"),
    }
    result: dict[str, object] = {}
    viewport: dict[str, object] = {}
    for key, value in mapping.items():
        if key not in aliases:
            raise ValueError(f"unsupported Urlbox option: {key}")
        group, target = aliases[key]
        if key == "retina":
            value = 2 if bool(value) else 1
        if key == "format" and value == "jpg":
            value = "jpeg"
        (viewport if group == "viewport" else result)[target] = value
    if viewport:
        result["viewport"] = viewport
    if result.get("selector") is not None and "full_page" not in result:
        result["full_page"] = False
    return RenderRequest.model_validate(result)
