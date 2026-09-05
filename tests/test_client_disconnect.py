from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from vipercapture.bulk_jobs import CLIENT_DISCONNECTED_STATE, BulkBodyLimitMiddleware
from vipercapture.main import (
    UNSETTLED_OPERATION_STATE,
    _await_while_connected,
    _finish_capture,
    _take_unsettled_operation,
    _track_browser_use,
)
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
        unsettled = details.value.unsettled_operation
        assert isinstance(unsettled, asyncio.Task)
        assert not unsettled.done()
        assert getattr(request.state, UNSETTLED_OPERATION_STATE) is unsettled

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


def test_cancellable_disconnect_releases_slot_after_finish_capture() -> None:
    """Cancelled waits settle immediately, so the next capture is not queued."""

    async def run() -> None:
        app = _fake_capture_app(2)
        abandoned = [asyncio.Event(), asyncio.Event()]
        started = [asyncio.Event(), asyncio.Event()]
        settlers: list[asyncio.Task] = []

        async def occupy(index: int) -> None:
            request = SimpleNamespace(
                state=SimpleNamespace(
                    **{CLIENT_DISCONNECTED_STATE: abandoned[index]}
                ),
                is_disconnected=None,
            )
            browser = object()
            await app.state.capture_slots.acquire()
            _track_browser_use(app, browser)
            started[index].set()
            with pytest.raises(RenderError) as details:
                await _await_while_connected(request, asyncio.sleep(10))
            assert details.value.code == "client_disconnected"
            settler = await _finish_capture(
                app,
                browser,
                tracked=True,
                render_attempted=False,
                unsettled_operation=_take_unsettled_operation(request),
            )
            if settler is not None:
                settlers.append(settler)

        async def third_render() -> int:
            await started[0].wait()
            await started[1].wait()
            await asyncio.sleep(0.05)
            abandoned[0].set()
            abandoned[1].set()
            started_at = time.perf_counter()
            await asyncio.wait_for(app.state.capture_slots.acquire(), timeout=1)
            queue_ms = round((time.perf_counter() - started_at) * 1000)
            app.state.capture_slots.release()
            return queue_ms

        first = asyncio.create_task(occupy(0))
        second = asyncio.create_task(occupy(1))
        queue_ms = await third_render()
        await asyncio.gather(first, second, *settlers)
        assert queue_ms < 750

    asyncio.run(run())


def test_two_abandoned_renders_do_not_block_third_slot() -> None:
    """Match the reported VIPERCAPTURE_MAX_CONCURRENCY=2 stress case."""

    async def run() -> None:
        slots = asyncio.Semaphore(2)
        abandoned = [asyncio.Event(), asyncio.Event()]
        started = [asyncio.Event(), asyncio.Event()]

        async def occupy(index: int) -> None:
            request = SimpleNamespace(
                state=SimpleNamespace(
                    **{CLIENT_DISCONNECTED_STATE: abandoned[index]}
                ),
                is_disconnected=None,
            )
            await slots.acquire()
            started[index].set()
            try:
                await _await_while_connected(request, asyncio.sleep(10))
            except RenderError as exc:
                assert exc.code == "client_disconnected"
            finally:
                slots.release()

        async def third_render() -> int:
            await started[0].wait()
            await started[1].wait()
            await asyncio.sleep(0.05)
            abandoned[0].set()
            abandoned[1].set()
            started_at = time.perf_counter()
            await asyncio.wait_for(slots.acquire(), timeout=1)
            queue_ms = round((time.perf_counter() - started_at) * 1000)
            slots.release()
            return queue_ms

        first = asyncio.create_task(occupy(0))
        second = asyncio.create_task(occupy(1))
        queue_ms = await third_render()
        await asyncio.gather(first, second)
        assert queue_ms < 750

    asyncio.run(run())


def test_await_while_connected_polls_is_disconnected_without_event() -> None:
    async def run() -> None:
        flags = {"gone": False}

        async def is_disconnected() -> bool:
            return flags["gone"]

        request = SimpleNamespace(state=SimpleNamespace(), is_disconnected=is_disconnected)

        async def drop_client() -> None:
            await asyncio.sleep(0.05)
            flags["gone"] = True

        drop = asyncio.create_task(drop_client())
        started = time.perf_counter()
        try:
            with pytest.raises(RenderError) as details:
                await _await_while_connected(request, asyncio.sleep(5))
        finally:
            drop.cancel()
            with suppress(asyncio.CancelledError):
                await drop
        assert details.value.status_code == 499
        assert time.perf_counter() - started < 1.0

    asyncio.run(run())


