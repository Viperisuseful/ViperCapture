import asyncio
from base64 import b64decode
from contextlib import asynccontextmanager, suppress
from datetime import datetime
import ipaddress
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Browser, Playwright, TimeoutError as PlaywrightTimeoutError, async_playwright


if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


BASE_DIR = Path(__file__).resolve().parent
CAPTURES_DIR = BASE_DIR / "captures"


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

HOSTED = os.getenv("SHOT_HOSTED") == "1"
ENABLE_GPU = os.getenv("SHOT_ENABLE_GPU") == "1"
MAX_CONCURRENT_CAPTURES = max(1, int(os.getenv("SHOT_MAX_CONCURRENCY", "1")))
MAX_SCREENSHOT_PIXELS = max(1, int(os.getenv("SHOT_MAX_PIXELS", "50000000")))
CAPTURE_QUEUE_TIMEOUT_SECONDS = 30
CAPTURE_TIMEOUT_SECONDS = 75
LAZY_SCROLL_MAX_STEPS = 40
LAZY_SCROLL_DELAY_SECONDS = 0.2

if not HOSTED:
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)


async def _launch_browser(playwright: Playwright) -> Browser:
    return await playwright.chromium.launch(
        headless=True,
        args=["--enable-gpu"] if ENABLE_GPU else [],
    )


