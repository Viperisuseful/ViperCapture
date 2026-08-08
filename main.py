import asyncio
import hmac
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Annotated, Awaitable, TypeVar
from urllib.parse import urlencode
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Browser, Playwright, async_playwright

from async_jobs import (
    AsyncJobService,
    RenderedArtifact,
    load_providers,
    public_job_document,
    settings_from_environment,
)
from bulk_jobs import BulkBodyLimitMiddleware, BulkJobRequest
from page_cleanup import (
    CleanupOptions,
    apply_visual_cleanup,
    finish_autoconsent,
    setup_autoconsent,
    should_block_resource,
)
from render_cache import RenderCache
from render_contract import OutputFormat, RenderRequest
from render_engine import CleanupHooks, RenderArtifact, RenderEngine, RenderLimits
from render_errors import RenderError, install_render_error_layer
from schedules import (
    ScheduleCreate,
    ScheduleService,
    ScheduleStore,
    ScheduleUpdate,
    public_schedule_document,
    schedule_cursor,
)
from signed_urls import sign_render_request, verify_render_request
from visual_diff import MAX_DIFF_INPUT_BYTES, compare_images, create_diff_bundle
from webhooks import WebhookDispatcher

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


BASE_DIR = Path(__file__).resolve().parent
CAPTURES_DIR = Path(
    os.getenv("VIPERCAPTURE_CAPTURES_DIR", str(BASE_DIR / "captures"))
).expanduser()
DESKTOP_TOKEN = os.getenv("VIPERCAPTURE_DESKTOP_TOKEN")
DESKTOP_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "VIPERCAPTURE_DESKTOP_ORIGINS",
        (
            "http://tauri.localhost,https://tauri.localhost,tauri://localhost,"
            "http://localhost:1420,http://127.0.0.1:1420"
        ),
    ).split(",")
    if origin.strip()
]
DESKTOP_ALLOW_HEADERS = [
    "Authorization",
    "Content-Type",
    "X-Request-Id",
]
DESKTOP_ALLOW_METHODS = ["GET", "POST", "PATCH", "DELETE"]


def _load_local_env() -> None:
    """Load machine-only KEY=VALUE settings without another dependency."""
    path = BASE_DIR / ".env.local"
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_local_env()

HOSTED = os.getenv("VIPERCAPTURE_HOSTED") == "1"
ALLOW_SCRIPTS = os.getenv("VIPERCAPTURE_ALLOW_SCRIPTS") == "1"
SIGNING_SECRET = os.getenv("VIPERCAPTURE_SIGNING_SECRET", "")
SIGNING_ADMIN_TOKEN = os.getenv("VIPERCAPTURE_SIGNING_ADMIN_TOKEN", "")
PUBLIC_URL = os.getenv("VIPERCAPTURE_PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("VIPERCAPTURE_WEBHOOK_SECRET", "")
ALLOW_PRIVATE_WEBHOOKS = os.getenv("VIPERCAPTURE_ALLOW_PRIVATE_WEBHOOKS") == "1"
if SIGNING_SECRET and len(SIGNING_SECRET.encode("utf-8")) < 32:
    raise ValueError("VIPERCAPTURE_SIGNING_SECRET must contain at least 32 bytes")
if WEBHOOK_SECRET and len(WEBHOOK_SECRET.encode("utf-8")) < 32:
    raise ValueError("VIPERCAPTURE_WEBHOOK_SECRET must contain at least 32 bytes")
GPU_MODE = os.getenv(
    "VIPERCAPTURE_GPU_MODE",
    "auto" if os.getenv("VIPERCAPTURE_ENABLE_GPU") == "1" else "off",
).lower()
GPU_BACKEND = os.getenv("VIPERCAPTURE_GPU_BACKEND", "default").lower()
if GPU_MODE not in {"off", "auto", "required"}:
    raise ValueError("VIPERCAPTURE_GPU_MODE must be off, auto, or required")
if GPU_BACKEND not in {"default", "vulkan"}:
    raise ValueError("VIPERCAPTURE_GPU_BACKEND must be default or vulkan")
MAX_CONCURRENT_CAPTURES = max(
    1, int(os.getenv("VIPERCAPTURE_MAX_CONCURRENCY", "1"))
)
MAX_ASYNC_RESULT_DOWNLOADS = max(
    1, int(os.getenv("VIPERCAPTURE_ASYNC_RESULT_CONCURRENCY", "2"))
)
MAX_SCREENSHOT_PIXELS = max(
    1, int(os.getenv("VIPERCAPTURE_MAX_PIXELS", "50000000"))
)
MAX_DIFF_CONCURRENCY = max(
    1, int(os.getenv("VIPERCAPTURE_DIFF_CONCURRENCY", "1"))
)
CAPTURE_QUEUE_TIMEOUT_SECONDS = 30
AwaitedResult = TypeVar("AwaitedResult")
ASYNC_JOBS_ENABLED = os.getenv(
    "VIPERCAPTURE_ASYNC_JOBS",
    "0" if os.name == "nt" else "1",
) != "0"
ASYNC_JOB_SETTINGS = (
    settings_from_environment(default_workers=MAX_CONCURRENT_CAPTURES)
    if ASYNC_JOBS_ENABLED
    else None
)
SCHEDULES_ENABLED = os.getenv(
    "VIPERCAPTURE_SCHEDULES",
    "0" if os.name == "nt" else "1",
) != "0"
CACHE_DIRECTORY = Path(
    os.getenv(
        "VIPERCAPTURE_CACHE_DIR",
        str(
            (
                ASYNC_JOB_SETTINGS.data_dir
                if ASYNC_JOB_SETTINGS
                else Path(
                    os.getenv(
                        "VIPERCAPTURE_DATA_DIR",
                        str(BASE_DIR / ".vipercapture"),
                    )
                ).expanduser()
            )
            / "cache"
        ),
    )
).expanduser()
CACHE_TTL_SECONDS = max(1, int(os.getenv("VIPERCAPTURE_CACHE_TTL_SECONDS", "900")))
CACHE_MAX_ENTRIES = max(1, int(os.getenv("VIPERCAPTURE_CACHE_MAX_ENTRIES", "1000")))
CACHE_MAX_BYTES = max(
    1,
    int(os.getenv("VIPERCAPTURE_CACHE_MAX_BYTES", str(512 * 1024 * 1024))),
)


class _SlotStreamingResponse(StreamingResponse):
    def __init__(self, *args, release_slot, **kwargs):
        super().__init__(*args, **kwargs)
        self._release_slot = release_slot

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release_slot()

if not HOSTED:
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)


