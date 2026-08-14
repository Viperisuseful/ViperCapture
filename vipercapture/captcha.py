"""Challenge detection and an operator-supplied CAPTCHA handler hook."""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from .render_errors import RenderError

CaptchaHandler = Callable[[Any, dict[str, object], str | None, int], Awaitable[bool]]

DETECT_CHALLENGE_SCRIPT = r"""({ status }) => {
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
        const ratio = area / viewportArea;
        return ratio >= 0.25 || (coversCenter && ratio >= 0.10);
    };
    const roots = [document];
    for (let index = 0; index < roots.length && roots.length < 256; index += 1) {
        for (const element of roots[index].querySelectorAll("*")) {
            if (element.shadowRoot && roots.length < 256) roots.push(element.shadowRoot);
        }
    }
    const query = (selectors) => selectors.flatMap((selector) =>
        roots.flatMap((root) => [...root.querySelectorAll(selector)]).filter(visible));
    const providers = {
        cloudflare: {
            widgets: [".cf-turnstile", "iframe[src*='challenges.cloudflare.com']"],
            blocking: ["#challenge-stage", "#challenge-running", "#challenge-form",
                "iframe[src*='/cdn-cgi/challenge-platform/']"]
        },
        recaptcha: {
            widgets: [".g-recaptcha", "[data-sitekey][data-callback]",
                "iframe[src*='google.com/recaptcha']", "iframe[src*='recaptcha.net/recaptcha']"],
            blocking: ["iframe[src*='/recaptcha/api2/bframe']", "iframe[src*='/recaptcha/enterprise/bframe']"]
        },
        hcaptcha: {
            widgets: [".h-captcha", "iframe[src*='hcaptcha.com/captcha']"],
            blocking: ["iframe[src*='newassets.hcaptcha.com/captcha']"]
        },
        funcaptcha: {
            widgets: [".arkose", "[data-pkey]", "iframe[src*='arkoselabs.com']",
                "iframe[src*='funcaptcha.com']"],
            blocking: ["iframe[src*='/fc/gc/']"]
        },
        datadome: {
            widgets: ["iframe[src*='captcha-delivery.com']", "#datadome-captcha"],
            blocking: ["iframe[src*='geo.captcha-delivery.com']"]
        },
        aws_waf: {
            widgets: ["#aws-waf-captcha-container", "[data-aws-waf-captcha]",
                "script[src*='awswaf.com']"],
            blocking: ["iframe[src*='awswaf.com']"]
        },
        geetest: {
            widgets: [".geetest_holder", ".geetest_panel", "[class*='geetest_']"],
            blocking: [".geetest_panel"]
        },
        friendlycaptcha: {
            widgets: [".frc-captcha", "[data-sitekey][class*='frc-']"],
            blocking: []
        },
        mtcaptcha: {
            widgets: [".mtcaptcha", "iframe[src*='mtcaptcha.com']"],
            blocking: ["iframe[src*='mtcaptcha.com']"]
        },
        imperva: {
            widgets: ["iframe[src*='incapsula.com']", "iframe[src*='_Incapsula_Resource']"],
            blocking: ["#incapsula-incident-id", "iframe[src*='incapsula.com']"]
        },
        perimeterx: {
            widgets: ["#px-captcha", "iframe[src*='perimeterx.net']", "iframe[src*='humansecurity.com']"],
            blocking: ["#px-captcha"]
        }
    };
    const title = (document.title || "").toLowerCase();
    const bodyText = (document.body?.innerText || "").slice(0, 30000).toLowerCase();
    const challengePhrases = [
        "checking your browser", "verify you are human", "verification required",
        "complete the security check", "performing security verification",
        "unusual traffic", "attention required", "security challenge",
        "prove you are human", "confirm you are human", "bot verification",
        "press and hold", "slide to verify"
    ];
    const challengeText = challengePhrases.some((phrase) => title.includes(phrase)) ||
        (bodyText.length <= 5000 && challengePhrases.some((phrase) => bodyText.includes(phrase)));
    const signals = [];
    let widgetMatch = null;
    let blockingMatch = null;
    for (const [name, selectors] of Object.entries(providers)) {
        const widgets = query(selectors.widgets);
        const blocking = query(selectors.blocking);
        if (!widgets.length && !blocking.length) continue;
        const elements = [...widgets, ...blocking];
        const obstructed = elements.some(obstruction);
        const match = {name, elements, widgets, blocking, obstructed};
        if (blocking.length || obstructed) {
            blockingMatch = match;
            break;
        }
        if (!widgetMatch) widgetMatch = match;
    }
    const match = blockingMatch || widgetMatch;
    const provider = match?.name || null;
    const elements = match?.elements || [];
    const hasBlockingElement = Boolean(match?.blocking.length);
    const hasObstruction = Boolean(match?.obstructed);
    if (match?.widgets.length) signals.push("provider_widget");
    if (hasBlockingElement) signals.push("challenge_form");
    if (hasObstruction) signals.push("viewport_obstruction");
    if (status === 429) signals.push("main_response_429");
    else if ([401, 403, 503].includes(status)) signals.push(`main_response_${status}`);
    if (challengeText) signals.push("challenge_copy");
    const current = location.href.toLowerCase();
    const challengeUrl = /captcha|challenge|verify/.test(current);
    if (challengeUrl) signals.push("challenge_url");

    let kind = null;
    const blockingSignal = hasBlockingElement || hasObstruction || challengeText ||
        (challengeUrl && [401, 403, 429, 503].includes(status));
    if (status === 429 && blockingSignal) kind = "rate_limited";
    else if ([401, 403, 503].includes(status) && blockingSignal) kind = "access_denied";
    else if (blockingSignal) kind = "blocking_interstitial";
    else if (provider) kind = "embedded_widget";
    if (!kind) return null;

    let sitekey = null;
    for (const element of elements) {
        sitekey = element.getAttribute("data-sitekey") || element.getAttribute("data-pkey");
        if (sitekey) break;
        const src = element.getAttribute("src");
        if (!src) continue;
        try {
            const url = new URL(src, location.href);
            sitekey = url.searchParams.get("k") || url.searchParams.get("sitekey") ||
                url.searchParams.get("public_key");
            if (sitekey) break;
        } catch {}
    }
    const confidence = kind === "embedded_widget" ? 0.72 :
        (provider && signals.length >= 2 ? 0.98 : 0.88);
    return {provider: provider || "unknown", kind, confidence, signals,
        ...(sitekey ? {sitekey} : {})};
}"""


