#!/usr/bin/env python3
"""Reproducible cross-provider latency and output benchmark.

No provider is contacted unless explicitly named on the command line. Results are
machine-readable and include the exact scenario set and runtime environment.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

import httpx
from PIL import Image


ROOT = Path(__file__).resolve().parent


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def rotated_provider_indexes(provider_count: int, offset: int) -> list[int]:
    """Return a deterministic rotation so no provider always runs first."""
    if provider_count < 1:
        return []
    start = offset % provider_count
    return list(range(start, provider_count)) + list(range(0, start))


async def render(client: httpx.AsyncClient, provider: str, endpoint: str, scenario: dict) -> bytes:
    lazy_load = scenario.get("lazy_load", "none")
    if provider != "viper" and lazy_load != "none":
        raise ValueError(
            f"{provider} benchmark adapter cannot preserve lazy_load={lazy_load}"
        )
    if provider == "viper":
        response = await client.post(endpoint.rstrip("/") + "/v1/render", json=scenario)
        response.raise_for_status()
        return response.content
    if provider == "screenshotone":
        key = os.environ.get("SCREENSHOTONE_ACCESS_KEY")
        if not key:
            raise RuntimeError("SCREENSHOTONE_ACCESS_KEY is required")
        wait = scenario.get("wait_for", {})
        if wait.get("text"):
            raise ValueError(
                "ScreenshotOne benchmark adapter cannot preserve wait_for.text"
            )
        wait_event = {
            "domcontentloaded": "domcontentloaded",
            "load": "load",
            "networkidle": "networkidle0",
        }.get(wait.get("event", "load"))
        if wait_event is None:
            raise ValueError(f"unsupported wait_for.event: {wait.get('event')}")
        payload = {
            "access_key": key,
            "url": scenario["url"],
            "format": scenario.get("output", "png"),
            "viewport_width": scenario["viewport"]["width"],
            "viewport_height": scenario["viewport"]["height"],
            "device_scale_factor": scenario["viewport"].get("device_scale_factor", 1),
            "full_page": scenario.get("full_page", True),
            "wait_until": wait_event,
            "timeout": wait.get("timeout_ms", 15_000) / 1_000,
        }
        if wait.get("delay_ms"):
            payload["delay"] = wait["delay_ms"] / 1_000
        if wait.get("selector"):
            payload["wait_for_selector"] = wait["selector"]
            payload["error_on_selector_not_found"] = True
        response = await client.post(endpoint, json=payload)
        response.raise_for_status()
        return response.content
    if provider == "urlbox":
        key = os.environ.get("URLBOX_SECRET")
        if not key:
            raise RuntimeError("URLBOX_SECRET is required")
        device_scale_factor = scenario["viewport"].get("device_scale_factor", 1)
        if device_scale_factor not in {1, 2}:
            raise ValueError(
                "Urlbox benchmark adapter supports device_scale_factor 1 or 2"
            )
        wait = scenario.get("wait_for", {})
        if wait.get("text"):
            raise ValueError("Urlbox benchmark adapter cannot preserve wait_for.text")
        wait_event = {
            "domcontentloaded": "domloaded",
            "load": "loaded",
            "networkidle": "requestsfinished",
        }.get(wait.get("event", "load"))
        if wait_event is None:
            raise ValueError(f"unsupported wait_for.event: {wait.get('event')}")
        payload = {
            "url": scenario["url"],
            "format": scenario.get("output", "png"),
            "width": scenario["viewport"]["width"],
            "height": scenario["viewport"]["height"],
            "retina": device_scale_factor == 2,
            "full_page": scenario.get("full_page", True),
            "wait_until": wait_event,
            "timeout": wait.get("timeout_ms", 15_000),
        }
        if wait.get("delay_ms"):
            payload["delay"] = wait["delay_ms"]
        if wait.get("selector"):
            payload["wait_for"] = wait["selector"]
            payload["wait_for_state"] = "visible"
            payload["wait_timeout"] = wait.get("timeout_ms", 15_000)
            payload["fail_if_selector_missing"] = True
        response = await client.post(endpoint, json=payload, headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        result = response.json()
        download = await client.get(result["renderUrl"])
        download.raise_for_status()
        return download.content
    if provider == "browserless":
        wait = scenario.get("wait_for", {})
        if wait.get("text"):
            raise ValueError(
                "Browserless benchmark adapter cannot preserve wait_for.text"
            )
        wait_event = wait.get("event", "load")
        browserless_event = {
            "domcontentloaded": "domcontentloaded",
            "load": "load",
            "networkidle": "networkidle0",
        }.get(wait_event)
        if browserless_event is None:
            raise ValueError(f"unsupported wait_for.event: {wait_event}")
        payload = {
            "url": scenario["url"],
            "viewport": {
                "width": scenario["viewport"]["width"],
                "height": scenario["viewport"]["height"],
                "deviceScaleFactor": scenario["viewport"].get(
                    "device_scale_factor", 1
                ),
            },
            "gotoOptions": {
                "waitUntil": browserless_event,
                "timeout": wait.get("timeout_ms", 15_000),
            },
            "options": {
                "type": "jpeg" if scenario.get("output") == "jpeg" else scenario.get("output", "png"),
                "fullPage": scenario.get("full_page", True),
            },
        }
        if wait.get("delay_ms"):
            payload["waitForTimeout"] = wait["delay_ms"]
        if wait.get("selector"):
            payload["waitForSelector"] = {
                "selector": wait["selector"],
                "timeout": wait.get("timeout_ms", 15_000),
                "visible": True,
            }
        token = os.environ.get("BROWSERLESS_TOKEN")
        target = endpoint.rstrip("/") + "/chromium/screenshot"
        if token:
            target += "?token=" + token
        response = await client.post(target, json=payload)
        response.raise_for_status()
        return response.content
    raise ValueError(f"unknown provider: {provider}")


async def run_benchmark(
    client: httpx.AsyncClient,
    providers: list[tuple[str, str]],
    scenarios: list[dict],
    runs: int,
    warmups: int,
) -> list[dict]:
    provider_results = [
        {"type": kind, "endpoint": endpoint, "cases": []}
        for kind, endpoint in providers
    ]
    for scenario_index, scenario in enumerate(scenarios):
        cases = [
            {"samples": [], "failures": [], "artifacts": []}
            for _ in providers
        ]
        for index in range(warmups + runs):
            order = rotated_provider_indexes(
                len(providers), scenario_index + index
            )
            for provider_index in order:
                kind, endpoint = providers[provider_index]
                case = cases[provider_index]
                started = time.perf_counter()
                try:
                    body = await render(client, kind, endpoint, scenario["request"])
                    elapsed = (time.perf_counter() - started) * 1_000
                    with Image.open(io.BytesIO(body)) as image:
                        dimensions = list(image.size)
                        image.verify()
                    if index >= warmups:
                        case["samples"].append(round(elapsed, 2))
                        case["artifacts"].append({"bytes": len(body), "sha256": sha256(body).hexdigest(), "dimensions": dimensions})
                except Exception as exc:
                    if index >= warmups:
                        case["failures"].append(type(exc).__name__)
        for provider_index, case in enumerate(cases):
            samples = case["samples"]
            failures = case["failures"]
            provider_results[provider_index]["cases"].append({
                "name": scenario["name"],
                "successes": len(samples),
                "failures": failures,
                "success_rate": len(samples) / runs,
                "latency_ms": {
                    "samples": samples,
                    "median": round(statistics.median(samples), 2) if samples else None,
                    "p95": round(percentile(samples, 0.95), 2) if samples else None,
                },
                "artifacts": case["artifacts"],
            })
    return provider_results


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        action="append",
        required=True,
        metavar="TYPE=URL",
        help="viper, browserless, screenshotone, or urlbox endpoint; repeat to compare",
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--scenarios", type=Path, default=ROOT / "scenarios.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.runs <= 100 or not 0 <= args.warmups <= 10:
        parser.error("runs must be 1..100 and warmups 0..10")
    scenarios = json.loads(args.scenarios.read_text("utf-8"))
    providers = []
    for specification in args.provider:
        kind, separator, endpoint = specification.partition("=")
        if not separator or kind not in {"viper", "browserless", "screenshotone", "urlbox"}:
            parser.error("provider must be TYPE=URL")
        providers.append((kind, endpoint))

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "configuration": {"runs": args.runs, "warmups": args.warmups, "scenarios": scenarios},
        "providers": [],
    }
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        report["providers"] = await run_benchmark(
            client, providers, scenarios, args.runs, args.warmups
        )
    serialized = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(serialized, "utf-8")
    sys.stdout.write(serialized)
    return 0 if all(not case["failures"] for provider in report["providers"] for case in provider["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
