"""Opt-in page cleanup controls shared by hosted and self-hosted rendering."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse


ConsentMode = Literal["none", "reject", "accept", "hide"]
BASE_DIR = Path(__file__).resolve().parent
AUTOCONSENT_DIR = BASE_DIR / "vendor" / "autoconsent"


@dataclass(frozen=True)
class CleanupOptions:
    consent_mode: ConsentMode = "none"
    block_ads: bool = False
    block_trackers: bool = False
    block_chats: bool = False
    block_newsletters: bool = False


@dataclass
class AutoConsentSession:
    mode: ConsentMode
    done: asyncio.Event
    outcome: dict[str, object]
    script: str = ""


AD_HOST_SUFFIXES = (
    "2mdn.net",
    "adnxs.com",
    "adsrvr.org",
    "amazon-adsystem.com",
    "criteo.com",
    "criteo.net",
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "openx.net",
    "pubmatic.com",
    "rubiconproject.com",
    "taboola.com",
    "teads.tv",
)

TRACKER_HOST_SUFFIXES = (
    "amplitude.com",
    "clarity.ms",
    "fullstory.com",
    "google-analytics.com",
    "googletagmanager.com",
    "heapanalytics.com",
    "hotjar.com",
    "mixpanel.com",
    "mouseflow.com",
    "newrelic.com",
    "segment.io",
    "segment.com",
    "sentry.io",
)

CHAT_HOST_SUFFIXES = (
    "crisp.chat",
    "drift.com",
    "driftt.com",
    "freshchat.com",
    "intercom.io",
    "intercomcdn.com",
    "livechatinc.com",
    "tawk.to",
    "userlike.com",
    "zendesk.com",
    "zopim.com",
)


def _matches_suffix(hostname: str, suffixes: tuple[str, ...]) -> bool:
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes)


def should_block_resource(url: str, options: CleanupOptions) -> str | None:
    """Return blocker category for known third-party resources, otherwise None."""
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not hostname:
        return None
    if options.block_ads and _matches_suffix(hostname, AD_HOST_SUFFIXES):
        return "ads"
    if options.block_trackers and _matches_suffix(hostname, TRACKER_HOST_SUFFIXES):
        return "trackers"
    if options.block_chats and _matches_suffix(hostname, CHAT_HOST_SUFFIXES):
        return "chats"
    return None


@lru_cache(maxsize=1)
def _autoconsent_assets() -> tuple[str, dict]:
    script = (AUTOCONSENT_DIR / "autoconsent.playwright.js").read_text(encoding="utf-8")
    rules = json.loads((AUTOCONSENT_DIR / "rules.json").read_text(encoding="utf-8"))
    return script, rules


async def setup_autoconsent(page, mode: ConsentMode) -> AutoConsentSession | None:
    if mode == "none":
        return None

    script, rules = _autoconsent_assets()
    session = AutoConsentSession(mode=mode, done=asyncio.Event(), outcome={}, script=script)
    auto_action = {"reject": "optOut", "accept": "optIn", "hide": None}[mode]
    config = {
        "enabled": True,
        "autoAction": auto_action,
        "disabledCmps": [],
        "enablePrehide": True,
        "enableCosmeticRules": True,
        "enableGeneratedRules": True,
        "detectRetries": 12,
        "isMainWorld": False,
        "prehideTimeout": 2000,
        "enableHeuristicDetection": True,
        "heuristicMode": "tier2",
        "logs": {
            "lifecycle": False,
            "rulesteps": False,
            "detectionsteps": False,
            "evals": False,
            "errors": False,
            "messages": False,
            "waits": False,
        },
    }

    async def send_to_page(message: dict) -> None:
        try:
            await page.evaluate(
                "message => window.autoconsentReceiveMessage?.(message)",
                message,
            )
        except Exception:
            pass

    async def handle_message(message) -> None:
        if not isinstance(message, dict):
            return
        message_type = message.get("type")
        if message_type == "init":
            await send_to_page({"type": "initResp", "config": config, "rules": rules})
        elif message_type == "eval":
            try:
                result = await page.evaluate(message.get("code", ""))
            except Exception:
                result = None
            await send_to_page({"type": "evalResp", "id": message.get("id"), "result": result})
        elif message_type in {"cmpDetected", "popupFound"}:
            session.outcome[message_type] = message.get("cmp")
        elif message_type in {"optOutResult", "optInResult"}:
            session.outcome[message_type] = bool(message.get("result"))
        elif message_type == "autoconsentDone":
            session.outcome["cmp"] = message.get("cmp")
            session.outcome["done"] = True
            session.done.set()
        elif message_type == "autoconsentError":
            session.outcome["error"] = True
            session.done.set()

    await page.expose_function("autoconsentSendMessage", handle_message)
    await page.add_init_script(script)
    return session


async def finish_autoconsent(page, session: AutoConsentSession | None) -> dict[str, object]:
    if session is None:
        return {"mode": "none"}
    try:
        await page.evaluate(session.script)
    except Exception:
        pass
    if session.mode == "hide":
        await asyncio.sleep(0.25)
        return {"mode": session.mode, **session.outcome}
    try:
        await asyncio.wait_for(session.done.wait(), timeout=2.5)
    except TimeoutError:
        session.outcome["timed_out"] = True
    return {"mode": session.mode, **session.outcome}


async def apply_visual_cleanup(page, options: CleanupOptions) -> None:
    selectors: list[str] = []
    if options.block_ads:
        selectors.extend([
            "[id^='google_ads_']", "[class*=' ad-container']", "[class^='ad-container']",
            "[data-ad]", "[data-ad-slot]", "iframe[src*='doubleclick.net']",
        ])
    if options.block_chats:
        selectors.extend([
            "#intercom-container", ".intercom-lightweight-app", "#crisp-chatbox",
            "iframe[src*='tawk.to']", "iframe[src*='drift.com']", "[class*='chat-widget']",
        ])
    if options.block_newsletters:
        selectors.extend([
            "[class*='newsletter-modal']", "[class*='newsletter-popup']",
            "[id*='newsletter-modal']", "[id*='newsletter-popup']",
            "[class*='subscribe-modal']", "[class*='subscribe-popup']",
        ])
    if options.consent_mode == "hide":
        selectors.extend([
            "[class*='cookie-banner']", "[class*='cookie-consent']", "[id*='cookie-banner']",
            "[id*='cookie-consent']", "#onetrust-banner-sdk", ".qc-cmp2-container",
        ])
    if not selectors:
        return
    css = ",\n".join(dict.fromkeys(selectors)) + " { display: none !important; visibility: hidden !important; }"
    await page.add_style_tag(content=css)
    if options.block_newsletters:
        await page.evaluate("""() => {
            const candidates = document.querySelectorAll(
                "dialog, [role='dialog'], [aria-modal='true'], [class*='modal'], [class*='popup']"
            );
            const phrases = ["newsletter", "subscribe", "join our mailing list", "email updates"];
            for (const element of candidates) {
                const text = (element.innerText || "").slice(0, 3000).toLowerCase();
                if (phrases.some((phrase) => text.includes(phrase))) {
                    element.style.setProperty("display", "none", "important");
                    element.style.setProperty("visibility", "hidden", "important");
                }
            }
        }""")
