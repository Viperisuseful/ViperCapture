from __future__ import annotations

import asyncio
import shutil
import subprocess
import time

from vipercapture.render_contract import LazyLoadMode
from vipercapture.render_engine import (
    LAZY_LOAD_FRAME_SETTLE_SCRIPT,
    LAZY_LOAD_LEGACY_TAIL_S,
    LAZY_LOAD_SETTLE_HELPERS,
    LAZY_LOAD_SETTLE_SCRIPT,
    LAZY_LOAD_TAIL_SETTLE_S,
    _lazy_load_timing,
    load_lazy_content,
)


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


class FakeFrameElement:
    def __init__(self, revealed: bool) -> None:
        self.revealed = revealed

    async def evaluate(self, _expression: str) -> bool:
        return self.revealed


class FakeFrame:
    """Playwright frame double used to assert cross-origin settle waits."""

    def __init__(
        self,
        *,
        revealed: bool = True,
        settles: bool = True,
        clock: FakeClock | None = None,
    ) -> None:
        self.revealed = revealed
        self.settles = settles
        self.clock = clock
        self.load_waits: list[float] = []
        self.settle_waits: list[float] = []

    def is_detached(self) -> bool:
        return False

    async def frame_element(self) -> FakeFrameElement:
        return FakeFrameElement(self.revealed)

    async def wait_for_load_state(self, _state: str, *, timeout: float) -> None:
        timeout_s = timeout / 1000
        self.load_waits.append(timeout_s)
        if self.clock is not None:
            self.clock.advance(timeout_s)
        if not self.settles:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError(f"timeout {timeout} exceeded")

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


class FakePage:
    """Page double with a fixed document height and controllable settling."""

    def __init__(
        self,
        *,
        height: int,
        settles: bool = True,
        clock: FakeClock | None = None,
        frames: list[FakeFrame] | None = None,
    ) -> None:
        self.height = height
        self.settles = settles
        self.clock = clock
        self.scroll_positions: list[int] = []
        self.settle_waits: list[float] = []
        self.main_frame = object()
        self.frames: list[object] = list(frames or [])

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


def _assert_close(values: list[float], expected: list[float], tol: float = 0.05) -> None:
    assert len(values) == len(expected), (values, expected)
    assert all(abs(left - right) <= tol for left, right in zip(values, expected)), (
        values,
        expected,
    )


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
    assert page.settle_waits == [0.5] * 19 + [LAZY_LOAD_TAIL_SETTLE_S]
    assert page.scroll_positions[-1] == 0
    assert elapsed < 1.0


def test_bottom_stability_checks_keep_grace_sleeps() -> None:
    page = FakePage(height=600)
    elapsed = _run(page, LazyLoadMode.ADAPTIVE)
    # Page fits the viewport: initial scroll, one stability re-check scroll,
    # and the return-to-top scroll; the remaining grace sleep plus the
    # final settle wait stay inside the legacy envelope.
    assert page.scroll_positions == [0, 0, 0]
    assert elapsed < 0.45


def test_exhausted_budget_stays_inside_legacy_phase() -> None:
    from unittest.mock import patch

    clock = FakeClock()
    page = FakePage(height=12000, settles=False, clock=clock)
    with patch("vipercapture.render_engine.time.monotonic", clock.monotonic):
        asyncio.run(load_lazy_content(page, 600, LazyLoadMode.ADAPTIVE))
    # Adaptive envelope is 24 * 75ms + 200ms = 2.0s. Hung 0.5s caps
    # consume that budget in 4 waits; leftover steps and the tail do
    # not add the old fixed sleeps on top.
    _, _, _, settle_cap, phase_budget = _lazy_load_timing(LazyLoadMode.ADAPTIVE)
    assert phase_budget == 24 * 0.075 + LAZY_LOAD_LEGACY_TAIL_S
    _assert_close(page.settle_waits, [settle_cap] * 4)
    assert sum(page.settle_waits) <= phase_budget + 0.05
    assert page.scroll_positions[-1] == 0


def test_thorough_exhausted_budget_stays_inside_legacy_phase() -> None:
    from unittest.mock import patch

    clock = FakeClock()
    page = FakePage(height=12000, settles=False, clock=clock)
    with patch("vipercapture.render_engine.time.monotonic", clock.monotonic):
        asyncio.run(load_lazy_content(page, 600, LazyLoadMode.THOROUGH))
    # Thorough envelope is 40 * 200ms + 200ms = 8.2s. Eight 1s caps
    # leave 0.2s, which the next progress step consumes; no extra
    # fallback sleeps are stacked after that.
    _, _, _, settle_cap, phase_budget = _lazy_load_timing(LazyLoadMode.THOROUGH)
    assert phase_budget == 40 * 0.2 + LAZY_LOAD_LEGACY_TAIL_S
    _assert_close(page.settle_waits, [settle_cap] * 8 + [0.2])
    assert sum(page.settle_waits) <= phase_budget + 0.05
    assert page.scroll_positions[-1] == 0


