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
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
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


def _cgroup_memory_sample() -> dict[str, int] | None:
    current = _cgroup_number("memory.current")
    if current is None:
        return None
    inactive_file = 0
    try:
        for line in (Path("/sys/fs/cgroup") / "memory.stat").read_text(
            "ascii"
        ).splitlines():
            name, value = line.split(maxsplit=1)
            if name == "inactive_file":
                inactive_file = int(value)
                break
    except (OSError, ValueError):
        inactive_file = 0
    return {
        "current_bytes": current,
        "inactive_file_bytes": inactive_file,
        "working_set_bytes": max(0, current - inactive_file),
    }


class LocalServer:
    def __init__(
        self,
        port: int,
        data_dir: Path,
        *,
        schedules: bool = False,
        webhook_secret: str = "",
    ) -> None:
        self.port = port
        self.data_dir = data_dir
        self.schedules = schedules
        self.webhook_secret = webhook_secret
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
                "VIPERCAPTURE_SCHEDULES": "1" if self.schedules else "0",
                "VIPERCAPTURE_DATA_DIR": str(self.data_dir),
                "VIPERCAPTURE_MAX_CONCURRENCY": "2",
                "VIPERCAPTURE_JOB_WORKERS": "1",
                "VIPERCAPTURE_JOB_POLL_SECONDS": "1",
            }
        )
        if self.webhook_secret:
            environment.update(
                {
                    "VIPERCAPTURE_WEBHOOK_SECRET": self.webhook_secret,
                    "VIPERCAPTURE_ALLOW_PRIVATE_WEBHOOKS": "1",
                }
            )
        else:
            environment.pop("VIPERCAPTURE_WEBHOOK_SECRET", None)
            environment.pop("VIPERCAPTURE_ALLOW_PRIVATE_WEBHOOKS", None)
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
    initial_memory = _cgroup_memory_sample()
    peak_working_set = (
        initial_memory["working_set_bytes"] if initial_memory is not None else None
    )
    memory_samples: list[dict[str, int | float]] = []
    stop_memory_sampling = asyncio.Event()
    issued = 0

    async def sample_memory() -> None:
        nonlocal peak_working_set
        next_record = 0.0
        while not stop_memory_sampling.is_set():
            sample = _cgroup_memory_sample()
            if sample is not None:
                peak_working_set = max(
                    peak_working_set or 0, sample["working_set_bytes"]
                )
                elapsed = time.perf_counter() - started
                if elapsed >= next_record:
                    memory_samples.append({
                        "elapsed_seconds": round(elapsed, 3),
                        **sample,
                    })
                    next_record = elapsed + 1
            try:
                await asyncio.wait_for(stop_memory_sampling.wait(), timeout=0.01)
            except TimeoutError:
                pass

    memory_sampler = asyncio.create_task(sample_memory())
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async def one(index: int) -> None:
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
                        failures.append(
                            {
                                "index": index,
                                "error": type(exc).__name__,
                                "message": str(exc)[:500],
                            }
                        )

            async def worker() -> None:
                nonlocal issued
                while issued < requests or time.perf_counter() < deadline:
                    index = issued
                    issued += 1
                    await one(index)

            await asyncio.gather(*(worker() for _ in range(concurrency)))
    finally:
        stop_memory_sampling.set()
        await memory_sampler

    cgroup_peak = _cgroup_number("memory.peak")

    memory_growth = None
    if len(memory_samples) >= 4:
        sample_values = [
            int(item["working_set_bytes"]) for item in memory_samples
        ]
        quarter = max(1, len(sample_values) // 4)
        memory_growth = int(
            statistics.median(sample_values[-quarter:])
            - statistics.median(sample_values[:quarter])
        )

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
            "peak_cgroup_bytes": cgroup_peak,
            "peak_working_set_bytes": peak_working_set,
            "first_to_last_quarter_median_working_set_growth_bytes": memory_growth,
            "samples": memory_samples,
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
                    first_attempt = document.get("attempts")
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
                        "attempt_after_restart": document.get("attempts"),
                        "artifact_bytes": len(result.content),
                        "dimensions": dimensions,
                    }
                if document["status"] in {"failed", "cancelled", "expired"}:
                    raise RuntimeError(f"recovered job became {document['status']}: {document}")
                await asyncio.sleep(0.25)
        raise TimeoutError("recovered job did not finish after restart")
    finally:
        await server.close()


class WebhookReceiver:
    def __init__(self) -> None:
        self.accepting = False
        self.accepted_job_ids: set[str] = set()
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        socket = self.server.sockets[0]
        self.port = int(socket.getsockname()[1])

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            length = 0
            for line in head.decode("ascii", "replace").split("\r\n")[1:]:
                name, separator, value = line.partition(":")
                if separator and name.lower() == "content-length":
                    length = int(value.strip())
                    break
            body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
            document = json.loads(body)
            job_id = str(document.get("job", {}).get("id", ""))
            if self.accepting and job_id:
                self.accepted_job_ids.add(job_id)
            status = b"204 No Content" if self.accepting else b"503 Unavailable"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


