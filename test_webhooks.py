import asyncio
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

    async def post(self, url, *, content, headers, extensions):
        self.requests.append((url, content, headers, extensions))
        return httpx.Response(self.statuses.pop(0))


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_sender_connects_directly_to_validated_address(self):
        received = asyncio.get_running_loop().create_future()

        async def handle(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            content_length = 0
            for line in head.decode("ascii").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1])
            body = await reader.readexactly(content_length)
            received.set_result((head, body))
            writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await WebhookDispatcher(
                secret="w" * 32,
                allow_private=True,
            ).deliver(
                f"http://127.0.0.1:{port}/hook?event=done",
                {"id": "job-id", "status": "succeeded"},
            )
            head, body = await asyncio.wait_for(received, timeout=1)
        finally:
            server.close()
            await server.wait_closed()
        self.assertIn(b"POST /hook?event=done HTTP/1.1", head)
        self.assertEqual(json.loads(body)["event"], "render.succeeded")

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
        with patch(
            "webhooks.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
        ):
            await dispatcher.deliver(
                "http://callback.example/hook",
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
        self.assertEqual(headers["Host"], "callback.example")
        self.assertEqual(client.requests[-1][0], "http://127.0.0.1/hook")
        sleep.assert_awaited_once()

    async def test_private_url_is_blocked_by_default(self):
        dispatcher = WebhookDispatcher(secret="w" * 32)
        with patch(
            "webhooks.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
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
        with patch(
            "webhooks.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
        ):
            with self.assertRaises(WebhookDeliveryError) as raised:
                await dispatcher.deliver(
                    "http://callback.example/hook",
                    {"id": "job-id", "status": "failed"},
                )
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(client.requests), 1)

    async def test_delivery_uses_the_address_that_was_validated(self):
        client = FakeClient([204])
        dispatcher = WebhookDispatcher(
            secret="w" * 32,
            client_factory=lambda: client,
        )
        public_address = "93.184.216.34"
        with patch(
            "webhooks.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", (public_address, 443))],
        ):
            await dispatcher.deliver(
                "https://callback.example/hooks?id=1",
                {"id": "job-id", "status": "succeeded"},
            )

        url, _body, headers, extensions = client.requests[0]
        self.assertEqual(url, f"https://{public_address}/hooks?id=1")
        self.assertEqual(headers["Host"], "callback.example")
        self.assertEqual(extensions["sni_hostname"], "callback.example")
