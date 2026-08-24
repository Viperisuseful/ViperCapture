from __future__ import annotations

import os
from unittest import mock

from vipercapture.main import default_browser_pool_size, default_max_concurrency
from vipercapture.render_contract import (
    CaptureProfile,
    LazyLoadMode,
    RenderRequest,
    canonical_render_document,
)
from vipercapture.render_engine import OutputFormat, cdp_screenshot_options


def test_preview_profile_enables_fast_png_and_adaptive_lazy_load() -> None:
    request = RenderRequest.model_validate(
        {"html": "<h1>ready</h1>", "output": "png", "full_page": True}
    )
    assert request.profile is CaptureProfile.PREVIEW
    assert request.lazy_load is LazyLoadMode.ADAPTIVE
    assert request.image.optimize_for_speed is True


def test_preview_does_not_enable_fast_encode_for_jpeg() -> None:
    request = RenderRequest.model_validate(
        {"html": "<h1>ready</h1>", "output": "jpeg"}
    )
    assert request.image.optimize_for_speed is False


def test_accurate_profile_keeps_thorough_lazy_load() -> None:
    request = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "profile": "accurate",
        }
    )
    assert request.profile is CaptureProfile.ACCURATE
    assert request.lazy_load is LazyLoadMode.THOROUGH
    assert request.image.optimize_for_speed is False


def test_explicit_lazy_load_wins_over_profile() -> None:
    request = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "profile": "preview",
            "lazy_load": "none",
            "image": {"optimize_for_speed": False},
        }
    )
    assert request.lazy_load is LazyLoadMode.NONE
    assert request.image.optimize_for_speed is False


def test_deterministic_preview_does_not_enable_fast_encode() -> None:
    request = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "deterministic": {"enabled": True},
        }
    )
    assert request.image.optimize_for_speed is False


def test_cache_key_requires_cache() -> None:
    try:
        RenderRequest.model_validate(
            {"html": "<h1>ready</h1>", "output": "png", "cache_key": "hero"}
        )
    except Exception as exc:
        assert "cache_key requires cache=true" in str(exc)
    else:
        raise AssertionError("cache_key without cache should fail")


def test_explicit_slow_encoder_survives_canonical_roundtrip() -> None:
    request = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "image": {"optimize_for_speed": False},
        }
    )
    restored = RenderRequest.model_validate(canonical_render_document(request))
    assert restored.image.optimize_for_speed is False


def test_cache_key_is_part_of_fingerprint() -> None:
    first = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "cache": True,
            "cache_key": "a",
        }
    )
    second = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "cache": True,
            "cache_key": "b",
        }
    )
    assert canonical_render_document(first) != canonical_render_document(second)


def test_cdp_options_include_optimize_for_speed() -> None:
    options = cdp_screenshot_options(
        OutputFormat.PNG,
        clip={"x": 0, "y": 0, "width": 10, "height": 10, "scale": 1},
        quality=None,
        optimize_for_speed=True,
        capture_beyond_viewport=True,
    )
    assert options["optimizeForSpeed"] is True
    assert options["fromSurface"] is True


def test_default_concurrency_is_cpu_sized() -> None:
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VIPERCAPTURE_MAX_CONCURRENCY", None)
        with mock.patch("os.cpu_count", return_value=4):
            assert default_max_concurrency() == 4
        with mock.patch("os.cpu_count", return_value=16):
            assert default_max_concurrency() == 8


def test_replace_idle_wait_ignores_calling_render() -> None:
    import asyncio

    from vipercapture.main import _wait_for_browser_idle

    class FakeBrowser:
        pass

    class FakeApp:
        def __init__(self) -> None:
            self.state = type("State", (), {})()

    browser = FakeBrowser()
    app = FakeApp()
    app.state.browser_in_flight = {id(browser): 1}
    assert asyncio.run(_wait_for_browser_idle(app, browser, timeout=0.2, reserved=1))
    app.state.browser_in_flight = {id(browser): 2}
    assert not asyncio.run(_wait_for_browser_idle(app, browser, timeout=0.15, reserved=1))


def test_pool_status_inspects_each_browser() -> None:
    from vipercapture.main import _chromium_ready, _pool_connected

    class FakeBrowser:
        def __init__(self, connected: bool) -> None:
            self._connected = connected

        def is_connected(self) -> bool:
            return self._connected

    class FakeApp:
        def __init__(self) -> None:
            self.state = type("State", (), {})()

    from vipercapture.render_contract import BrowserEngine

    app = FakeApp()
    app.state.browser = FakeBrowser(False)
    app.state.browsers = {
        BrowserEngine.CHROMIUM: [FakeBrowser(False), FakeBrowser(True)]
    }
    assert _chromium_ready(app) is True
    assert _pool_connected(app.state.browsers[BrowserEngine.CHROMIUM]) is True
    assert _pool_connected(FakeBrowser(True)) is True


def test_browser_pool_size_is_half_of_concurrency() -> None:
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VIPERCAPTURE_BROWSER_POOL_SIZE", None)
        assert default_browser_pool_size(4) == 2
        assert default_browser_pool_size(1) == 1