def _render_payload(*, delay_ms: int = 0) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "html": "<!doctype html><h1>recovery matrix</h1>",
        "output": "png",
        "full_page": False,
        "viewport": {"width": 640, "height": 480},
    }
    if delay_ms:
        payload["actions"] = [{"type": "wait", "delay_ms": delay_ms}]
    return payload


async def _submit_job(
    client: httpx.AsyncClient,
    server: LocalServer,
    payload: dict[str, Any],
) -> str:
    response = await client.post(
        server.endpoint + "/v1/jobs",
        json=payload,
        headers={"X-Request-Id": f"recovery-{uuid4()}"},
    )
    response.raise_for_status()
    return str(response.json()["id"])


async def _wait_for_job(
    client: httpx.AsyncClient,
    server: LocalServer,
    job_id: str,
    statuses: set[str],
    *,
    attempts: int = 320,
) -> dict[str, Any]:
    for _ in range(attempts):
        response = await client.get(server.endpoint + f"/v1/jobs/{job_id}")
        response.raise_for_status()
        document = response.json()
        if document["status"] in statuses:
            return document
        if document["status"] in {"failed", "cancelled", "expired"}:
            raise RuntimeError(f"job became {document['status']}: {document}")
        await asyncio.sleep(0.25)
    raise TimeoutError(f"job {job_id} did not reach {sorted(statuses)}")


async def _verify_job_result(
    client: httpx.AsyncClient, server: LocalServer, job_id: str
) -> dict[str, Any]:
    document = await _wait_for_job(client, server, job_id, {"succeeded"})
    response = await client.get(server.endpoint + f"/v1/jobs/{job_id}/result")
    response.raise_for_status()
    with Image.open(io.BytesIO(response.content)) as image:
        dimensions = list(image.size)
        image.verify()
    return {
        "job_id": job_id,
        "status": document["status"],
        "attempts": document.get("attempts"),
        "artifact_bytes": len(response.content),
        "dimensions": dimensions,
    }


async def _recovery_case(
    state: str,
    index: int,
    port: int,
    data_dir: Path,
) -> dict[str, Any]:
    case_dir = data_dir / f"{state}-{index + 1}"
    case_dir.mkdir(parents=True, mode=0o700)
    receiver = WebhookReceiver() if state == "webhook-pending" else None
    if receiver is not None:
        await receiver.start()
    server = LocalServer(
        port,
        case_dir,
        schedules=state == "scheduled",
        webhook_secret=("recovery-webhook-secret-32-bytes" if receiver else ""),
    )
    job_id = ""
    accepted_job_ids: list[str] = []
    before: dict[str, Any] = {}
    try:
        await server.start()
        async with httpx.AsyncClient(timeout=30) as client:
            if state == "queued":
                blocker_id = await _submit_job(
                    client, server, _render_payload(delay_ms=8000)
                )
                accepted_job_ids.append(blocker_id)
                await _wait_for_job(client, server, blocker_id, {"running"})
                job_id = await _submit_job(client, server, _render_payload())
                accepted_job_ids.append(job_id)
                before = await _wait_for_job(client, server, job_id, {"queued"})
            elif state == "running":
                job_id = await _submit_job(
                    client, server, _render_payload(delay_ms=8000)
                )
                accepted_job_ids.append(job_id)
                before = await _wait_for_job(client, server, job_id, {"running"})
            elif state == "succeeded":
                job_id = await _submit_job(client, server, _render_payload())
                accepted_job_ids.append(job_id)
                before = await _wait_for_job(client, server, job_id, {"succeeded"})
                await _verify_job_result(client, server, job_id)
            elif state == "webhook-pending":
                assert receiver is not None
                payload = _render_payload()
                payload["delivery"] = {
                    "webhook_url": f"http://127.0.0.1:{receiver.port}/events"
                }
                job_id = await _submit_job(client, server, payload)
                accepted_job_ids.append(job_id)
                before = await _wait_for_job(client, server, job_id, {"succeeded"})
            elif state == "scheduled":
                response = await client.post(
                    server.endpoint + "/v1/schedules",
                    json={
                        "name": f"recovery-{index + 1}",
                        "cron": "0 0 1 1 *",
                        "timezone": "UTC",
                        "render": _render_payload(),
                    },
                )
                response.raise_for_status()
                before = response.json()
                job_id = str(before["id"])
            else:
                raise ValueError(f"unsupported recovery state: {state}")

        await server.crash()
        if state == "scheduled":
            due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            with sqlite3.connect(case_dir / "schedules.sqlite3") as connection:
                connection.execute(
                    "UPDATE schedules SET next_run_at=?, updated_at=? WHERE id=?",
                    (due, due, job_id),
                )
                connection.commit()
        if receiver is not None:
            receiver.accepting = True
        await server.start()
        async with httpx.AsyncClient(timeout=30) as client:
            if state == "scheduled":
                schedule_id = job_id
                for _ in range(320):
                    response = await client.get(
                        server.endpoint + f"/v1/schedules/{schedule_id}"
                    )
                    response.raise_for_status()
                    scheduled = response.json()
                    if scheduled.get("last_job_id"):
                        job_id = str(scheduled["last_job_id"])
                        accepted_job_ids.append(job_id)
                        break
                    await asyncio.sleep(0.25)
                else:
                    raise TimeoutError("schedule did not create a job after restart")
            accepted_jobs = [
                await _verify_job_result(client, server, accepted_job_id)
                for accepted_job_id in accepted_job_ids
            ]
            result = next(
                item for item in accepted_jobs if item["job_id"] == job_id
            )
            if receiver is not None:
                for _ in range(320):
                    if job_id in receiver.accepted_job_ids:
                        break
                    await asyncio.sleep(0.25)
                else:
                    raise TimeoutError("pending webhook was not delivered after restart")
                result["webhook_delivered"] = True
            return {
                "state": state,
                "before": before,
                "after": result,
                "accepted_jobs": accepted_jobs,
            }
    finally:
        await server.close()
        if receiver is not None:
            await receiver.close()


