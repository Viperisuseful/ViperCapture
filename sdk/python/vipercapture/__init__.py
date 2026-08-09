"""Dependency-free ViperCapture API client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class ViperCaptureError(RuntimeError):
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"ViperCapture returned HTTP {status}: {body.decode('utf-8', 'replace')}")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 180) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def render(self, request: dict[str, Any]) -> bytes:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        operation = urllib.request.Request(
            f"{self.base_url}/v1/render",
            data=json.dumps(request).encode(),
            headers=headers,
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(_RejectRedirects())
            with opener.open(operation, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ViperCaptureError(exc.code, exc.read()) from None


__all__ = ["Client", "ViperCaptureError"]
