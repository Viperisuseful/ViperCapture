import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bulk_jobs import (
    MAX_DIFF_BODY_BYTES,
    MAX_JSON_BODY_BYTES,
    BulkBodyLimitMiddleware,
    BulkJobRequest,
)
from render_errors import RenderError, install_render_error_layer


class BulkJobContractTests(unittest.TestCase):
    def test_bulk_request_accepts_named_render_items(self):
        request = BulkJobRequest.model_validate(
            {
                "items": [
                    {
                        "id": "homepage",
                        "request_id": "release-42-homepage",
                        "render": {"url": "https://example.com"},
                    },
                    {
                        "id": "mobile",
                        "render": {
                            "url": "https://example.com",
                            "viewport": {"width": 390, "height": 844},
                        },
                    },
                ]
            }
        )
        self.assertEqual(len(request.items), 2)
        self.assertEqual(request.items[0].id, "homepage")

    def test_bulk_request_is_bounded(self):
        with self.assertRaises(ValidationError):
            BulkJobRequest.model_validate({"items": []})
        with self.assertRaises(ValidationError):
            BulkJobRequest.model_validate(
                {"items": [{"render": {"url": "https://example.com"}}] * 101}
            )


class BulkBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_limits_json_and_multipart_routes_before_receive(self):
        called = False

        async def downstream(_scope, _receive, _send):
            nonlocal called
            called = True

        async def receive():
            raise AssertionError("oversized declared body must not be read")

        middleware = BulkBodyLimitMiddleware(downstream, max_bytes=5)
        for path, size in (
            ("/v1/render", MAX_JSON_BODY_BYTES + 1),
            ("/v1/diff", MAX_DIFF_BODY_BYTES + 1),
            (
                "/v1/schedules/00000000-0000-0000-0000-000000000001",
                MAX_JSON_BODY_BYTES + 1,
            ),
        ):
            with self.assertRaises(RenderError):
                await middleware(
                    {
                        "type": "http",
                        "method": "PATCH" if "schedules/" in path else "POST",
                        "path": path,
                        "headers": [(b"content-length", str(size).encode())],
                    },
                    receive,
                    lambda _message: None,
                )
        self.assertFalse(called)

    async def test_rejects_streamed_body_before_downstream_parsing(self):
        called = False

        async def downstream(_scope, _receive, _send):
            nonlocal called
            called = True

        chunks = iter(
            [
                {"type": "http.request", "body": b"123", "more_body": True},
                {"type": "http.request", "body": b"456", "more_body": False},
            ]
        )

        async def receive():
            return next(chunks)

        middleware = BulkBodyLimitMiddleware(downstream, max_bytes=5)
        with self.assertRaises(RenderError) as raised:
            await middleware(
                {"type": "http", "method": "POST", "path": "/v1/jobs/bulk", "headers": []},
                receive,
                lambda _message: None,
            )
        self.assertEqual(raised.exception.code, "request_body_too_large")
        self.assertFalse(called)

    def test_limit_uses_standard_error_envelope(self):
        app = FastAPI()
        app.add_middleware(BulkBodyLimitMiddleware, max_bytes=5)
        install_render_error_layer(app)

        @app.post("/v1/jobs/bulk")
        async def accept_bulk():
            return {"accepted": True}

        response = TestClient(app, raise_server_exceptions=False).post(
            "/v1/jobs/bulk", content=b"123456"
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_body_too_large")
        self.assertEqual(response.headers["x-request-id"], response.json()["error"]["request_id"])
