import asyncio
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from async_jobs import JobRecord
from bulk_jobs import BulkJobRequest
from render_contract import RenderRequest
from render_engine import RenderArtifact
from render_errors import RenderError


class PlatformRouteTests(unittest.TestCase):
    def test_openapi_exposes_platform_workflows(self):
        paths = main.app.openapi()["paths"]
        expected = {
            "/v1/render": {"post"},
            "/v1/render/signed": {"get"},
            "/v1/signed-url": {"post"},
            "/v1/diff": {"post"},
            "/v1/jobs": {"post"},
            "/v1/jobs/bulk": {"post"},
            "/v1/schedules": {"get", "post"},
            "/v1/schedules/{schedule_id}": {"get", "patch", "delete"},
        }
        for path, methods in expected.items():
            self.assertIn(path, paths)
            self.assertTrue(methods.issubset(paths[path]), path)

    def test_desktop_cors_allows_schedule_updates(self):
        self.assertIn("PATCH", main.DESKTOP_ALLOW_METHODS)


class PlatformRouteReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_bypasses_desktop_token(self):
        expected = main.Response(status_code=200)
        call_next = AsyncMock(return_value=expected)
        request = SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/health"),
            headers={},
        )
        with patch("main.DESKTOP_TOKEN", "secret"):
            response = await main.require_desktop_token(request, call_next)
        self.assertIs(response, expected)
        call_next.assert_awaited_once_with(request)

    async def test_signed_routes_use_their_own_authentication(self):
        expected = main.Response(status_code=200)
        call_next = AsyncMock(return_value=expected)
        with (
            patch("main.DESKTOP_TOKEN", "desktop"),
            patch("main.SIGNING_SECRET", "signing-secret"),
            patch("main.SIGNING_ADMIN_TOKEN", "signing-admin"),
        ):
            for method, path, headers in (
                ("GET", "/v1/render/signed", {}),
                (
                    "POST",
                    "/v1/signed-url",
                    {"authorization": "Bearer signing-admin"},
                ),
            ):
                request = SimpleNamespace(
                    method=method,
                    url=SimpleNamespace(path=path),
                    headers=headers,
                )
                response = await main.require_desktop_token(
                    request, call_next
                )
                self.assertIs(response, expected)
        self.assertEqual(call_next.await_count, 2)

    async def test_cached_render_bypasses_saturated_chromium_slots(self):
        original_cache = getattr(main.app.state, "render_cache", None)
        original_slots = getattr(main.app.state, "capture_slots", None)
        main.app.state.render_cache = SimpleNamespace(
            get=AsyncMock(
                return_value=RenderArtifact(
                    b"cached", "image/png", "capture.png", {}
                )
            )
        )
        main.app.state.capture_slots = asyncio.Semaphore(0)
        request = SimpleNamespace(
            is_disconnected=AsyncMock(return_value=False)
        )
        try:
            response = await asyncio.wait_for(
                main._render_response(
                    RenderRequest(html="cached", cache=True), request
                ),
                timeout=0.25,
            )
        finally:
            main.app.state.render_cache = original_cache
            main.app.state.capture_slots = original_slots
        self.assertEqual(response.body, b"cached")
        self.assertEqual(response.headers["x-vipercapture-cache"], "hit")

    async def test_cache_miss_is_rechecked_after_chromium_slot(self):
        original_cache = getattr(main.app.state, "render_cache", None)
        original_slots = getattr(main.app.state, "capture_slots", None)
        original_browser = getattr(main.app.state, "browser", None)
        cached = RenderArtifact(b"cached", "image/png", "capture.png")
        cache = SimpleNamespace(
            get=AsyncMock(side_effect=[None, cached]),
            put=AsyncMock(),
        )
        main.app.state.render_cache = cache
        main.app.state.capture_slots = asyncio.Semaphore(1)
        main.app.state.browser = SimpleNamespace(is_connected=lambda: True)
        engine = SimpleNamespace(render_image=AsyncMock())
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        try:
            with patch("main._render_engine", return_value=engine):
                response = await main._render_response(
                    RenderRequest(html="cached", cache=True), request
                )
        finally:
            main.app.state.render_cache = original_cache
            main.app.state.capture_slots = original_slots
            main.app.state.browser = original_browser
        self.assertEqual(response.body, b"cached")
        self.assertEqual(response.headers["x-vipercapture-cache"], "hit")
        self.assertEqual(cache.get.await_count, 2)
        engine.render_image.assert_not_awaited()

    async def test_signed_url_rejects_webhook_delivery(self):
        payload = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "delivery": {"webhook_url": "https://hooks.example/test"},
            }
        )
        request = SimpleNamespace(headers={"authorization": "Bearer admin"})
        with (
            patch("main.SIGNING_SECRET", "s" * 32),
            patch("main.SIGNING_ADMIN_TOKEN", "admin"),
        ):
            with self.assertRaises(RenderError) as raised:
                await main.create_signed_url(payload, request)
        self.assertEqual(raised.exception.code, "delivery_requires_async_job")

    async def test_signed_html_remains_an_attachment(self):
        html_request = RenderRequest.model_validate(
            {"url": "https://example.com", "output": "html"}
        )
        rendered = main.Response(
            b"<script>unsafe()</script>",
            media_type="text/html",
            headers={
                "Content-Disposition": 'attachment; filename="capture.html"'
            },
        )
        with (
            patch("main.SIGNING_SECRET", "s" * 32),
            patch("main.verify_render_request", return_value=html_request),
            patch("main._render_response", AsyncMock(return_value=rendered)),
        ):
            response = await main.render_signed(
                SimpleNamespace(),
                "payload",
                1,
                "signature",
            )
        self.assertTrue(
            response.headers["content-disposition"].startswith("attachment")
        )

    async def test_bulk_webhook_validation_failure_is_per_item(self):
        original_service = getattr(main.app.state, "async_jobs", None)
        original_dispatcher = getattr(main.app.state, "webhooks", None)
        now = datetime.now(timezone.utc)
        job = JobRecord(
            id="00000000-0000-0000-0000-000000000001",
            request_id="bulk-2",
            status="queued",
            payload=b"encrypted",
            attempt_count=0,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
        )
        service = SimpleNamespace(
            submit=AsyncMock(return_value=job),
            existing=AsyncMock(return_value=None),
        )
        dispatcher = SimpleNamespace(
            validate_url=AsyncMock(
                side_effect=[
                    RenderError(
                        "webhook_url_not_public", "blocked", 422, False
                    ),
                    None,
                ]
            )
        )
        main.app.state.async_jobs = service
        main.app.state.webhooks = dispatcher
        payload = BulkJobRequest.model_validate(
            {
                "items": [
                    {
                        "id": "bad",
                        "render": {
                            "url": "https://example.com/one",
                            "delivery": {
                                "webhook_url": "https://bad.example/hook"
                            },
                        },
                    },
                    {
                        "id": "good",
                        "render": {
                            "url": "https://example.com/two",
                            "delivery": {
                                "webhook_url": "https://good.example/hook"
                            },
                        },
                    },
                ]
            }
        )
        try:
            response = await main.create_bulk_render_jobs(
                payload,
                SimpleNamespace(state=SimpleNamespace(request_id="bulk")),
            )
        finally:
            main.app.state.async_jobs = original_service
            main.app.state.webhooks = original_dispatcher
        document = json.loads(response.body)
        self.assertEqual(response.status_code, 207)
        self.assertFalse(document["results"][0]["accepted"])
        self.assertTrue(document["results"][1]["accepted"])
        service.submit.assert_awaited_once()

    async def test_bulk_webhook_validation_has_aggregate_deadline(self):
        original_service = getattr(main.app.state, "async_jobs", None)
        original_dispatcher = getattr(main.app.state, "webhooks", None)
        service = SimpleNamespace(
            submit=AsyncMock(),
            existing=AsyncMock(return_value=None),
        )

        async def never_validates(_url):
            await asyncio.Event().wait()

        main.app.state.async_jobs = service
        main.app.state.webhooks = SimpleNamespace(
            validate_url=never_validates
        )
        payload = BulkJobRequest.model_validate(
            {
                "items": [
                    {
                        "id": str(index),
                        "render": {
                            "url": f"https://example.com/{index}",
                            "delivery": {
                                "webhook_url": f"https://hooks{index}.example/callback"
                            },
                        },
                    }
                    for index in range(2)
                ]
            }
        )
        try:
            with patch(
                "main.BULK_WEBHOOK_VALIDATION_TIMEOUT_SECONDS", 0.01
            ):
                response = await main.create_bulk_render_jobs(
                    payload,
                    SimpleNamespace(
                        state=SimpleNamespace(request_id="deadline")
                    ),
                )
        finally:
            main.app.state.async_jobs = original_service
            main.app.state.webhooks = original_dispatcher
        document = json.loads(response.body)
        self.assertEqual(response.status_code, 207)
        self.assertEqual(document["failed"], 2)
        self.assertTrue(
            all(
                item["error"]["code"]
                == "bulk_webhook_validation_timeout"
                for item in document["results"]
            )
        )
        service.submit.assert_not_awaited()

    async def test_bulk_idempotent_replay_skips_webhook_validation(self):
        original_service = getattr(main.app.state, "async_jobs", None)
        original_dispatcher = getattr(main.app.state, "webhooks", None)
        now = datetime.now(timezone.utc)
        existing = JobRecord(
            id="00000000-0000-0000-0000-000000000002",
            request_id="replay-1",
            status="queued",
            payload=b"encrypted",
            attempt_count=0,
            available_at=now,
            queue_expires_at=now + timedelta(minutes=1),
            created_at=now,
        )
        service = SimpleNamespace(
            existing=AsyncMock(return_value=existing),
            submit=AsyncMock(),
        )
        validate = AsyncMock(side_effect=RuntimeError("must not run"))
        main.app.state.async_jobs = service
        main.app.state.webhooks = SimpleNamespace(validate_url=validate)
        payload = BulkJobRequest.model_validate(
            {
                "items": [
                    {
                        "request_id": "replay-1",
                        "render": {
                            "url": "https://example.com",
                            "delivery": {
                                "webhook_url": "https://hooks.example/callback"
                            },
                        },
                    }
                ]
            }
        )
        try:
            response = await main.create_bulk_render_jobs(
                payload,
                SimpleNamespace(state=SimpleNamespace(request_id="bulk")),
            )
        finally:
            main.app.state.async_jobs = original_service
            main.app.state.webhooks = original_dispatcher
        self.assertEqual(response.status_code, 202)
        validate.assert_not_awaited()
        service.submit.assert_not_awaited()

    async def test_async_cache_hit_bypasses_chromium_slot(self):
        original_cache = getattr(main.app.state, "render_cache", None)
        original_slots = getattr(main.app.state, "capture_slots", None)
        main.app.state.render_cache = SimpleNamespace(
            get=AsyncMock(
                return_value=RenderArtifact(
                    b"cached", "image/png", "capture.png"
                )
            )
        )
        main.app.state.capture_slots = asyncio.Semaphore(0)
        try:
            artifact = await asyncio.wait_for(
                main._render_async_image(
                    RenderRequest(html="cached", cache=True)
                ),
                timeout=0.25,
            )
        finally:
            main.app.state.render_cache = original_cache
            main.app.state.capture_slots = original_slots
        self.assertEqual(artifact.body, b"cached")

    async def test_visual_diff_queue_is_bounded(self):
        original_slots = getattr(main.app.state, "diff_slots", None)
        main.app.state.diff_slots = asyncio.Semaphore(0)
        try:
            with patch("main.CAPTURE_QUEUE_TIMEOUT_SECONDS", 0.01):
                with self.assertRaises(RenderError) as raised:
                    await main.visual_diff(None, None)
        finally:
            main.app.state.diff_slots = original_slots
        self.assertEqual(raised.exception.code, "diff_queue_busy")

    async def test_visual_diff_keeps_slot_until_cancelled_thread_settles(self):
        original_slots = getattr(main.app.state, "diff_slots", None)
        main.app.state.diff_slots = asyncio.Semaphore(1)
        started = threading.Event()
        release = threading.Event()

        def blocked_compare(*_args, **_kwargs):
            started.set()
            release.wait(timeout=2)

        upload = SimpleNamespace(read=AsyncMock(return_value=b"image"))
        try:
            with patch("main.compare_images", side_effect=blocked_compare):
                operation = asyncio.create_task(main.visual_diff(upload, upload))
                self.assertTrue(
                    await asyncio.wait_for(
                        asyncio.to_thread(started.wait, 1), timeout=1
                    )
                )
                operation.cancel()
                await asyncio.sleep(0)
                self.assertTrue(main.app.state.diff_slots.locked())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
                self.assertFalse(main.app.state.diff_slots.locked())
        finally:
            release.set()
            main.app.state.diff_slots = original_slots


if __name__ == "__main__":
    unittest.main()
