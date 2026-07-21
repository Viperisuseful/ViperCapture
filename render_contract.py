"""Validated JSON contract for the open-source ViperCapture image engine."""

from __future__ import annotations

from enum import Enum
import re

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


MAX_HEADERS = 32
MAX_HEADER_NAME_BYTES = 128
MAX_HEADER_VALUE_BYTES = 4 * 1024
MAX_HEADER_BYTES = 16 * 1024
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
BLOCKED_HEADER_NAMES = {
    "connection", "content-length", "forwarded", "host", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade",
}
BLOCKED_HEADER_PREFIXES = ("proxy-", "sec-", "x-forwarded-")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputFormat(str, Enum):
    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"


class WaitEvent(str, Enum):
    DOMCONTENTLOADED = "domcontentloaded"
    LOAD = "load"
    NETWORKIDLE = "networkidle"


class Viewport(StrictModel):
    width: int = Field(default=1280, ge=1, le=7680)
    height: int = Field(default=720, ge=1, le=4320)
    device_scale_factor: float = Field(default=1, ge=0.1, le=4)


class ImageOptions(StrictModel):
    quality: int | None = Field(default=None, ge=1, le=100)
    transparent_background: bool = False


class WaitOptions(StrictModel):
    event: WaitEvent = WaitEvent.LOAD
    selector: str | None = Field(default=None, min_length=1, max_length=2_048)
    text: str | None = Field(default=None, min_length=1, max_length=4_096)
    delay_ms: int = Field(default=0, ge=0, le=15_000)
    timeout_ms: int = Field(default=15_000, ge=1, le=30_000)


def _validate_headers(headers: dict[str, str]) -> dict[str, str]:
    if len(headers) > MAX_HEADERS:
        raise ValueError(f"headers may contain at most {MAX_HEADERS} entries")
    total = 0
    seen: set[str] = set()
    for name, value in headers.items():
        lowered = name.lower()
        name_bytes = name.encode("utf-8")
        value_bytes = value.encode("utf-8")
        if not name_bytes or len(name_bytes) > MAX_HEADER_NAME_BYTES:
            raise ValueError("header names must be 1 through 128 bytes")
        if not HEADER_NAME_PATTERN.fullmatch(name):
            raise ValueError("header names must use HTTP token characters")
        if lowered in seen:
            raise ValueError("header names must be unique case-insensitively")
        seen.add(lowered)
        if lowered in BLOCKED_HEADER_NAMES or lowered.startswith(BLOCKED_HEADER_PREFIXES):
            raise ValueError(f"header {name!r} is managed by ViperCapture")
        if len(value_bytes) > MAX_HEADER_VALUE_BYTES:
            raise ValueError("individual header values may not exceed 4096 bytes")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("header values may not contain control characters")
        total += len(name_bytes) + len(value_bytes) + 4
    if total > MAX_HEADER_BYTES:
        raise ValueError("serialized headers may not exceed 16384 bytes")
    return headers


class RenderRequest(StrictModel):
    url: HttpUrl
    output: OutputFormat = OutputFormat.PNG
    viewport: Viewport = Field(default_factory=Viewport)
    full_page: bool = True
    selector: str | None = Field(default=None, min_length=1, max_length=2_048)
    image: ImageOptions = Field(default_factory=ImageOptions)
    headers: dict[str, str] = Field(default_factory=dict)
    wait_for: WaitOptions = Field(default_factory=WaitOptions)
    proceed_on_captcha: bool = Field(
        default=False,
        description="Capture a detected page-level CAPTCHA instead of returning captcha_detected.",
    )

    @model_validator(mode="after")
    def validate_contract(self) -> "RenderRequest":
        if self.selector is not None and self.full_page:
            raise ValueError("selector requires full_page=false")
        if self.image.quality is not None and self.output not in {
            OutputFormat.JPEG,
            OutputFormat.WEBP,
        }:
            raise ValueError("quality is accepted only for JPEG or WebP")
        if self.image.transparent_background and self.output is OutputFormat.JPEG:
            raise ValueError("transparent_background is accepted only for PNG or WebP")
        self.headers = _validate_headers(self.headers)
        return self

    @property
    def source_type(self) -> str:
        return "url"

    @property
    def credit_cost(self) -> int:
        return 1
