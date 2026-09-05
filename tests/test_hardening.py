from __future__ import annotations

import asyncio

import pytest

from vipercapture.render_contract import RenderRequest
from vipercapture.render_errors import RenderError
from vipercapture.signed_urls import sign_render_request, verify_render_request
from vipercapture.webhooks import WebhookDispatcher

SECRET = "0123456789abcdef0123456789abcdef"


def _dispatcher() -> WebhookDispatcher:
    return WebhookDispatcher(secret=SECRET)


def test_webhook_rejects_private_ipv4_mapped_ipv6() -> None:
    dispatcher = _dispatcher()

    async def check() -> None:
        await dispatcher.validate_url("http://[::ffff:169.254.169.254]/latest")

    with pytest.raises(RenderError) as details:
        asyncio.run(check())
    assert details.value.status_code == 422


def test_webhook_rejects_out_of_range_port() -> None:
    dispatcher = _dispatcher()

    async def check() -> None:
        await dispatcher.validate_url("http://example.com:70000/hook")

    with pytest.raises(RenderError) as details:
        asyncio.run(check())
    assert details.value.status_code == 422


def test_verify_render_request_rejects_empty_secret() -> None:
    payload, expires, signature = sign_render_request(
        RenderRequest.model_validate({"html": "<h1>x</h1>", "output": "png"}),
        secret=SECRET,
        ttl_seconds=60,
        now=1_000_000,
    )
    with pytest.raises(ValueError):
        verify_render_request(payload, expires, signature, secret="", now=1_000_000)


def test_responses_carry_nosniff_and_frame_ancestors() -> None:
    from fastapi.testclient import TestClient

    from vipercapture.main import app

    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_html_and_markdown_skip_main_target_and_use_about_blank() -> None:
    html = RenderRequest.model_validate(
        {
            "html": '<h1>h</h1><img src="http://169.254.169.254/latest/meta-data/">',
            "output": "metadata",
        }
    )
    markdown = RenderRequest.model_validate(
        {
            "markdown": "![x](http://169.254.169.254/latest/meta-data/)",
            "output": "png",
        }
    )
    for request in (html, markdown):
        assert request.url is None
        target = str(request.url or request.base_url or "about:blank")
        assert target == "about:blank"


def test_hosted_success_path_rejects_blocked_private_subresources() -> None:
    from vipercapture.render_engine import (
        _private_subresource_error,
        _reject_blocked_private_subresources,
    )

    _reject_blocked_private_subresources(False)

    with pytest.raises(RenderError) as details:
        _reject_blocked_private_subresources(True)
    error = details.value
    assert error.code == "subresource_not_public"
    assert error.message == "The page requested a private or non-public resource."
    assert error.status_code == 400
    assert error.retryable is False

    constructed = _private_subresource_error()
    assert constructed.code == error.code
    assert constructed.status_code == error.status_code
    assert constructed.retryable is False


def test_render_single_rejects_private_subresources_before_persist_and_typed_errors() -> None:
    import inspect

    from vipercapture.render_engine import RenderEngine

    source = inspect.getsource(RenderEngine._render_single)
    lines = [line.strip() for line in source.splitlines()]
    persist_indexes = [
        index
        for index, line in enumerate(lines)
        if line.startswith("await self._persist_profile")
    ]
    assert persist_indexes
    for index in persist_indexes:
        assert (
            lines[index - 1]
            == "_reject_blocked_private_subresources(blocked_private_subresources)"
        ), "do not persist profile state from a rejected hosted render"
        assert (
            lines[index - 2]
            == "await _await_pending_hosted_validations(pending_hosted_validations)"
        ), "await in-flight hosted validations before inspecting the flag"
    assert "except TimeoutError as exc:" in source
    assert "except RenderError as exc:" in source
    assert source.count("if blocked_private_subresources") >= 3
    assert source.count("await _await_pending_hosted_validations(pending_hosted_validations)") >= 14
    assert "blocked_private_subresources = True" in source
    assert "public_urls.is_public(web_socket.url)" in source


