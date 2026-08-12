import asyncio
import hmac
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Awaitable, Literal, TypeVar
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

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
from playwright_stealth import Stealth
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .async_jobs import (
    AsyncJobService,
    JobDeferred,
    RenderedArtifact,
    current_job,
    load_providers,
    public_job_document,
    settings_from_environment,
)
from .bulk_jobs import BulkBodyLimitMiddleware, BulkJobRequest
from .compatibility import screenshotone_request, urlbox_request
from .control_plane import (
    BaselineQuotaError,
    ControlPlane,
    Metrics,
    ProfileQuotaError,
    ScheduleQuotaError,
)
from .page_cleanup import (
    CleanupOptions,
    apply_visual_cleanup,
    finish_autoconsent,
    setup_autoconsent,
    should_block_resource,
)
from .render_cache import RenderCache
from .render_contract import BrowserEngine, OutputFormat, RenderRequest
from .render_engine import (
    CleanupHooks,
    RenderArtifact,
    RenderEngine,
    RenderLimits,
    _settled_thread,
    certification_public_key,
    ffmpeg_has_encoder,
)
from .render_errors import RenderError, error_response, install_render_error_layer
from .schedules import (
    ScheduleCreate,
    ScheduleService,
    ScheduleUpdate,
    load_schedule_store,
    public_schedule_document,
    schedule_cursor,
)
from .signed_urls import sign_render_request, verify_render_request
from .telemetry import configure_telemetry
from .visual_diff import (
    MAX_DIFF_INPUT_BYTES,
    compare_images,
    create_diff_bundle,
    validate_image,
)
from .webhooks import WebhookDispatcher

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


BASE_DIR = Path(__file__).resolve().parent.parent
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
DESKTOP_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


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
PROCESS_ROLE = os.getenv("VIPERCAPTURE_ROLE", "all").lower()
if PROCESS_ROLE not in {"all", "api", "worker"}:
    raise ValueError("VIPERCAPTURE_ROLE must be all, api, or worker")
MAX_ASYNC_RESULT_DOWNLOADS = max(
    1, int(os.getenv("VIPERCAPTURE_ASYNC_RESULT_CONCURRENCY", "2"))
)
MAX_SCREENSHOT_PIXELS = max(
    1, int(os.getenv("VIPERCAPTURE_MAX_PIXELS", "500000000"))
)
MAX_VIEWPORT_WIDTH = max(
    1, int(os.getenv("VIPERCAPTURE_MAX_WIDTH", "16384"))
)
MAX_VIEWPORT_HEIGHT = max(
    1, int(os.getenv("VIPERCAPTURE_MAX_HEIGHT", "16384"))
)
MAX_FULL_PAGE_HEIGHT = max(
    1, int(os.getenv("VIPERCAPTURE_MAX_FULL_PAGE_HEIGHT", "100000"))
)
MAX_OUTPUT_BYTES = max(
    1, int(os.getenv("VIPERCAPTURE_MAX_OUTPUT_BYTES", str(1024 * 1024 * 1024)))
)
MAX_DIFF_CONCURRENCY = max(
    1, int(os.getenv("VIPERCAPTURE_DIFF_CONCURRENCY", "1"))
)
CAPTURE_QUEUE_TIMEOUT_SECONDS = 30
BULK_WEBHOOK_VALIDATION_TIMEOUT_SECONDS = 30
AwaitedResult = TypeVar("AwaitedResult")
ASYNC_JOBS_ENABLED = os.getenv(
    "VIPERCAPTURE_ASYNC_JOBS",
    "0" if os.name == "nt" else "1",
) != "0"
if PROCESS_ROLE == "worker" and not ASYNC_JOBS_ENABLED:
    raise ValueError("VIPERCAPTURE_ROLE=worker requires VIPERCAPTURE_ASYNC_JOBS=1")
ASYNC_JOB_SETTINGS = (
    settings_from_environment(
        default_workers=0 if PROCESS_ROLE == "api" else MAX_CONCURRENT_CAPTURES,
        allow_zero_workers=PROCESS_ROLE == "api",
    )
    if ASYNC_JOBS_ENABLED
    else None
)
if ASYNC_JOB_SETTINGS is not None and PROCESS_ROLE == "api":
    ASYNC_JOB_SETTINGS = replace(ASYNC_JOB_SETTINGS, worker_count=0)


def _job_ownership_ttl() -> int | None:
    if ASYNC_JOB_SETTINGS is None:
        return None
    return int(
        (
            ASYNC_JOB_SETTINGS.queue_ttl
            + ASYNC_JOB_SETTINGS.result_ttl
            + ASYNC_JOB_SETTINGS.metadata_ttl
        ).total_seconds()
    )


def _internal_project_id(request_id: str) -> str | None:
    candidate = request_id.partition(":")[0]
    if re.fullmatch(r"_project-[0-9a-f]{24}", candidate):
        return candidate.removeprefix("_project-")
    return None


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
CONTROL_ADMIN_TOKEN = os.getenv("VIPERCAPTURE_ADMIN_TOKEN", "")
CONTROL_ENABLED = bool(CONTROL_ADMIN_TOKEN)
if CONTROL_ENABLED and len(CONTROL_ADMIN_TOKEN.encode()) < 32:
    raise ValueError("VIPERCAPTURE_ADMIN_TOKEN must contain at least 32 bytes")
CONTROL_SECRET = os.getenv("VIPERCAPTURE_CONTROL_SECRET", "")
if CONTROL_ENABLED and len(CONTROL_SECRET.encode()) < 32:
    raise ValueError(
        "VIPERCAPTURE_CONTROL_SECRET must contain at least 32 bytes when the control plane is enabled"
    )