async def restart_recovery_matrix(
    port: int,
    data_dir: Path,
    cases_per_state: int = 4,
) -> list[dict[str, Any]]:
    if os.name == "nt":
        raise RuntimeError("the durable recovery matrix requires Linux POSIX permissions")
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    states = ("queued", "running", "succeeded", "webhook-pending", "scheduled")
    results = []
    for state in states:
        for index in range(cases_per_state):
            results.append(await _recovery_case(state, index, port, data_dir))
    return results


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


async def _restart_recovery_matrix_gate(
    port: int,
    data_dir: Path,
    cases_per_state: int,
    output: Path | None,
    generated_at: str,
) -> int:
    diagnostics = {
        "data_dir": str(data_dir),
        "cases_per_state": cases_per_state,
    }
    try:
        results = await restart_recovery_matrix(
            port, data_dir, cases_per_state=cases_per_state
        )
    except Exception as exc:
        report = {
            "schema_version": 1,
            "generated_at": generated_at,
            "gate": "restart-recovery-matrix",
            "result": None,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "diagnostics": diagnostics,
        }
        _write(report, output)
        return 1
    report = {
        "schema_version": 1,
        "generated_at": generated_at,
        "gate": "restart-recovery-matrix",
        "result": {
            "cases": results,
            "case_count": len(results),
            "states": sorted({item["state"] for item in results}),
        },
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
    load.add_argument("--max-memory-growth-bytes", type=int, default=0)
    load.add_argument("--require-memory-limit", action="store_true")
    load.add_argument("--output", type=Path)

    recovery = subparsers.add_parser("restart-recovery")
    recovery.add_argument("--port", type=int, default=8017)
    recovery.add_argument("--data-dir", type=Path)
    recovery.add_argument("--output", type=Path)
    recovery_matrix = subparsers.add_parser("restart-recovery-matrix")
    recovery_matrix.add_argument("--port", type=int, default=8017)
    recovery_matrix.add_argument("--data-dir", type=Path)
    recovery_matrix.add_argument("--cases-per-state", type=int, default=4)
    recovery_matrix.add_argument("--output", type=Path)
    args = parser.parse_args()

    generated_at = datetime.now(timezone.utc).isoformat()
    if args.command == "load":
        if (
            args.requests < 1
            or args.concurrency < 1
            or args.concurrency > args.requests
            or args.duration_seconds < 0
            or args.max_memory_growth_bytes < 0
        ):
            parser.error(
                "requests and concurrency must be positive, concurrency <= requests, "
                "and duration/memory growth must be non-negative"
            )
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
        memory_growth = result["memory"][
            "first_to_last_quarter_median_working_set_growth_bytes"
        ]
        memory_growth_ok = args.max_memory_growth_bytes <= 0 or (
            memory_growth is not None
            and memory_growth <= args.max_memory_growth_bytes
        )
        latency = result["latency_ms"]["p95"]
        return 0 if (
            result["success_rate"] >= args.min_success_rate
            and latency is not None
            and latency <= args.max_p95_ms
            and memory_ok
            and memory_growth_ok
        ) else 1

    if args.command == "restart-recovery-matrix":
        if args.cases_per_state < 1:
            parser.error("cases-per-state must be positive")
        if args.data_dir:
            args.data_dir.mkdir(parents=True, exist_ok=True)
            return await _restart_recovery_matrix_gate(
                args.port,
                args.data_dir,
                args.cases_per_state,
                args.output,
                generated_at,
            )
        if args.output:
            data_dir = args.output.parent / f"{args.output.stem}-data"
            data_dir.mkdir(parents=True, exist_ok=True)
            return await _restart_recovery_matrix_gate(
                args.port,
                data_dir,
                args.cases_per_state,
                args.output,
                generated_at,
            )
        with tempfile.TemporaryDirectory(
            prefix="vipercapture-recovery-matrix-"
        ) as directory:
            return await _restart_recovery_matrix_gate(
                args.port,
                Path(directory),
                args.cases_per_state,
                args.output,
                generated_at,
            )

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