def test_link_local_metadata_url_is_not_public() -> None:
    from vipercapture.render_engine import PublicUrlValidator

    async def check() -> None:
        validator = PublicUrlValidator()
        assert await validator.is_public("http://169.254.169.254/latest/meta-data/") is False
        assert await validator.is_public("http://127.0.0.1/") is False

    asyncio.run(check())


def test_websocket_urls_use_the_same_publicness_policy_as_http() -> None:
    from vipercapture.render_engine import PublicUrlValidator, normalized_origin

    assert normalized_origin("ws://127.0.0.1:8080/socket") == ("ws", "127.0.0.1", 8080)
    assert normalized_origin("wss://Example.COM/socket") == ("wss", "example.com", 443)
    assert normalized_origin("ftp://example.com/") is None

    async def check() -> None:
        validator = PublicUrlValidator()
        assert await validator.is_public("ws://127.0.0.1:8080") is False
        assert await validator.is_public("wss://169.254.169.254/latest/meta-data/") is False
        assert await validator.is_public("ws://[::1]:8080") is False

    asyncio.run(check())


def test_hosted_mode_always_installs_request_routing() -> None:
    from vipercapture.render_engine import needs_request_routing

    assert needs_request_routing(True, {}, False) is True
    assert needs_request_routing(False, {}, False) is False


class _FakeRoute:
    def __init__(self, url: str, resource_type: str = "image") -> None:
        self.request = type(
            "Req",
            (),
            {
                "url": url,
                "resource_type": resource_type,
                "headers": {},
                "frame": type("Frame", (), {"parent_frame": None})(),
                "is_navigation_request": lambda self: False,
            },
        )()
        self.aborted = False

    async def abort(self, _reason: str) -> None:
        self.aborted = True

    async def continue_(self, headers=None) -> None:
        return None


class _FakeWebSocket:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False
        self.connected = False

    async def close(self, code: int = 1008, reason: str = "") -> None:
        self.closed = True

    async def connect_to_server(self) -> None:
        self.connected = True


def _fake_browser(
    route_urls: list[str],
    handler_slot: dict | None = None,
    *,
    websocket_urls: list[str] | None = None,
    background_routes: bool = False,
):
    route_handler: dict[str, object] = handler_slot if handler_slot is not None else {}
    socket_urls = list(websocket_urls or [])

    async def _dispatch(handler, target) -> None:
        started = handler(target)
        if background_routes:
            asyncio.create_task(started)
            return
        await started

    class FakePage:
        url = "about:blank"
        frames: list[object] = []

        def on(self, *_args, **_kwargs) -> None:
            return None

        async def set_content(self, _document, **_kwargs) -> None:
            handler = route_handler.get("fn")
            if handler is not None:
                for url in route_urls:
                    await _dispatch(handler, _FakeRoute(url))
            ws_handler = route_handler.get("ws")
            if ws_handler is not None:
                for url in socket_urls:
                    await _dispatch(ws_handler, _FakeWebSocket(url))

        async def close(self) -> None:
            return None

    class FakeContext:
        async def route(self, _pattern, handler) -> None:
            route_handler["fn"] = handler

        async def route_web_socket(self, _pattern, handler) -> None:
            route_handler["ws"] = handler

        async def new_page(self) -> FakePage:
            return FakePage()

        async def close(self) -> None:
            return None

        async def storage_state(self) -> dict[str, object]:
            return {"cookies": [{"name": "leaked"}]}

    class FakeBrowser:
        async def new_context(self, **_kwargs) -> FakeContext:
            return FakeContext()

        def is_connected(self) -> bool:
            return True

    return FakeBrowser()


def _run_isolated_render(engine, request, browser, *, metadata_calls: list | None = None):
    from unittest.mock import AsyncMock, patch

    from vipercapture.render_engine import RenderArtifact, RenderLimits

    async def fake_metadata(_page, _selectors):
        if metadata_calls is not None:
            metadata_calls.append(True)
        return RenderArtifact(b'{"title":"h"}', "application/json", "meta.json")

    async def run():
        with (
            patch.object(engine, "_wait", new=AsyncMock()),
            patch.object(engine, "_wait_for_images", new=AsyncMock()),
            patch.object(engine, "_run_actions", new=AsyncMock()),
            patch.object(engine, "_check_assertions", new=AsyncMock()),
            patch("vipercapture.render_engine.render_metadata", fake_metadata),
        ):
            return await engine._render_single(browser, request, RenderLimits())

    return asyncio.run(run())