CONTROL_DATABASE = Path(
    os.getenv("VIPERCAPTURE_CONTROL_DATABASE", str(CACHE_DIRECTORY.parent / "control.sqlite3"))
).expanduser()
METRICS = Metrics()
STEALTH = Stealth(
    navigator_platform_override=(
        "Win32"
        if sys.platform.startswith("win")
        else "MacIntel"
        if sys.platform == "darwin"
        else "Linux x86_64"
    ),
    sec_ch_ua=False,
    webgl_vendor=False,
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
    engine: BrowserEngine = BrowserEngine.CHROMIUM,
) -> Browser:
    selected_mode = gpu_mode or GPU_MODE
    browser_type = getattr(playwright, engine.value)
    launch_options = {
        "headless": True,
        "args": (
            gpu_launch_args(selected_mode)
            if engine.value == BrowserEngine.CHROMIUM.value
            else []
        ),
    }
    if engine.value == BrowserEngine.CHROMIUM.value:
        launch_options.update(
            channel="chromium",
            env={
                **os.environ,
                "XDG_CACHE_HOME": "/tmp/chromium-cache",
                "XDG_CONFIG_HOME": "/tmp/chromium-config",
            },
        )
    browser = await browser_type.launch(
        **launch_options,
    )
    if selected_mode == "required" and engine.value == BrowserEngine.CHROMIUM.value:
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


async def _browser_for(app: FastAPI, engine: BrowserEngine) -> Browser:
    browsers = getattr(app.state, "browsers", None)
    if browsers is None:
        return app.state.browser
    browser = browsers.get(engine)
    if browser is not None and browser.is_connected():
        return browser
    async with app.state.browser_restart_lock:
        browser = browsers.get(engine)
        if browser is not None and browser.is_connected():
            return browser
        try:
            browser = await asyncio.wait_for(
                _launch_browser(
                    app.state.playwright,
                    app.state.gpu_mode,
                    engine,
                ),
                timeout=20,
            )
        except Exception as exc:
            raise RenderError(
                "browser_unavailable",
                f"The {engine.value} browser is not installed or could not start.",
                503,
                False,
            ) from exc
        browsers[engine] = browser
        if engine.value == BrowserEngine.CHROMIUM.value:
            app.state.browser = browser
        return browser


