"""Compact HMAC-signed render URLs for embeds and short-lived sharing."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
import hashlib
import hmac
import json
import time

from render_contract import RenderRequest, canonical_render_document
from render_errors import RenderError


SIGNED_URL_VERSION = "v1"
MAX_SIGNED_URL_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_SIGNED_URL_PAYLOAD_CHARS = 32 * 1024


def encode_render_request(request: RenderRequest) -> str:
    body = json.dumps(
        canonical_render_document(
            request,
            exclude_none=True,
            exclude_defaults=True,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return urlsafe_b64encode(body).decode("ascii").rstrip("=")


def decode_render_request(payload: str) -> RenderRequest:
    if not payload or len(payload) > MAX_SIGNED_URL_PAYLOAD_CHARS:
        raise RenderError(
            "signed_url_invalid",
            "The signed render payload is invalid.",
            400,
            False,
        )
    try:
        padding = "=" * (-len(payload) % 4)
        document = json.loads(urlsafe_b64decode(payload + padding))
        return RenderRequest.model_validate(document)
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(
            "signed_url_invalid",
            "The signed render payload is invalid.",
            400,
            False,
        ) from exc


def signature_for(payload: str, expires: int, secret: str) -> str:
    message = f"{SIGNED_URL_VERSION}\n{expires}\n{payload}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign_render_request(
    request: RenderRequest,
    *,
    secret: str,
    ttl_seconds: int = 3600,
    now: int | None = None,
) -> tuple[str, int, str]:
    if not secret:
        raise ValueError("a signing secret is required")
    if ttl_seconds < 1 or ttl_seconds > MAX_SIGNED_URL_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be between 1 and {MAX_SIGNED_URL_TTL_SECONDS}"
        )
    payload = encode_render_request(request)
    if len(payload) > MAX_SIGNED_URL_PAYLOAD_CHARS:
        raise ValueError(
            "the render request is too large for a signed URL; use POST /v1/render"
        )
    expires = (int(time.time()) if now is None else now) + ttl_seconds
    return payload, expires, signature_for(payload, expires, secret)


def verify_render_request(
    payload: str,
    expires: int,
    signature: str,
    *,
    secret: str,
    now: int | None = None,
) -> RenderRequest:
    current = int(time.time()) if now is None else now
    if expires < current:
        raise RenderError(
            "signed_url_expired",
            "The signed render URL has expired.",
            410,
            False,
        )
    if expires - current > MAX_SIGNED_URL_TTL_SECONDS:
        raise RenderError(
            "signed_url_invalid",
            "The signed render URL expiry exceeds the configured maximum.",
            400,
            False,
        )
    expected = signature_for(payload, expires, secret)
    if not hmac.compare_digest(signature.lower(), expected):
        raise RenderError(
            "signed_url_invalid",
            "The signed render URL signature is invalid.",
            401,
            False,
        )
    return decode_render_request(payload)
