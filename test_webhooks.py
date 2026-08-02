import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from render_errors import RenderError
from webhooks import WebhookDeliveryError, WebhookDispatcher


class FakeClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, content, headers):
        self.requests.append((url, content, headers))
        return httpx.Response(self.statuses.pop(0))


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_delivery_retries_and_absolutizes_results(self):
        client = FakeClient([503, 204])
        sleep = AsyncMock()
        dispatcher = WebhookDispatcher(
            secret="w" * 32,
            public_url="https://capture.example",
            allow_private=True,
            client_factory=lambda: client,
            sleep=sleep,
        )
        await dispatcher.deliver(
            "http://127.0.0.1/hook",
            {
                "id": "job-id",
                "status": "succeeded",
                "status_url": "/v1/jobs/job-id",
                "result_url": "/v1/jobs/job-id/result",
            },
        )
        self.assertEqual(len(client.requests), 2)
        body = json.loads(client.requests[-1][1])
        self.assertEqual(body["event"], "render.succeeded")
        self.assertEqual(
            body["job"]["result_url"],
            "https://capture.example/v1/jobs/job-id/result",
        )
        headers = client.requests[-1][2]
        self.assertTrue(headers["X-ViperCapture-Webhook-Signature"].startswith("v1="))
        sleep.assert_awaited_once()

    async def test_private_url_is_blocked_by_default(self):
        dispatcher = WebhookDispatcher(secret="w" * 32)
        with patch.object(
            dispatcher.validator, "is_public", AsyncMock(return_value=False)
        ):
            with self.assertRaises(RenderError) as raised:
                await dispatcher.validate_url("http://127.0.0.1/hook")
        self.assertEqual(raised.exception.code, "webhook_url_not_public")

    async def test_permanent_failure_is_not_retried(self):
        client = FakeClient([400])
        dispatcher = WebhookDispatcher(
            secret="w" * 32,
            allow_private=True,
            client_factory=lambda: client,
            sleep=AsyncMock(),
        )
        with self.assertRaises(WebhookDeliveryError):
            await dispatcher.deliver(
                "http://127.0.0.1/hook",
                {"id": "job-id", "status": "failed"},
            )
        self.assertEqual(len(client.requests), 1)
