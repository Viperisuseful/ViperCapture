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


async def render(client: httpx.AsyncClient, provider: str, endpoint: str, scenario: dict) -> bytes:
    if provider == "viper":
        response = await client.post(endpoint.rstrip("/") + "/v1/render", json=scenario)
        response.raise_for_status()
        return response.content
    if provider == "screenshotone":
        key = os.environ.get("SCREENSHOTONE_ACCESS_KEY")
        if not key:
            raise RuntimeError("SCREENSHOTONE_ACCESS_KEY is required")
        payload = {
            "access_key": key,
            "url": scenario["url"],
            "format": scenario.get("output", "png"),
            "viewport_width": scenario["viewport"]["width"],
            "viewport_height": scenario["viewport"]["height"],
            "full_page": scenario.get("full_page", True),
        }
        response = await client.post(endpoint, json=payload)
        response.raise_for_status()
        return response.content
    if provider == "urlbox":
        key = os.environ.get("URLBOX_SECRET")
        if not key:
            raise RuntimeError("URLBOX_SECRET is required")
        payload = {
            "url": scenario["url"],
            "format": scenario.get("output", "png"),
            "width": scenario["viewport"]["width"],
            "height": scenario["viewport"]["height"],
            "full_page": scenario.get("full_page", True),
        }
        response = await client.post(endpoint, json=payload, headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        result = response.json()
        download = await client.get(result["renderUrl"])
        download.raise_for_status()
        return download.content
    raise ValueError(f"unknown provider: {provider}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        action="append",
        required=True,
        metavar="TYPE=URL",
        help="viper, screenshotone, or urlbox endpoint; repeat to compare",
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
        if not separator or kind not in {"viper", "screenshotone", "urlbox"}:
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
        for kind, endpoint in providers:
            provider_result = {"type": kind, "endpoint": endpoint, "cases": []}
            for scenario in scenarios:
                samples = []
                failures = []
                artifacts = []
                for index in range(args.warmups + args.runs):
                    started = time.perf_counter()
                    try:
                        body = await render(client, kind, endpoint, scenario["request"])
                        elapsed = (time.perf_counter() - started) * 1_000
                        with Image.open(io.BytesIO(body)) as image:
                            dimensions = list(image.size)
                            image.verify()
                        if index >= args.warmups:
                            samples.append(round(elapsed, 2))
                            artifacts.append({"bytes": len(body), "sha256": sha256(body).hexdigest(), "dimensions": dimensions})
                    except Exception as exc:
                        if index >= args.warmups:
                            failures.append(type(exc).__name__)
                provider_result["cases"].append({
                    "name": scenario["name"],
                    "successes": len(samples),
                    "failures": failures,
                    "success_rate": len(samples) / args.runs,
                    "latency_ms": {
                        "samples": samples,
                        "median": round(statistics.median(samples), 2) if samples else None,
                        "p95": round(percentile(samples, 0.95), 2) if samples else None,
                    },
                    "artifacts": artifacts,
                })
            report["providers"].append(provider_result)
    serialized = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(serialized, "utf-8")
    sys.stdout.write(serialized)
    return 0 if all(not case["failures"] for provider in report["providers"] for case in provider["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
