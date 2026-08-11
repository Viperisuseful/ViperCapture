#!/usr/bin/env python3
"""Turn a benchmark JSON document into a source-reviewable Markdown report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def markdown(report: dict, title: str) -> str:
    lines = [f"# {title}", "", "## Summary", ""]
    lines.extend(
        [
            "| Scenario | Provider | Success | Median | p95 | Median bytes |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for provider in report["providers"]:
        for case in provider["cases"]:
            artifacts = case.get("artifacts", [])
            median_bytes = "—"
            if artifacts:
                sizes = sorted(item["bytes"] for item in artifacts)
                median_bytes = f"{sizes[len(sizes) // 2]:,}"
            latency = case["latency_ms"]
            median = "—" if latency["median"] is None else f'{latency["median"]:,.2f} ms'
            p95 = "—" if latency["p95"] is None else f'{latency["p95"]:,.2f} ms'
            attempts = case["successes"] + len(case["failures"])
            lines.append(
                f'| {case["name"]} | {provider["type"]} | '
                f'{case["successes"]}/{attempts} | {median} | {p95} | {median_bytes} |'
            )
    environment = report["environment"]
    configuration = report["configuration"]
    lines.extend(
        [
            "",
            "## Method",
            "",
            f'- Generated at `{report["generated_at"]}`.',
            f'- Python `{environment["python"]}` on `{environment["platform"]}` '
            f'(`{environment["machine"]}`).',
            f'- `{configuration["runs"]}` measured runs after '
            f'`{configuration["warmups"]}` warm-up run(s) for every provider and scenario.',
            "- Providers were invoked sequentially from the same benchmark process and host.",
            "- Caches must be disabled or cold-equivalent; provider credentials are read from the environment.",
            "",
            "## Limitations",
            "",
            "Real sites and provider fleets change. These measurements are a dated engineering snapshot, "
            "not a universal latency or rendering-quality ranking. Review the JSON artifact for raw samples, "
            "hashes, dimensions, failures, and exact inputs.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title", default="ViperCapture benchmark")
    args = parser.parse_args()
    report = json.loads(args.input.read_text("utf-8"))
    args.output.write_text(markdown(report, args.title), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