def test_hosted_html_private_subresource_fails_on_success_path() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": '<h1>h</h1><img src="http://169.254.169.254/latest/meta-data/">',
            "output": "metadata",
            "full_page": False,
        }
    )
    metadata_calls: list[bool] = []
    with pytest.raises(RenderError) as details:
        _run_isolated_render(
            engine,
            request,
            _fake_browser(["http://169.254.169.254/latest/meta-data/"]),
            metadata_calls=metadata_calls,
        )
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400
    assert details.value.retryable is False
    assert metadata_calls == []


def test_hosted_markdown_private_subresource_fails_on_success_path() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "markdown": "![x](http://169.254.169.254/latest/meta-data/)",
            "output": "metadata",
            "full_page": False,
        }
    )
    with pytest.raises(RenderError) as details:
        _run_isolated_render(
            engine,
            request,
            _fake_browser(["http://169.254.169.254/latest/meta-data/"]),
        )
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400


def test_hosted_html_without_private_subresource_succeeds() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": "<h1>h</h1><img src=\"data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7\">",
            "output": "metadata",
            "full_page": False,
        }
    )
    artifact = _run_isolated_render(
        engine,
        request,
        _fake_browser(
            [
                "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
            ]
        ),
    )
    assert artifact.media_type == "application/json"
    assert artifact.metadata.get("blocked_subresources") == 0


def test_hosted_private_subresource_still_wins_on_unrelated_exception() -> None:
    from unittest.mock import AsyncMock, patch

    from vipercapture.render_engine import RenderEngine, RenderLimits

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {"html": "<h1>h</h1>", "output": "metadata", "full_page": False}
    )
    handler_slot: dict[str, object] = {}

    async def boom(*_args, **_kwargs):
        await handler_slot["fn"](_FakeRoute("http://169.254.169.254/latest/meta-data/"))
        raise RuntimeError("unrelated capture failure")

    async def run() -> None:
        with (
            patch.object(engine, "_wait", new=boom),
            patch.object(engine, "_wait_for_images", new=AsyncMock()),
            patch.object(engine, "_run_actions", new=AsyncMock()),
            patch.object(engine, "_check_assertions", new=AsyncMock()),
        ):
            await engine._render_single(
                _fake_browser([], handler_slot=handler_slot),
                request,
                RenderLimits(),
            )

    with pytest.raises(RenderError) as details:
        asyncio.run(run())
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400
    assert "unrelated capture failure" in str(details.value.__cause__)


def test_self_host_html_private_subresource_is_allowed() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=False)
    request = RenderRequest.model_validate(
        {
            "html": '<h1>h</h1><img src="http://169.254.169.254/latest/meta-data/">',
            "output": "metadata",
            "full_page": False,
        }
    )
    artifact = _run_isolated_render(
        engine,
        request,
        _fake_browser(["http://169.254.169.254/latest/meta-data/"]),
    )
    assert artifact.media_type == "application/json"
    assert artifact.metadata.get("blocked_subresources") == 0


def test_hosted_direct_private_url_is_target_not_public() -> None:
    from vipercapture.render_engine import RenderEngine, RenderLimits

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {"url": "http://169.254.169.254/latest/meta-data/", "output": "png"}
    )

    class UnusedBrowser:
        async def new_context(self, **_kwargs):
            raise AssertionError("private main target must fail before a browser context")

        def is_connected(self) -> bool:
            return True

    async def run() -> None:
        await engine._render_single(UnusedBrowser(), request, RenderLimits())

    with pytest.raises(RenderError) as details:
        asyncio.run(run())
    assert details.value.code == "target_not_public"
    assert details.value.status_code == 400


