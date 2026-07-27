"""Packaged entry point for the ViperCapture desktop renderer."""

from __future__ import annotations

import asyncio
import ctypes
import os

import uvicorn

from main import app


def parent_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


async def watch_parent(server: uvicorn.Server, parent_pid: int) -> None:
    while parent_is_running(parent_pid):
        await asyncio.sleep(0.5)
    server.should_exit = True


def run() -> None:
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=int(os.environ["VIPERCAPTURE_PORT"]),
        access_log=False,
    )
    server = uvicorn.Server(config)
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    parent_pid = int(os.environ["VIPERCAPTURE_PARENT_PID"])

    async def serve() -> None:
        watcher = asyncio.create_task(watch_parent(server, parent_pid))
        try:
            await server.serve()
        finally:
            watcher.cancel()

    asyncio.run(serve())


if __name__ == "__main__":
    run()