def test_await_while_connected_returns_completed_work() -> None:
    async def run() -> None:
        request = SimpleNamespace(state=SimpleNamespace(), is_disconnected=None)

        async def quick() -> str:
            return "done"

        assert await _await_while_connected(request, quick()) == "done"

    asyncio.run(run())


def _fake_capture_app(slots: int = 1):
    return SimpleNamespace(
        state=SimpleNamespace(
            capture_slots=asyncio.Semaphore(slots),
            browser_in_flight={},
            settling_captures=set(),
        )
    )


async def _shielded_work(delay: float, running: set[int] | None = None) -> str:
    marker = id(asyncio.current_task())
    if running is not None:
        running.add(marker)
    nested = asyncio.create_task(asyncio.sleep(delay))
    try:
        return await asyncio.shield(nested)
    except asyncio.CancelledError:
        with suppress(Exception):
            await asyncio.shield(nested)
        raise
    finally:
        if running is not None:
            running.discard(marker)


def test_disconnect_keeps_slot_until_shielded_work_settles() -> None:
    async def run() -> None:
        app = _fake_capture_app(1)
        browser = object()
        disconnected = asyncio.Event()
        request = SimpleNamespace(
            state=SimpleNamespace(**{CLIENT_DISCONNECTED_STATE: disconnected}),
            is_disconnected=None,
        )
        await app.state.capture_slots.acquire()
        _track_browser_use(app, browser)

        drop = asyncio.create_task(_sleep_then(disconnected, 0.05))
        started = time.perf_counter()
        try:
            with pytest.raises(RenderError) as details:
                await _await_while_connected(request, _shielded_work(0.4))
        finally:
            drop.cancel()
            with suppress(asyncio.CancelledError):
                await drop
        assert time.perf_counter() - started < 0.25
        assert details.value.status_code == 499

        settler = await _finish_capture(
            app,
            browser,
            tracked=True,
            render_attempted=True,
            unsettled_operation=_take_unsettled_operation(request),
        )
        assert settler is not None
        assert app.state.capture_slots.locked()
        assert app.state.browser_in_flight[id(browser)] == 1

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(app.state.capture_slots.acquire(), timeout=0.05)

        await asyncio.wait_for(settler, timeout=1)
        assert not app.state.capture_slots.locked()
        assert app.state.browser_in_flight == {}
        await asyncio.wait_for(app.state.capture_slots.acquire(), timeout=0.2)
        app.state.capture_slots.release()

    asyncio.run(run())


def test_disconnect_storm_does_not_exceed_concurrency() -> None:
    async def run() -> None:
        app = _fake_capture_app(2)
        running: set[int] = set()
        peak = 0
        settlers: list[asyncio.Task] = []

        async def occupy() -> None:
            nonlocal peak
            disconnected = asyncio.Event()
            request = SimpleNamespace(
                state=SimpleNamespace(**{CLIENT_DISCONNECTED_STATE: disconnected}),
                is_disconnected=None,
            )
            browser = object()
            await app.state.capture_slots.acquire()
            _track_browser_use(app, browser)
            drop = asyncio.create_task(_sleep_then(disconnected, 0.02))
            try:
                with pytest.raises(RenderError) as details:
                    await _await_while_connected(
                        request, _shielded_work(0.35, running)
                    )
                peak = max(peak, len(running))
                assert details.value.status_code == 499
            finally:
                drop.cancel()
                with suppress(asyncio.CancelledError):
                    await drop
            settler = await _finish_capture(
                app,
                browser,
                tracked=True,
                render_attempted=True,
                unsettled_operation=_take_unsettled_operation(request),
            )
            if settler is not None:
                settlers.append(settler)
            peak = max(peak, len(running), len(app.state.browser_in_flight))

        first = asyncio.create_task(occupy())
        second = asyncio.create_task(occupy())
        await asyncio.gather(first, second)
        assert peak <= 2
        assert app.state.capture_slots.locked()
        assert sum(app.state.browser_in_flight.values()) == 2

        third_started = time.perf_counter()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(app.state.capture_slots.acquire(), timeout=0.08)
        assert time.perf_counter() - third_started < 0.2

        await asyncio.gather(*settlers)
        assert app.state.browser_in_flight == {}
        await asyncio.wait_for(app.state.capture_slots.acquire(), timeout=0.2)
        app.state.capture_slots.release()
        await asyncio.wait_for(app.state.capture_slots.acquire(), timeout=0.2)
        app.state.capture_slots.release()

    asyncio.run(run())


async def _sleep_then(event: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    event.set()
