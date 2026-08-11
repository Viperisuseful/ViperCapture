#!/usr/bin/env python3
"""Render one deterministic PNG with every bundled Playwright engine."""

from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from vipercapture.render_contract import BrowserEngine, RenderRequest
from vipercapture.render_engine import RenderEngine, RenderLimits


async def main() -> None:
    limits = RenderLimits(1920, 1080, 2_073_600)
    async with async_playwright() as playwright:
        for engine in BrowserEngine:
            browser = await getattr(playwright, engine.value).launch(headless=True)
            try:
                artifact = await RenderEngine(hosted=False).render_image(
                    browser,
                    RenderRequest.model_validate(
                        {
                            "html": "<main><h1>ViperCapture</h1></main>",
                            "engine": engine.value,
                            "output": "png",
                            "full_page": False,
                            "viewport": {"width": 320, "height": 240},
                            "lazy_load": "none",
                        }
                    ),
                    limits,
                )
                assert artifact.body.startswith(b"\x89PNG\r\n\x1a\n"), engine.value
                print(f"{engine.value}: {len(artifact.body)} bytes")
            finally:
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