def test_thorough_mode_also_uses_readiness_waits() -> None:
    page = FakePage(height=12000)
    elapsed = _run(page, LazyLoadMode.THOROUGH)
    # 480px steps settle 23 times before the clamp-to-bottom settle, plus
    # the tail settle after returning to the top.
    assert page.settle_waits == [1.0] * 24 + [LAZY_LOAD_TAIL_SETTLE_S]
    assert elapsed < 2.0


def test_revealed_cross_origin_frames_get_a_load_path() -> None:
    frame = FakeFrame(revealed=True)
    page = FakePage(height=600, frames=[frame])
    _run(page, LazyLoadMode.ADAPTIVE)
    assert frame.load_waits, "revealed frames must wait_for_load_state"
    assert frame.settle_waits, "revealed frames must settle inside the frame"
    assert "readyState" in LAZY_LOAD_FRAME_SETTLE_SCRIPT


def test_below_fold_frames_are_not_settled() -> None:
    frame = FakeFrame(revealed=False)
    page = FakePage(height=12000, frames=[frame])
    _run(page, LazyLoadMode.ADAPTIVE)
    assert frame.load_waits == []
    assert frame.settle_waits == []


def test_settle_script_does_not_skip_inaccessible_frames() -> None:
    assert "if (!doc) continue" not in LAZY_LOAD_SETTLE_SCRIPT
    assert "iframeHasStarted" in LAZY_LOAD_SETTLE_SCRIPT
    assert "isSourceLessLazyCandidate" in LAZY_LOAD_SETTLE_SCRIPT
    assert "data-(src|srcset|original|lazy|bg)" in LAZY_LOAD_SETTLE_SCRIPT


def test_settle_helpers_cover_sourceless_images_and_cross_origin_frames() -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node is required to execute settle helper predicates")
    script = f"""
    {LAZY_LOAD_SETTLE_HELPERS}
    const assert = (cond, msg) => {{ if (!cond) {{ throw new Error(msg); }} }};
    assert(isSourceLessLazyCandidate({{
        currentSrc: '', loading: 'eager', attributes: [{{name: 'data-src'}}], className: ''
    }}), 'data-src without currentSrc must be a lazy candidate');
    assert(isSourceLessLazyCandidate({{
        currentSrc: '', loading: 'lazy', attributes: [], className: ''
    }}), 'native loading=lazy without currentSrc must wait');
    assert(isSourceLessLazyCandidate({{
        currentSrc: '', loading: 'eager', attributes: [], className: 'hero lazyload'
    }}), 'lazysizes class without currentSrc must wait');
    assert(!isSourceLessLazyCandidate({{
        currentSrc: '', loading: 'eager', attributes: [], className: ''
    }}), 'placeholder img without a deferred source is complete');
    assert(!isSourceLessLazyCandidate({{
        currentSrc: 'https://cdn.example/a.png', loading: 'lazy',
        attributes: [{{name: 'data-src'}}], className: ''
    }}), 'an already-requested image is not source-less');
    assert(!iframeHasStarted({{
        contentDocument: {{ URL: 'about:blank' }},
        src: 'https://ads.example/embed',
        getAttribute: (name) => name === 'src' ? 'https://ads.example/embed' : ''
    }}), 'lazy iframe still on about:blank has not started');
    assert(!iframeHasStarted({{
        contentDocument: null,
        src: '',
        dataset: {{ src: 'https://other.example/embed' }},
        contentWindow: {{ location: {{ href: 'about:blank' }} }}
    }}), 'data-src iframe that has not navigated has not started');
    assert(iframeHasStarted({{
        contentDocument: {{ URL: 'https://same.example/x' }},
        src: 'https://same.example/x'
    }}), 'same-origin navigated iframe has started');
    assert(iframeHasStarted({{
        contentDocument: null,
        src: 'https://other.example/embed',
        contentWindow: {{ get location() {{ throw new Error('SecurityError'); }} }}
    }}), 'cross-origin document (location throws) has started');
    console.log('ok');
    """
    completed = subprocess.run(
        [node, "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "ok" in completed.stdout