async def _replace_browser(app: FastAPI, failed_browser: Browser) -> None:
    async with app.state.browser_restart_lock:
        browsers = getattr(app.state, "browsers", None)
        engine = next(
            (
                name
                for name, browser in (browsers or {}).items()
                if browser is failed_browser
            ),
            BrowserEngine.CHROMIUM if app.state.browser is failed_browser else None,
        )
        if engine is None:
            return
        with suppress(Exception):
            await asyncio.wait_for(failed_browser.close(), timeout=5)
        replacement = await asyncio.wait_for(
            _launch_browser(app.state.playwright, app.state.gpu_mode, engine),
            timeout=15,
        )
        if browsers is not None:
            browsers[engine] = replacement
        if engine.value == BrowserEngine.CHROMIUM.value:
            app.state.browser = replacement
            app.state.gpu_hardware_active = await _detect_hardware_gpu(
                replacement,
                app.state.gpu_mode,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.control = None
    if CONTROL_ENABLED:
        if PROCESS_ROLE != "all":
            raise RuntimeError(
                "the built-in SQLite control plane supports only VIPERCAPTURE_ROLE=all; "
                "put authentication and quotas in shared infrastructure for split roles"
            )
        control = ControlPlane(CONTROL_DATABASE, encryption_secret=CONTROL_SECRET)
        await asyncio.to_thread(control.initialize)
        app.state.control = control
    if PROCESS_ROLE in {"api", "worker"} and ASYNC_JOBS_ENABLED:
        if not os.getenv("VIPERCAPTURE_JOB_STORE_FACTORY"):
            raise RuntimeError("split api/worker roles require VIPERCAPTURE_JOB_STORE_FACTORY")
        if not (
            os.getenv("VIPERCAPTURE_ARTIFACT_STORE_FACTORY")
            or os.getenv("VIPERCAPTURE_S3_BUCKET")
        ):
            raise RuntimeError("split api/worker roles require shared artifact storage")
        if not os.getenv("VIPERCAPTURE_JOB_SECRET"):
            raise RuntimeError("split api/worker roles require VIPERCAPTURE_JOB_SECRET")
        if SCHEDULES_ENABLED and not os.getenv(
            "VIPERCAPTURE_SCHEDULE_STORE_FACTORY"
        ):
            raise RuntimeError(
                "split api/worker roles require VIPERCAPTURE_SCHEDULE_STORE_FACTORY or VIPERCAPTURE_SCHEDULES=0"
            )
    playwright: Playwright = await async_playwright().start()
    STEALTH.hook_playwright_context(playwright)
    browser = await _launch_browser(playwright)
    app.state.playwright = playwright
    app.state.browser = browser
    app.state.browsers = {BrowserEngine.CHROMIUM: browser}
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
        security_namespace=(
            f"hosted={int(HOSTED)};scripts={int(ALLOW_SCRIPTS)};"
            f"max_pixels={MAX_SCREENSHOT_PIXELS}"
        ),
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
            if PROCESS_ROLE == "worker" and not callable(
                getattr(job_store, "recover_stale", None)
            ):
                raise RuntimeError(
                    "split workers require a job store with lease-based recover_stale()"
                )

            async def reserve_job_ownership(job_id: str, request_id: str) -> None:
                control = getattr(app.state, "control", None)
                project_id = _internal_project_id(request_id)
                if control is None or project_id is None:
                    return
                await _settled_thread(
                    control.own,
                    "job",
                    job_id,
                    project_id,
                    _job_ownership_ttl(),
                )
                if not await asyncio.to_thread(
                    control.is_owner, "job", job_id, project_id
                ):
                    raise RuntimeError("job ownership reservation conflicted")

            async def release_job_ownership(job_id: str) -> None:
                control = getattr(app.state, "control", None)
                if control is not None:
                    await _settled_thread(control.disown, "job", job_id)

            service = AsyncJobService(
                ASYNC_JOB_SETTINGS,
                job_store,
                artifact_store,
                _render_async_image,
                notifier=_notify_job if app.state.webhooks is not None else None,
                recover_running=PROCESS_ROLE == "all",
                recover_stale=PROCESS_ROLE == "worker",
                ownership_reserver=(
                    reserve_job_ownership if CONTROL_ENABLED else None
                ),
                ownership_releaser=(
                    release_job_ownership if CONTROL_ENABLED else None
                ),
            )
            await service.start()
            app.state.async_jobs = service
            if SCHEDULES_ENABLED:
                async def own_scheduled_job(schedule_id: str, job_id: str) -> None:
                    control = getattr(app.state, "control", None)
                    if control is None:
                        return
                    project_id = await asyncio.to_thread(control.owner, "schedule", schedule_id)
                    if project_id is not None:
                        await asyncio.to_thread(
                            control.own,
                            "job",
                            job_id,
                            project_id,
                            _job_ownership_ttl(),
                        )

                async def scheduled_project(schedule_id: str) -> str | None:
                    control = getattr(app.state, "control", None)
                    if control is None:
                        return None
                    return await asyncio.to_thread(
                        control.owner, "schedule", schedule_id
                    )

                async def resize_scheduled_payload(
                    schedule_id: str,
                    project_id: str | None,
                    size_bytes: int,
                ) -> None:
                    control = getattr(app.state, "control", None)
                    if control is None:
                        return
                    owner = project_id or await asyncio.to_thread(
                        control.owner, "schedule", schedule_id
                    )
                    if owner is not None:
                        await _settled_thread(
                            control.resize_schedule,
                            schedule_id,
                            owner,
                            size_bytes,
                        )

                scheduler = ScheduleService(
                    load_schedule_store(ASYNC_JOB_SETTINGS),
                    service,
                    service.cipher,
                    on_job_created=own_scheduled_job,
                    project_for_schedule=scheduled_project,
                    on_schedule_resize=resize_scheduled_payload,
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
            closed_browser_ids: set[int] = set()
            for active_browser in app.state.browsers.values():
                if id(active_browser) in closed_browser_ids:
                    continue
                closed_browser_ids.add(id(active_browser))
                with suppress(Exception):
                    await active_browser.close()
            await playwright.stop()


app = FastAPI(lifespan=lifespan)
TELEMETRY_ENABLED = configure_telemetry(app)
app.add_middleware(BulkBodyLimitMiddleware)
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
    protected_api = path.startswith("/v1") or path == "/take" or path.startswith("/compat/")
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
    if not hasattr(request, "state"):
        request.state = SimpleNamespace()
    request.state.project_id = None
    request.state.key_id = None
    request.state.scopes = []
    request.state.is_admin = False
    request.state.trusted_local = False
    request_app = getattr(request, "app", app)
    control = getattr(request_app.state, "control", None)
    acquired_project = None
    authorization = request.headers.get("authorization", "")
    desktop_authenticated = bool(
        DESKTOP_TOKEN
        and hmac.compare_digest(authorization, f"Bearer {DESKTOP_TOKEN}")
    )
    request.state.trusted_local = desktop_authenticated
    if CONTROL_ENABLED and hmac.compare_digest(authorization, f"Bearer {CONTROL_ADMIN_TOKEN}"):
        request.state.is_admin = True
    elif (
        CONTROL_ENABLED
        and request.method != "OPTIONS"
        and protected_api
        and not signed_render
        and not signing_admin
        and not desktop_authenticated
    ):
        raw = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not raw and path == "/take":
            raw = request.query_params.get("access_key", "")
        identity = await asyncio.to_thread(control.authenticate, raw) if raw else None
        if identity is None:
            return error_response(
                request,
                code="unauthorized",
                message="A valid project API key is required.",
                status_code=401,
                retryable=False,
            )
        uses_render_capacity = (
            path == "/take"
            or path == "/compat/urlbox/v1/render/sync"
            or (path == "/v1/render" and request.method == "POST")
        )
        allowed, reason = await control.acquire(
            identity, concurrency=uses_render_capacity
        )
        if not allowed:
            return error_response(
                request,
                code=str(reason),
                message="The project request limit was reached.",
                status_code=429,
                retryable=True,
                headers={"Retry-After": "1"},
            )
        request.state.project_id = str(identity["project_id"])
        request.state.key_id = str(identity["key_id"])
        request.state.scopes = list(identity["scopes"])
        required_scopes = {"render"}
        if path.startswith("/v1/jobs") or path.endswith("/async"):
            required_scopes = {"jobs"}
        elif path.startswith("/v1/schedules"):
            required_scopes = {"schedules"}
        elif path.startswith("/v1/profiles"):
            required_scopes = {"profiles"}
        elif path.startswith("/v1/baselines") or path == "/v1/diff":
            required_scopes = {"baselines"}
        elif path == "/v1/certification/public-key":
            required_scopes = {"render", "jobs"}
        if required_scopes.isdisjoint(request.state.scopes):
            if uses_render_capacity:
                await control.release(request.state.project_id)
            return error_response(
                request,
                code="insufficient_scope",
                message=(
                    "The API key requires one of these scopes: "
                    + ", ".join(sorted(required_scopes))
                    + "."
                ),
                status_code=403,
                retryable=False,
            )
        if uses_render_capacity:
            acquired_project = request.state.project_id
    if (
        DESKTOP_TOKEN
        and request.method != "OPTIONS"
        and path not in {"/health", "/ready"}
        and not signed_render
        and not signing_admin
        and not desktop_authenticated
        and not request.state.is_admin
        and request.state.project_id is None
    ):
        if acquired_project is not None:
            await control.release(acquired_project)
        return error_response(
            request,
            code="unauthorized",
            message="A valid desktop or project credential is required.",
            status_code=401,
            retryable=False,
        )
    if (
        PROCESS_ROLE == "worker"
        and protected_api
        and not path.startswith("/v1/admin/")
    ):
        if acquired_project is not None:
            await control.release(acquired_project)
        return error_response(
            request,
            code="worker_only",
            message="This process accepts background work only.",
            status_code=503,
            retryable=False,
        )
    try:
        response = await call_next(request)
    finally:
        if acquired_project is not None:
            await control.release(acquired_project)
    return response


@app.middleware("http")
async def record_http_metrics(request: Request, call_next):
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        matched_route = getattr(request, "scope", {}).get("route")
        metric_route = getattr(matched_route, "path", None) or "unmatched"
        METRICS.inc(
            "http_requests_total",
            method=request.method,
            route=metric_route,
            status=status,
        )
        METRICS.inc(
            "http_request_duration_seconds_sum", time.perf_counter() - started
        )


install_render_error_layer(app)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ready": True}


@app.get("/ready")
async def ready() -> JSONResponse:
    browser = getattr(app.state, "browser", None)
    is_ready = browser is not None and browser.is_connected()
    return JSONResponse({
        "ready": is_ready,
        "role": PROCESS_ROLE,
        "async_jobs": getattr(app.state, "async_jobs", None) is not None,
    }, status_code=200 if is_ready else 503)


@app.get("/metrics", response_class=Response)
async def metrics() -> Response:
    METRICS.gauge("capture_capacity", MAX_CONCURRENT_CAPTURES)
    METRICS.gauge("worker_count", ASYNC_JOB_SETTINGS.worker_count if ASYNC_JOB_SETTINGS else 0)
    return Response(METRICS.prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/v1/certification/public-key")
async def certified_capture_public_key() -> dict[str, str]:
    secret = os.getenv("VIPERCAPTURE_CERTIFICATION_SECRET", "")
    if len(secret.encode()) < 32:
        raise RenderError("certification_disabled", "Certified captures are disabled.", 503, False)
    return {"algorithm": "Ed25519", "public_key": certification_public_key(secret)}


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    requests_per_minute: int = Field(default=60, ge=1, le=100_000)
    concurrency: int = Field(default=2, ge=1, le=1_000)


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="default", min_length=1, max_length=128)
    scopes: list[Literal["render", "jobs", "schedules", "profiles", "baselines"]] = Field(
        default_factory=lambda: ["render", "jobs", "schedules", "profiles", "baselines"],
        min_length=1,
    )


class BrowserCookie(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    value: str
    domain: str
    path: str
    expires: float
    http_only: bool = Field(alias="httpOnly")
    secure: bool
    same_site: Literal["Strict", "Lax", "None"] = Field(alias="sameSite")


class BrowserLocalStorageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    value: str


class BrowserOriginState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    origin: str = Field(min_length=1)
    local_storage: list[BrowserLocalStorageEntry] = Field(
        default_factory=list, alias="localStorage"
    )

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("origin has an invalid port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("origin must be an HTTP(S) origin without a path")
        return f"{parsed.scheme}://{parsed.netloc}"


class BrowserStorageState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cookies: list[BrowserCookie] = Field(default_factory=list)
    origins: list[BrowserOriginState] = Field(default_factory=list)


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_state: BrowserStorageState
    ttl_seconds: int | None = Field(default=None, ge=60, le=31_536_000)


def _admin(request: Request) -> ControlPlane:
    control = getattr(request.app.state, "control", None)
    if control is None:
        raise RenderError("control_plane_disabled", "Set VIPERCAPTURE_ADMIN_TOKEN to enable the control plane.", 503, False)
    if not request.state.is_admin:
        raise RenderError("admin_unauthorized", "A valid administrator token is required.", 401, False)
    return control


@app.post("/v1/admin/projects", status_code=201)
async def create_project(payload: ProjectCreate, request: Request) -> JSONResponse:
    control = _admin(request)
    project = await asyncio.to_thread(control.create_project, payload.name, payload.requests_per_minute, payload.concurrency)
    await asyncio.to_thread(control.audit, str(project["id"]), "admin", "project.created", str(project["id"]))
    return JSONResponse(project, status_code=201)


@app.get("/v1/admin/projects")
async def list_projects(request: Request) -> list[dict[str, object]]:
    return await asyncio.to_thread(_admin(request).list_projects)


@app.post("/v1/admin/projects/{project_id}/keys", status_code=201)
async def create_api_key(project_id: str, payload: ApiKeyCreate, request: Request) -> JSONResponse:
    control = _admin(request)
    try:
        document = await asyncio.to_thread(control.create_key, project_id, payload.name, payload.scopes)
    except KeyError as exc:
        raise RenderError("project_not_found", "The project was not found.", 404, False) from exc
    await asyncio.to_thread(control.audit, project_id, "admin", "api_key.created", document["id"])
    return JSONResponse(document, status_code=201, headers={"Cache-Control": "private, no-store"})


@app.delete("/v1/admin/keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: str, request: Request) -> Response:
    control = _admin(request)
    if not await asyncio.to_thread(control.revoke_key, key_id):
        raise RenderError("api_key_not_found", "The API key was not found.", 404, False)
    await asyncio.to_thread(control.audit, None, "admin", "api_key.revoked", key_id)
    return Response(status_code=204)


@app.get("/v1/admin/audit")
async def audit_events(request: Request, limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> list[dict[str, object]]:
    return await asyncio.to_thread(_admin(request).audits, limit)


@app.get("/v1/admin/status")
async def operator_status(request: Request) -> dict[str, object]:
    _admin(request)
    service = getattr(app.state, "async_jobs", None)
    return {
        "role": PROCESS_ROLE,
        "browser_connected": app.state.browser.is_connected(),
        "browsers": {
            engine.value: browser.is_connected()
            for engine, browser in getattr(app.state, "browsers", {}).items()
        },
        "async_jobs": service is not None,
        "worker_count": ASYNC_JOB_SETTINGS.worker_count if ASYNC_JOB_SETTINGS else 0,
        "control_plane": CONTROL_ENABLED,
    }


@app.post("/v1/profiles", status_code=201)
async def create_profile(payload: ProfileCreate, request: Request) -> JSONResponse:
    control = getattr(request.app.state, "control", None)
    if control is None or (request.state.project_id is None and not request.state.is_admin):
        raise RenderError("profiles_disabled", "Project authentication is required for profiles.", 503, False)
    project_id = request.state.project_id
    if project_id is None:
        raise RenderError("project_required", "Use a project API key to create a profile.", 422, False)
    profile_id = secrets.token_urlsafe(24).replace("-", "_")
    try:
        await _settled_thread(
            control.put_profile,
            project_id,
            profile_id,
            payload.storage_state.model_dump(mode="json", by_alias=True),
            payload.ttl_seconds,
        )
    except ProfileQuotaError as exc:
        raise RenderError(
            "profile_quota_exceeded",
            "The project profile storage quota was reached.",
            413,
            False,
        ) from exc
    except asyncio.CancelledError:
        await _settled_thread(control.delete_profile, project_id, profile_id)
        raise
    await asyncio.to_thread(control.audit, project_id, request.state.key_id, "profile.created", profile_id)
    return JSONResponse({"id": profile_id, "expires_in": payload.ttl_seconds}, status_code=201, headers={"Cache-Control": "private, no-store"})


@app.delete("/v1/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str, request: Request) -> Response:
    control = getattr(request.app.state, "control", None)
    project_id = request.state.project_id
    if control is None or project_id is None or not await asyncio.to_thread(control.is_owner, "profile", profile_id, project_id):
        raise RenderError("profile_not_found", "The browser profile was not found.", 404, False)
    await asyncio.to_thread(control.delete_profile, project_id, profile_id)
    await asyncio.to_thread(control.audit, project_id, request.state.key_id, "profile.deleted", profile_id)
    return Response(status_code=204)


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

    async def load_profile(profile_id: str):
        control = getattr(app.state, "control", None)
        if control is None:
            return None
        return await asyncio.to_thread(control.get_profile_any, profile_id)

    async def save_profile(profile_id: str, state: dict[str, object]):
        control = getattr(app.state, "control", None)
        try:
            saved = control is not None and await asyncio.to_thread(
                control.put_profile_any, profile_id, state
            )
        except ProfileQuotaError as exc:
            raise RenderError(
                "profile_quota_exceeded",
                "The project profile storage quota was reached.",
                413,
                False,
            ) from exc
        if not saved:
            raise RenderError("profile_not_found", "The browser profile was not found.", 404, False)

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
        profile_loader=load_profile,
        profile_saver=save_profile,
    )


def _record_render_metrics(
    payload: RenderRequest,
    *,
    cache_hit: bool,
    render_ms: int,
    queue_ms: int,
) -> None:
    METRICS.inc(
        "renders_total",
        output=payload.recorded_output_type,
        cache=(
            "hit" if cache_hit else "miss" if payload.cache else "disabled"
        ),
    )
    METRICS.inc(
        "render_seconds_sum",
        render_ms / 1000,
        output=payload.recorded_output_type,
    )
    METRICS.inc("queue_seconds_sum", queue_ms / 1000)


async def _render_async_image(payload: RenderRequest) -> RenderedArtifact:
    started = time.perf_counter()
    job = current_job()
    durable_queue_ms = (
        max(
            0,
            round((job.started_at - job.created_at).total_seconds() * 1000),
        )
        if job is not None and job.started_at is not None
        else None
    )
    project_id = (
        _internal_project_id(job.request_id)
        if CONTROL_ENABLED and job is not None
        else None
    )
    control = getattr(app.state, "control", None)
    project_acquired = False
    if project_id is not None and control is not None:
        project_acquired = await control.acquire_worker(project_id)
        if not project_acquired:
            raise JobDeferred()
    cache = getattr(app.state, "render_cache", None)
    try:
        if payload.cache and cache is not None:
            cached = await cache.get(payload, project_id)
            if cached is not None:
                _record_render_metrics(
                    payload,
                    cache_hit=True,
                    render_ms=0,
                    queue_ms=durable_queue_ms or 0,
                )
                return RenderedArtifact(
                    body=cached.body,
                    media_type=cached.media_type,
                    filename=cached.filename,
                    render_ms=round((time.perf_counter() - started) * 1000),
                )
        queue_started = time.perf_counter()
        await app.state.capture_slots.acquire()
        slot_queue_ms = round((time.perf_counter() - queue_started) * 1000)
        queue_ms = (
            durable_queue_ms
            if durable_queue_ms is not None
            else slot_queue_ms
        )
        try:
            browser = await _browser_for(app, payload.engine)
            engine = _render_engine()
            render_started = time.perf_counter()
            try:
                artifact, cache_hit = await _render_with_cache(
                    engine, browser, payload, namespace=project_id
                )
            except RenderError:
                if not browser.is_connected():
                    with suppress(Exception):
                        await _replace_browser(app, browser)
                raise
        finally:
            app.state.capture_slots.release()
        render_ms = (
            0
            if cache_hit
            else round((time.perf_counter() - render_started) * 1000)
        )
        _record_render_metrics(
            payload,
            cache_hit=cache_hit,
            render_ms=render_ms,
            queue_ms=queue_ms,
        )
        return RenderedArtifact(
            body=artifact.body,
            media_type=artifact.media_type,
            filename=artifact.filename,
            render_ms=round((time.perf_counter() - started) * 1000),
        )
    finally:
        if project_acquired:
            await control.release(project_id)


async def _render_with_cache(
    engine: RenderEngine,
    browser: Browser,
    payload: RenderRequest,
    *,
    namespace: str | None = None,
) -> tuple[RenderArtifact, bool]:
    cache = getattr(app.state, "render_cache", None)
    if payload.cache and cache is not None:
        cached = await cache.get(payload, namespace)
        if cached is not None:
            return cached, True
    artifact = await engine.render_image(
        browser,
        payload,
        RenderLimits(
            max_width=MAX_VIEWPORT_WIDTH,
            max_height=MAX_VIEWPORT_HEIGHT,
            max_pixels=MAX_SCREENSHOT_PIXELS,
            max_full_page_height=MAX_FULL_PAGE_HEIGHT,
            output_bytes=MAX_OUTPUT_BYTES,
        ),
    )
    if payload.cache and cache is not None:
        await cache.put(payload, artifact, namespace)
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
    await _validate_profile_access(payload, request)
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
    cache_namespace = getattr(getattr(request, "state", None), "project_id", None)
    if payload.cache and cache is not None:
        artifact = await _await_while_connected(
            request, cache.get(payload, cache_namespace)
        )
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
        try:
            browser = await _browser_for(app, payload.engine)
            engine = _render_engine()
            try:
                if payload.cache and cache is not None:
                    artifact = await _await_while_connected(
                        request, cache.get(payload, cache_namespace)
                    )
                    cache_hit = artifact is not None
                if artifact is None:
                    render_started = time.perf_counter()
                    artifact = await _await_while_connected(
                        request,
                        engine.render_image(
                            browser,
                            payload,
                            RenderLimits(
                                max_width=MAX_VIEWPORT_WIDTH,
                                max_height=MAX_VIEWPORT_HEIGHT,
                                max_pixels=MAX_SCREENSHOT_PIXELS,
                                max_full_page_height=MAX_FULL_PAGE_HEIGHT,
                                output_bytes=MAX_OUTPUT_BYTES,
                            ),
                        ),
                    )
                    render_ms = round(
                        (time.perf_counter() - render_started) * 1000
                    )
                    if payload.cache and cache is not None:
                        await cache.put(payload, artifact, cache_namespace)
            except RenderError:
                if not browser.is_connected():
                    with suppress(Exception):
                        await _replace_browser(app, browser)
                raise
        finally:
            app.state.capture_slots.release()
    metadata = artifact.metadata or {}
    _record_render_metrics(
        payload,
        cache_hit=cache_hit,
        render_ms=render_ms,
        queue_ms=queue_ms,
    )
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


@app.get("/take", response_class=Response)
async def screenshotone_compatible(request: Request) -> Response:
    try:
        payload = screenshotone_request(dict(request.query_params))
    except (TypeError, ValueError) as exc:
        raise RenderError("compatibility_options_invalid", str(exc), 422, False) from exc
    return await _render_response(payload, request)


@app.post("/compat/urlbox/v1/render/sync", response_class=Response)
async def urlbox_compatible_sync(document: dict[str, object], request: Request) -> Response:
    try:
        payload = urlbox_request(document)
    except (TypeError, ValueError) as exc:
        raise RenderError("compatibility_options_invalid", str(exc), 422, False) from exc
    return await _render_response(payload, request)


@app.post("/compat/urlbox/v1/render/async", status_code=202)
async def urlbox_compatible_async(document: dict[str, object], request: Request) -> JSONResponse:
    try:
        payload = urlbox_request(document)
    except (TypeError, ValueError) as exc:
        raise RenderError("compatibility_options_invalid", str(exc), 422, False) from exc
    await _validate_profile_access(payload, request)
    job = await _submit_job(
        _async_job_service(),
        payload,
        request_id=_project_request_id(request, request.state.request_id),
    )
    await _own_resource(request, "job", job.id)
    result = public_job_document(job)
    return JSONResponse(result, status_code=202, headers={"Location": str(result["status_url"]), "Retry-After": "1"})


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
        result = await _settled_thread(
            compare_images,
            baseline_body,
            current_body,
            pixel_threshold=pixel_threshold,
            max_difference_ratio=max_difference_ratio,
        )
        bundle = await _settled_thread(create_diff_bundle, result)
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


def _baseline_context(request: Request, name: str) -> tuple[ControlPlane, str]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
        raise RenderError("baseline_name_invalid", "Baseline names use letters, numbers, dot, dash, and underscore.", 422, False)
    control = getattr(request.app.state, "control", None)
    if control is None or request.state.project_id is None:
        raise RenderError("baselines_disabled", "Project authentication is required for baselines.", 503, False)
    return control, request.state.project_id


@app.put("/v1/baselines/{name}")
async def put_baseline(name: str, image: UploadFile, request: Request) -> JSONResponse:
    control, project_id = _baseline_context(request, name)
    body = await image.read(MAX_DIFF_INPUT_BYTES + 1)
    if not body or len(body) > MAX_DIFF_INPUT_BYTES:
        raise RenderError("baseline_too_large", "The baseline image exceeds the input limit.", 413, False)
    await _settled_thread(validate_image, body, "baseline")
    try:
        document = await asyncio.to_thread(
            control.put_baseline, project_id, name, body
        )
    except BaselineQuotaError as exc:
        raise RenderError(
            "baseline_quota_exceeded",
            "The project baseline storage quota was reached.",
            413,
            False,
        ) from exc
    await asyncio.to_thread(control.audit, project_id, request.state.key_id, "baseline.updated", name)
    return JSONResponse(document)


@app.get("/v1/baselines")
async def list_baselines(request: Request) -> list[dict[str, object]]:
    control, project_id = _baseline_context(request, "all")
    return await asyncio.to_thread(control.list_baselines, project_id)


@app.delete("/v1/baselines/{name}", status_code=204)
async def delete_baseline(name: str, request: Request) -> Response:
    control, project_id = _baseline_context(request, name)
    if not await asyncio.to_thread(control.delete_baseline, project_id, name):
        raise RenderError("baseline_not_found", "The baseline was not found.", 404, False)
    await asyncio.to_thread(
        control.audit,
        project_id,
        request.state.key_id,
        "baseline.deleted",
        name,
    )
    return Response(status_code=204)


@app.post("/v1/baselines/{name}/compare", response_class=Response)
async def compare_baseline(
    name: str,
    current: UploadFile,
    request: Request,
    pixel_threshold: int = Form(0),
    max_difference_ratio: float = Form(0),
) -> Response:
    control, project_id = _baseline_context(request, name)
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
        baseline = await asyncio.to_thread(control.get_baseline, project_id, name)
        if baseline is None:
            raise RenderError("baseline_not_found", "The baseline was not found.", 404, False)
        current_body = await current.read(MAX_DIFF_INPUT_BYTES + 1)
        try:
            result = await _settled_thread(compare_images, baseline, current_body, pixel_threshold=pixel_threshold, max_difference_ratio=max_difference_ratio)
            bundle = await _settled_thread(create_diff_bundle, result)
        except ValueError as exc:
            raise RenderError("diff_options_invalid", str(exc), 422, False) from exc
    finally:
        app.state.diff_slots.release()
    return Response(bundle, media_type="application/zip", headers={"X-ViperCapture-Diff-Passed": str(result.passed).lower(), "X-ViperCapture-Difference-Ratio": f"{result.ratio:.8f}"})


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
    if render_request.profile_id is not None:
        raise RenderError(
            "signed_profile_unsupported",
            "Signed render URLs cannot use persistent browser profiles.",
            422,
            False,
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
    if payload.profile_id is not None:
        raise RenderError(
            "signed_profile_unsupported",
            "Signed render URLs cannot use persistent browser profiles.",
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


def _project_request_id(request: Request, request_id: str) -> str:
    project_id = getattr(request.state, "project_id", None)
    return f"_project-{project_id}:{request_id}" if project_id else request_id


async def _validate_profile_access(payload: RenderRequest, request: Request) -> None:
    if (
        payload.profile_id is None
        or not CONTROL_ENABLED
        or request.state.is_admin
        or request.state.trusted_local
    ):
        return
    project_id = getattr(request.state, "project_id", None)
    if project_id is None or not await asyncio.to_thread(
        app.state.control.is_owner,
        "profile",
        payload.profile_id,
        project_id,
    ):
        raise RenderError("profile_not_found", "The browser profile was not found.", 404, False)


async def _own_resource(request: Request, kind: str, resource_id: str) -> None:
    if CONTROL_ENABLED and request.state.project_id is not None:
        ttl = _job_ownership_ttl() if kind == "job" else None
        await asyncio.to_thread(
            app.state.control.own,
            kind,
            resource_id,
            request.state.project_id,
            ttl,
        )
        if not await asyncio.to_thread(
            app.state.control.is_owner, kind, resource_id, request.state.project_id
        ):
            raise RenderError("resource_conflict", "The resource belongs to another project.", 409, False)
        await asyncio.to_thread(app.state.control.audit, request.state.project_id, request.state.key_id, f"{kind}.created", resource_id)


async def _require_resource(request: Request | None, kind: str, resource_id: str) -> None:
    if (
        request is None
        or not CONTROL_ENABLED
        or request.state.is_admin
        or request.state.trusted_local
    ):
        return
    if request.state.project_id is None or not await asyncio.to_thread(
        app.state.control.is_owner, kind, resource_id, request.state.project_id
    ):
        raise RenderError(f"{kind}_not_found", f"The {kind} was not found.", 404, False)


@app.post("/v1/jobs", status_code=202)
async def create_render_job(
    payload: RenderRequest,
    request: Request,
) -> JSONResponse:
    await _validate_profile_access(payload, request)
    job = await _submit_job(
        _async_job_service(),
        payload,
        request_id=_project_request_id(request, request.state.request_id),
    )
    await _own_resource(request, "job", job.id)
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
    existing_jobs = [None] * len(payload.items)
    preflight_errors: list[RenderError | None] = [None] * len(payload.items)
    request_ids = [
        _project_request_id(
            request,
            item.request_id or f"{request.state.request_id}-{index + 1}",
        )
        for index, item in enumerate(payload.items)
    ]
    for index, item in enumerate(payload.items):
        try:
            await _validate_profile_access(item.render, request)
            existing_jobs[index] = await service.existing(
                item.render,
                request_id=request_ids[index],
            )
        except RenderError as exc:
            preflight_errors[index] = exc
    validation_tasks = {
        index: asyncio.create_task(_validate_webhook(item.render))
        for index, item in enumerate(payload.items)
        if existing_jobs[index] is None and preflight_errors[index] is None
    }
    if validation_tasks:
        _done, pending = await asyncio.wait(
            set(validation_tasks.values()),
            timeout=BULK_WEBHOOK_VALIDATION_TIMEOUT_SECONDS,
        )
    else:
        pending = set()
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    validation_errors: list[RenderError | None] = list(preflight_errors)
    for index, task in validation_tasks.items():
        if task in pending:
            validation_errors[index] = (
                RenderError(
                    "bulk_webhook_validation_timeout",
                    "Bulk webhook validation exceeded its aggregate deadline.",
                    504,
                    True,
                )
            )
            continue
        try:
            task.result()
        except RenderError as exc:
            validation_errors[index] = exc
    for index, item in enumerate(payload.items):
        try:
            job = existing_jobs[index]
            if job is None:
                error = validation_errors[index]
                if error is not None:
                    raise error
                job = await service.submit(
                    item.render,
                    request_id=request_ids[index],
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
            await _own_resource(request, "job", job.id)
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
async def create_schedule(payload: ScheduleCreate, request: Request) -> JSONResponse:
    await _validate_profile_access(payload.render, request)
    await _validate_webhook(payload.render)
    service = _schedule_service()
    schedule_id = str(uuid4())
    control = getattr(request.app.state, "control", None)
    project_id = request.state.project_id
    reserved = CONTROL_ENABLED and control is not None and project_id is not None
    if reserved:
        try:
            size_bytes = await _settled_thread(
                service.payload_size, schedule_id, payload.render
            )
            await _settled_thread(
                control.reserve_schedule,
                schedule_id,
                project_id,
                size_bytes,
            )
        except asyncio.CancelledError:
            await _settled_thread(
                control.disown, "schedule", schedule_id, project_id
            )
            raise
        except ScheduleQuotaError as exc:
            raise RenderError(
                "schedule_quota_exceeded",
                "The project schedule storage quota was reached.",
                413,
                False,
            ) from exc
    try:
        record = await service.create(
            payload, schedule_id=schedule_id, project_id=project_id
        )
    except BaseException:
        if reserved and await service.store.get(schedule_id) is None:
            await _settled_thread(
                control.disown, "schedule", schedule_id, project_id
            )
        raise
    await _own_resource(request, "schedule", record.id)
    return JSONResponse(
        public_schedule_document(record),
        status_code=201,
        headers={"Location": f"/v1/schedules/{record.id}", "Cache-Control": "private, no-store"},
    )


@app.get("/v1/schedules")
async def list_schedules(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after: Annotated[str | None, Query(max_length=256)] = None,
) -> JSONResponse:
    service = _schedule_service()
    if CONTROL_ENABLED and not request.state.is_admin:
        records = await service.store.list(
            limit=limit + 1,
            after=after,
            project_id=request.state.project_id,
        )
    else:
        records = await service.store.list(limit=limit + 1, after=after)
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
async def read_schedule(schedule_id: UUID, request: Request) -> JSONResponse:
    await _require_resource(request, "schedule", str(schedule_id))
    record = await _schedule_service().store.get(str(schedule_id))
    if record is None:
        raise RenderError("schedule_not_found", "The schedule was not found.", 404, False)
    return JSONResponse(public_schedule_document(record), headers={"Cache-Control": "private, no-store"})


@app.patch("/v1/schedules/{schedule_id}")
async def update_schedule(schedule_id: UUID, payload: ScheduleUpdate, request: Request) -> JSONResponse:
    await _require_resource(request, "schedule", str(schedule_id))
    service = _schedule_service()
    record = await service.store.get(str(schedule_id))
    if record is None:
        raise RenderError("schedule_not_found", "The schedule was not found.", 404, False)
    if payload.render is not None:
        await _validate_profile_access(payload.render, request)
        await _validate_webhook(payload.render)
    try:
        updated = await service.update(record, payload)
    except ScheduleQuotaError as exc:
        raise RenderError(
            "schedule_quota_exceeded",
            "The project schedule storage quota was reached.",
            413,
            False,
        ) from exc
    return JSONResponse(public_schedule_document(updated), headers={"Cache-Control": "private, no-store"})


@app.delete("/v1/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: UUID, request: Request) -> Response:
    await _require_resource(request, "schedule", str(schedule_id))
    owner = None
    if CONTROL_ENABLED:
        owner = await asyncio.to_thread(
            app.state.control.owner, "schedule", str(schedule_id)
        )
    service = _schedule_service()
    try:
        deleted = await service.delete(str(schedule_id))
    except asyncio.CancelledError:
        current = await service.store.get(str(schedule_id))
        if CONTROL_ENABLED and owner is not None and current is None:
            await _settled_thread(
                app.state.control.disown,
                "schedule",
                str(schedule_id),
                owner,
            )
        raise
    if not deleted:
        raise RenderError("schedule_not_found", "The schedule was not found.", 404, False)
    if CONTROL_ENABLED and owner is not None:
        await _settled_thread(
            app.state.control.disown,
            "schedule",
            str(schedule_id),
            owner,
        )
    return Response(status_code=204)


@app.get("/v1/jobs/{job_id}")
async def read_render_job(job_id: UUID, request: Request = None) -> JSONResponse:
    await _require_resource(request, "job", str(job_id))
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
async def cancel_render_job(job_id: UUID, request: Request) -> JSONResponse:
    await _require_resource(request, "job", str(job_id))
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
    await _require_resource(request, "job", str(job_id))
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
    output_formats = [output.value for output in OutputFormat]
    if not await _settled_thread(ffmpeg_has_encoder, "libx264"):
        output_formats.remove(OutputFormat.MP4.value)
    return {
        "server_saves": not HOSTED,
        "control_plane": CONTROL_ENABLED,
        "max_screenshot_pixels": MAX_SCREENSHOT_PIXELS,
        "max_viewport_width": MAX_VIEWPORT_WIDTH,
        "max_viewport_height": MAX_VIEWPORT_HEIGHT,
        "max_full_page_height": MAX_FULL_PAGE_HEIGHT,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "browser_engines": [engine.value for engine in BrowserEngine],
        "output_formats": output_formats,
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
                app.state.browsers[BrowserEngine.CHROMIUM] = replacement
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
        "vipercapture.main:app",
        host=os.getenv("VIPERCAPTURE_HOST", "127.0.0.1"),
        port=int(os.getenv("VIPERCAPTURE_PORT", "8000")),
        reload=False,
    )
