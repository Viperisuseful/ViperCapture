"""Isolated Playwright rendering for ViperCapture artifacts."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import tempfile
import zipfile
from base64 import b64decode
from contextlib import suppress
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from render_contract import (
    ActionType,
    DevicePreset,
    LazyLoadMode,
    OutputFormat,
    RenderRequest,
    Viewport,
)
from render_errors import RenderError

ALLOWED_INTERNAL_SCHEMES = {"about", "blob", "data"}
MEDIA_TYPES = {
    OutputFormat.PNG: "image/png",
    OutputFormat.JPEG: "image/jpeg",
    OutputFormat.WEBP: "image/webp",
}
EXTENSIONS = {
    OutputFormat.PNG: "png",
    OutputFormat.JPEG: "jpg",
    OutputFormat.WEBP: "webp",
}
DEVICE_DESCRIPTOR_NAMES = {
    DevicePreset.IPHONE_14: "iPhone 14",
    DevicePreset.PIXEL_7: "Pixel 7",
    DevicePreset.IPAD: "iPad (gen 7)",
}
DEVICE_PLATFORMS = {
    DevicePreset.IPHONE_14: "iPhone",
    DevicePreset.PIXEL_7: "Linux armv8l",
    DevicePreset.IPAD: "iPad",
}
MAX_METADATA_ITEMS = 100
MAX_METADATA_VALUE_CHARS = 2_048
DNS_RESOLUTION_TIMEOUT_SECONDS = 5
MAX_DNS_CONCURRENCY = 8
MAX_DNS_ORIGINS = 100
PUBLIC_DNS_SLOTS = asyncio.Semaphore(MAX_DNS_CONCURRENCY)
MAX_DIAGNOSTIC_EVENTS = 500


def _ffmpeg_executable() -> Path:
    installed = shutil.which("ffmpeg")
    if installed:
        return Path(installed)
    roots = []
    configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        roots.append(Path(configured))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        for candidate in sorted(root.glob("ffmpeg-*/ffmpeg-*")):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    raise RenderError(
        "video_encoder_unavailable",
        "The Playwright FFmpeg runtime is unavailable.",
        503,
        False,
    )


def _timestamp_ms(value: str) -> int:
    hours, minutes, seconds = value.split(":")
    return round(
        (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000
    )


async def _run_process(command: list[str], timeout: float) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except BaseException:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.kill()
                await process.wait()
        raise
    return process.returncode or 0, stderr


async def _settled_thread(operation, *args, **kwargs):
    task = asyncio.create_task(
        asyncio.to_thread(operation, *args, **kwargs)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await asyncio.shield(task)
        raise


async def _webm_duration_ms(ffmpeg: Path, path: Path) -> int:
    _, diagnostic_bytes = await _run_process(
        [str(ffmpeg), "-hide_banner", "-i", str(path)], 10
    )
    diagnostic = diagnostic_bytes.decode("utf-8", "replace")
    match = re.search(r"Duration:\s*(\d{2}:\d{2}:\d{2}(?:\.\d+)?)", diagnostic)
    if match is None:
        raise RenderError(
            "video_duration_unavailable",
            "The encoded WebM duration could not be verified.",
            502,
            True,
        )
    return _timestamp_ms(match.group(1))


async def _trim_webm(
    source: Path,
    destination: Path,
    *,
    duration_ms: int,
) -> int:
    ffmpeg = _ffmpeg_executable()
    source_duration_ms = await _webm_duration_ms(ffmpeg, source)
    start_ms = max(0, source_duration_ms - duration_ms)
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-an",
        "-c:v",
        "libvpx",
        "-deadline",
        "realtime",
        "-cpu-used",
        "8",
        "-y",
        str(destination),
    ]
    returncode, _ = await _run_process(command, 45)
    if returncode != 0 or not destination.is_file():
        raise RenderError(
            "video_encode_failed",
            "The requested WebM recording window could not be encoded.",
            502,
            True,
        )
    return await _webm_duration_ms(ffmpeg, destination)


def diagnostic_url(value: str) -> str:
    """Retain useful routing context without leaking query strings or credentials."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path[:2_048], "", ""))
    except ValueError:
        return "invalid-url"


