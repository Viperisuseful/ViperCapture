"""Validated best-effort bulk async render submission contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from render_contract import RenderRequest
from render_errors import RenderError

MAX_BULK_BODY_BYTES = 6 * 1024 * 1024
MAX_DIFF_BODY_BYTES = 2 * 20 * 1024 * 1024 + 1024 * 1024
JSON_BODY_PATHS = {
    "/v1/render",
    "/v1/signed-url",
    "/v1/jobs",
    "/v1/jobs/bulk",
    "/v1/schedules",
}


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
    """Reject oversized API bodies before JSON or multipart parsing."""

    def __init__(self, app, *, max_bytes: int = MAX_BULK_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        method = scope.get("method")
        if scope["type"] != "http" or method not in {"POST", "PATCH"}:
            await self.app(scope, receive, send)
            return
        if path == "/v1/diff":
            maximum = MAX_DIFF_BODY_BYTES
        elif path in JSON_BODY_PATHS or (
            method == "PATCH" and path.startswith("/v1/schedules/")
        ):
            maximum = self.max_bytes
        else:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        if content_length > maximum:
            self._raise_limit(maximum)

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > maximum:
                self._raise_limit(maximum)
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

    def _raise_limit(self, maximum: int) -> None:
        raise RenderError(
            "request_body_too_large",
            "The request body exceeds the route limit.",
            413,
            False,
            {"max_bytes": maximum},
        )