def gpu_launch_args(
    mode: str = GPU_MODE,
    backend: str = GPU_BACKEND,
    platform: str = sys.platform,
) -> list[str]:
    if mode == "off":
        return []
    args = ["--enable-gpu"]
    if backend == "vulkan" and platform.startswith("linux"):
        args.append("--use-angle=vulkan")
    return args


def hardware_gpu_active(info: dict[str, object]) -> bool:
    gpu = info.get("gpu")
    if not isinstance(gpu, dict):
        return False
    description = " ".join(
        str(value)
        for value in (
            gpu.get("devices", []),
            gpu.get("auxAttributes", {}),
        )
    ).lower()
    if any(
        marker in description
        for marker in ("swiftshader", "llvmpipe", "software rasterizer")
    ):
        return False
    features = gpu.get("featureStatus")
    if not isinstance(features, dict):
        return False
    return str(features.get("gpu_compositing", "")).startswith("enabled")


async def _hardware_gpu_active(browser: Browser) -> bool:
    session = await browser.new_browser_cdp_session()
    try:
        return hardware_gpu_active(await session.send("SystemInfo.getInfo"))
    finally:
        await session.detach()


async def _detect_hardware_gpu(browser: Browser, mode: str) -> bool:
    if mode == "off":
        return False
    try:
        return await _hardware_gpu_active(browser)
    except Exception:
        return False


async def _launch_browser(
    playwright: Playwright,
    gpu_mode: str | None = None,
) -> Browser:
    selected_mode = gpu_mode or GPU_MODE
    browser = await playwright.chromium.launch(
        headless=True,
        args=gpu_launch_args(selected_mode),
    )
    if selected_mode == "required":
        try:
            active = await _hardware_gpu_active(browser)
        except Exception as exc:
            await browser.close()
            raise RuntimeError("Chromium hardware GPU verification failed") from exc
        if not active:
            await browser.close()
            raise RuntimeError(
                "A hardware GPU was required but Chromium is using software rendering"
            )
    return browser


