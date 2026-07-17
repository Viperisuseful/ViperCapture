"""Request identifiers and stable API error responses."""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


logger = logging.getLogger("vipercapture.errors")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
STATUS_CODES = {
    400: "invalid_request", 401: "unauthorized", 403: "forbidden",
    404: "not_found", 405: "method_not_allowed", 409: "conflict",
    413: "limit_exceeded", 422: "invalid_request", 429: "rate_limited",
    502: "upstream_error", 504: "target_timeout",
}
STATUS_MESSAGES = {
    400: "The request is invalid.",
    401: "A valid API key is required.",
    403: "This operation is not available for the current account.",
    404: "The requested resource was not found.",
    405: "The request method is not allowed.",
    409: "The request conflicts with the current render state.",
    413: "The request exceeds an allowed limit.",
    422: "One or more request fields are invalid.",
    429: "A request limit has been reached.",
    502: "The target or upstream provider failed.",
    504: "The target did not become ready in time.",
}


class RenderError(Exception):
    def __init__(self, code: str, message: str, status_code: int, retryable: bool,
                 details: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers or {}


def request_id_for(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else str(uuid4())


def error_response(request: Request, *, code: str, message: str, status_code: int,
                   retryable: bool, details: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    request_id = request_id_for(request)
    response_headers = dict(headers or {})
    response_headers["X-Request-Id"] = request_id
    return JSONResponse(status_code=status_code, headers=response_headers, content={
        "error": {"code": code, "message": message, "request_id": request_id,
                  "retryable": retryable, "details": details or {}}
    })


def _safe_validation_details(exc: RequestValidationError) -> dict[str, Any]:
    return {"fields": [
        {"path": [str(part) for part in error.get("loc", ())],
         "constraint": error.get("type", "invalid"),
         "message": error.get("msg", "Invalid value")}
        for error in exc.errors()
    ]}


def install_render_error_layer(app: FastAPI) -> None:
    if getattr(app.state, "render_error_layer_installed", False):
        return
    app.state.render_error_layer_installed = True

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request.state.request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid4())
        try:
            response = await call_next(request)
        except RenderError as exc:
            response = error_response(request, code=exc.code, message=exc.message,
                                      status_code=exc.status_code, retryable=exc.retryable,
                                      details=exc.details, headers=exc.headers)
        except Exception as exc:
            logger.error("Unhandled exception_type=%s request_id=%s",
                         type(exc).__name__, request.state.request_id)
            response = error_response(request, code="internal_error",
                                      message="The render could not be completed.",
                                      status_code=500, retryable=True)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(RenderError)
    async def handle_render_error(request: Request, exc: RenderError) -> JSONResponse:
        return error_response(request, code=exc.code, message=exc.message,
                              status_code=exc.status_code, retryable=exc.retryable,
                              details=exc.details, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(request, code="invalid_request", message=STATUS_MESSAGES[422],
                              status_code=422, retryable=False,
                              details=_safe_validation_details(exc))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        status_code = exc.status_code
        message = exc.detail if isinstance(exc.detail, str) and status_code < 500 else None
        return error_response(request, code=STATUS_CODES.get(status_code, "request_error"),
                              message=message or STATUS_MESSAGES.get(status_code, "The request failed."),
                              status_code=status_code,
                              retryable=status_code in {429, 502, 503, 504},
                              headers=dict(exc.headers or {}))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled exception_type=%s request_id=%s",
                     type(exc).__name__, request_id_for(request))
        return error_response(request, code="internal_error",
                              message="The render could not be completed.",
                              status_code=500, retryable=True)
