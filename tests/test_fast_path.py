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


def test_signed_url_preserves_explicit_slow_encoder() -> None:
    from vipercapture.signed_urls import decode_render_request, encode_render_request

    request = RenderRequest.model_validate(
        {
            "html": "<h1>ready</h1>",
            "output": "png",
            "image": {"optimize_for_speed": False},
        }
    )
    restored = decode_render_request(encode_render_request(request))
    assert restored.image.optimize_for_speed is False


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


def test_stealth_context_options_prefers_cdp_over_throwaway_page() -> None:
    import asyncio
    from vipercapture.main import _stealth_context_options

    class FakeCdpSession:
        def __init__(self) -> None:
            self.detached = False

        async def send(self, method: str) -> dict[str, str]:
            assert method == "Browser.getVersion"
            return {"userAgent": "Mozilla/5.0 HeadlessChrome/153.0.0.0 Safari/537.36"}

        async def detach(self) -> None:
            self.detached = True

    class FakeBrowser:
        def __init__(self) -> None:
            self.session = FakeCdpSession()
            self.new_page_calls = 0

        async def new_browser_cdp_session(self) -> FakeCdpSession:
            return self.session

        async def new_page(self):
            self.new_page_calls += 1
            raise AssertionError("new_page should not be called when CDP succeeds")

    browser = FakeBrowser()
    req = RenderRequest.model_validate({"html": "<h1>ok</h1>", "output": "png"})
    opts = asyncio.run(_stealth_context_options(browser, req))  # type: ignore[arg-type]
    assert opts == {"user_agent": "Mozilla/5.0 Chrome/153.0.0.0 Safari/537.36"}
    assert browser.session.detached is True
    assert browser.new_page_calls == 0
    # Second call returns cached UA without creating another CDP session
    opts2 = asyncio.run(_stealth_context_options(browser, req))  # type: ignore[arg-type]
    assert opts2 == opts


def test_page_cleanup_frozenset_domain_matching() -> None:
    from vipercapture.page_cleanup import CleanupOptions, should_block_resource

    opts = CleanupOptions(block_ads=True, block_trackers=True, block_chats=True)
    assert should_block_resource("https://doubleclick.net/a.js", opts) == "ads"
    assert should_block_resource("https://stats.g.doubleclick.net/a.js", opts) == "ads"
    assert should_block_resource("https://notdoubleclick.net/a.js", opts) is None
    assert should_block_resource("https://www.google-analytics.com/g.js", opts) == "trackers"
    assert should_block_resource("https://widget.intercom.io/v1", opts) == "chats"
    assert should_block_resource("https://example.com/app.js", opts) is None


def test_lifespan_cancellation_closes_launched_browser() -> None:
    import asyncio
    from types import SimpleNamespace
    import vipercapture.main as main_mod

    browser_closed = False
    pw_stopped = False
    browser_ready = asyncio.Event()
    cache_gate = asyncio.Event()

    class FakeBrowser:
        async def close(self) -> None:
            nonlocal browser_closed
            browser_closed = True

    class FakePlaywright:
        devices = {}

        async def stop(self) -> None:
            nonlocal pw_stopped
            pw_stopped = True

    class FakePlaywrightCtx:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    async def fake_launch_browser(_pw) -> FakeBrowser:
        browser_ready.set()
        return FakeBrowser()

    async def blocked_cache_start(_self) -> None:
        await cache_gate.wait()

    async def run() -> None:
        fake_app = SimpleNamespace(state=SimpleNamespace())
        with (
            mock.patch.object(main_mod, "async_playwright", return_value=FakePlaywrightCtx()),
            mock.patch.object(main_mod, "_launch_browser", side_effect=fake_launch_browser),
            mock.patch.object(main_mod.RenderCache, "start", blocked_cache_start),
            mock.patch.object(main_mod, "CONTROL_ENABLED", False),
            mock.patch.object(main_mod, "ASYNC_JOBS_ENABLED", False),
            mock.patch.object(main_mod, "SCHEDULES_ENABLED", False),
        ):
            async def enter_lifespan() -> None:
                async with main_mod.lifespan(fake_app):  # type: ignore[arg-type]
                    pass

            task = asyncio.create_task(enter_lifespan())
            await browser_ready.wait()
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    assert browser_closed is True
    assert pw_stopped is True