async def _replace_browser(app: FastAPI, failed_browser: Browser) -> None:
    async with app.state.browser_restart_lock:
        if app.state.browser is not failed_browser:
            return
        with suppress(Exception):
            await asyncio.wait_for(failed_browser.close(), timeout=5)
        app.state.browser = await asyncio.wait_for(
            _launch_browser(app.state.playwright, app.state.gpu_mode),
            timeout=15,
        )
        app.state.gpu_hardware_active = await _detect_hardware_gpu(
            app.state.browser,
            app.state.gpu_mode,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    playwright: Playwright = await async_playwright().start()
    browser = await _launch_browser(playwright)
    app.state.playwright = playwright
    app.state.browser = browser
    app.state.gpu_mode = GPU_MODE
    app.state.gpu_hardware_active = await _detect_hardware_gpu(browser, GPU_MODE)
    app.state.capture_slots = asyncio.Semaphore(MAX_CONCURRENT_CAPTURES)
    app.state.diff_slots = asyncio.Semaphore(MAX_DIFF_CONCURRENCY)
    app.state.async_result_slots = asyncio.Semaphore(MAX_ASYNC_RESULT_DOWNLOADS)
    app.state.browser_restart_lock = asyncio.Lock()
    app.state.async_jobs = None
    app.state.schedules = None
    app.state.render_cache = RenderCache(
        CACHE_DIRECTORY,
        ttl_seconds=CACHE_TTL_SECONDS,
        max_entries=CACHE_MAX_ENTRIES,
        max_bytes=CACHE_MAX_BYTES,
    )
    try:
        await app.state.render_cache.start()
    except BaseException:
        with suppress(Exception):
            await browser.close()
        await playwright.stop()
        raise
    app.state.webhooks = (
        WebhookDispatcher(
            secret=WEBHOOK_SECRET,
            public_url=PUBLIC_URL,
            allow_private=ALLOW_PRIVATE_WEBHOOKS,
        )
        if WEBHOOK_SECRET
        else None
    )
    try:
        if ASYNC_JOBS_ENABLED:
            if ASYNC_JOB_SETTINGS is None:
                raise RuntimeError("async job settings were not initialized")
            job_store, artifact_store = load_providers(ASYNC_JOB_SETTINGS)
            service = AsyncJobService(
                ASYNC_JOB_SETTINGS,
                job_store,
                artifact_store,
                _render_async_image,
                notifier=_notify_job if app.state.webhooks is not None else None,
            )
            await service.start()
            app.state.async_jobs = service
            if SCHEDULES_ENABLED:
                scheduler = ScheduleService(
                    ScheduleStore(ASYNC_JOB_SETTINGS.data_dir / "schedules.sqlite3"),
                    service,
                    service.cipher,
                )
                await scheduler.start()
                app.state.schedules = scheduler
        yield
    finally:
        try:
            if app.state.schedules is not None:
                await app.state.schedules.close()
            if app.state.async_jobs is not None:
                await app.state.async_jobs.close()
        finally:
            with suppress(Exception):
                await app.state.browser.close()
            await playwright.stop()


app = FastAPI(lifespan=lifespan)
app.add_middleware(BulkBodyLimitMiddleware)
install_render_error_layer(app)
if DESKTOP_TOKEN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DESKTOP_ORIGINS,
        allow_credentials=False,
        allow_methods=DESKTOP_ALLOW_METHODS,
        allow_headers=DESKTOP_ALLOW_HEADERS,
        expose_headers=[
            "Content-Disposition",
            "Location",
            "Retry-After",
            "X-Request-ID",
        ],
    )
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def require_desktop_token(request: Request, call_next):
    path = request.url.path
    signed_render = request.method == "GET" and path == "/v1/render/signed"
    signing_token = SIGNING_ADMIN_TOKEN or SIGNING_SECRET
    signing_admin = (
        request.method == "POST"
        and path == "/v1/signed-url"
        and signing_token
        and hmac.compare_digest(
            request.headers.get("authorization", ""),
            f"Bearer {signing_token}",
        )
    )
    if (
        DESKTOP_TOKEN
        and request.method != "OPTIONS"
        and path != "/health"
        and not signed_render
        and not signing_admin
        and request.headers.get("authorization") != f"Bearer {DESKTOP_TOKEN}"
    ):
        return Response(status_code=401)
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ready": True}


if DESKTOP_TOKEN:
    @app.post("/shutdown")
    async def shutdown(request: Request) -> dict[str, bool]:
        callback = getattr(request.app.state, "shutdown_callback", None)
        if callback is None:
            raise HTTPException(status_code=503, detail="Shutdown is unavailable")
        asyncio.get_running_loop().call_later(0.1, callback)
        return {"shutting_down": True}


async def _await_while_connected(
    request: Request,
    operation: Awaitable[AwaitedResult],
) -> AwaitedResult:
    """Cancel queued or rendering work when the client disconnects."""
    is_disconnected = getattr(request, "is_disconnected", None)
    if not callable(is_disconnected):
        return await operation

    operation_task = asyncio.ensure_future(operation)
    try:
        while True:
            done, _pending = await asyncio.wait({operation_task}, timeout=0.1)
            if operation_task in done:
                return await operation_task
            if await is_disconnected():
                if operation_task.done() or not operation_task.cancel():
                    return await operation_task
                with suppress(asyncio.CancelledError):
                    await operation_task
                raise RenderError(
                    "client_disconnected",
                    "The capture was cancelled.",
                    499,
                    False,
                )
    finally:
        if not operation_task.done():
            operation_task.cancel()
            with suppress(asyncio.CancelledError):
                await operation_task


@app.get("/")
async def index() -> FileResponse:
    frontend = BASE_DIR / "static" / "app" / "index.html"
    return FileResponse(frontend if frontend.exists() else BASE_DIR / "templates" / "index.html")


