from __future__ import annotations

import asyncio
import time

from vipercapture.render_contract import LazyLoadMode
from vipercapture.render_engine import load_lazy_content


class FakeClock:
    """Monotonic double that fast-forwards by simulated wait durations.

    Backed by the real clock because asyncio.sleep schedules against
    time.monotonic; freezing it outright would hang the event loop.
    """

    def __init__(self) -> None:
        self.offset = 0.0
        self._real = time.monotonic

    def monotonic(self) -> float:
        return self._real() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


class FakePage:
    """Page double with a fixed document height and controllable settling."""

    def __init__(
        self,
        *,
        height: int,
        settles: bool = True,
        clock: FakeClock | None = None,
    ) -> None:
        self.height = height
        self.settles = settles
        self.clock = clock
        self.scroll_positions: list[int] = []
        self.settle_waits: list[float] = []

    async def evaluate(self, expression: str, arg: object = None) -> object:
        if "scrollTo" in expression:
            self.scroll_positions.append(arg)
            return None
        if "scrollHeight" in expression:
            return self.height
        raise AssertionError(f"unexpected evaluate: {expression[:40]}")

    async def wait_for_function(
        self, _expression: str, *, timeout: float, polling: object = None
    ) -> None:
        timeout_s = timeout / 1000
        self.settle_waits.append(timeout_s)
        if self.clock is not None:
            self.clock.advance(timeout_s)
        if not self.settles:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError(f"timeout {timeout} exceeded")


def _run(page: FakePage, mode: LazyLoadMode, viewport: int = 600) -> float:
    started = time.monotonic()
    asyncio.run(load_lazy_content(page, viewport, mode))
    return time.monotonic() - started


def test_none_mode_never_touches_the_page() -> None:
    page = FakePage(height=12000)
    _run(page, LazyLoadMode.NONE)
    assert page.scroll_positions == []
    assert page.settle_waits == []


def test_settled_tall_page_waits_for_readiness_instead_of_sleeping() -> None:
    page = FakePage(height=12000)
    elapsed = _run(page, LazyLoadMode.ADAPTIVE)
    # 18 progress settles plus the settle on the step that clamps to the
    # bottom (where the last images get revealed) and the 0.25s tail settle
    # replace 19 x 75ms of blind sleeping plus a 200ms tail; only the two
    # no-movement stability re-checks keep the fixed grace sleep.
    assert page.settle_waits == [0.5] * 19 + [0.25]
    assert page.scroll_positions[-1] == 0
    assert elapsed < 1.0


def test_bottom_stability_checks_keep_grace_sleeps() -> None:
    page = FakePage(height=600)
    elapsed = _run(page, LazyLoadMode.ADAPTIVE)
    # Page fits the viewport: initial scroll, one stability re-check scroll,
    # and the return-to-top scroll; two fixed grace sleeps (2 x 75ms) plus
    # the final settle wait replace the old 2 x 75ms + 200ms tail.
    assert page.scroll_positions == [0, 0, 0]
    assert elapsed < 0.45


def test_exhausted_budget_falls_back_to_fixed_delays() -> None:
    from unittest.mock import patch

    clock = FakeClock()
    page = FakePage(height=12000, settles=False, clock=clock)
    with patch("vipercapture.render_engine.time.monotonic", clock.monotonic):
        asyncio.run(load_lazy_content(page, 600, LazyLoadMode.ADAPTIVE))
    # Adaptive budget of 5s drains after 10 max-cap waits of 0.5s; the
    # remaining progress steps must skip wait_for_function entirely.
    # The tail settle after returning to top always runs, hence 10 + 1.
    assert len(page.settle_waits) == 11
    assert page.scroll_positions[-1] == 0


def test_thorough_mode_also_uses_readiness_waits() -> None:
    page = FakePage(height=12000)
    elapsed = _run(page, LazyLoadMode.THOROUGH)
    # 480px steps settle 23 times before the clamp-to-bottom settle, plus
    # the tail settle after returning to the top.
    assert page.settle_waits == [1.0] * 24 + [0.25]
    assert elapsed < 2.0
