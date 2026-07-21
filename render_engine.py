"""Isolated Playwright rendering for the open-source image engine."""

from __future__ import annotations

import asyncio
from base64 import b64decode
from contextlib import suppress
from dataclasses import dataclass, field
import ipaddress
import math
import socket
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError

from render_contract import OutputFormat, RenderRequest
from render_errors import RenderError


MEDIA_TYPES = {OutputFormat.PNG: "image/png", OutputFormat.JPEG: "image/jpeg", OutputFormat.WEBP: "image/webp"}
EXTENSIONS = {OutputFormat.PNG: "png", OutputFormat.JPEG: "jpg", OutputFormat.WEBP: "webp"}


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


def normalized_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        return parsed.scheme.lower(), parsed.hostname.lower().rstrip("."), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def routed_headers(request_url: str, original_url: str, browser_headers: dict[str, str], custom_headers: dict[str, str]) -> dict[str, str]:
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


async def is_public_http_url(target: str) -> bool:
    origin = normalized_origin(target)
    if origin is None:
        return False
    _, hostname, port = origin
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global for item in addresses)
    except (OSError, ValueError):
        return False


def ensure_dimensions(width: float, height: float, scale: float, limits: RenderLimits) -> None:
    output_width, output_height = math.ceil(width * scale), math.ceil(height * scale)
    requested, allowed = sorted((output_width, output_height)), sorted((limits.max_width, limits.max_height))
    if requested[0] > allowed[0] or requested[1] > allowed[1]:
        raise RenderError("output_dimensions_exceeded", "The requested output dimensions exceed the limit.", 413, False)
    if output_width * output_height > limits.max_pixels:
        raise RenderError("pixel_limit_exceeded", "The requested output exceeds the pixel limit.", 413, False)