def test_self_host_direct_private_url_passes_public_check() -> None:
    from vipercapture.render_engine import RenderEngine, RenderLimits

    engine = RenderEngine(hosted=False)
    request = RenderRequest.model_validate(
        {"url": "http://127.0.0.1/", "output": "png"}
    )
    reached = {"context": False}

    class ProbeBrowser:
        async def new_context(self, **_kwargs):
            reached["context"] = True
            raise RuntimeError("self-host-allowed-private-target")

        def is_connected(self) -> bool:
            return True

    async def run() -> None:
        await engine._render_single(ProbeBrowser(), request, RenderLimits())

    with pytest.raises(RenderError) as details:
        asyncio.run(run())
    assert reached["context"] is True
    assert details.value.code == "render_failed"
    assert "self-host-allowed-private-target" in str(details.value.__cause__)


PRIVATE_IMG = "http://169.254.169.254/latest/meta-data/"


def test_hosted_private_subresource_wins_over_block_resource_types() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": f'<h1>h</h1><img src="{PRIVATE_IMG}">',
            "output": "metadata",
            "full_page": False,
            "network": {"block_resource_types": ["image"]},
        }
    )
    with pytest.raises(RenderError) as details:
        _run_isolated_render(engine, request, _fake_browser([PRIVATE_IMG]))
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400


def test_hosted_private_subresource_wins_over_block_url_patterns() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": f'<h1>h</h1><img src="{PRIVATE_IMG}">',
            "output": "metadata",
            "full_page": False,
            "network": {"block_url_patterns": ["*169.254*"]},
        }
    )
    with pytest.raises(RenderError) as details:
        _run_isolated_render(engine, request, _fake_browser([PRIVATE_IMG]))
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400


def test_hosted_private_subresource_wins_over_cleanup_category() -> None:
    from vipercapture.render_engine import CleanupHooks, RenderEngine

    async def setup(_page, _mode):
        return None

    async def finish(_page, _session):
        return {}

    async def apply(_page, _cleanup):
        return {}

    engine = RenderEngine(
        hosted=True,
        cleanup_hooks=CleanupHooks(
            setup=setup,
            finish=finish,
            apply=apply,
            blocked_category=lambda url, _cleanup: "ads" if "169.254" in url else None,
        ),
    )
    request = RenderRequest.model_validate(
        {
            "html": f'<h1>h</h1><img src="{PRIVATE_IMG}">',
            "output": "metadata",
            "full_page": False,
        }
    )
    with pytest.raises(RenderError) as details:
        _run_isolated_render(engine, request, _fake_browser([PRIVATE_IMG]))
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400


def test_hosted_private_subresource_wins_over_typed_render_error() -> None:
    from unittest.mock import AsyncMock, patch

    from vipercapture.render_engine import RenderEngine, RenderLimits

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {"html": "<h1>h</h1>", "output": "metadata", "full_page": False}
    )
    handler_slot: dict[str, object] = {}

    async def fail_assertion(*_args, **_kwargs):
        await handler_slot["fn"](_FakeRoute(PRIVATE_IMG))
        raise RenderError(
            "content_assertion_failed",
            "Required page content was not present.",
            424,
            False,
        )

    async def run() -> None:
        with (
            patch.object(engine, "_wait", new=AsyncMock()),
            patch.object(engine, "_wait_for_images", new=AsyncMock()),
            patch.object(engine, "_run_actions", new=AsyncMock()),
            patch.object(engine, "_check_assertions", new=fail_assertion),
        ):
            await engine._render_single(
                _fake_browser([], handler_slot=handler_slot),
                request,
                RenderLimits(),
            )

    with pytest.raises(RenderError) as details:
        asyncio.run(run())
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400
    assert details.value.__cause__.code == "content_assertion_failed"


