import asyncio
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

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
        self.assertIn("PUT", main.DESKTOP_ALLOW_METHODS)

    def test_profile_storage_state_is_validated_before_persistence(self):
        with self.assertRaises(ValidationError):
            main.ProfileCreate(storage_state={"cookies": "invalid", "origins": []})
        with self.assertRaises(ValidationError):
            main.ProfileCreate(
                storage_state={
                    "cookies": [{"name": "missing-fields"}],
                    "origins": [],
                }
            )
        for origin in ("https://example.com:abc", "https://example.com:70000"):
            with self.subTest(origin=origin), self.assertRaises(ValidationError):
                main.ProfileCreate(
                    storage_state={
                        "cookies": [],
                        "origins": [
                            {"origin": origin, "localStorage": []}
                        ],
                    }
                )
        with self.assertRaises(ValidationError):
            main.ProfileCreate(
                storage_state={
                    "cookies": [],
                    "origins": [
                        {"origin": "not-an-origin", "localStorage": []}
                    ],
                }
            )
        profile = main.ProfileCreate(
            storage_state={
                "cookies": [],
                "origins": [
                    {
                        "origin": "https://example.com/",
                        "localStorage": [],
                    }
                ],
            }
        )
        self.assertEqual(
            profile.storage_state.origins[0].origin, "https://example.com"
        )


class PlatformRouteReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_baseline_is_rejected_before_persistence(self):
        control = SimpleNamespace(put_baseline=Mock(), audit=Mock())
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(control=control)),
            state=SimpleNamespace(project_id="project", key_id="key"),
        )
        image = SimpleNamespace(read=AsyncMock(return_value=b"not-an-image"))
        with self.assertRaises(RenderError) as raised:
            await main.put_baseline("home", image, request)
        self.assertEqual(raised.exception.code, "diff_input_invalid")
        control.put_baseline.assert_not_called()

    async def test_readiness_is_unavailable_without_a_browser(self):
        original_browser = getattr(main.app.state, "browser", None)
        main.app.state.browser = None
        try:
            response = await main.ready()
        finally:
            main.app.state.browser = original_browser
        self.assertEqual(response.status_code, 503)
        self.assertFalse(json.loads(response.body)["ready"])

    async def test_health_and_readiness_bypass_desktop_token(self):
        expected = main.Response(status_code=200)
        call_next = AsyncMock(return_value=expected)
        with patch("main.DESKTOP_TOKEN", "secret"):
            for path in ("/health", "/ready"):
                request = SimpleNamespace(
                    method="GET",
                    url=SimpleNamespace(path=path),
                    headers={},
                )
                response = await main.require_desktop_token(request, call_next)
                self.assertIs(response, expected)
        self.assertEqual(call_next.await_count, 2)

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

    async def test_control_credentials_also_satisfy_desktop_auth(self):
        expected = main.Response(status_code=200)
        call_next = AsyncMock(return_value=expected)
        control = SimpleNamespace(
            authenticate=Mock(
                return_value={
                    "project_id": "project",
                    "key_id": "key",
                    "scopes": ["render"],
                }
            ),
            acquire=AsyncMock(return_value=(True, None)),
            release=AsyncMock(),
        )
        with (
            patch("main.DESKTOP_TOKEN", "desktop"),
            patch("main.CONTROL_ENABLED", True),
            patch("main.CONTROL_ADMIN_TOKEN", "admin"),
        ):
            for authorization in ("Bearer admin", "Bearer project-key"):
                request = SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path="/v1/render"),
                    headers={"authorization": authorization},
                    query_params={},
                    app=SimpleNamespace(state=SimpleNamespace(control=control)),
                )
                response = await main.require_desktop_token(request, call_next)
                self.assertIs(response, expected)
        self.assertEqual(call_next.await_count, 2)

    async def test_metrics_include_authentication_rejections(self):
        metrics = main.Metrics()
        request = SimpleNamespace(
            method="POST",
            url=SimpleNamespace(path="/v1/render"),
            headers={},
            query_params={},
            scope={},
        )

        async def authenticate(inner_request):
            return await main.require_desktop_token(
                inner_request,
                AsyncMock(return_value=main.Response(status_code=200)),
            )

        with (
            patch("main.METRICS", metrics),
            patch("main.DESKTOP_TOKEN", "desktop"),
        ):
            response = await main.record_http_metrics(request, authenticate)
        self.assertEqual(response.status_code, 401)
        self.assertIn(
            'vipercapture_http_requests_total{method="POST",route="unmatched",status="401"} 1',
            metrics.prometheus(),
        )

    def test_early_auth_failure_has_correlated_request_id(self):
        with (
            patch("main.DESKTOP_TOKEN", "desktop"),
            patch("main.CONTROL_ENABLED", False),
        ):
            response = TestClient(main.app).get(
                "/v1/jobs", headers={"X-Request-Id": "early-auth"}
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["X-Request-Id"], "early-auth")
        self.assertEqual(response.json()["error"]["request_id"], "early-auth")

    async def test_status_requests_skip_concurrency_and_jobs_can_read_cert_key(self):
        expected = main.Response(status_code=200)
        call_next = AsyncMock(return_value=expected)
        control = SimpleNamespace(
            authenticate=Mock(
                return_value={
                    "project_id": "project",
                    "key_id": "key",
                    "scopes": ["jobs"],
                }
            ),
            acquire=AsyncMock(return_value=(True, None)),
            release=AsyncMock(),
        )
        with (
            patch("main.CONTROL_ENABLED", True),
            patch("main.CONTROL_ADMIN_TOKEN", "admin"),
        ):
            for path in (
                "/v1/jobs/00000000-0000-0000-0000-000000000000",
                "/v1/certification/public-key",
            ):
                request = SimpleNamespace(
                    method="GET",
                    url=SimpleNamespace(path=path),
                    headers={"authorization": "Bearer project-key"},
                    query_params={},
                    app=SimpleNamespace(state=SimpleNamespace(control=control)),
                )
                self.assertIs(
                    await main.require_desktop_token(request, call_next), expected
                )
        self.assertTrue(
            all(
                call.kwargs["concurrency"] is False
                for call in control.acquire.await_args_list
            )
        )
        control.release.assert_not_awaited()

    async def test_urlbox_async_submission_skips_render_concurrency(self):
        expected = main.Response(status_code=200)
        call_next = AsyncMock(return_value=expected)
        control = SimpleNamespace(
            authenticate=Mock(
                return_value={
                    "project_id": "project",
                    "key_id": "key",
                    "scopes": ["render", "jobs"],
                }
            ),
            acquire=AsyncMock(return_value=(True, None)),
            release=AsyncMock(),
        )
        with (
            patch("main.CONTROL_ENABLED", True),
            patch("main.CONTROL_ADMIN_TOKEN", "admin"),
        ):
            for path in (
                "/compat/urlbox/v1/render/async",
                "/compat/urlbox/v1/render/sync",
            ):
                request = SimpleNamespace(
                    method="POST",
                    url=SimpleNamespace(path=path),
                    headers={"authorization": "Bearer project-key"},
                    query_params={},
                    app=SimpleNamespace(state=SimpleNamespace(control=control)),
                )
                await main.require_desktop_token(request, call_next)
        self.assertFalse(control.acquire.await_args_list[0].kwargs["concurrency"])
        self.assertTrue(control.acquire.await_args_list[1].kwargs["concurrency"])
        control.release.assert_awaited_once_with("project")

    async def test_admin_schedule_delete_releases_stored_owner_quota(self):
        schedule_id = "00000000-0000-0000-0000-000000000001"
        control = SimpleNamespace(owner=Mock(return_value="project"), disown=Mock())
        service = SimpleNamespace(delete=AsyncMock(return_value=True))
        original_control = getattr(main.app.state, "control", None)
        original_schedules = getattr(main.app.state, "schedules", None)
        main.app.state.control = control
        main.app.state.schedules = service
        request = SimpleNamespace(
            state=SimpleNamespace(
                is_admin=True, trusted_local=False, project_id=None
            )
        )
        try:
            with patch("main.CONTROL_ENABLED", True):
                response = await main.delete_schedule(schedule_id, request)
        finally:
            main.app.state.control = original_control
            main.app.state.schedules = original_schedules
        self.assertEqual(response.status_code, 204)
        control.disown.assert_called_once_with(
            "schedule", schedule_id, "project"
        )

    async def test_cancelled_committed_schedule_delete_releases_quota(self):
        schedule_id = "00000000-0000-0000-0000-000000000003"
        control = SimpleNamespace(owner=Mock(return_value="project"), disown=Mock())
        service = SimpleNamespace(
            delete=AsyncMock(side_effect=asyncio.CancelledError),
            store=SimpleNamespace(get=AsyncMock(return_value=None)),
        )
        original_control = getattr(main.app.state, "control", None)
        original_schedules = getattr(main.app.state, "schedules", None)
        main.app.state.control = control
        main.app.state.schedules = service
        request = SimpleNamespace(
            state=SimpleNamespace(
                is_admin=True, trusted_local=False, project_id=None
            )
        )
        try:
            with patch("main.CONTROL_ENABLED", True), self.assertRaises(
                asyncio.CancelledError
            ):
                await main.delete_schedule(schedule_id, request)
        finally:
            main.app.state.control = original_control
            main.app.state.schedules = original_schedules
        control.disown.assert_called_once_with(
            "schedule", schedule_id, "project"
        )

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

    async def test_signed_url_rejects_persistent_profiles(self):
        payload = RenderRequest.model_validate(
            {"url": "https://example.com", "profile_id": "profile-1"}
        )
        request = SimpleNamespace(headers={"authorization": "Bearer admin"})
        with (
            patch("main.SIGNING_SECRET", "s" * 32),
            patch("main.SIGNING_ADMIN_TOKEN", "admin"),
            self.assertRaises(RenderError) as raised,
        ):
            await main.create_signed_url(payload, request)
        self.assertEqual(raised.exception.code, "signed_profile_unsupported")

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
        metrics = SimpleNamespace(inc=Mock())
        try:
            with patch("main.METRICS", metrics):
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
        self.assertEqual(
            metrics.inc.call_args_list,
            [
                unittest.mock.call(
                    "renders_total", output="png", cache="hit"
                ),
                unittest.mock.call(
                    "render_seconds_sum", 0, output="png"
                ),
                unittest.mock.call("queue_seconds_sum", 0),
            ],
        )

    async def test_async_render_records_miss_and_timing_metrics(self):
        original_slots = getattr(main.app.state, "capture_slots", None)
        original_browser = getattr(main.app.state, "browser", None)
        main.app.state.capture_slots = asyncio.Semaphore(1)
        main.app.state.browser = SimpleNamespace(is_connected=Mock(return_value=True))
        metrics = SimpleNamespace(inc=Mock())
        artifact = RenderArtifact(b"rendered", "image/png", "capture.png")
        created_at = datetime.now(timezone.utc)
        job = SimpleNamespace(
            request_id="metrics-job",
            created_at=created_at,
            started_at=created_at + timedelta(seconds=7),
        )
        try:
            with (
                patch("main.METRICS", metrics),
                patch("main.current_job", return_value=job),
                patch("main._render_with_cache", AsyncMock(return_value=(artifact, False))),
            ):
                result = await main._render_async_image(
                    RenderRequest(html="render", cache=True)
                )
        finally:
            main.app.state.capture_slots = original_slots
            main.app.state.browser = original_browser
        self.assertEqual(result.body, b"rendered")
        self.assertEqual(metrics.inc.call_args_list[0].kwargs["cache"], "miss")
        self.assertEqual(metrics.inc.call_args_list[0].args[0], "renders_total")
        self.assertEqual(metrics.inc.call_args_list[1].args[0], "render_seconds_sum")
        self.assertEqual(metrics.inc.call_args_list[2].args[0], "queue_seconds_sum")
        self.assertEqual(metrics.inc.call_args_list[2].args[1], 7)

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
