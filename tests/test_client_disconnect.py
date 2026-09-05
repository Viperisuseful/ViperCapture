from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from vipercapture.bulk_jobs import CLIENT_DISCONNECTED_STATE, BulkBodyLimitMiddleware
from vipercapture.main import _await_while_connected
from vipercapture.render_errors import RenderError


def _http_scope(path: str = "/v1/render") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-length", b"2"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "state": {},
    }


def test_body_middleware_forwards_disconnect_after_body() -> None:
    seen: list[str] = []

    async def inner(scope, receive, send) -> None:
        request = Request(scope, receive)
        assert await request.body() == b"{}"
        for _ in range(40):
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.02)
        seen.append("disconnected" if await request.is_disconnected() else "connected")
        event = getattr(request.state, CLIENT_DISCONNECTED_STATE, None)
        seen.append("event-set" if isinstance(event, asyncio.Event) and event.is_set() else "event-missing")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run() -> None:
        middleware = BulkBodyLimitMiddleware(inner)
        queued = [
            {"type": "http.request", "body": b"{}", "more_body": False},
        ]
        released = asyncio.Event()

        async def receive():
            if queued:
                return queued.pop(0)
            await released.wait()
            return {"type": "http.disconnect"}

        sent: list[dict] = []

        async def send(message) -> None:
            sent.append(message)

        async def drop_client() -> None:
            await asyncio.sleep(0.03)
            released.set()

        await asyncio.gather(
            middleware(_http_scope(), receive, send),
            drop_client(),
        )
        assert seen == ["disconnected", "event-set"]
        assert sent[0]["status"] == 200

    asyncio.run(run())


def test_await_while_connected_raises_499_without_waiting_for_shield() -> None:
    async def run() -> None:
        disconnected = asyncio.Event()
        request = SimpleNamespace(
            state=SimpleNamespace(**{CLIENT_DISCONNECTED_STATE: disconnected}),
            is_disconnected=None,
        )

        async def shielded_work() -> str:
            nested = asyncio.create_task(asyncio.sleep(5))
            try:
                return await asyncio.shield(nested)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.shield(nested)
                raise

        async def drop_client() -> None:
            await asyncio.sleep(0.05)
            disconnected.set()

        started = time.perf_counter()
        drop = asyncio.create_task(drop_client())
        try:
            with pytest.raises(RenderError) as details:
                await _await_while_connected(request, shielded_work())
        finally:
            drop.cancel()
            with suppress(asyncio.CancelledError):
                await drop
        elapsed = time.perf_counter() - started
        assert details.value.status_code == 499
        assert details.value.code == "client_disconnected"
        assert elapsed < 1.0

    asyncio.run(run())


def test_disconnect_releases_capture_slot_promptly() -> None:
    async def run() -> None:
        slots = asyncio.Semaphore(1)
        disconnected = asyncio.Event()
        request = SimpleNamespace(
            state=SimpleNamespace(**{CLIENT_DISCONNECTED_STATE: disconnected}),
            is_disconnected=None,
        )
        holder_started = asyncio.Event()

        async def hold_slot() -> None:
            await slots.acquire()
            holder_started.set()
            try:
                await _await_while_connected(request, asyncio.sleep(8))
            except RenderError as exc:
                assert exc.status_code == 499
            finally:
                slots.release()

        async def next_acquire() -> float:
            await holder_started.wait()
            await asyncio.sleep(0.05)
            disconnected.set()
            started = time.perf_counter()
            await asyncio.wait_for(slots.acquire(), timeout=1)
            waited = time.perf_counter() - started
            slots.release()
            return waited

        holder = asyncio.create_task(hold_slot())
        waited = await next_acquire()
        await holder
        assert waited < 0.75

    asyncio.run(run())


def test_await_while_connected_returns_completed_work() -> None:
    async def run() -> None:
        request = SimpleNamespace(state=SimpleNamespace(), is_disconnected=None)

        async def quick() -> str:
            return "done"

        assert await _await_while_connected(request, quick()) == "done"

    asyncio.run(run())
