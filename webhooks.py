"""Signed, bounded async-job webhook delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urljoin, urlsplit

import httpx

from render_engine import PublicUrlValidator
from render_errors import RenderError


class WebhookDeliveryError(RuntimeError):
    pass


class WebhookDispatcher:
    def __init__(
        self,
        *,
        secret: str,
        public_url: str = "",
        allow_private: bool = False,
        attempts: int = 3,
        client_factory=None,
        sleep=asyncio.sleep,
    ) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("the webhook secret must contain at least 32 bytes")
        self.secret = secret
        self.public_url = public_url.rstrip("/")
        self.allow_private = allow_private
        self.attempts = max(1, min(attempts, 5))
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
            )
        )
        self.sleep = sleep
        self.validator = PublicUrlValidator()

    async def validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RenderError(
                "webhook_url_invalid",
                "The webhook URL must use HTTP or HTTPS.",
                422,
                False,
            )
        if parsed.username or parsed.password:
            raise RenderError(
                "webhook_url_invalid",
                "Webhook URLs may not contain credentials.",
                422,
                False,
            )
        if not self.allow_private and not await self.validator.is_public(url):
            raise RenderError(
                "webhook_url_not_public",
                "Private and non-public webhook URLs are blocked.",
                422,
                False,
            )

    def _document(self, job: dict[str, object]) -> dict[str, object]:
        document = dict(job)
        result_url = document.get("result_url")
        status_url = document.get("status_url")
        if self.public_url:
            if isinstance(result_url, str):
                document["result_url"] = urljoin(
                    f"{self.public_url}/", result_url.lstrip("/")
                )
            if isinstance(status_url, str):
                document["status_url"] = urljoin(
                    f"{self.public_url}/", status_url.lstrip("/")
                )
        return {
            "schema_version": 1,
            "event": f"render.{document.get('status', 'unknown')}",
            "job": document,
        }

    async def deliver(self, url: str, job: dict[str, object]) -> None:
        await self.validate_url(url)
        body = json.dumps(
            self._document(job),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.secret.encode("utf-8"),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ViperCapture-Webhook/1.0",
            "X-ViperCapture-Webhook-Timestamp": timestamp,
            "X-ViperCapture-Webhook-Signature": f"v1={signature}",
            "X-ViperCapture-Webhook-Id": str(job.get("id", "")),
        }
        last_error = "unknown failure"
        async with self.client_factory() as client:
            for attempt in range(self.attempts):
                try:
                    response = await client.post(url, content=body, headers=headers)
                    if 200 <= response.status_code < 300:
                        return
                    last_error = f"HTTP {response.status_code}"
                    if response.status_code < 500 and response.status_code != 429:
                        break
                except httpx.RequestError as exc:
                    last_error = type(exc).__name__
                if attempt + 1 < self.attempts:
                    await self.sleep(min(2**attempt, 4))
        raise WebhookDeliveryError(f"webhook delivery failed: {last_error}")