def _write_diagnostic_zip(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return output.getvalue()


async def diagnostic_bundle(
    artifact: "RenderArtifact",
    request: RenderRequest,
    console_events: list[dict[str, object]],
    network_events: list[dict[str, object]],
    limits: "RenderLimits",
) -> "RenderArtifact":
    if not request.diagnostics.bundle:
        return artifact
    artifact_metadata = dict(artifact.metadata)
    if isinstance(artifact_metadata.get("final_url"), str):
        artifact_metadata["final_url"] = diagnostic_url(artifact_metadata["final_url"])
    manifest = {
        "schema_version": 1,
        "artifact": {
            "filename": artifact.filename,
            "media_type": artifact.media_type,
            "bytes": len(artifact.body),
            "metadata": artifact_metadata,
        },
        "privacy": "Network query strings, credentials, request headers, cookies, and bodies are omitted.",
    }
    entries = [
        (artifact.filename, artifact.body),
        (
            "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        ),
    ]
    if request.diagnostics.include_console:
        entries.append(
            ("console.json", (json.dumps(console_events, ensure_ascii=False, indent=2) + "\n").encode())
        )
    if request.diagnostics.include_network:
        entries.append(
            ("network.json", (json.dumps(network_events, ensure_ascii=False, indent=2) + "\n").encode())
        )
    if sum(len(entry) for _, entry in entries) > limits.output_bytes:
        raise RenderError("output_too_large", "The diagnostic bundle exceeds the output limit.", 413, False)
    body = await _settled_thread(_write_diagnostic_zip, entries)
    if len(body) > limits.output_bytes:
        raise RenderError("output_too_large", "The diagnostic bundle exceeds the output limit.", 413, False)
    return RenderArtifact(body, "application/zip", "vipercapture-diagnostics.zip", artifact.metadata)


@dataclass(frozen=True)
class RenderLimits:
    max_width: int = 7680
    max_height: int = 4320
    max_pixels: int = 50_000_000
    max_full_page_height: int = 20_000
    wait_timeout_ms: int = 30_000
    delay_ms: int = 15_000
    deadline_seconds: int = 75
    output_bytes: int = 50 * 1024 * 1024


@dataclass(frozen=True)
class RenderArtifact:
    body: bytes
    media_type: str
    filename: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CleanupHooks:
    setup: Callable[[Page, str], Awaitable[object | None]]
    finish: Callable[[Page, object | None], Awaitable[dict[str, object]]]
    apply: Callable[[Page, object], Awaitable[dict[str, int]]]
    blocked_category: Callable[[str, object], str | None]


def normalized_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme.lower(), parsed.hostname.lower().rstrip("."), port
    except ValueError:
        return None


def routed_headers(
    request_url: str,
    original_url: str,
    browser_headers: dict[str, str],
    custom_headers: dict[str, str],
) -> dict[str, str]:
    result = dict(browser_headers)
    custom_names = {name.lower() for name in custom_headers}
    if normalized_origin(request_url) == normalized_origin(original_url):
        for name, value in custom_headers.items():
            for existing in tuple(result):
                if existing.lower() == name.lower():
                    result.pop(existing)
            result[name] = value
    else:
        for existing in tuple(result):
            if existing.lower() in custom_names:
                result.pop(existing)
    return result


async def _resolve_public_origin(hostname: str, port: int) -> bool:
    await PUBLIC_DNS_SLOTS.acquire()
    resolution = asyncio.create_task(
        asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    )

    def release_resolution(completed: asyncio.Task) -> None:
        PUBLIC_DNS_SLOTS.release()
        with suppress(BaseException):
            completed.result()

    resolution.add_done_callback(release_resolution)
    try:
        addresses = await asyncio.wait_for(
            asyncio.shield(resolution),
            timeout=DNS_RESOLUTION_TIMEOUT_SECONDS,
        )
        return bool(addresses) and all(
            ipaddress.ip_address(address[4][0].split("%", 1)[0]).is_global
            for address in addresses
        )
    except (OSError, TimeoutError, ValueError):
        return False


class PublicUrlValidator:
    """Coalesce simultaneous DNS checks without caching their results."""

    def __init__(self) -> None:
        self._checks: dict[tuple[str, str, int], asyncio.Task[bool]] = {}
        self._origins: set[tuple[str, str, int]] = set()
        self._slots = asyncio.Semaphore(MAX_DNS_CONCURRENCY)

    async def _resolve(self, hostname: str, port: int) -> bool:
        async with self._slots:
            return await _resolve_public_origin(hostname, port)

    async def is_public(self, target: str) -> bool:
        origin = normalized_origin(target)
        if origin is None:
            return False
        task = self._checks.get(origin)
        if task is None or task.done():
            if origin not in self._origins and len(self._origins) >= MAX_DNS_ORIGINS:
                return False
            self._origins.add(origin)
            _, hostname, port = origin
            task = asyncio.create_task(self._resolve(hostname, port))
            self._checks[origin] = task

            def forget(completed: asyncio.Task[bool]) -> None:
                if self._checks.get(origin) is completed:
                    self._checks.pop(origin, None)

            task.add_done_callback(forget)
        return await asyncio.shield(task)


async def is_public_http_url(target: str) -> bool:
    return await PublicUrlValidator().is_public(target)


def needs_request_routing(
    hosted: bool,
    custom_headers: dict[str, str],
    cleanup_enabled: bool = False,
) -> bool:
    """Avoid Playwright interception when it provides no behavior."""
    return hosted or bool(custom_headers) or cleanup_enabled


def ensure_dimensions(width: float, height: float, scale: float, limits: RenderLimits) -> None:
    output_width = math.ceil(width * scale)
    output_height = math.ceil(height * scale)
    requested = sorted((output_width, output_height))
    allowed = sorted((limits.max_width, limits.max_height))
    if requested[0] > allowed[0] or requested[1] > allowed[1]:
        raise RenderError(
            "output_dimensions_exceeded",
            "The requested output dimensions exceed the account limit.",
            413,
            False,
            {"max_width": limits.max_width, "max_height": limits.max_height},
        )
    if output_width * output_height > limits.max_pixels:
        raise RenderError(
            "pixel_limit_exceeded",
            "The requested output exceeds the pixel limit.",
            413,
            False,
            {"max_pixels": limits.max_pixels},
        )


def ensure_full_page_dimensions(
    width: float,
    height: float,
    scale: float,
    limits: RenderLimits,
    *,
    viewport_width: float | None = None,
) -> None:
    """Validate scroll captures without treating viewport height as page height."""
    output_width = math.ceil(width * scale)
    output_height = math.ceil(height * scale)
    if output_width > max(limits.max_width, limits.max_height):
        details: dict[str, object] = {
            "max_width": limits.max_width,
            "max_height": limits.max_height,
        }
        if viewport_width is not None and width > viewport_width:
            details.update(
                {
                    "page_width": math.ceil(width),
                    "viewport_width": math.ceil(viewport_width),
                    "suggested_action": "preserve_viewport_width",
                }
            )
        raise RenderError(
            "output_dimensions_exceeded",
            "This page is wider than your account limit.",
            413,
            False,
            details,
        )
    if height > limits.max_full_page_height:
        raise RenderError(
            "page_too_tall",
            "The page is too tall to capture safely.",
            413,
            False,
            {"max_full_page_height": limits.max_full_page_height},
        )
    if output_width * output_height > limits.max_pixels:
        raise RenderError(
            "pixel_limit_exceeded",
            "The requested output exceeds the pixel limit.",
            413,
            False,
            {"max_pixels": limits.max_pixels},
        )


async def measure_page_dimensions(page: Page) -> tuple[float, float]:
    dimensions = await page.evaluate("""() => ({
        width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
        height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)
    })""")
    return float(dimensions["width"]), float(dimensions["height"])


def ensure_page_width(
    page_width: float,
    viewport_width: float,
    scale: float,
    limits: RenderLimits,
) -> None:
    """Fail wide full-page captures early so the UI can offer a recovery."""
    if math.ceil(page_width * scale) <= max(limits.max_width, limits.max_height):
        return
    ensure_full_page_dimensions(
        page_width,
        1,
        scale,
        limits,
        viewport_width=viewport_width,
    )


async def load_lazy_content(
    page: Page,
    viewport_height: int,
    mode: LazyLoadMode = LazyLoadMode.THOROUGH,
) -> None:
    if mode is LazyLoadMode.NONE:
        return
    scroll = "(y) => window.scrollTo({ top: y, left: 0, behavior: 'instant' })"
    document_height = """Math.max(
        document.documentElement.scrollHeight,
        document.documentElement.offsetHeight,
        document.body?.scrollHeight || 0,
        document.body?.offsetHeight || 0
    )"""
    max_steps, delay, step_ratio = (
        (24, 0.075, 1.0)
        if mode is LazyLoadMode.ADAPTIVE
        else (40, 0.2, 0.8)
    )
    step = max(1, math.ceil(viewport_height * step_ratio))
    position = 0
    stable_bottom_checks = 0
    await page.evaluate(scroll, 0)
    try:
        for _ in range(max_steps):
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
            await asyncio.sleep(delay)
    finally:
        with suppress(Exception):
            await page.evaluate(scroll, 0)
            await asyncio.sleep(0.2)


async def capture_cdp_image(
    page: Page,
    *,
    output: OutputFormat,
    clip: dict[str, float],
    quality: int | None,
    transparent: bool,
    optimize_for_speed: bool,
) -> bytes:
    """Capture PNG or WebP through CDP with optional fast encoding."""
    if output not in {OutputFormat.PNG, OutputFormat.WEBP}:
        raise ValueError("CDP capture supports only PNG and WebP")
    session = await page.context.new_cdp_session(page)
    fast_png = output is OutputFormat.PNG and optimize_for_speed
    try:
        with suppress(Exception):
            if fast_png:
                await page.evaluate("""() => {
                    document.getAnimations().forEach((animation) => animation.pause());
                    const style = document.createElement("style");
                    style.dataset.vipercaptureScreenshot = "true";
                    style.textContent = "*, *::before, *::after { caret-color: transparent !important; }";
                    document.documentElement.append(style);
                    void document.documentElement.offsetWidth;
                }""")
            else:
                await page.evaluate(
                    "() => document.getAnimations().forEach(a => a.pause())"
                )
        if transparent:
            await session.send(
                "Emulation.setDefaultBackgroundColorOverride",
                {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
            )
        options: dict[str, object] = {
            "format": output.value,
            "fromSurface": True,
            "captureBeyondViewport": True,
            "clip": clip,
        }
        if optimize_for_speed:
            options["optimizeForSpeed"] = True
        if output is OutputFormat.WEBP:
            options["quality"] = quality if quality is not None else 80
        result = await session.send("Page.captureScreenshot", options)
        return b64decode(result["data"])
    finally:
        if fast_png:
            with suppress(Exception):
                await page.evaluate(
                    """() => document.querySelectorAll(
                        "style[data-vipercapture-screenshot]"
                    ).forEach((style) => style.remove())"""
                )
        if transparent:
            with suppress(Exception):
                await session.send("Emulation.setDefaultBackgroundColorOverride")
        with suppress(Exception):
            await session.detach()


async def capture_webp(
    page: Page,
    *,
    clip: dict[str, float],
    quality: int | None,
    transparent: bool,
    optimize_for_speed: bool,
) -> bytes:
    """Compatibility wrapper for the native WebP CDP encoder."""
    return await capture_cdp_image(
        page,
        output=OutputFormat.WEBP,
        clip=clip,
        quality=quality,
        transparent=transparent,
        optimize_for_speed=optimize_for_speed,
    )


async def capture_clipped_image(
    page: Page,
    *,
    output: OutputFormat,
    clip: dict[str, float],
    quality: int | None,
    transparent: bool,
) -> bytes:
    """Capture a tall explicit clip beyond the visible viewport."""
    session = await page.context.new_cdp_session(page)
    try:
        with suppress(Exception):
            await page.evaluate("() => document.getAnimations().forEach(a => a.pause())")
        if transparent:
            await session.send(
                "Emulation.setDefaultBackgroundColorOverride",
                {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
            )
        options: dict[str, object] = {
            "format": output.value,
            "fromSurface": True,
            "captureBeyondViewport": True,
            "clip": clip,
        }
        if output is OutputFormat.JPEG:
            options["quality"] = quality if quality is not None else 80
        result = await session.send("Page.captureScreenshot", options)
        return b64decode(result["data"])
    finally:
        if transparent:
            with suppress(Exception):
                await session.send("Emulation.setDefaultBackgroundColorOverride")
        with suppress(Exception):
            await session.detach()


async def render_metadata(page: Page) -> RenderArtifact:
    """Extract a bounded, predictable metadata document from the final DOM."""
    payload = await page.evaluate(
        """({maxItems, maxChars}) => {
            const clean = (value, limit = maxChars) =>
                typeof value === "string" ? value.trim().slice(0, limit) : null;
            const attr = (selector, name = "content") =>
                clean(document.querySelector(selector)?.getAttribute(name));
            const pairs = (attribute, keyPrefix) => {
                const result = {};
                for (const element of [...document.querySelectorAll(`meta[${attribute}]`)]) {
                    const key = clean(element.getAttribute(attribute), 128);
                    const value = clean(element.getAttribute("content"));
                    if (key && key.startsWith(keyPrefix) && value && !(key in result)) result[key] = value;
                    if (Object.keys(result).length >= 32) break;
                }
                return result;
            };
            const links = [...document.querySelectorAll("a[href]")];
            const images = [...document.images];
            return {
                title: clean(document.title),
                description: attr('meta[name="description"]'),
                canonical_url: attr('link[rel="canonical"]', "href"),
                language: clean(document.documentElement.lang, 64),
                robots: attr('meta[name="robots"]'),
                theme_color: attr('meta[name="theme-color"]'),
                open_graph: pairs("property", "og:"),
                twitter: pairs("name", "twitter:"),
                icons: [...document.querySelectorAll('link[rel~="icon"][href]')]
                    .slice(0, 16)
                    .map((element) => ({
                        rel: clean(element.getAttribute("rel"), 64),
                        href: clean(element.href),
                        sizes: clean(element.getAttribute("sizes"), 64),
                        type: clean(element.getAttribute("type"), 128)
                    })),
                headings: [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")]
                    .slice(0, maxItems)
                    .map((element) => ({
                        level: Number(element.tagName.slice(1)),
                        text: clean(element.textContent)
                    }))
                    .filter((item) => item.text),
                links: {
                    total: links.length,
                    sample: links.slice(0, maxItems).map((element) => ({
                        text: clean(element.textContent, 512),
                        href: clean(element.href)
                    }))
                },
                images: {
                    total: images.length,
                    sample: images.slice(0, maxItems).map((element) => ({
                        src: clean(element.currentSrc || element.src),
                        alt: clean(element.alt, 512),
                        width: Number(element.naturalWidth || element.width || 0),
                        height: Number(element.naturalHeight || element.height || 0)
                    }))
                }
            };
        }""",
        {"maxItems": MAX_METADATA_ITEMS, "maxChars": MAX_METADATA_VALUE_CHARS},
    )
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return RenderArtifact(body, "application/json", "vipercapture-metadata.json")


class RenderEngine:
    def __init__(
        self,
        *,
        hosted: bool,
        cleanup_hooks: CleanupHooks | None = None,
        challenge_checker: Callable[[Page, bool, int | None], Awaitable[None]] | None = None,
        browser_replacer: Callable[[Browser], Awaitable[None]] | None = None,
        device_descriptors: dict[str, dict[str, object]] | None = None,
        allow_scripts: bool = False,
    ) -> None:
        self.hosted = hosted
        self.cleanup_hooks = cleanup_hooks
        self.challenge_checker = challenge_checker
        self.browser_replacer = browser_replacer
        self.device_descriptors = device_descriptors or {}
        self.allow_scripts = allow_scripts

    async def _wait(self, page: Page, request: RenderRequest, limits: RenderLimits) -> None:
        wait = request.wait_for
        timeout = min(wait.timeout_ms, limits.wait_timeout_ms)
        if wait.selector:
            try:
                await page.locator(wait.selector).wait_for(state="visible", timeout=timeout)
            except PlaywrightTimeoutError as exc:
                raise RenderError(
                    "wait_selector_timeout",
                    "The wait selector did not become visible in time.",
                    504,
                    True,
                ) from exc
        if wait.text:
            try:
                await page.wait_for_function(
                    "text => Boolean(document.body?.innerText.includes(text))",
                    arg=wait.text,
                    timeout=timeout,
                )
            except PlaywrightTimeoutError as exc:
                raise RenderError(
                    "wait_text_timeout",
                    "The requested text did not become visible in time.",
                    504,
                    True,
                ) from exc
        if wait.delay_ms:
            await page.wait_for_timeout(min(wait.delay_ms, limits.delay_ms))

    async def _run_actions(
        self,
        page: Page,
        request: RenderRequest,
        limits: RenderLimits,
    ) -> None:
        for index, action in enumerate(request.actions):
            timeout = min(action.timeout_ms, limits.wait_timeout_ms)
            try:
                locator = page.locator(action.selector) if action.selector else None
                first = locator.first if locator is not None else None
                if action.type is ActionType.CLICK:
                    await first.click(timeout=timeout)
                elif action.type is ActionType.HOVER:
                    await first.hover(timeout=timeout)
                elif action.type is ActionType.FILL:
                    await first.fill(action.value or "", timeout=timeout)
                elif action.type is ActionType.PRESS:
                    if first is not None:
                        await first.press(action.key or "", timeout=timeout)
                    else:
                        await page.keyboard.press(action.key or "")
                elif action.type is ActionType.SELECT:
                    await first.select_option(action.values or [], timeout=timeout)
                elif action.type is ActionType.SCROLL:
                    if first is not None:
                        await first.scroll_into_view_if_needed(timeout=timeout)
                    if action.x is not None or action.y is not None:
                        await page.evaluate(
                            "([x, y]) => window.scrollBy({left: x, top: y, behavior: 'instant'})",
                            [action.x or 0, action.y or 0],
                        )
                elif action.type is ActionType.WAIT:
                    if first is not None:
                        await first.wait_for(state="visible", timeout=timeout)
                    if action.value is not None:
                        await page.wait_for_function(
                            "text => Boolean(document.body?.innerText.includes(text))",
                            arg=action.value,
                            timeout=timeout,
                        )
                    if action.delay_ms:
                        await page.wait_for_timeout(min(action.delay_ms, limits.delay_ms))
                elif action.type is ActionType.HIDE:
                    await locator.evaluate_all(
                        "elements => elements.forEach((element) => "
                        "element.style.setProperty('display', 'none', 'important'))"
                    )
                elif action.type is ActionType.JAVASCRIPT:
                    if not self.allow_scripts:
                        raise RenderError(
                            "scripts_disabled",
                            "JavaScript actions are disabled by this ViperCapture instance.",
                            403,
                            False,
                        )
                    await page.evaluate(action.value or "")
                if action.delay_ms and action.type is not ActionType.WAIT:
                    await page.wait_for_timeout(min(action.delay_ms, limits.delay_ms))
            except RenderError:
                raise
            except PlaywrightTimeoutError as exc:
                raise RenderError(
                    "action_timeout",
                    f"Action {index} ({action.type.value}) timed out.",
                    504,
                    True,
                    {"action_index": index, "action_type": action.type.value},
                ) from exc
            except PlaywrightError:
                raise
            except Exception as exc:
                raise RenderError(
                    "action_failed",
                    f"Action {index} ({action.type.value}) failed.",
                    422,
                    False,
                    {"action_index": index, "action_type": action.type.value},
                ) from exc

    async def _check_assertions(
        self,
        page: Page,
        request: RenderRequest,
        failed_requests: list[dict[str, object]],
        matched_failure_patterns: set[str] | None = None,
    ) -> None:
        if matched_failure_patterns is None:
            matched_failure_patterns = set()
        for expected in request.assertions.content_includes:
            present = await page.evaluate(
                "text => Boolean(document.documentElement?.innerText.includes(text))",
                expected,
            )
            if not present:
                raise RenderError(
                    "content_assertion_failed",
                    "Required page content was not present.",
                    424,
                    False,
                    {"assertion": "content_includes", "value": expected},
                )
        for forbidden in request.assertions.content_excludes:
            present = await page.evaluate(
                "text => Boolean(document.documentElement?.innerText.includes(text))",
                forbidden,
            )
            if present:
                raise RenderError(
                    "content_assertion_failed",
                    "Forbidden page content was present.",
                    424,
                    False,
                    {"assertion": "content_excludes", "value": forbidden},
                )
        for pattern in request.assertions.request_failures:
            matching = [
                failure
                for failure in failed_requests
                if fnmatchcase(str(failure.get("url", "")), pattern)
            ]
            if pattern in matched_failure_patterns or matching:
                raise RenderError(
                    "request_assertion_failed",
                    "A matching page request failed.",
                    424,
                    True,
                    {
                        "pattern": pattern,
                        "failures": matching[:10],
                    },
                )

    async def render(
        self,
        browser: Browser,
        request: RenderRequest,
        limits: RenderLimits,
    ) -> RenderArtifact:
        if request.viewports is None:
            return await self._render_single(browser, request, limits)

        try:
            async with asyncio.timeout(limits.deadline_seconds):
                outputs: list[tuple[str, RenderArtifact]] = []
                output_bytes = 0
                for viewport in request.viewports:
                    environment = request.environment.model_copy(
                        update={"device": viewport.device}
                    )
                    single = request.model_copy(
                        update={
                            "viewport": Viewport(
                                width=viewport.width,
                                height=viewport.height,
                                device_scale_factor=viewport.device_scale_factor,
                            ),
                            "viewports": None,
                            "environment": environment,
                        }
                    )
                    artifact = await self._render_single(browser, single, limits)
                    output_bytes += len(artifact.body)
                    if output_bytes > limits.output_bytes:
                        raise RenderError(
                            "output_too_large",
                            "The viewport artifacts exceed the aggregate output limit.",
                            413,
                            False,
                        )
                    outputs.append((viewport.name, artifact))

                manifest_outputs = []
                archive_buffer = io.BytesIO()
                with zipfile.ZipFile(
                    archive_buffer, "w", compression=zipfile.ZIP_STORED
                ) as archive:
                    for name, artifact in outputs:
                        filename = f"{name}.{EXTENSIONS[request.output]}"
                        archive.writestr(filename, artifact.body)
                        manifest_outputs.append(
                            {
                                "name": name,
                                "filename": filename,
                                "media_type": artifact.media_type,
                                "width": artifact.metadata.get("width"),
                                "height": artifact.metadata.get("height"),
                                "navigation_status": artifact.metadata.get("navigation_status"),
                            }
                        )
                    manifest = {
                        "schema_version": 1,
                        "output": request.output.value,
                        "count": len(outputs),
                        "outputs": manifest_outputs,
                    }
                    archive.writestr(
                        "manifest.json",
                        json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
                    )
                body = archive_buffer.getvalue()
                if len(body) > limits.output_bytes:
                    raise RenderError(
                        "output_too_large",
                        "The viewport archive exceeds the output limit.",
                        413,
                        False,
                    )
                statuses = {
                    artifact.metadata.get("navigation_status")
                    for _, artifact in outputs
                    if artifact.metadata.get("navigation_status") is not None
                }
                metadata: dict[str, object] = {
                    "output_count": len(outputs),
                    "outputs": manifest_outputs,
                }
                if len(statuses) == 1:
                    metadata["navigation_status"] = statuses.pop()
                return RenderArtifact(
                    body,
                    "application/zip",
                    "vipercapture-viewports.zip",
                    metadata,
                )
        except TimeoutError as exc:
            raise RenderError(
                "render_timeout",
                "The viewport pack exceeded its total deadline.",
                504,
                True,
            ) from exc

    async def _render_single(
        self,
        browser: Browser,
        request: RenderRequest,
        limits: RenderLimits,
    ) -> RenderArtifact:
        from content_rendering import input_document, render_document_output

        target = str(request.url or request.base_url or "about:blank")
        public_urls = PublicUrlValidator()
        if self.hosted and target != "about:blank" and not await public_urls.is_public(target):
            raise RenderError(
                "target_not_public",
                "Private or non-public target URLs are blocked.",
                400,
                False,
            )
        ensure_dimensions(
            request.viewport.width,
            request.viewport.height,
            request.viewport.device_scale_factor,
            limits,
        )
        if request.wait_for.timeout_ms > limits.wait_timeout_ms:
            raise RenderError("wait_limit_exceeded", "The wait timeout exceeds the plan limit.", 413, False)
        if request.wait_for.delay_ms > limits.delay_ms:
            raise RenderError("delay_limit_exceeded", "The wait delay exceeds the plan limit.", 413, False)

        context = None
        video_directory = None
        blocked_subresources = 0
        blocked_private_subresources = False
        failed_requests: list[dict[str, object]] = []
        matched_failure_patterns: set[str] = set()
        console_events: list[dict[str, object]] = []
        network_events: list[dict[str, object]] = []
        cleanup_routing = (
            request.cleanup.block_ads
            or request.cleanup.block_trackers
            or request.cleanup.block_chats
        )
        request_routing = needs_request_routing(
            self.hosted,
            request.headers,
            cleanup_routing
            or bool(request.network.block_url_patterns)
            or bool(request.network.block_resource_types),
        )
        try:
            async with asyncio.timeout(limits.deadline_seconds):
                context_options: dict[str, object] = {}
                if request.output is OutputFormat.WEBM:
                    video_directory = tempfile.TemporaryDirectory(prefix="vipercapture-video-")
                    context_options["record_video_dir"] = video_directory.name
                    context_options["record_video_size"] = {
                        "width": request.viewport.width,
                        "height": request.viewport.height,
                    }
                descriptor_name = DEVICE_DESCRIPTOR_NAMES.get(request.environment.device)
                if descriptor_name:
                    context_options.update(self.device_descriptors.get(descriptor_name, {}))
                    context_options.pop("default_browser_type", None)
                context_options.update(
                    {
                        "viewport": {
                            "width": request.viewport.width,
                            "height": request.viewport.height,
                        },
                        "screen": {
                            "width": request.viewport.width,
                            "height": request.viewport.height,
                        },
                        "device_scale_factor": request.viewport.device_scale_factor,
                        "service_workers": (
                            "block"
                            if (
                                self.hosted
                                or request.headers
                                or request.network.block_url_patterns
                                or request.network.block_resource_types
                                or cleanup_routing
                            )
                            else "allow"
                        ),
                    }
                )
                if request.environment.color_scheme is not None:
                    context_options["color_scheme"] = request.environment.color_scheme.value
                if request.environment.reduced_motion is not None:
                    context_options["reduced_motion"] = request.environment.reduced_motion.value
                if request.environment.locale is not None:
                    context_options["locale"] = request.environment.locale
                if request.environment.timezone is not None:
                    context_options["timezone_id"] = request.environment.timezone
                if request.network.user_agent is not None:
                    context_options["user_agent"] = request.network.user_agent
                if request.network.geolocation is not None:
                    context_options["geolocation"] = request.network.geolocation.model_dump()
                    context_options["permissions"] = ["geolocation"]
                if request.network.proxy is not None:
                    if self.hosted:
                        raise RenderError(
                            "proxy_not_allowed",
                            "Per-request proxies are disabled in hosted security mode.",
                            403,
                            False,
                        )
                    context_options["proxy"] = request.network.proxy.model_dump(
                        exclude_none=True
                    )
                context_options["bypass_csp"] = request.network.bypass_csp
                context_options["ignore_https_errors"] = request.network.ignore_https_errors
                context = await browser.new_context(**context_options)
                if request.network.cookies:
                    target_host = urlsplit(target).hostname or ""
                    cookies = []
                    for cookie in request.network.cookies:
                        normalized_domain = cookie.domain.lstrip(".").lower()
                        if self.hosted and not (
                            target_host.lower() == normalized_domain
                            or target_host.lower().endswith(f".{normalized_domain}")
                        ):
                            raise RenderError(
                                "cookie_domain_not_allowed",
                                "Cookies may target only the requested site in hosted security mode.",
                                403,
                                False,
                            )
                        document = cookie.model_dump(exclude_none=True)
                        document["httpOnly"] = document.pop("http_only")
                        document["sameSite"] = document.pop("same_site")
                        cookies.append(document)
                    await context.add_cookies(cookies)
                device_platform = DEVICE_PLATFORMS.get(request.environment.device)
                if device_platform:
                    await context.add_init_script(
                        script=f"""Object.defineProperty(
                            Navigator.prototype,
                            "platform",
                            {{ configurable: true, get: () => {json.dumps(device_platform)} }}
                        )""",
                    )

                async def route_request(route) -> None:
                    nonlocal blocked_private_subresources, blocked_subresources
                    request_url = route.request.url
                    try:
                        scheme = urlsplit(request_url).scheme.lower()
                    except ValueError:
                        scheme = ""
                    if scheme in ALLOWED_INTERNAL_SCHEMES:
                        await route.continue_()
                        return
                    blocked_by_type = route.request.resource_type in {
                        resource.value for resource in request.network.block_resource_types
                    }
                    main_document = (
                        route.request.resource_type == "document"
                        and route.request.is_navigation_request()
                        and route.request.frame.parent_frame is None
                    )
                    if blocked_by_type and not main_document:
                        blocked_subresources += 1
                        await route.abort("blockedbyclient")
                        return
                    if not main_document and any(
                        fnmatchcase(request_url, pattern)
                        for pattern in request.network.block_url_patterns
                    ):
                        blocked_subresources += 1
                        await route.abort("blockedbyclient")
                        return
                    if self.cleanup_hooks:
                        category = self.cleanup_hooks.blocked_category(request_url, request.cleanup)
                        if category and not main_document:
                            blocked_subresources += 1
                            await route.abort("blockedbyclient")
                            return
                    if self.hosted and not await public_urls.is_public(request_url):
                        blocked_subresources += 1
                        blocked_private_subresources = True
                        await route.abort("blockedbyclient")
                        return
                    await route.continue_(
                        headers=routed_headers(
                            request_url,
                            target,
                            dict(route.request.headers),
                            request.headers,
                        )
                    )

                if request_routing:
                    await context.route("**/*", route_request)
                block_websocket_type = any(
                    resource.value == "websocket"
                    for resource in request.network.block_resource_types
                )
                if (
                    self.hosted
                    or block_websocket_type
                    or request.network.block_url_patterns
                    or cleanup_routing
                ):
                    async def block_web_socket(web_socket) -> None:
                        nonlocal blocked_subresources
                        blocked = (
                            self.hosted
                            or block_websocket_type
                            or any(
                                fnmatchcase(web_socket.url, pattern)
                                for pattern in request.network.block_url_patterns
                            )
                            or (
                                self.cleanup_hooks is not None
                                and self.cleanup_hooks.blocked_category(
                                    web_socket.url, request.cleanup
                                )
                                is not None
                            )
                        )
                        if blocked:
                            blocked_subresources += 1
                            await web_socket.close(
                                code=1008, reason="Blocked by render network policy"
                            )
                        else:
                            await web_socket.connect_to_server()
                    await context.route_web_socket("**/*", block_web_socket)

                page = await context.new_page()

                def record_console(message) -> None:
                    if len(console_events) >= MAX_DIAGNOSTIC_EVENTS:
                        return
                    console_events.append({
                        "type": message.type,
                        "text": message.text[:4_096],
                    })

                def record_network(response) -> None:
                    if len(network_events) >= MAX_DIAGNOSTIC_EVENTS:
                        return
                    network_events.append({
                        "method": response.request.method,
                        "url": diagnostic_url(response.url),
                        "status": response.status,
                        "resource_type": response.request.resource_type,
                    })

                if request.diagnostics.bundle:
                    page.on("console", record_console)
                    page.on("response", record_network)

                def record_failed(page_request) -> None:
                    for pattern in request.assertions.request_failures:
                        if fnmatchcase(page_request.url, pattern):
                            matched_failure_patterns.add(pattern)
                    if len(failed_requests) >= 200:
                        return
                    failure = page_request.failure
                    failed_requests.append(
                        {
                            "url": page_request.url[:2_048],
                            "error": str(failure or "request_failed")[:512],
                        }
                    )

                def record_error_response(response) -> None:
                    if response.status < 400:
                        return
                    for pattern in request.assertions.request_failures:
                        if fnmatchcase(response.url, pattern):
                            matched_failure_patterns.add(pattern)
                    if len(failed_requests) >= 200:
                        return
                    failed_requests.append(
                        {
                            "url": response.url[:2_048],
                            "status": response.status,
                        }
                    )

                page.on("requestfailed", record_failed)
                page.on("response", record_error_response)

                async def close_popup(popup) -> None:
                    with suppress(Exception):
                        await popup.close()
                page.on("popup", lambda popup: asyncio.create_task(close_popup(popup)))

                cleanup_session = None
                if self.cleanup_hooks:
                    cleanup_session = await self.cleanup_hooks.setup(
                        page, request.cleanup.consent_mode.value
                    )
                try:
                    if request.url is not None:
                        navigation = await page.goto(
                            target,
                            wait_until=request.wait_for.event.value,
                            timeout=min(request.wait_for.timeout_ms, limits.wait_timeout_ms),
                        )
                    else:
                        navigation = None
                        document = await asyncio.to_thread(input_document, request)
                        if len(document.encode("utf-8")) > limits.output_bytes:
                            raise RenderError(
                                "document_too_large",
                                "The generated input document exceeds the output limit.",
                                413,
                                False,
                                {"max_bytes": limits.output_bytes},
                            )
                        await page.set_content(
                            document,
                            wait_until=request.wait_for.event.value,
                            timeout=min(request.wait_for.timeout_ms, limits.wait_timeout_ms),
                        )
                except PlaywrightTimeoutError as exc:
                    raise RenderError(
                        "target_timeout",
                        "The target did not become ready in time.",
                        504,
                        True,
                    ) from exc
                if self.hosted and request.url is not None and not await public_urls.is_public(page.url):
                    raise RenderError(
                        "redirect_not_public",
                        "The target redirected to a private or non-public URL.",
                        400,
                        False,
                    )
                navigation_status = navigation.status if navigation else None
                if navigation_status in request.fail_on_status:
                    raise RenderError(
                        "target_status_failed",
                        f"The target returned configured failure status {navigation_status}.",
                        424,
                        navigation_status == 429 or navigation_status >= 500,
                        {"target_status": navigation_status},
                        headers={
                            "X-ViperCapture-Navigation-Status": str(navigation_status)
                        },
                    )
                if self.cleanup_hooks:
                    await self.cleanup_hooks.finish(page, cleanup_session)
                if request.custom_css:
                    try:
                        await page.add_style_tag(content=request.custom_css)
                    except Exception as exc:
                        raise RenderError(
                            "custom_css_invalid",
                            "The custom CSS could not be applied.",
                            422,
                            False,
                        ) from exc
                await self._wait(page, request, limits)
                if self.cleanup_hooks:
                    await self.cleanup_hooks.apply(page, request.cleanup)
                await self._run_actions(page, request, limits)
                if self.cleanup_hooks:
                    await self.cleanup_hooks.apply(page, request.cleanup)
                if self.challenge_checker:
                    await self.challenge_checker(page, request.proceed_on_captcha, navigation_status)
                if request.full_page:
                    if (
                        request.output in MEDIA_TYPES
                        and not request.preserve_viewport_width
                    ):
                        page_width, _ = await measure_page_dimensions(page)
                        ensure_page_width(
                            max(page_width, request.viewport.width),
                            request.viewport.width,
                            request.viewport.device_scale_factor,
                            limits,
                        )
                    await load_lazy_content(
                        page,
                        request.viewport.height,
                        request.lazy_load,
                    )
                    if self.cleanup_hooks:
                        await self.cleanup_hooks.apply(page, request.cleanup)
                    if self.challenge_checker:
                        await self.challenge_checker(page, request.proceed_on_captcha, navigation_status)
                await self._check_assertions(
                    page, request, failed_requests, matched_failure_patterns
                )

                if request.output is OutputFormat.WEBM:
                    options = request.video
                    if options is None or page.video is None:
                        raise RenderError("video_unavailable", "Chromium video recording is unavailable.", 500, True)
                    if video_directory is None:
                        raise RenderError("video_unavailable", "Video recording did not start.", 500, True)
                    if options.scroll:
                        elapsed = 0
                        while elapsed < options.duration_ms:
                            await page.evaluate(
                                "step => window.scrollBy({top: step, left: 0, behavior: 'smooth'})",
                                options.scroll_step,
                            )
                            delay = min(options.scroll_delay_ms, options.duration_ms - elapsed)
                            await page.wait_for_timeout(delay)
                            elapsed += delay
                    else:
                        await page.wait_for_timeout(options.duration_ms)
                    if self.challenge_checker:
                        await self.challenge_checker(
                            page,
                            request.proceed_on_captcha,
                            navigation_status,
                        )
                    await self._check_assertions(
                        page,
                        request,
                        failed_requests,
                        matched_failure_patterns,
                    )
                    final_url = page.url
                    video = page.video
                    await page.close()
                    await context.close()
                    context = None
                    path = await video.path()
                    trimmed_path = Path(video_directory.name) / "trimmed.webm"
                    actual_duration_ms = await _trim_webm(
                        Path(path),
                        trimmed_path,
                        duration_ms=options.duration_ms,
                    )
                    size = (await asyncio.to_thread(trimmed_path.stat)).st_size
                    if not size:
                        raise RenderError("empty_output", "The renderer produced an empty video.", 502, True)
                    if size > limits.output_bytes:
                        raise RenderError("output_too_large", "The rendered video exceeds the output limit.", 413, False)
                    body = await asyncio.to_thread(trimmed_path.read_bytes)
                    artifact = RenderArtifact(
                        body,
                        "video/webm",
                        "vipercapture.webm",
                        {
                            "width": request.viewport.width,
                            "height": request.viewport.height,
                            "duration_ms": actual_duration_ms,
                            "navigation_status": navigation_status,
                            "final_url": final_url,
                            "blocked_subresources": blocked_subresources,
                            "output_count": 1,
                        },
                    )
                    return await diagnostic_bundle(artifact, request, console_events, network_events, limits)

                if request.output not in MEDIA_TYPES:
                    if request.output is OutputFormat.METADATA:
                        metadata_artifact = await render_metadata(page)
                        metadata_document = json.loads(metadata_artifact.body)
                        metadata_document.update(
                            {
                                "schema_version": 1,
                                "source_type": request.source_type,
                                "final_url": page.url,
                                "navigation_status": navigation_status,
                                "blocked_subresources": blocked_subresources,
                            }
                        )
                        artifact = RenderArtifact(
                            json.dumps(
                                metadata_document,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                            metadata_artifact.media_type,
                            metadata_artifact.filename,
                        )
                    else:
                        artifact = await render_document_output(page, request, limits)
                    if len(artifact.body) > limits.output_bytes:
                        raise RenderError(
                            "output_too_large",
                            "The rendered document exceeds the output limit.",
                            413,
                            False,
                        )
                    artifact = RenderArtifact(
                        artifact.body,
                        artifact.media_type,
                        artifact.filename,
                        {
                            **artifact.metadata,
                            "navigation_status": navigation_status,
                            "final_url": page.url,
                            "blocked_subresources": blocked_subresources,
                            "output_count": 1,
                        },
                    )
                    return await diagnostic_bundle(artifact, request, console_events, network_events, limits)

                screenshot_options: dict[str, object] = {
                    "type": request.output.value,
                    "animations": "disabled",
                    "omit_background": request.image.transparent_background,
                }
                if request.image.quality is not None:
                    screenshot_options["quality"] = request.image.quality

                box = None
                if request.selector:
                    locator = page.locator(request.selector).first
                    if not await locator.is_visible():
                        raise RenderError(
                            "selector_not_found",
                            "The capture selector did not resolve to a visible element.",
                            404,
                            False,
                        )
                    box = await locator.bounding_box()
                    if not box:
                        raise RenderError(
                            "selector_not_found",
                            "The capture selector did not resolve to a visible element.",
                            404,
                            False,
                        )
                    ensure_dimensions(box["width"], box["height"], request.viewport.device_scale_factor, limits)
                    width, height = box["width"], box["height"]
                elif request.clip:
                    page_width, page_height = await measure_page_dimensions(page)
                    clip = request.clip
                    if clip.x + clip.width > page_width or clip.y + clip.height > page_height:
                        raise RenderError(
                            "clip_out_of_bounds",
                            "The requested clip extends beyond the rendered document.",
                            422,
                            False,
                            {
                                "document_width": math.ceil(page_width),
                                "document_height": math.ceil(page_height),
                            },
                        )
                    ensure_dimensions(
                        clip.width,
                        clip.height,
                        request.viewport.device_scale_factor,
                        limits,
                    )
                    width, height = clip.width, clip.height
                else:
                    if request.full_page:
                        page_width, page_height = await measure_page_dimensions(page)
                        width = (
                            request.viewport.width
                            if request.preserve_viewport_width
                            else max(page_width, request.viewport.width)
                        )
                        height = max(page_height, request.viewport.height)
                        ensure_full_page_dimensions(
                            width,
                            height,
                            request.viewport.device_scale_factor,
                            limits,
                            viewport_width=request.viewport.width,
                        )
                    else:
                        width, height = request.viewport.width, request.viewport.height
                if request.output is OutputFormat.WEBP or (
                    request.output is OutputFormat.PNG
                    and request.image.optimize_for_speed
                ):
                    scroll = {"x": 0, "y": 0}
                    if not request.full_page and not request.clip:
                        measured_scroll = await page.evaluate(
                            "() => ({x: window.scrollX, y: window.scrollY})"
                        )
                        if isinstance(measured_scroll, dict):
                            scroll = measured_scroll
                    clip = {
                        "x": (
                            float(box["x"]) + float(scroll["x"])
                            if request.selector
                            else (
                                float(request.clip.x)
                                if request.clip
                                else float(scroll["x"])
                            )
                        ),
                        "y": (
                            float(box["y"]) + float(scroll["y"])
                            if request.selector
                            else (
                                float(request.clip.y)
                                if request.clip
                                else float(scroll["y"])
                            )
                        ),
                        "width": float(width),
                        "height": float(height),
                        "scale": request.viewport.device_scale_factor,
                    }
                    if request.output is OutputFormat.WEBP:
                        image = await capture_webp(
                            page,
                            clip=clip,
                            quality=request.image.quality,
                            transparent=request.image.transparent_background,
                            optimize_for_speed=request.image.optimize_for_speed,
                        )
                    else:
                        image = await capture_cdp_image(
                            page,
                            output=request.output,
                            clip=clip,
                            quality=request.image.quality,
                            transparent=request.image.transparent_background,
                            optimize_for_speed=request.image.optimize_for_speed,
                        )
                elif request.selector:
                    image = await locator.screenshot(**screenshot_options)
                elif request.clip:
                    image = await capture_clipped_image(
                        page,
                        output=request.output,
                        clip={
                            "x": float(request.clip.x),
                            "y": float(request.clip.y),
                            "width": float(width),
                            "height": float(height),
                            "scale": request.viewport.device_scale_factor,
                        },
                        quality=request.image.quality,
                        transparent=request.image.transparent_background,
                    )
                elif request.full_page and request.preserve_viewport_width:
                    image = await capture_clipped_image(
                        page,
                        output=request.output,
                        clip={
                            "x": 0,
                            "y": 0,
                            "width": float(width),
                            "height": float(height),
                            "scale": request.viewport.device_scale_factor,
                        },
                        quality=request.image.quality,
                        transparent=request.image.transparent_background,
                    )
                elif request.full_page:
                    image = await capture_clipped_image(
                        page,
                        output=request.output,
                        clip={
                            "x": 0,
                            "y": 0,
                            "width": float(width),
                            "height": float(height),
                            "scale": request.viewport.device_scale_factor,
                        },
                        quality=request.image.quality,
                        transparent=request.image.transparent_background,
                    )
                else:
                    image = await page.screenshot(
                        full_page=request.full_page,
                        **screenshot_options,
                    )

                if not image:
                    raise RenderError("empty_output", "The renderer produced an empty image.", 502, True)
                if len(image) > limits.output_bytes:
                    raise RenderError("output_too_large", "The rendered image exceeds the output limit.", 413, False)
                artifact = RenderArtifact(
                    body=image,
                    media_type=MEDIA_TYPES[request.output],
                    filename=f"vipercapture.{EXTENSIONS[request.output]}",
                    metadata={
                        "width": math.ceil(width * request.viewport.device_scale_factor),
                        "height": math.ceil(height * request.viewport.device_scale_factor),
                        "navigation_status": navigation_status,
                        "final_url": page.url,
                        "blocked_subresources": blocked_subresources,
                        "output_count": 1,
                    },
                )
                return await diagnostic_bundle(artifact, request, console_events, network_events, limits)
        except TimeoutError as exc:
            raise RenderError("render_timeout", "The render exceeded its total deadline.", 504, True) from exc
        except RenderError:
            raise
        except Exception as exc:
            if blocked_private_subresources:
                raise RenderError(
                    "subresource_not_public",
                    "The page requested a private or non-public resource.",
                    400,
                    False,
                ) from exc
            raise RenderError("render_failed", "The image render failed.", 500, True) from exc
        finally:
            if context is not None:
                cleanup_failed = False
                try:
                    await asyncio.wait_for(context.close(), timeout=5)
                except Exception:
                    cleanup_failed = True
                if self.browser_replacer and (cleanup_failed or not browser.is_connected()):
                    with suppress(Exception):
                        await self.browser_replacer(browser)
            if video_directory is not None:
                video_directory.cleanup()

    async def render_image(
        self,
        browser: Browser,
        request: RenderRequest,
        limits: RenderLimits,
    ) -> RenderArtifact:
        """Compatibility entry point retained for the limited open renderer tests."""
        return await self.render(browser, request, limits)