async def _replace_browser(app: FastAPI, failed_browser: Browser) -> None:
    async with app.state.browser_restart_lock:
        if app.state.browser is not failed_browser:
            return
        with suppress(Exception):
            await asyncio.wait_for(failed_browser.close(), timeout=5)
        app.state.browser = await asyncio.wait_for(
            _launch_browser(app.state.playwright),
            timeout=15,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    playwright: Playwright = await async_playwright().start()
    browser = await _launch_browser(playwright)
    app.state.playwright = playwright
    app.state.browser = browser
    app.state.capture_slots = asyncio.Semaphore(MAX_CONCURRENT_CAPTURES)
    app.state.browser_restart_lock = asyncio.Lock()
    try:
        yield
    finally:
        await app.state.browser.close()
        await playwright.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/SCPLogo.png", include_in_schema=False)
async def logo() -> FileResponse:
    return FileResponse(
        BASE_DIR / "SCPLogo.png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _validate_url(target: str) -> str:
    try:
        parsed = urlparse(target)
        if (
            parsed.scheme in {"http", "https"} and parsed.hostname
        ) or (
            not HOSTED and parsed.scheme == "file"
        ):
            return target
    except ValueError:
        pass
    raise HTTPException(status_code=400, detail="Invalid or unsupported URL")


async def _is_public_url(target: str, cache: dict[str, bool]) -> bool:
    """Reject private/link-local destinations when running as a hosted service."""
    try:
        parsed = urlparse(target)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    if hostname in cache:
        return cache[hostname]

    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        allowed = bool(addresses) and all(
            ipaddress.ip_address(address[4][0].split("%", 1)[0]).is_global
            for address in addresses
        )
    except (OSError, ValueError):
        allowed = False

    cache[hostname] = allowed
    return allowed


def _ensure_pixel_budget(width: float, height: float, scale: float) -> None:
    output_pixels = math.ceil(width * scale) * math.ceil(height * scale)
    if output_pixels > MAX_SCREENSHOT_PIXELS:
        raise HTTPException(
            status_code=413,
            detail=f"Screenshot exceeds the {MAX_SCREENSHOT_PIXELS:,}-pixel limit",
        )


async def _load_lazy_content(page, viewport_height: int) -> None:
    """Briefly walk the page so viewport-triggered content has a chance to load."""
    scroll = "(y) => window.scrollTo({ top: y, left: 0, behavior: 'instant' })"
    document_height = """Math.max(
        document.documentElement.scrollHeight,
        document.documentElement.offsetHeight,
        document.body?.scrollHeight || 0,
        document.body?.offsetHeight || 0
    )"""
    step = max(1, viewport_height * 4 // 5)
    position = 0
    stable_bottom_checks = 0

    await page.evaluate(scroll, 0)
    try:
        for _ in range(LAZY_SCROLL_MAX_STEPS):
            height = math.ceil(await page.evaluate(document_height))
            bottom = max(0, height - viewport_height)
            if position >= bottom:
                stable_bottom_checks += 1
                if stable_bottom_checks >= 2:
                    break
            else:
                position = min(position + step, bottom)
                stable_bottom_checks = 0

            await page.evaluate(scroll, position)
            await asyncio.sleep(LAZY_SCROLL_DELAY_SECONDS)
    finally:
        with suppress(Exception):
            await page.evaluate(scroll, 0)
            await asyncio.sleep(LAZY_SCROLL_DELAY_SECONDS)


async def _check_captcha(page, proceed_on_captcha: bool) -> None:
    provider = await page.evaluate("""() => {
        const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" &&
                Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
        };
        const providers = {
            cloudflare: [
                ".cf-turnstile", "#challenge-stage", "#challenge-running",
                "#challenge-form", "iframe[src*='challenges.cloudflare.com']",
                "iframe[src*='/cdn-cgi/challenge-platform/']"
            ],
            recaptcha: [
                ".g-recaptcha", "iframe[src*='google.com/recaptcha']",
                "iframe[src*='recaptcha.net/recaptcha']"
            ],
            hcaptcha: [
                ".h-captcha", "iframe[src*='hcaptcha.com/captcha']"
            ]
        };
        for (const [name, selectors] of Object.entries(providers)) {
            if (selectors.some((selector) =>
                [...document.querySelectorAll(selector)].some(visible))) return name;
        }
        return null;
    }""")
    if provider and not proceed_on_captcha:
        raise HTTPException(
            status_code=409,
            detail={"code": "captcha_detected", "provider": provider},
        )


def _safe_filename(filename: str) -> str:
    name = filename.strip() or "screenshot.png"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    if not name.lower().endswith(".png"):
        name = f"{name}.png"
    return name


def _unique_capture_path(filename: str) -> Path:
    safe_name = _safe_filename(filename)
    target = CAPTURES_DIR / safe_name
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return CAPTURES_DIR / f"{stem}_{timestamp}{suffix}"


@app.get("/screenshot", response_class=Response)
async def capture_screenshot(
    url: str = Query(..., description="Page URL to capture"),
    width: int = Query(1920, ge=1, le=7680, description="Logical viewport width (pre-scale)"),
    height: int = Query(1080, ge=1, le=4320, description="Logical viewport height (pre-scale)"),
    device_scale_factor: float = Query(2.0, ge=1.0, le=4.0, description="Device scale factor to reach target pixel density"),
    wait: int = Query(1, ge=0, le=15, description="Extra wait time in seconds after page load"),
    dark_mode: bool = Query(False, description="Enable dark color scheme"),
    auth_username: str | None = Query(None, description="Optional HTTP basic auth username (local mode only)"),
    auth_password: str | None = Query(None, description="Optional HTTP basic auth password (local mode only)"),
    proceed_on_captcha: bool = Query(False, description="Capture even when a CAPTCHA is visible"),
):
    target_url = _validate_url(url)
    _ensure_pixel_budget(width, height, device_scale_factor)
    try:
        await asyncio.wait_for(
            app.state.capture_slots.acquire(),
            timeout=CAPTURE_QUEUE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Screenshot queue is busy") from exc

    browser: Browser = app.state.browser
    context = None
    blocked_urls: list[str] = []
    restart_browser = False
    try:
        async with asyncio.timeout(CAPTURE_TIMEOUT_SECONDS):
            validated_hosts: dict[str, bool] = {}
            if HOSTED and not await _is_public_url(target_url, validated_hosts):
                raise HTTPException(status_code=400, detail="Private or non-public URLs are blocked")
            if HOSTED and (auth_username or auth_password):
                raise HTTPException(status_code=400, detail="Target credentials are disabled in hosted mode")

            credentials = None
            if auth_username or auth_password:
                credentials = {
                    "username": auth_username or "",
                    "password": auth_password or "",
                }

            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=device_scale_factor,
                color_scheme="dark" if dark_mode else "light",
                http_credentials=credentials,
                service_workers="block" if HOSTED else "allow",
            )

            if HOSTED:
                async def guard_request(route):
                    request_url = route.request.url
                    try:
                        scheme = urlparse(request_url).scheme
                    except ValueError:
                        scheme = ""

                    if scheme in {"data", "blob", "about"}:
                        await route.continue_()
                        return
                    if scheme in {"http", "https"} and await _is_public_url(request_url, validated_hosts):
                        await route.continue_()
                        return

                    blocked_urls.append(request_url)
                    await route.abort("blockedbyclient")

                async def block_web_socket(web_socket):
                    blocked_urls.append(web_socket.url)
                    await web_socket.close(code=1008, reason="Blocked in hosted mode")

                await context.route("**/*", guard_request)
                await context.route_web_socket("**/*", block_web_socket)

            page = await context.new_page()

            async def close_popup(popup):
                with suppress(Exception):
                    await popup.close()

            page.on("popup", lambda popup: asyncio.create_task(close_popup(popup)))
            await page.goto(target_url, wait_until="load", timeout=45_000)
            if wait:
                await asyncio.sleep(wait)
            await _check_captcha(page, proceed_on_captcha)
            await _load_lazy_content(page, height)
            await _check_captcha(page, proceed_on_captcha)

            cdp = await context.new_cdp_session(page)
            metrics = await cdp.send("Page.getLayoutMetrics")
            content_size = metrics.get("cssContentSize") or metrics["contentSize"]
            clip_width = max(float(content_size["width"]), width)
            clip_height = max(float(content_size["height"]), height)
            _ensure_pixel_budget(clip_width, clip_height, device_scale_factor)
            screenshot = await cdp.send(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "captureBeyondViewport": True,
                    "optimizeForSpeed": True,
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": clip_width,
                        "height": clip_height,
                        "scale": device_scale_factor,
                    },
                },
            )
            image = b64decode(screenshot["data"])
    except PlaywrightTimeoutError as exc:
        raise HTTPException(status_code=504, detail="Page load timed out") from exc
    except TimeoutError as exc:
        restart_browser = True
        raise HTTPException(status_code=504, detail="Screenshot capture timed out") from exc
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive fallback
        restart_browser = not browser.is_connected()
        if blocked_urls:
            raise HTTPException(status_code=400, detail="Page requested a private or non-public URL") from exc
        raise HTTPException(status_code=500, detail="Failed to capture screenshot") from exc
    finally:
        cleanup_failed = False
        if context is not None:
            try:
                await asyncio.wait_for(context.close(), timeout=5)
            except Exception:
                cleanup_failed = True
        if restart_browser or cleanup_failed:
            with suppress(Exception):
                await _replace_browser(app, browser)
        app.state.capture_slots.release()

    return Response(content=image, media_type="image/png")


@app.get("/app-config")
async def app_config():
    return {
        "server_saves": not HOSTED,
        "max_screenshot_pixels": MAX_SCREENSHOT_PIXELS,
    }


if not HOSTED:
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
            os.getenv("SHOT_DOWNLOADS_DIR", str(Path.home() / "Downloads"))
        ).expanduser()
        try:
            if sys.platform.startswith("win"):
                override = os.getenv("SHOT_DOWNLOADS_DIR")
                if override:
                    os.startfile(str(downloads))
                else:
                    subprocess.Popen(["explorer.exe", "shell:Downloads"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(downloads)])
            else:
                # ponytail: SHOT_DOWNLOADS_DIR covers custom browser locations.
                subprocess.Popen(["xdg-open", str(downloads)])
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to open Downloads folder") from exc

        return {"opened": True, "directory": str(downloads)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("SHOT_HOST", "127.0.0.1"),
        port=int(os.getenv("SHOT_PORT", "8000")),
        reload=False,
    )
