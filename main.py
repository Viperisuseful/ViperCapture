from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse

import asyncio
import os
import sys
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Browser, Playwright, TimeoutError as PlaywrightTimeoutError, async_playwright


if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


BASE_DIR = Path(__file__).resolve().parent
CAPTURES_DIR = BASE_DIR / "captures"
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    playwright: Playwright = await async_playwright().start()
    browser: Browser = await playwright.chromium.launch(headless=True)
    app.state.playwright = playwright
    app.state.browser = browser
    try:
        yield
    finally:
        await browser.close()
        await playwright.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "templates" / "index.html")


def _validate_url(target: str) -> str:
    parsed = urlparse(target)
    if (parsed.scheme in {"http", "https"} and parsed.netloc) or parsed.scheme == "file":
        return target
    raise HTTPException(status_code=400, detail="Invalid or unsupported URL")


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
    width: int = Query(1920, ge=1, description="Logical viewport width (pre-scale)"),
    height: int = Query(1080, ge=1, description="Logical viewport height (pre-scale)"),
    device_scale_factor: float = Query(2.0, ge=1.0, le=4.0, description="Device scale factor to reach target pixel density"),
    wait: int = Query(4, ge=0, description="Extra wait time in seconds after network idle"),
    dark_mode: bool = Query(False, description="Enable dark color scheme"),
    auth_username: str | None = Query(None, description="Optional HTTP basic auth username"),
    auth_password: str | None = Query(None, description="Optional HTTP basic auth password"),
):
    target_url = _validate_url(url)
    browser: Browser = app.state.browser
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
    )
    page = await context.new_page()
    try:
        await page.goto(target_url, wait_until="networkidle", timeout=60_000)
        await asyncio.sleep(wait)
        image = await page.screenshot(full_page=True, type="png")
    except PlaywrightTimeoutError as exc:
        raise HTTPException(status_code=504, detail="Page load timed out") from exc
    except Exception as exc:  # pragma: no cover - defensive fallback
        raise HTTPException(status_code=500, detail="Failed to capture screenshot") from exc
    finally:
        await context.close()

    return Response(content=image, media_type="image/png")


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


@app.post("/open-captures-folder")
async def open_captures_folder():
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(CAPTURES_DIR))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(CAPTURES_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(CAPTURES_DIR)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to open captures folder") from exc

    return {"opened": True, "directory": str(CAPTURES_DIR)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