def test_lifespan_serves_before_chromium_starts_and_renders_wait_for_it() -> None:
    import asyncio
    from types import SimpleNamespace
    import vipercapture.main as main_mod

    launch_gate = asyncio.Event()
    closed: list[str] = []

    class FakeBrowser:
        def is_connected(self) -> bool:
            return True

        async def close(self) -> None:
            closed.append("browser")

    class FakePlaywright:
        devices = {}

        async def stop(self) -> None:
            closed.append("playwright")

    class FakePlaywrightCtx:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    async def gated_launch_browser(_pw) -> FakeBrowser:
        await launch_gate.wait()
        return FakeBrowser()

    async def no_cache_start(_self) -> None:
        return None

    async def run() -> None:
        fake_app = SimpleNamespace(state=SimpleNamespace())
        with (
            mock.patch.object(main_mod, "async_playwright", return_value=FakePlaywrightCtx()),
            mock.patch.object(main_mod, "_launch_browser", side_effect=gated_launch_browser),
            mock.patch.object(main_mod.RenderCache, "start", no_cache_start),
            mock.patch.object(main_mod, "CONTROL_ENABLED", False),
            mock.patch.object(main_mod, "ASYNC_JOBS_ENABLED", False),
            mock.patch.object(main_mod, "SCHEDULES_ENABLED", False),
            mock.patch.object(main_mod, "GPU_MODE", "off"),
            mock.patch.object(main_mod, "BROWSER_POOL_SIZE", 1),
        ):
            lifespan = main_mod.lifespan(fake_app)  # type: ignore[arg-type]
            await asyncio.wait_for(lifespan.__aenter__(), timeout=2)
            assert main_mod._chromium_ready(fake_app) is False  # type: ignore[arg-type]
            pending = asyncio.create_task(
                main_mod._browser_for(fake_app, main_mod.BrowserEngine.CHROMIUM)  # type: ignore[arg-type]
            )
            await asyncio.sleep(0)
            assert not pending.done()
            launch_gate.set()
            browser = await asyncio.wait_for(pending, timeout=2)
            assert isinstance(browser, FakeBrowser)
            assert fake_app.state.browser is browser
            assert main_mod._chromium_ready(fake_app) is True  # type: ignore[arg-type]
            await lifespan.__aexit__(None, None, None)

    asyncio.run(run())
    assert closed == ["browser", "playwright"]


def test_async_job_defers_without_attempt_when_chromium_failed_to_start() -> None:
    import asyncio
    import pytest
    import vipercapture.main as main_mod
    from vipercapture.async_jobs import JobDeferred

    async def failed_start() -> None:
        raise RuntimeError("chromium missing")

    async def run() -> None:
        startup = asyncio.ensure_future(failed_start())
        await asyncio.gather(startup, return_exceptions=True)
        with (
            mock.patch.object(main_mod.app.state, "browser_startup", startup, create=True),
            mock.patch.object(main_mod.app.state, "playwright", None, create=True),
            mock.patch.object(main_mod, "current_job", return_value=None),
        ):
            with pytest.raises(JobDeferred):
                await main_mod._render_async_image(mock.Mock())

    asyncio.run(run())


def test_render_cache_indexed_put_evicts_without_rescanning_disk() -> None:
    import asyncio
    import tempfile
    from pathlib import Path
    from vipercapture.render_cache import RenderCache
    from vipercapture.render_engine import RenderArtifact

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = RenderCache(Path(tmp), ttl_seconds=300, max_entries=2, max_bytes=1024 * 1024)
            await cache.start()
            with mock.patch.object(cache, "_trim", side_effect=AssertionError("put should use index")):
                reqs = [
                    RenderRequest.model_validate({"html": f"<h1>{i}</h1>", "output": "png", "cache": True})
                    for i in range(3)
                ]
                for i, req in enumerate(reqs):
                    await cache.put(req, RenderArtifact(f"img-{i}".encode(), "image/png", f"{i}.png", {}))
                    await asyncio.sleep(0.02)
            assert await cache.get(reqs[0]) is None
            assert (await cache.get(reqs[1])) is not None
            assert (await cache.get(reqs[2])) is not None

    asyncio.run(run())