async def _check_captcha(
    page,
    proceed_on_captcha: bool,
    navigation_status: int | None = None,
) -> None:
    challenge = await page.evaluate("""({ status }) => {
        const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" &&
                Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
        };
        const obstruction = (element) => {
            const rect = element.getBoundingClientRect();
            const viewportArea = Math.max(1, innerWidth * innerHeight);
            const area = Math.max(0, rect.width) * Math.max(0, rect.height);
            const coversCenter = rect.left <= innerWidth / 2 && rect.right >= innerWidth / 2 &&
                rect.top <= innerHeight / 2 && rect.bottom >= innerHeight / 2;
            const areaRatio = area / viewportArea;
            return areaRatio >= 0.25 || (coversCenter && areaRatio >= 0.10);
        };
        const providers = {
            cloudflare: {
                widgets: [".cf-turnstile", "iframe[src*='challenges.cloudflare.com']"],
                blocking: ["#challenge-stage", "#challenge-running", "#challenge-form",
                    "iframe[src*='/cdn-cgi/challenge-platform/']"]
            },
            recaptcha: {
                widgets: [".g-recaptcha", "iframe[src*='google.com/recaptcha']",
                    "iframe[src*='recaptcha.net/recaptcha']"],
                blocking: ["iframe[src*='/recaptcha/api2/bframe']"]
            },
            hcaptcha: {
                widgets: [".h-captcha", "iframe[src*='hcaptcha.com/captcha']"],
                blocking: ["iframe[src*='newassets.hcaptcha.com/captcha']"]
            },
            funcaptcha: {
                widgets: [".arkose", "iframe[src*='arkoselabs.com']"],
                blocking: ["iframe[src*='/fc/gc/']"]
            },
            datadome: {
                widgets: ["iframe[src*='captcha-delivery.com']", "#datadome-captcha"],
                blocking: ["iframe[src*='geo.captcha-delivery.com']"]
            }
        };
        const title = (document.title || "").toLowerCase();
        const bodyText = (document.body?.innerText || "").slice(0, 20000).toLowerCase();
        const challengeText = [
            "checking your browser", "verify you are human", "verification required",
            "complete the security check", "performing security verification",
            "unusual traffic", "attention required"
        ].some((phrase) => title.includes(phrase) || bodyText.includes(phrase));
        const signals = [];
        let provider = null;
        let hasBlockingElement = false;
        let hasObstruction = false;
        for (const [name, selectors] of Object.entries(providers)) {
            const widgetElements = selectors.widgets.flatMap((selector) =>
                [...document.querySelectorAll(selector)].filter(visible));
            const blockingElements = selectors.blocking.flatMap((selector) =>
                [...document.querySelectorAll(selector)].filter(visible));
            if (!widgetElements.length && !blockingElements.length) continue;
            provider = name;
            if (widgetElements.length) signals.push("provider_widget");
            if (blockingElements.length) {
                signals.push("challenge_form");
                hasBlockingElement = true;
            }
            hasObstruction = [...widgetElements, ...blockingElements].some(obstruction);
            if (hasObstruction) signals.push("viewport_obstruction");
            break;
        }
        if (status === 429) signals.push("main_response_429");
        else if ([403, 503].includes(status)) signals.push(`main_response_${status}`);
        if (challengeText) signals.push("challenge_copy");

        let kind = null;
        if (status === 429) kind = "rate_limited";
        else if (status === 403 && !provider && !challengeText) kind = "access_denied";
        else if (hasBlockingElement || hasObstruction || challengeText) kind = "blocking_interstitial";
        else if (provider) kind = "embedded_widget";
        if (!kind) return null;

        const confidence = kind === "embedded_widget" ? 0.72 :
            (provider && signals.length >= 2 ? 0.98 : 0.88);
        return { provider: provider || "unknown", kind, confidence, signals };
    }""", {"status": navigation_status})
    if (
        challenge
        and challenge.get("kind") != "embedded_widget"
        and not proceed_on_captcha
    ):
        provider = str(challenge.get("provider") or "unknown")
        provider_label = {
            "cloudflare": "Cloudflare",
            "recaptcha": "Google reCAPTCHA",
            "hcaptcha": "hCaptcha",
            "funcaptcha": "Arkose Labs",
            "datadome": "DataDome",
            "unknown": "A page-level",
        }.get(provider, provider.replace("_", " ").title())
        raise RenderError(
            "captcha_detected",
            f"{provider_label} challenge blocked the page.",
            409,
            False,
            challenge,
        )


def _page_cleanup_options(options) -> CleanupOptions:
    return CleanupOptions(
        consent_mode=options.consent_mode.value,
        block_ads=options.block_ads,
        block_trackers=options.block_trackers,
        block_chats=options.block_chats,
        block_newsletters=options.block_newsletters,
    )


async def _apply_page_cleanup(page, options) -> dict[str, int]:
    await apply_visual_cleanup(page, _page_cleanup_options(options))
    return {}


def _blocked_resource_category(url: str, options) -> str | None:
    return should_block_resource(url, _page_cleanup_options(options))


def _render_engine() -> RenderEngine:
    playwright = getattr(app.state, "playwright", None)
    return RenderEngine(
        hosted=HOSTED,
        cleanup_hooks=CleanupHooks(
            setup=setup_autoconsent,
            finish=finish_autoconsent,
            apply=_apply_page_cleanup,
            blocked_category=_blocked_resource_category,
        ),
        challenge_checker=_check_captcha,
        browser_replacer=lambda failed: _replace_browser(app, failed),
        device_descriptors=(
            dict(playwright.devices) if playwright is not None else None
        ),
        allow_scripts=ALLOW_SCRIPTS,
    )


async def _render_async_image(payload: RenderRequest) -> RenderedArtifact:
    started = time.perf_counter()
    await app.state.capture_slots.acquire()
    browser: Browser = app.state.browser
    engine = _render_engine()
    try:
        artifact, _cache_hit = await _render_with_cache(engine, browser, payload)
    except RenderError:
        if not browser.is_connected():
            with suppress(Exception):
                await _replace_browser(app, browser)
        raise
    finally:
        app.state.capture_slots.release()
    return RenderedArtifact(
        body=artifact.body,
        media_type=artifact.media_type,
        filename=artifact.filename,
        render_ms=round((time.perf_counter() - started) * 1000),
    )


