"""Validated best-effort bulk async render submission contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from render_contract import RenderRequest
from render_errors import RenderError

MAX_BULK_BODY_BYTES = 6 * 1024 * 1024

# A bulk request may carry 100 independently valid renders; without an
# aggregate cap, 100 near-5 MiB sources approach 500 MiB of accepted source
# text in one JSON body. Bounding the combined embedded source keeps a single
# valid bulk request from exhausting process memory.
MAX_BULK_SOURCE_BYTES = 20 * 1024 * 1024

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BulkJobItem(StrictModel):
    id: str | None = Field(default=None, min_length=1, max_length=128)
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    render: RenderRequest


class BulkJobRequest(StrictModel):
    items: list[BulkJobItem] = Field(min_length=1, max_length=100)


class BulkBodyLimitMiddleware:
    """Reject oversized bulk bodies before JSON or source fields are parsed."""

    def __init__(self, app, *, max_bytes: int = MAX_BULK_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if not (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/v1/jobs/bulk"
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        if content_length > self.max_bytes:
            self._raise_limit()

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                self._raise_limit()
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)

    def _raise_limit(self) -> None:
        raise RenderError(
            "bulk_payload_too_large",
            "The bulk request body exceeds the aggregate limit.",
            413,
            False,
            {"max_bytes": self.max_bytes},
        )