def test_hosted_private_subresource_wins_over_deadline_timeout() -> None:
    from unittest.mock import AsyncMock, patch

    from vipercapture.render_engine import RenderEngine, RenderLimits

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {"html": "<h1>h</h1>", "output": "metadata", "full_page": False}
    )
    handler_slot: dict[str, object] = {}

    async def timeout_after_private(*_args, **_kwargs):
        await handler_slot["fn"](_FakeRoute(PRIVATE_IMG))
        raise TimeoutError("render deadline")

    async def run() -> None:
        with (
            patch.object(engine, "_wait", new=timeout_after_private),
            patch.object(engine, "_wait_for_images", new=AsyncMock()),
            patch.object(engine, "_run_actions", new=AsyncMock()),
            patch.object(engine, "_check_assertions", new=AsyncMock()),
        ):
            await engine._render_single(
                _fake_browser([], handler_slot=handler_slot),
                request,
                RenderLimits(),
            )

    with pytest.raises(RenderError) as details:
        asyncio.run(run())
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400
    assert isinstance(details.value.__cause__, TimeoutError)


def test_hosted_private_subresource_does_not_persist_profile() -> None:
    from vipercapture.render_engine import RenderEngine

    saved: list[tuple[str, dict[str, object]]] = []

    async def load_profile(_profile_id: str):
        return {}

    async def save_profile(profile_id: str, state: dict[str, object]) -> None:
        saved.append((profile_id, state))

    engine = RenderEngine(
        hosted=True,
        profile_loader=load_profile,
        profile_saver=save_profile,
    )
    request = RenderRequest.model_validate(
        {
            "html": f'<h1>h</h1><img src="{PRIVATE_IMG}">',
            "output": "metadata",
            "full_page": False,
            "profile_id": "site-a",
            "save_profile": True,
        }
    )
    with pytest.raises(RenderError) as details:
        _run_isolated_render(engine, request, _fake_browser([PRIVATE_IMG]))
    assert details.value.code == "subresource_not_public"
    assert saved == []


def test_hosted_action_private_subresource_stops_before_encode() -> None:
    from unittest.mock import AsyncMock, patch

    from vipercapture.render_engine import RenderEngine, RenderLimits

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {"html": "<h1>h</h1>", "output": "metadata", "full_page": False}
    )
    handler_slot: dict[str, object] = {}
    metadata_calls: list[bool] = []

    async def action_fetches_private(*_args, **_kwargs):
        await handler_slot["fn"](_FakeRoute(PRIVATE_IMG))

    async def fake_metadata(_page, _selectors):
        metadata_calls.append(True)
        raise AssertionError("encode must not run after a private subresource")

    async def run() -> None:
        with (
            patch.object(engine, "_wait", new=AsyncMock()),
            patch.object(engine, "_wait_for_images", new=AsyncMock()),
            patch.object(engine, "_run_actions", new=action_fetches_private),
            patch.object(engine, "_check_assertions", new=AsyncMock()),
            patch("vipercapture.render_engine.render_metadata", fake_metadata),
        ):
            await engine._render_single(
                _fake_browser([], handler_slot=handler_slot),
                request,
                RenderLimits(),
            )

    with pytest.raises(RenderError) as details:
        asyncio.run(run())
    assert details.value.code == "subresource_not_public"
    assert metadata_calls == []


PRIVATE_WS = "ws://127.0.0.1:8080"


def test_hosted_private_websocket_fails_on_success_path() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": f'<h1>h</h1><script>new WebSocket("{PRIVATE_WS}")</script>',
            "output": "metadata",
            "full_page": False,
        }
    )
    metadata_calls: list[bool] = []
    with pytest.raises(RenderError) as details:
        _run_isolated_render(
            engine,
            request,
            _fake_browser([], websocket_urls=[PRIVATE_WS]),
            metadata_calls=metadata_calls,
        )
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400
    assert details.value.retryable is False
    assert metadata_calls == []


def test_hosted_private_wss_link_local_fails_on_success_path() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": '<h1>h</h1><script>new WebSocket("wss://169.254.169.254/socket")</script>',
            "output": "metadata",
            "full_page": False,
        }
    )
    with pytest.raises(RenderError) as details:
        _run_isolated_render(
            engine,
            request,
            _fake_browser([], websocket_urls=["wss://169.254.169.254/socket"]),
        )
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400


