import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

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

    async def test_schedule_listing_is_paginated_and_payload_free(self):
        original_schedules = getattr(main.app.state, "schedules", None)
        pages = {
            None: [
                {"id": f"00000000-0000-0000-0000-00000000000{index}", "name": f"Job {index}"}
                for index in range(2)
            ],
            "00000000-0000-0000-0000-000000000001": [
                {"id": "00000000-0000-0000-0000-000000000002", "name": "Job 2"}
            ],
        }
        calls = []

        async def list_page(*, after=None, limit=100):
            calls.append((after, limit))
            return pages[after]

        main.app.state.schedules = SimpleNamespace(
            store=SimpleNamespace(list_page=list_page)
        )
        try:
            first = await main.list_schedules(after=None, limit=2)
            second = await main.list_schedules(
                after=UUID("00000000-0000-0000-0000-000000000001"), limit=2
            )
        finally:
            main.app.state.schedules = original_schedules
        first_document = json.loads(first.body)
        second_document = json.loads(second.body)
        self.assertEqual(first_document["count"], 2)
        self.assertEqual(
            first_document["next_cursor"], "00000000-0000-0000-0000-000000000001"
        )
        self.assertIsNone(second_document["next_cursor"])
        self.assertNotIn("payload", first_document["schedules"][0])
        self.assertEqual(
            calls,
            [(None, 2), ("00000000-0000-0000-0000-000000000001", 2)],
        )


if __name__ == "__main__":
    unittest.main()