async def _render_with_cache(
    engine: RenderEngine,
    browser: Browser,
    payload: RenderRequest,
) -> tuple[RenderArtifact, bool]:
    cache = getattr(app.state, "render_cache", None)
    if payload.cache and cache is not None:
        cached = await cache.get(payload)
        if cached is not None:
            return cached, True
    artifact = await engine.render_image(
        browser,
        payload,
        RenderLimits(max_pixels=MAX_SCREENSHOT_PIXELS),
    )
    if payload.cache and cache is not None:
        await cache.put(payload, artifact)
    return artifact, False


async def _notify_job(webhook_url: str, job) -> None:
    dispatcher = getattr(app.state, "webhooks", None)
    if dispatcher is None:
        return
    await dispatcher.deliver(
        webhook_url,
        public_job_document(job),
    )


async def _render_response(payload: RenderRequest, request: Request) -> Response:
    if payload.delivery.webhook_url is not None:
        raise RenderError(
            "delivery_requires_async_job",
            "Webhook delivery is accepted only by POST /v1/jobs.",
            422,
            False,
        )
    cache = getattr(app.state, "render_cache", None)
    artifact = None
    cache_hit = False
    queue_ms = 0
    render_ms = 0
    if payload.cache and cache is not None:
        artifact = await _await_while_connected(request, cache.get(payload))
        cache_hit = artifact is not None

    if artifact is None:
        queue_started = time.perf_counter()
        try:
            await _await_while_connected(
                request,
                asyncio.wait_for(
                    app.state.capture_slots.acquire(),
                    timeout=CAPTURE_QUEUE_TIMEOUT_SECONDS,
                ),
            )
        except TimeoutError as exc:
            raise RenderError(
                "capture_queue_busy", "The render queue is busy.", 503, True
            ) from exc
        queue_ms = round((time.perf_counter() - queue_started) * 1000)
        browser: Browser = app.state.browser
        engine = _render_engine()
        render_started = time.perf_counter()
        try:
            artifact = await _await_while_connected(
                request,
                engine.render_image(
                    browser,
                    payload,
                    RenderLimits(max_pixels=MAX_SCREENSHOT_PIXELS),
                ),
            )
            if payload.cache and cache is not None:
                await cache.put(payload, artifact)
        except RenderError:
            if not browser.is_connected():
                with suppress(Exception):
                    await _replace_browser(app, browser)
            raise
        finally:
            app.state.capture_slots.release()
        render_ms = round((time.perf_counter() - render_started) * 1000)
    metadata = artifact.metadata or {}
    diagnostic_headers = {
        "X-ViperCapture-Queue-Ms": str(max(0, queue_ms)),
        "X-ViperCapture-Render-Ms": str(max(0, render_ms)),
        "X-ViperCapture-Cache": "hit" if cache_hit else ("miss" if payload.cache else "disabled"),
    }
    for key, header in (
        ("width", "X-ViperCapture-Width"),
        ("height", "X-ViperCapture-Height"),
        ("navigation_status", "X-ViperCapture-Navigation-Status"),
    ):
        value = metadata.get(key)
        if isinstance(value, (int, float)):
            diagnostic_headers[header] = str(round(value))
    return Response(
        artifact.body,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            **diagnostic_headers,
        },
    )


@app.post("/v1/render", response_class=Response)
async def render_v1(payload: RenderRequest, request: Request) -> Response:
    return await _render_response(payload, request)