def load_captcha_handler(spec: str) -> CaptchaHandler | None:
    """Load an operator hook from ``module:function`` without bundling a solver."""
    if not spec:
        return None
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(
            "VIPERCAPTURE_CAPTCHA_HANDLER_FACTORY must use module:function syntax"
        )
    handler = getattr(importlib.import_module(module_name), attribute)()
    if not callable(handler):
        raise TypeError("CAPTCHA handler factory must return an async callable")
    return handler


async def detect_challenge(
    page: Any, navigation_status: int | None
) -> dict[str, object] | None:
    return await page.evaluate(DETECT_CHALLENGE_SCRIPT, {"status": navigation_status})


async def handle_challenge(
    page: Any,
    *,
    navigation_status: int | None,
    action: str,
    handler: CaptchaHandler | None,
    solver: str | None,
    timeout_ms: int,
) -> None:
    challenge = await detect_challenge(page, navigation_status)
    if not challenge or challenge.get("kind") == "embedded_widget":
        return
    if action == "capture":
        return
    if action == "external":
        if handler is None:
            raise RenderError(
                "captcha_handler_unavailable",
                "No external CAPTCHA handler is configured.",
                503,
                False,
                challenge,
            )
        solved = handler(page, challenge, solver, timeout_ms)
        if not inspect.isawaitable(solved):
            raise TypeError("CAPTCHA handlers must return an awaitable")
        try:
            cleared = await asyncio.wait_for(solved, timeout=timeout_ms / 1_000)
        except TimeoutError as exc:
            raise RenderError(
                "captcha_handler_timeout",
                "The configured CAPTCHA handler timed out.",
                504,
                True,
                challenge,
            ) from exc
        if cleared:
            remaining = await detect_challenge(page, navigation_status)
            if not remaining or remaining.get("kind") == "embedded_widget":
                return
            challenge = remaining
        raise RenderError(
            "captcha_handler_failed",
            "The configured CAPTCHA handler did not clear the challenge.",
            409,
            False,
            challenge,
        )

    provider = str(challenge.get("provider") or "unknown")
    provider_label = {
        "cloudflare": "Cloudflare",
        "recaptcha": "Google reCAPTCHA",
        "hcaptcha": "hCaptcha",
        "funcaptcha": "Arkose Labs",
        "datadome": "DataDome",
        "aws_waf": "AWS WAF",
        "geetest": "GeeTest",
        "friendlycaptcha": "Friendly Captcha",
        "mtcaptcha": "MTCaptcha",
        "imperva": "Imperva",
        "perimeterx": "HUMAN/PerimeterX",
        "unknown": "A page-level",
    }.get(provider, provider.replace("_", " ").title())
    raise RenderError(
        "captcha_detected",
        f"{provider_label} challenge blocked the page.",
        409,
        False,
        challenge,
    )
