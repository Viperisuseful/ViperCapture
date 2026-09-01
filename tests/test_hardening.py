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
