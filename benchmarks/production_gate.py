#!/usr/bin/env python3
"""Operational readiness probes for sustained load, crash recovery, and cgroups.

These probes intentionally run outside the unit-test process. They exercise a real
Uvicorn server and real browser processes, emit machine-readable evidence, and fail
when their declared acceptance threshold is missed.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4

import httpx
from PIL import Image
import io


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _cgroup_number(name: str) -> int | None:
    path = Path("/sys/fs/cgroup") / name
    try:
        value = path.read_text("ascii").strip()
    except OSError:
        return None
    if value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None


class LocalServer:
    def __init__(self, port: int, data_dir: Path) -> None:
        self.port = port
        self.data_dir = data_dir
        self.process: subprocess.Popen[bytes] | None = None
        self.log_path = data_dir / "operational-server.log"

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "VIPERCAPTURE_ASYNC_JOBS": "1",
                "VIPERCAPTURE_SCHEDULES": "0",
                "VIPERCAPTURE_DATA_DIR": str(self.data_dir),
                "VIPERCAPTURE_CAPTURES_DIR": str(self.data_dir / "captures"),
                "VIPERCAPTURE_MAX_CONCURRENCY": "2",
                "VIPERCAPTURE_JOB_WORKERS": "1",
                "VIPERCAPTURE_JOB_POLL_SECONDS": "1",
            }
        )
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "vipercapture.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        log.close()
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(120):
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"server exited during startup; see {self.log_path}"
                    )
                try:
                    response = await client.get(self.endpoint + "/ready")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
        raise TimeoutError(f"server did not become ready; see {self.log_path}")

    async def crash(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=10)

    async def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        if os.name == "nt":
            self.process.terminate()
        else:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            await self.crash()


async def sustained_load(
    endpoint: str,
    *,
    requests: int,
    concurrency: int,
    timeout: float,
    duration_seconds: float = 0,
) -> dict[str, Any]:
    payload = {
        "html": (
            "<!doctype html><meta charset=utf-8><style>"
            "body{font:16px system-ui;background:#10131a;color:#f4f6ff;padding:48px}"
            ".card{width:560px;padding:32px;border:1px solid #556;border-radius:16px}"
            "</style><div class=card><h1>ViperCapture load gate</h1>"
            "<p>Deterministic local rendering under sustained concurrency.</p></div>"
        ),
        "output": "png",
        "full_page": False,
        "viewport": {"width": 800, "height": 600, "device_scale_factor": 1},
        "deterministic": {"enabled": True, "random_seed": 42},
        "wait_for": {"event": "load"},
    }
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    failures: list[dict[str, Any]] = []
    sizes: list[int] = []
    dimensions: list[list[int]] = []
    started = time.perf_counter()
    deadline = started + duration_seconds
    peak_memory = _cgroup_number("memory.current")
    issued = 0

    async with httpx.AsyncClient(timeout=timeout) as client:
        async def one(index: int) -> None:
            nonlocal peak_memory
            async with semaphore:
                request_started = time.perf_counter()
                try:
                    response = await client.post(
                        endpoint.rstrip("/") + "/v1/render", json=payload
                    )
                    response.raise_for_status()
                    with Image.open(io.BytesIO(response.content)) as image:
                        current_dimensions = list(image.size)
                        image.verify()
                    latencies.append((time.perf_counter() - request_started) * 1000)
                    sizes.append(len(response.content))
                    dimensions.append(current_dimensions)
                except Exception as exc:
                    failures.append({"index": index, "error": type(exc).__name__, "message": str(exc)[:500]})
                current = _cgroup_number("memory.current")
                if current is not None:
                    peak_memory = max(peak_memory or 0, current)

        async def worker() -> None:
            nonlocal issued
            while issued < requests or time.perf_counter() < deadline:
                index = issued
                issued += 1
                await one(index)

        await asyncio.gather(*(worker() for _ in range(concurrency)))

    elapsed = time.perf_counter() - started
    completed = len(latencies) + len(failures)
    return {
        "minimum_requests": requests,
        "duration_target_seconds": duration_seconds,
        "requests": completed,
        "concurrency": concurrency,
        "successes": len(latencies),
        "failures": failures,
        "success_rate": len(latencies) / completed if completed else 0,
        "elapsed_seconds": round(elapsed, 3),
        "throughput_per_second": round(len(latencies) / elapsed, 3),
        "latency_ms": {
            "median": round(statistics.median(latencies), 2) if latencies else None,
            "p95": round(percentile(latencies, 0.95), 2) if latencies else None,
            "p99": round(percentile(latencies, 0.99), 2) if latencies else None,
        },
        "artifact_bytes": {
            "minimum": min(sizes) if sizes else None,
            "maximum": max(sizes) if sizes else None,
        },
        "dimensions": sorted({tuple(item) for item in dimensions}),
        "memory": {
            "cgroup_limit_bytes": _cgroup_number("memory.max"),
            "peak_cgroup_bytes": peak_memory,
        },
    }


async def restart_recovery(port: int, data_dir: Path) -> dict[str, Any]:
    server = LocalServer(port, data_dir)
    job_id = None
    first_attempt = None
    try:
        await server.start()
        payload = {
            "html": "<!doctype html><h1>restart recovery</h1>",
            "output": "png",
            "full_page": False,
            "viewport": {"width": 640, "height": 480},
            "actions": [{"type": "wait", "delay_ms": 10000}],
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                server.endpoint + "/v1/jobs",
                json=payload,
                headers={"X-Request-Id": f"recovery-{uuid4()}"},
            )
            response.raise_for_status()
            job_id = response.json()["id"]
            for _ in range(120):
                status = await client.get(server.endpoint + f"/v1/jobs/{job_id}")
                status.raise_for_status()
                document = status.json()
                if document["status"] == "running":
                    first_attempt = document.get("attempt_count")
                    break
                await asyncio.sleep(0.1)
            else:
                raise TimeoutError("job did not enter running state before crash")

        await server.crash()
        await server.start()
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(180):
                status = await client.get(server.endpoint + f"/v1/jobs/{job_id}")
                status.raise_for_status()
                document = status.json()
                if document["status"] == "succeeded":
                    result = await client.get(server.endpoint + f"/v1/jobs/{job_id}/result")
                    result.raise_for_status()
                    with Image.open(io.BytesIO(result.content)) as image:
                        dimensions = list(image.size)
                        image.verify()
                    return {
                        "job_id": job_id,
                        "status": document["status"],
                        "attempt_before_crash": first_attempt,
                        "attempt_after_restart": document.get("attempt_count"),
                        "artifact_bytes": len(result.content),
                        "dimensions": dimensions,
                    }
                if document["status"] in {"failed", "cancelled", "expired"}:
                    raise RuntimeError(f"recovered job became {document['status']}: {document}")
                await asyncio.sleep(0.25)
        raise TimeoutError("recovered job did not finish after restart")
    finally:
        await server.close()


def _write(report: dict[str, Any], output: Path | None) -> None:
    serialized = json.dumps(report, indent=2, default=list) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, "utf-8")
    sys.stdout.write(serialized)


async def _restart_recovery_gate(
    port: int,
    data_dir: Path,
    output: Path | None,
    generated_at: str,
) -> int:
    diagnostics = {
        "data_dir": str(data_dir),
        "server_log": str(data_dir / "operational-server.log"),
    }
    try:
        result = await restart_recovery(port, data_dir)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "generated_at": generated_at,
            "gate": "restart-recovery",
            "result": None,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "diagnostics": diagnostics,
        }
        _write(report, output)
        return 1
    report = {
        "schema_version": 1,
        "generated_at": generated_at,
        "gate": "restart-recovery",
        "result": result,
        "diagnostics": diagnostics,
    }
    _write(report, output)
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load")
    load.add_argument("--api-url", default="http://127.0.0.1:8000")
    load.add_argument("--requests", type=int, default=120)
    load.add_argument("--concurrency", type=int, default=4)
    load.add_argument("--timeout", type=float, default=120)
    load.add_argument("--duration-seconds", type=float, default=0)
    load.add_argument("--min-success-rate", type=float, default=1.0)
    load.add_argument("--max-p95-ms", type=float, default=15000)
    load.add_argument("--require-memory-limit", action="store_true")
    load.add_argument("--output", type=Path)

    recovery = subparsers.add_parser("restart-recovery")
    recovery.add_argument("--port", type=int, default=8017)
    recovery.add_argument("--data-dir", type=Path)
    recovery.add_argument("--output", type=Path)
    args = parser.parse_args()

    generated_at = datetime.now(timezone.utc).isoformat()
    if args.command == "load":
        if (
            args.requests < 1
            or args.concurrency < 1
            or args.concurrency > args.requests
            or args.duration_seconds < 0
        ):
            parser.error("requests and concurrency must be positive, and concurrency <= requests")
        result = await sustained_load(
            args.api_url,
            requests=args.requests,
            concurrency=args.concurrency,
            timeout=args.timeout,
            duration_seconds=args.duration_seconds,
        )
        report = {"schema_version": 1, "generated_at": generated_at, "gate": "sustained-load", "result": result}
        _write(report, args.output)
        memory_ok = not args.require_memory_limit or result["memory"]["cgroup_limit_bytes"] is not None
        latency = result["latency_ms"]["p95"]
        return 0 if (
            result["success_rate"] >= args.min_success_rate
            and latency is not None
            and latency <= args.max_p95_ms
            and memory_ok
        ) else 1

    if args.data_dir:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        return await _restart_recovery_gate(
            args.port, args.data_dir, args.output, generated_at
        )
    if args.output:
        data_dir = args.output.parent / f"{args.output.stem}-data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return await _restart_recovery_gate(
            args.port, data_dir, args.output, generated_at
        )
    else:
        with tempfile.TemporaryDirectory(prefix="vipercapture-recovery-") as directory:
            return await _restart_recovery_gate(
                args.port, Path(directory), args.output, generated_at
            )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
