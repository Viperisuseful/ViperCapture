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


def test_render_single_rejects_private_subresources_before_every_success_return() -> None:
    import inspect

    from vipercapture.render_engine import RenderEngine

    source = inspect.getsource(RenderEngine._render_single)
    lines = [line.strip() for line in source.splitlines()]
    success_returns = [
        index for index, line in enumerate(lines) if line == "return finalized"
    ]
    assert success_returns
    for index in success_returns:
        assert (
            lines[index - 1]
            == "_reject_blocked_private_subresources(blocked_private_subresources)"
        ), "hosted private-subresource policy must run on the success path"


def test_link_local_metadata_url_is_not_public() -> None:
    from vipercapture.render_engine import PublicUrlValidator

    async def check() -> None:
        validator = PublicUrlValidator()
        assert await validator.is_public("http://169.254.169.254/latest/meta-data/") is False
        assert await validator.is_public("http://127.0.0.1/") is False

    asyncio.run(check())


def test_hosted_mode_always_installs_request_routing() -> None:
    from vipercapture.render_engine import needs_request_routing

    assert needs_request_routing(True, {}, False) is True
    assert needs_request_routing(False, {}, False) is False
