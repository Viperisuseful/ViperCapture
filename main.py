import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Awaitable, TypeVar
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
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
from render_contract import RenderRequest
from render_engine import RenderEngine, RenderLimits
from render_errors import RenderError, install_render_error_layer


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
    app.state.async_result_slots = asyncio.Semaphore(MAX_ASYNC_RESULT_DOWNLOADS)
    app.state.browser_restart_lock = asyncio.Lock()
    app.state.async_jobs = None
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
            )
            await service.start()
            app.state.async_jobs = service
        yield
    finally:
        try:
            if app.state.async_jobs is not None:
                await app.state.async_jobs.close()
        finally:
            with suppress(Exception):
                await app.state.browser.close()
            await playwright.stop()


app = FastAPI(lifespan=lifespan)
install_render_error_layer(app)
if DESKTOP_TOKEN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DESKTOP_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
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
    if (
        DESKTOP_TOKEN
        and request.method != "OPTIONS"
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


async def _render_async_image(payload: RenderRequest) -> RenderedArtifact:
    started = time.perf_counter()
    await app.state.capture_slots.acquire()
    browser: Browser = app.state.browser
    engine = RenderEngine(
        hosted=HOSTED,
        challenge_checker=_check_captcha,
        browser_replacer=lambda failed: _replace_browser(app, failed),
    )
    try:
        artifact = await engine.render_image(
            browser,
            payload,
            RenderLimits(max_pixels=MAX_SCREENSHOT_PIXELS),
        )
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


@app.post("/v1/render", response_class=Response)
async def render_v1(payload: RenderRequest, request: Request) -> Response:
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
        raise RenderError("capture_queue_busy", "The render queue is busy.", 503, True) from exc
    queue_ms = round((time.perf_counter() - queue_started) * 1000)
    browser: Browser = app.state.browser
    engine = RenderEngine(
        hosted=HOSTED,
        challenge_checker=_check_captcha,
        browser_replacer=lambda failed: _replace_browser(app, failed),
    )
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
    job = await _async_job_service().submit(
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
    await _await_while_connected(request, slots.acquire())
    is_disconnected = getattr(request, "is_disconnected", None)
    if callable(is_disconnected) and await is_disconnected():
        slots.release()
        raise RenderError(
            "client_disconnected",
            "The result download was cancelled.",
            499,
            False,
        )
    try:
        artifact = await service.result(job)
    except BaseException:
        slots.release()
        raise
    if artifact is None:
        slots.release()
        raise RenderError(
            "async_result_expired",
            "The async job result is no longer available.",
            410,
            False,
        )
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