async def load_lazy_content(page: Page, viewport_height: int) -> None:
    position = 0
    stable = 0
    try:
        for _ in range(40):
            height = math.ceil(await page.evaluate("Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)"))
            bottom = max(0, height - viewport_height)
            if position >= bottom:
                stable += 1
                if stable >= 2:
                    break
            else:
                position = min(position + max(1, viewport_height * 4 // 5), bottom)
                stable = 0
            await page.evaluate("y => window.scrollTo(0, y)", position)
            await asyncio.sleep(0.2)
    finally:
        with suppress(Exception):
            await page.evaluate("window.scrollTo(0, 0)")


async def capture_webp(
    page: Page,
    *,
    clip: dict[str, float],
    quality: int | None,
    transparent: bool,
) -> bytes:
    """Use Chromium's native WebP encoder, which Playwright does not expose."""
    session = await page.context.new_cdp_session(page)
    try:
        with suppress(Exception):
            await page.evaluate("() => document.getAnimations().forEach(a => a.pause())")
        if transparent:
            await session.send(
                "Emulation.setDefaultBackgroundColorOverride",
                {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
            )
        result = await session.send(
            "Page.captureScreenshot",
            {
                "format": "webp",
                "quality": quality if quality is not None else 80,
                "fromSurface": True,
                "captureBeyondViewport": True,
                "clip": clip,
            },
        )
        return b64decode(result["data"])
    finally:
        if transparent:
            with suppress(Exception):
                await session.send("Emulation.setDefaultBackgroundColorOverride")
        with suppress(Exception):
            await session.detach()


class RenderEngine:
    def __init__(self, *, hosted: bool,
                 challenge_checker: Callable[[Page, bool, int | None], Awaitable[None]] | None = None,
                 browser_replacer: Callable[[Browser], Awaitable[None]] | None = None) -> None:
        self.hosted = hosted
        self.challenge_checker = challenge_checker
        self.browser_replacer = browser_replacer

    async def render_image(self, browser: Browser, request: RenderRequest, limits: RenderLimits) -> RenderArtifact:
        target = str(request.url)
        if self.hosted and not await is_public_http_url(target):
            raise RenderError("target_not_public", "Private or non-public target URLs are blocked.", 400, False)
        ensure_dimensions(request.viewport.width, request.viewport.height, request.viewport.device_scale_factor, limits)
        if request.wait_for.timeout_ms > limits.wait_timeout_ms or request.wait_for.delay_ms > limits.delay_ms:
            raise RenderError("wait_limit_exceeded", "The requested wait exceeds the limit.", 413, False)
        context = None
        blocked_urls: list[str] = []
        try:
            async with asyncio.timeout(limits.deadline_seconds):
                context = await browser.new_context(viewport={"width": request.viewport.width, "height": request.viewport.height}, device_scale_factor=request.viewport.device_scale_factor, service_workers="block" if self.hosted else "allow")

                async def route_request(route) -> None:
                    request_url = route.request.url
                    scheme = urlsplit(request_url).scheme.lower()
                    if scheme in {"about", "blob", "data"}:
                        await route.continue_()
                    elif self.hosted and not await is_public_http_url(request_url):
                        blocked_urls.append(request_url)
                        await route.abort("blockedbyclient")
                    else:
                        await route.continue_(headers=routed_headers(request_url, target, dict(route.request.headers), request.headers))

                await context.route("**/*", route_request)
                if self.hosted:
                    async def block_web_socket(web_socket) -> None:
                        blocked_urls.append(web_socket.url)
                        await web_socket.close(code=1008, reason="Blocked in hosted mode")
                    await context.route_web_socket("**/*", block_web_socket)
                page = await context.new_page()
                try:
                    navigation = await page.goto(target, wait_until=request.wait_for.event.value, timeout=min(request.wait_for.timeout_ms, limits.wait_timeout_ms))
                except PlaywrightTimeoutError as exc:
                    raise RenderError("target_timeout", "The target did not become ready in time.", 504, True) from exc
                if self.hosted and not await is_public_http_url(page.url):
                    raise RenderError("redirect_not_public", "The target redirected to a private or non-public URL.", 400, False)
                if request.wait_for.selector:
                    await page.locator(request.wait_for.selector).wait_for(state="visible", timeout=min(request.wait_for.timeout_ms, limits.wait_timeout_ms))
                if request.wait_for.text:
                    await page.wait_for_function("text => Boolean(document.body?.innerText.includes(text))", arg=request.wait_for.text, timeout=min(request.wait_for.timeout_ms, limits.wait_timeout_ms))
                if request.wait_for.delay_ms:
                    await page.wait_for_timeout(request.wait_for.delay_ms)
                if self.challenge_checker:
                    await self.challenge_checker(page, request.proceed_on_captcha, navigation.status if navigation else None)
                if request.full_page:
                    await load_lazy_content(page, request.viewport.height)
                    if self.challenge_checker:
                        await self.challenge_checker(page, request.proceed_on_captcha, navigation.status if navigation else None)
                options: dict[str, object] = {"type": request.output.value, "animations": "disabled", "omit_background": request.image.transparent_background}
                if request.image.quality is not None:
                    options["quality"] = request.image.quality
                if request.selector:
                    locator = page.locator(request.selector).first
                    if not await locator.is_visible() or not (box := await locator.bounding_box()):
                        raise RenderError("selector_not_found", "The capture selector did not resolve to a visible element.", 404, False)
                    ensure_dimensions(box["width"], box["height"], request.viewport.device_scale_factor, limits)
                    width, height = box["width"], box["height"]
                else:
                    width, height = request.viewport.width, request.viewport.height
                    if request.full_page:
                        size = await page.evaluate("() => ({width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0), height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)})")
                        width, height = max(float(size["width"]), width), max(float(size["height"]), height)
                        if height > limits.max_full_page_height:
                            raise RenderError("page_too_tall", "The page is too tall to capture safely.", 413, False)
                        ensure_dimensions(width, height, request.viewport.device_scale_factor, limits)
                if request.output is OutputFormat.WEBP:
                    image = await capture_webp(
                        page,
                        clip={
                            "x": float(box["x"]) if request.selector else 0,
                            "y": float(box["y"]) if request.selector else 0,
                            "width": float(width),
                            "height": float(height),
                            "scale": request.viewport.device_scale_factor,
                        },
                        quality=request.image.quality,
                        transparent=request.image.transparent_background,
                    )
                elif request.selector:
                    image = await locator.screenshot(**options)
                else:
                    image = await page.screenshot(full_page=request.full_page, **options)
                if not image:
                    raise RenderError("empty_output", "The renderer produced an empty image.", 502, True)
                if len(image) > limits.output_bytes:
                    raise RenderError("output_too_large", "The rendered image exceeds the output limit.", 413, False)
                return RenderArtifact(image, MEDIA_TYPES[request.output], f"vipercapture.{EXTENSIONS[request.output]}", {"width": math.ceil(width), "height": math.ceil(height)})
        except PlaywrightTimeoutError as exc:
            raise RenderError("wait_timeout", "A wait condition timed out.", 504, True) from exc
        except TimeoutError as exc:
            raise RenderError("render_timeout", "The render exceeded its total deadline.", 504, True) from exc
        except RenderError:
            raise
        except Exception as exc:
            if blocked_urls:
                raise RenderError("subresource_not_public", "The page requested a private or non-public resource.", 400, False) from exc
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