@app.post("/v1/diff", response_class=Response)
async def visual_diff(
    baseline: UploadFile = File(...),
    current: UploadFile = File(...),
    pixel_threshold: int = Form(0),
    max_difference_ratio: float = Form(0),
) -> Response:
    try:
        await asyncio.wait_for(
            app.state.diff_slots.acquire(),
            timeout=CAPTURE_QUEUE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise RenderError(
            "diff_queue_busy", "The visual diff queue is busy.", 503, True
        ) from exc
    try:
        baseline_body = await baseline.read(MAX_DIFF_INPUT_BYTES + 1)
        current_body = await current.read(MAX_DIFF_INPUT_BYTES + 1)
        result = await asyncio.to_thread(
            compare_images,
            baseline_body,
            current_body,
            pixel_threshold=pixel_threshold,
            max_difference_ratio=max_difference_ratio,
        )
        bundle = await asyncio.to_thread(create_diff_bundle, result)
    except ValueError as exc:
        raise RenderError("diff_options_invalid", str(exc), 422, False) from exc
    finally:
        app.state.diff_slots.release()
    return Response(
        bundle,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="visual-diff.zip"',
            "X-ViperCapture-Diff-Passed": str(result.passed).lower(),
            "X-ViperCapture-Difference-Ratio": f"{result.ratio:.8f}",
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/v1/render/signed", response_class=Response)
async def render_signed(
    request: Request,
    payload: str,
    expires: int,
    signature: str,
) -> Response:
    if not SIGNING_SECRET:
        raise RenderError(
            "signed_urls_disabled",
            "Signed render URLs are disabled for this ViperCapture instance.",
            503,
            False,
        )
    render_request = verify_render_request(
        payload,
        expires,
        signature,
        secret=SIGNING_SECRET,
    )
    response = await _render_response(render_request, request)
    if render_request.output is not OutputFormat.HTML:
        disposition = response.headers.get("Content-Disposition", "")
        response.headers["Content-Disposition"] = disposition.replace(
            "attachment", "inline", 1
        )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.post("/v1/signed-url")
async def create_signed_url(
    payload: RenderRequest,
    request: Request,
    ttl_seconds: int = 3600,
) -> JSONResponse:
    if not SIGNING_SECRET:
        raise RenderError(
            "signed_urls_disabled",
            "Signed render URLs are disabled for this ViperCapture instance.",
            503,
            False,
        )
    expected_token = SIGNING_ADMIN_TOKEN or SIGNING_SECRET
    supplied = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not supplied.startswith(prefix) or not hmac.compare_digest(
        supplied[len(prefix):], expected_token
    ):
        raise RenderError(
            "signing_unauthorized",
            "A valid signing administrator token is required.",
            401,
            False,
        )
    if payload.delivery.webhook_url is not None:
        raise RenderError(
            "delivery_requires_async_job",
            "Webhook delivery is accepted only by POST /v1/jobs.",
            422,
            False,
        )
    try:
        encoded, expires, signature = sign_render_request(
            payload,
            secret=SIGNING_SECRET,
            ttl_seconds=ttl_seconds,
        )
    except ValueError as exc:
        raise RenderError("signed_url_invalid", str(exc), 422, False) from exc
    path = "/v1/render/signed?" + urlencode(
        {"payload": encoded, "expires": expires, "signature": signature}
    )
    return JSONResponse(
        {
            "url": f"{PUBLIC_URL}{path}" if PUBLIC_URL else path,
            "expires": expires,
        },
        headers={"Cache-Control": "private, no-store"},
    )


def _async_job_service() -> AsyncJobService:
    service = getattr(app.state, "async_jobs", None)
    if service is None:
        raise RenderError(
            "async_jobs_disabled",
            "Async jobs are disabled for this ViperCapture instance.",
            503,
            False,
        )
    return service


@app.post("/v1/jobs", status_code=202)
async def create_render_job(
    payload: RenderRequest,
    request: Request,
) -> JSONResponse:
    job = await _submit_job(
        _async_job_service(),
        payload,
        request_id=request.state.request_id,
    )
    document = public_job_document(job)
    return JSONResponse(
        document,
        status_code=202,
        headers={
            "Location": str(document["status_url"]),
            "Retry-After": "1",
            "Cache-Control": "private, no-store",
        },
    )


@app.post("/v1/jobs/bulk", status_code=202)
async def create_bulk_render_jobs(
    payload: BulkJobRequest,
    request: Request,
) -> JSONResponse:
    service = _async_job_service()
    results = []
    failures = 0
    for index, item in enumerate(payload.items):
        request_id = item.request_id or f"{request.state.request_id}-{index + 1}"
        try:
            job = await _submit_job(
                service,
                item.render,
                request_id=request_id,
            )
            results.append(
                {
                    "index": index,
                    "id": item.id,
                    "accepted": True,
                    "job": public_job_document(job),
                    "error": None,
                }
            )
        except RenderError as exc:
            failures += 1
            results.append(
                {
                    "index": index,
                    "id": item.id,
                    "accepted": False,
                    "job": None,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "retryable": exc.retryable,
                        "details": exc.details,
                    },
                }
            )
    return JSONResponse(
        {
            "count": len(results),
            "accepted": len(results) - failures,
            "failed": failures,
            "results": results,
        },
        status_code=207 if failures else 202,
        headers={"Cache-Control": "private, no-store"},
    )


def _schedule_service() -> ScheduleService:
    service = getattr(app.state, "schedules", None)
    if service is None:
        raise RenderError(
            "schedules_disabled",
            "Schedules are disabled for this ViperCapture instance.",
            503,
            False,
        )
    return service


async def _validate_webhook(payload: RenderRequest) -> None:
    if payload.delivery.webhook_url is None:
        return
    dispatcher = getattr(app.state, "webhooks", None)
    if dispatcher is None:
        raise RenderError(
            "webhooks_disabled",
            "Webhook delivery is disabled for this ViperCapture instance.",
            503,
            False,
        )
    await dispatcher.validate_url(str(payload.delivery.webhook_url))


async def _submit_job(
    service: AsyncJobService,
    payload: RenderRequest,
    *,
    request_id: str,
):
    existing = await service.existing(payload, request_id=request_id)
    if existing is not None:
        return existing
    try:
        await _validate_webhook(payload)
    except RenderError:
        # A concurrent request may have committed while validation was in
        # flight. Preserve idempotent replay semantics in that race too.
        existing = await service.existing(payload, request_id=request_id)
        if existing is not None:
            return existing
        raise
    return await service.submit(payload, request_id=request_id)


@app.post("/v1/schedules", status_code=201)
async def create_schedule(payload: ScheduleCreate) -> JSONResponse:
    await _validate_webhook(payload.render)
    record = await _schedule_service().create(payload)
    return JSONResponse(
        public_schedule_document(record),
        status_code=201,
        headers={"Location": f"/v1/schedules/{record.id}", "Cache-Control": "private, no-store"},
    )


@app.get("/v1/schedules")
async def list_schedules(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after: Annotated[str | None, Query(max_length=256)] = None,
) -> JSONResponse:
    records = await _schedule_service().store.list(
        limit=limit + 1,
        after=after,
    )
    has_more = len(records) > limit
    records = records[:limit]
    return JSONResponse(
        {
            "count": len(records),
            "schedules": [public_schedule_document(item) for item in records],
            "next_cursor": schedule_cursor(records[-1]) if has_more else None,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/v1/schedules/{schedule_id}")
async def read_schedule(schedule_id: UUID) -> JSONResponse:
    record = await _schedule_service().store.get(str(schedule_id))
    if record is None:
        raise RenderError("schedule_not_found", "The schedule was not found.", 404, False)
    return JSONResponse(public_schedule_document(record), headers={"Cache-Control": "private, no-store"})


@app.patch("/v1/schedules/{schedule_id}")
async def update_schedule(schedule_id: UUID, payload: ScheduleUpdate) -> JSONResponse:
    service = _schedule_service()
    record = await service.store.get(str(schedule_id))
    if record is None:
        raise RenderError("schedule_not_found", "The schedule was not found.", 404, False)
    if payload.render is not None:
        await _validate_webhook(payload.render)
    updated = await service.update(record, payload)
    return JSONResponse(public_schedule_document(updated), headers={"Cache-Control": "private, no-store"})


@app.delete("/v1/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: UUID) -> Response:
    deleted = await _schedule_service().store.delete(str(schedule_id))
    if not deleted:
        raise RenderError("schedule_not_found", "The schedule was not found.", 404, False)
    return Response(status_code=204)


@app.get("/v1/jobs/{job_id}")
async def read_render_job(job_id: UUID) -> JSONResponse:
    job = await _async_job_service().get(str(job_id))
    if job is None:
        raise RenderError(
            "job_not_found",
            "The async job was not found.",
            404,
            False,
        )
    return JSONResponse(
        public_job_document(job),
        headers={"Cache-Control": "private, no-store"},
    )


@app.delete("/v1/jobs/{job_id}")
async def cancel_render_job(job_id: UUID) -> JSONResponse:
    job = await _async_job_service().cancel(str(job_id))
    if job is None:
        raise RenderError(
            "job_not_found",
            "The async job was not found.",
            404,
            False,
        )
    return JSONResponse(
        public_job_document(job),
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/v1/jobs/{job_id}/result", response_class=Response)
async def read_render_job_result(job_id: UUID, request: Request) -> Response:
    service = _async_job_service()
    job = await service.get(str(job_id))
    if job is None:
        raise RenderError(
            "job_not_found",
            "The async job was not found.",
            404,
            False,
        )
    if job.status == "expired" and job.error_code == "async_result_expired":
        raise RenderError(
            "async_result_expired",
            "The async job result is no longer available.",
            410,
            False,
        )
    if job.status != "succeeded":
        raise RenderError(
            "job_not_ready",
            "The async job result is not available.",
            409,
            job.status in {"queued", "running"},
            {"status": job.status},
            {"Retry-After": "1"} if job.status in {"queued", "running"} else None,
        )
    slots = app.state.async_result_slots
    acquire_task = asyncio.create_task(slots.acquire())
    acquired = False
    try:
        await _await_while_connected(request, acquire_task)
        acquired = True
        is_disconnected = getattr(request, "is_disconnected", None)
        if callable(is_disconnected) and await is_disconnected():
            raise RenderError(
                "client_disconnected",
                "The result download was cancelled.",
                499,
                False,
            )
        artifact = await _await_while_connected(
            request,
            service.result(job),
        )
        if artifact is None:
            raise RenderError(
                "async_result_expired",
                "The async job result is no longer available.",
                410,
                False,
            )
    except BaseException:
        if acquired:
            slots.release()
        elif acquire_task.done() and not acquire_task.cancelled():
            with suppress(BaseException):
                if acquire_task.result():
                    slots.release()
        raise
    released = False

    def release_slot() -> None:
        nonlocal released
        if not released:
            released = True
            slots.release()

    async def stream_body():
        try:
            yield artifact.body
        finally:
            release_slot()

    return _SlotStreamingResponse(
        stream_body(),
        release_slot=release_slot,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "Cache-Control": "private, no-store",
        },
    )


def _safe_filename(filename: str, image_format: str | None = None) -> str:
    name = filename.strip() or "screenshot.png"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    existing = re.search(r"\.(png|jpe?g|webp)$", name, flags=re.IGNORECASE)
    selected = image_format or (
        "jpeg" if existing and existing.group(1).lower() in {"jpg", "jpeg"}
        else existing.group(1).lower() if existing else "png"
    )
    extension = "jpg" if selected == "jpeg" else selected
    stem = name[:existing.start()] if existing else name
    return f"{stem}.{extension}"


def _unique_capture_path(filename: str) -> Path:
    safe_name = _safe_filename(filename)
    target = CAPTURES_DIR / safe_name
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return CAPTURES_DIR / f"{stem}_{timestamp}{suffix}"


def _is_local_control_request(request: Request) -> bool:
    if DESKTOP_TOKEN:
        return request.headers.get("authorization") == f"Bearer {DESKTOP_TOKEN}"
    client = request.client.host if request.client else ""
    host_header = request.headers.get("host", "").lower()
    host = (
        host_header[1:host_header.find("]")]
        if host_header.startswith("[") and "]" in host_header
        else host_header.split(":", 1)[0]
    )
    origin = request.headers.get("origin")
    expected_origins = {
        f"http://127.0.0.1:{request.url.port or 80}",
        f"http://localhost:{request.url.port or 80}",
        f"http://[::1]:{request.url.port or 80}",
    }
    return (
        client in {"127.0.0.1", "::1"}
        and host in {"127.0.0.1", "localhost", "::1"}
        and origin in expected_origins
    )


async def _gpu_config(app: FastAPI) -> dict[str, object]:
    return {
        "mode": app.state.gpu_mode,
        "hardware_active": app.state.gpu_hardware_active,
        "mutable": not HOSTED,
    }


@app.get("/app-config")
async def app_config():
    return {
        "server_saves": not HOSTED,
        "max_screenshot_pixels": MAX_SCREENSHOT_PIXELS,
        "async_jobs": {
            "enabled": ASYNC_JOBS_ENABLED,
            "workers": (
                ASYNC_JOB_SETTINGS.worker_count
                if ASYNC_JOB_SETTINGS is not None
                else 0
            ),
            "queue_limit": (
                ASYNC_JOB_SETTINGS.queue_limit
                if ASYNC_JOB_SETTINGS is not None
                else 0
            ),
        },
        "gpu": await _gpu_config(app),
    }


if not HOSTED:
    @app.post("/local/gpu-mode")
    async def set_local_gpu_mode(request: Request):
        if not _is_local_control_request(request):
            raise HTTPException(
                status_code=403,
                detail="GPU mode can only be changed from the local ViperCapture interface",
            )
        payload = await request.json()
        mode = payload.get("mode") if isinstance(payload, dict) else None
        if mode not in {"off", "auto"}:
            raise HTTPException(status_code=422, detail="GPU mode must be off or auto")
        if mode == app.state.gpu_mode:
            return {"gpu": await _gpu_config(app)}

        acquired = 0
        try:
            for _ in range(MAX_CONCURRENT_CAPTURES):
                await asyncio.wait_for(
                    app.state.capture_slots.acquire(),
                    timeout=CAPTURE_QUEUE_TIMEOUT_SECONDS,
                )
                acquired += 1
            async with app.state.browser_restart_lock:
                replacement = await asyncio.wait_for(
                    _launch_browser(app.state.playwright, mode),
                    timeout=15,
                )
                hardware_active = await _detect_hardware_gpu(replacement, mode)
                previous = app.state.browser
                app.state.browser = replacement
                app.state.gpu_mode = mode
                app.state.gpu_hardware_active = hardware_active
                with suppress(Exception):
                    await asyncio.wait_for(previous.close(), timeout=5)
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503,
                detail="The renderer is busy; try the GPU switch again shortly",
            ) from exc
        finally:
            for _ in range(acquired):
                app.state.capture_slots.release()

        return {"gpu": await _gpu_config(app)}


    @app.post("/save-screenshot")
    async def save_screenshot(
        screenshot: UploadFile = File(...),
        filename: str = Form("screenshot.png"),
    ):
        data = await screenshot.read()
        if not data:
            raise HTTPException(status_code=400, detail="No screenshot data provided")

        target = _unique_capture_path(filename)
        target.write_bytes(data)
        return {
            "saved": True,
            "filename": target.name,
            "path": str(target),
            "directory": str(CAPTURES_DIR),
        }


    @app.post("/open-downloads-folder")
    async def open_downloads_folder():
        downloads = Path(
            os.getenv("VIPERCAPTURE_DOWNLOADS_DIR", str(Path.home() / "Downloads"))
        ).expanduser()
        try:
            if sys.platform.startswith("win"):
                override = os.getenv("VIPERCAPTURE_DOWNLOADS_DIR")
                if override:
                    os.startfile(str(downloads))
                else:
                    subprocess.Popen(["explorer.exe", "shell:Downloads"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(downloads)])
            else:
                # ponytail: VIPERCAPTURE_DOWNLOADS_DIR covers custom browser locations.
                subprocess.Popen(["xdg-open", str(downloads)])
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to open Downloads folder") from exc

        return {"opened": True, "directory": str(downloads)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("VIPERCAPTURE_HOST", "127.0.0.1"),
        port=int(os.getenv("VIPERCAPTURE_PORT", "8000")),
        reload=False,
    )
