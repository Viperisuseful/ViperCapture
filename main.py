import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Browser, Playwright, async_playwright

from render_contract import RenderRequest
from render_engine import RenderEngine, RenderLimits
from render_errors import RenderError, install_render_error_layer


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
install_render_error_layer(app)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "templates" / "index.html")


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


@app.post("/v1/render", response_class=Response)
async def render_v1(payload: RenderRequest) -> Response:
    try:
        await asyncio.wait_for(
            app.state.capture_slots.acquire(), timeout=CAPTURE_QUEUE_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        raise RenderError("capture_queue_busy", "The render queue is busy.", 503, True) from exc
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
    return Response(
        artifact.body,
        media_type=artifact.media_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
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
