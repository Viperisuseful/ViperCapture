from __future__ import annotations

from vipercapture.render_contract import DevicePreset, NamedViewport, RenderRequest
from vipercapture.render_engine import (
    DEVICE_DESCRIPTOR_FALLBACKS,
    apply_device_metrics,
    device_context_options,
    viewport_from_named,
)

IPHONE_14 = {
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "viewport": {"width": 390, "height": 664},
    "screen": {"width": 390, "height": 844},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
    "default_browser_type": "webkit",
}
DESCRIPTORS = {"iPhone 14": IPHONE_14}


def _device_only() -> RenderRequest:
    return RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "full_page": False,
            "environment": {"device": "iphone_14"},
        }
    )


def test_device_only_applies_playwright_viewport_dsf_and_touch() -> None:
    request = _device_only()
    resolved = apply_device_metrics(request, DESCRIPTORS)
    options = device_context_options(request, DESCRIPTORS)

    assert resolved.viewport.width == 390
    assert resolved.viewport.height == 664
    assert resolved.viewport.device_scale_factor == 3
    assert options["viewport"] == {"width": 390, "height": 664}
    assert options["screen"] == {"width": 390, "height": 844}
    assert options["device_scale_factor"] == 3
    assert options["has_touch"] is True
    assert options["user_agent"] == IPHONE_14["user_agent"]
    assert "default_browser_type" not in options


def test_device_only_does_not_keep_desktop_defaults() -> None:
    request = _device_only()
    options = device_context_options(request, DESCRIPTORS)
    assert options["viewport"] != {"width": 1280, "height": 720}
    assert options["device_scale_factor"] != 1


def test_explicit_viewport_and_dsf_override_device() -> None:
    request = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "full_page": False,
            "environment": {"device": "iphone_14"},
            "viewport": {"width": 800, "height": 600, "device_scale_factor": 2},
        }
    )
    resolved = apply_device_metrics(request, DESCRIPTORS)
    options = device_context_options(request, DESCRIPTORS)

    assert resolved.viewport.width == 800
    assert resolved.viewport.height == 600
    assert resolved.viewport.device_scale_factor == 2
    assert options["viewport"] == {"width": 800, "height": 600}
    assert options["screen"] == {"width": 800, "height": 600}
    assert options["device_scale_factor"] == 2
    assert options["has_touch"] is True
    assert options["user_agent"] == IPHONE_14["user_agent"]


def test_partial_viewport_override_keeps_device_height_and_dsf() -> None:
    request = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "full_page": False,
            "environment": {"device": "iphone_14"},
            "viewport": {"width": 360},
        }
    )
    resolved = apply_device_metrics(request, DESCRIPTORS)
    options = device_context_options(request, DESCRIPTORS)

    assert resolved.viewport.width == 360
    assert resolved.viewport.height == 664
    assert resolved.viewport.device_scale_factor == 3
    assert options["viewport"] == {"width": 360, "height": 664}
    assert options["device_scale_factor"] == 3


def test_desktop_keeps_default_viewport() -> None:
    request = RenderRequest.model_validate(
        {"html": "<h1>ready</h1>", "output": "png", "full_page": False}
    )
    resolved = apply_device_metrics(request, DESCRIPTORS)
    options = device_context_options(request, DESCRIPTORS)

    assert resolved.viewport.width == 1280
    assert resolved.viewport.height == 720
    assert resolved.viewport.device_scale_factor == 1
    assert options["viewport"] == {"width": 1280, "height": 720}
    assert options["device_scale_factor"] == 1
    assert options.get("has_touch") is not True


def test_named_viewport_device_only_stays_implicit() -> None:
    named = NamedViewport.model_validate({"name": "phone", "device": "iphone_14"})
    copied = viewport_from_named(named)
    assert copied.model_fields_set == set()
    request = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "full_page": False,
            "environment": {"device": named.device.value},
        }
    ).model_copy(update={"viewport": copied})
    resolved = apply_device_metrics(request, DESCRIPTORS)
    assert resolved.viewport.width == 390
    assert resolved.viewport.height == 664
    assert resolved.environment.device is DevicePreset.IPHONE_14


def test_signed_url_snapshots_device_metrics() -> None:
    from vipercapture.signed_urls import decode_render_request, encode_render_request

    restored = decode_render_request(encode_render_request(_device_only()))
    fallback = DEVICE_DESCRIPTOR_FALLBACKS[DevicePreset.IPHONE_14]
    assert restored.viewport.width == fallback["viewport"]["width"]
    assert restored.viewport.height == fallback["viewport"]["height"]
    assert restored.viewport.device_scale_factor == fallback["device_scale_factor"]


def test_fallback_metrics_when_playwright_registry_missing() -> None:
    request = _device_only()
    resolved = apply_device_metrics(request, {})
    options = device_context_options(request, {})
    fallback = DEVICE_DESCRIPTOR_FALLBACKS[DevicePreset.IPHONE_14]

    assert resolved.viewport.width == fallback["viewport"]["width"]
    assert resolved.viewport.height == fallback["viewport"]["height"]
    assert resolved.viewport.device_scale_factor == fallback["device_scale_factor"]
    assert options["has_touch"] is True
    assert options["viewport"] == fallback["viewport"]