def test_hosted_public_websocket_is_blocked_without_private_rejection() -> None:
    from unittest.mock import patch

    from vipercapture.render_engine import PublicUrlValidator, RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": '<h1>h</h1><script>new WebSocket("wss://example.com/socket")</script>',
            "output": "metadata",
            "full_page": False,
        }
    )

    async def public_example(_self, target: str) -> bool:
        return "example.com" in target

    with patch.object(PublicUrlValidator, "is_public", public_example):
        artifact = _run_isolated_render(
            engine,
            request,
            _fake_browser([], websocket_urls=["wss://example.com/socket"]),
        )
    assert artifact.media_type == "application/json"
    assert artifact.metadata.get("blocked_subresources") == 1


def test_hosted_private_websocket_wins_over_block_resource_types() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": f'<h1>h</h1><script>new WebSocket("{PRIVATE_WS}")</script>',
            "output": "metadata",
            "full_page": False,
            "network": {"block_resource_types": ["websocket"]},
        }
    )
    with pytest.raises(RenderError) as details:
        _run_isolated_render(
            engine,
            request,
            _fake_browser([], websocket_urls=[PRIVATE_WS]),
        )
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400


def test_self_host_private_websocket_is_allowed() -> None:
    from vipercapture.render_engine import RenderEngine

    engine = RenderEngine(hosted=False)
    request = RenderRequest.model_validate(
        {
            "html": f'<h1>h</h1><script>new WebSocket("{PRIVATE_WS}")</script>',
            "output": "metadata",
            "full_page": False,
        }
    )
    artifact = _run_isolated_render(
        engine,
        request,
        _fake_browser([], websocket_urls=[PRIVATE_WS]),
    )
    assert artifact.media_type == "application/json"
    assert artifact.metadata.get("blocked_subresources") == 0


def test_hosted_awaits_in_flight_private_url_validation() -> None:
    from unittest.mock import patch

    from vipercapture.render_engine import PublicUrlValidator, RenderEngine

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": f'<h1>h</h1><img src="{PRIVATE_IMG}">',
            "output": "metadata",
            "full_page": False,
            "wait_for": {"event": "domcontentloaded", "images": False},
        }
    )
    original = PublicUrlValidator.is_public

    async def slow_is_public(self, target: str) -> bool:
        await asyncio.sleep(0.05)
        return await original(self, target)

    metadata_calls: list[bool] = []
    with patch.object(PublicUrlValidator, "is_public", slow_is_public):
        with pytest.raises(RenderError) as details:
            _run_isolated_render(
                engine,
                request,
                _fake_browser([PRIVATE_IMG], background_routes=True),
                metadata_calls=metadata_calls,
            )
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400
    assert metadata_calls == []


def test_hosted_timeout_awaits_in_flight_private_validation() -> None:
    from unittest.mock import AsyncMock, patch

    from vipercapture.render_engine import PublicUrlValidator, RenderEngine, RenderLimits

    engine = RenderEngine(hosted=True)
    request = RenderRequest.model_validate(
        {
            "html": "<h1>h</h1>",
            "output": "metadata",
            "full_page": False,
            "wait_for": {"event": "domcontentloaded", "images": False},
        }
    )
    handler_slot: dict[str, object] = {}
    original = PublicUrlValidator.is_public

    async def slow_is_public(self, target: str) -> bool:
        await asyncio.sleep(0.05)
        return await original(self, target)

    async def timeout_after_starting_route(*_args, **_kwargs):
        asyncio.create_task(handler_slot["fn"](_FakeRoute(PRIVATE_IMG)))
        raise TimeoutError("render deadline")

    async def run() -> None:
        with (
            patch.object(PublicUrlValidator, "is_public", slow_is_public),
            patch.object(engine, "_wait", new=timeout_after_starting_route),
            patch.object(engine, "_wait_for_images", new=AsyncMock()),
            patch.object(engine, "_run_actions", new=AsyncMock()),
            patch.object(engine, "_check_assertions", new=AsyncMock()),
        ):
            await engine._render_single(
                _fake_browser([], handler_slot=handler_slot),
                request,
                RenderLimits(),
            )

    with pytest.raises(RenderError) as details:
        asyncio.run(run())
    assert details.value.code == "subresource_not_public"
    assert details.value.status_code == 400
    assert isinstance(details.value.__cause__, TimeoutError)
