"""Signed, bounded async-job webhook delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from render_errors import RenderError


class WebhookDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidatedWebhookTarget:
    original_url: str
    hostname: str
    host_header: str
    port: int
    addresses: tuple[str, ...]

    def pinned_url(self, address: str) -> str:
        parsed = urlsplit(self.original_url)
        literal = f"[{address}]" if ":" in address else address
        default_port = 443 if parsed.scheme == "https" else 80
        authority = literal if self.port == default_port else f"{literal}:{self.port}"
        return urlunsplit(
            (parsed.scheme, authority, parsed.path or "/", parsed.query, "")
        )


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
        self.client_factory = client_factory
        self.sleep = sleep

    async def _validate_target(self, url: str) -> ValidatedWebhookTarget:
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
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            resolved = await asyncio.wait_for(
                asyncio.to_thread(
                    socket.getaddrinfo,
                    parsed.hostname,
                    port,
                    type=socket.SOCK_STREAM,
                ),
                timeout=5,
            )
            addresses = tuple(
                dict.fromkeys(item[4][0].split("%", 1)[0] for item in resolved)
            )
        except (OSError, TimeoutError, ValueError) as exc:
            raise RenderError(
                "webhook_url_not_public",
                "The webhook hostname could not be resolved safely.",
                422,
                False,
            ) from exc
        if not addresses or (
            not self.allow_private
            and not all(ipaddress.ip_address(value).is_global for value in addresses)
        ):
            raise RenderError(
                "webhook_url_not_public",
                "Private and non-public webhook URLs are blocked.",
                422,
                False,
            )
        default_port = 443 if parsed.scheme == "https" else 80
        hostname = parsed.hostname.rstrip(".")
        host_literal = f"[{hostname}]" if ":" in hostname else hostname
        host_header = (
            host_literal if port == default_port else f"{host_literal}:{port}"
        )
        return ValidatedWebhookTarget(
            original_url=url,
            hostname=hostname,
            host_header=host_header,
            port=port,
            addresses=addresses,
        )

    async def validate_url(self, url: str) -> None:
        await self._validate_target(url)

    async def _post_pinned(
        self,
        target: ValidatedWebhookTarget,
        address: str,
        body: bytes,
        headers: dict[str, str],
    ) -> int:
        parsed = urlsplit(target.original_url)
        context = ssl.create_default_context() if parsed.scheme == "https" else None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    address,
                    target.port,
                    ssl=context,
                    server_hostname=(target.hostname if context is not None else None),
                ),
                timeout=5,
            )
            request_headers = {
                **headers,
                "Host": target.host_header,
                "Content-Length": str(len(body)),
                "Connection": "close",
            }
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            head = f"POST {path} HTTP/1.1\r\n" + "".join(
                f"{name}: {value}\r\n" for name, value in request_headers.items()
            )
            writer.write(head.encode("ascii") + b"\r\n" + body)
            await asyncio.wait_for(writer.drain(), timeout=10)
            status_line = await asyncio.wait_for(reader.readline(), timeout=10)
            parts = status_line.decode("ascii", "replace").split(" ", 2)
            if len(parts) < 2 or not parts[0].startswith("HTTP/"):
                raise OSError("invalid HTTP response")
            return int(parts[1])
        finally:
            if "writer" in locals():
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, ssl.SSLError):
                    pass

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
        target = await self._validate_target(url)
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
        client_context = self.client_factory() if self.client_factory else None
        if client_context is not None:
            client = await client_context.__aenter__()
        else:
            client = None
        try:
            for attempt in range(self.attempts):
                try:
                    address = target.addresses[attempt % len(target.addresses)]
                    if client is None:
                        status_code = await self._post_pinned(
                            target, address, body, headers
                        )
                    else:
                        response = await client.post(
                            target.pinned_url(address),
                            content=body,
                            headers={**headers, "Host": target.host_header},
                            extensions={"sni_hostname": target.hostname},
                        )
                        status_code = response.status_code
                    if 200 <= status_code < 300:
                        return
                    last_error = f"HTTP {status_code}"
                    if status_code < 500 and status_code != 429:
                        break
                except (httpx.RequestError, OSError, TimeoutError, ValueError) as exc:
                    last_error = type(exc).__name__
                if attempt + 1 < self.attempts:
                    await self.sleep(min(2**attempt, 4))
        finally:
            if client_context is not None:
                await client_context.__aexit__(None, None, None)
        raise WebhookDeliveryError(f"webhook delivery failed: {last_error}")
