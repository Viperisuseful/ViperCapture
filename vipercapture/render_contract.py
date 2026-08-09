"""Validated JSON contract for ViperCapture render requests."""

from __future__ import annotations

import ipaddress
import re
from enum import Enum
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_HEADERS = 32
MAX_HEADER_NAME_BYTES = 128
MAX_HEADER_VALUE_BYTES = 4 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_SELECTOR_CHARS = 2_048
MAX_WAIT_TEXT_CHARS = 4_096
MAX_CUSTOM_CSS_BYTES = 64 * 1024
MAX_MULTI_VIEWPORTS = 3
MAX_FAIL_STATUSES = 32
MAX_ACTIONS = 32
MAX_ACTION_VALUE_CHARS = 16_384
MAX_BLOCK_PATTERNS = 64
MAX_COOKIES = 64
MAX_ASSERTIONS = 32
MAX_PDF_PAGES = 50
VIEWPORT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
BLOCKED_HEADER_NAMES = {
    "connection",
    "content-length",
    "forwarded",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
BLOCKED_HEADER_PREFIXES = ("proxy-", "sec-", "x-forwarded-")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputFormat(str, Enum):
    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"
    PDF = "pdf"
    HTML = "html"
    MARKDOWN = "markdown"
    METADATA = "metadata"
    WEBM = "webm"
    MP4 = "mp4"
    GIF = "gif"
    AVIF = "avif"


class BrowserEngine(str, Enum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


class DevicePreset(str, Enum):
    DESKTOP = "desktop"
    IPHONE_14 = "iphone_14"
    PIXEL_7 = "pixel_7"
    IPAD = "ipad"


class ColorScheme(str, Enum):
    LIGHT = "light"
    DARK = "dark"
    NO_PREFERENCE = "no-preference"


class ReducedMotion(str, Enum):
    REDUCE = "reduce"
    NO_PREFERENCE = "no-preference"


class WaitEvent(str, Enum):
    DOMCONTENTLOADED = "domcontentloaded"
    LOAD = "load"
    NETWORKIDLE = "networkidle"


class LazyLoadMode(str, Enum):
    NONE = "none"
    ADAPTIVE = "adaptive"
    THOROUGH = "thorough"


class ActionType(str, Enum):
    CLICK = "click"
    HOVER = "hover"
    FILL = "fill"
    PRESS = "press"
    SELECT = "select"
    SCROLL = "scroll"
    WAIT = "wait"
    HIDE = "hide"
    JAVASCRIPT = "javascript"


class ResourceType(str, Enum):
    DOCUMENT = "document"
    STYLESHEET = "stylesheet"
    IMAGE = "image"
    MEDIA = "media"
    FONT = "font"
    SCRIPT = "script"
    TEXTTRACK = "texttrack"
    XHR = "xhr"
    FETCH = "fetch"
    EVENTSOURCE = "eventsource"
    WEBSOCKET = "websocket"
    MANIFEST = "manifest"
    OTHER = "other"


class ExtractMode(str, Enum):
    DOCUMENT = "document"
    ARTICLE = "article"


class PdfMode(str, Enum):
    PRINT = "print"
    SINGLE_PAGE = "single_page"


class PaperSize(str, Enum):
    A0 = "A0"
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"
    A4 = "A4"
    A5 = "A5"
    A6 = "A6"
    LEGAL = "Legal"
    LETTER = "Letter"
    TABLOID = "Tabloid"


class Orientation(str, Enum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


class ConsentMode(str, Enum):
    NONE = "none"
    REJECT = "reject"
    ACCEPT = "accept"
    HIDE = "hide"


class Viewport(StrictModel):
    width: int = Field(default=1280, ge=1, le=65_535)
    height: int = Field(default=720, ge=1, le=65_535)
    device_scale_factor: float = Field(default=1, ge=0.1, le=8)


class NamedViewport(Viewport):
    name: str = Field(min_length=1, max_length=32, pattern=VIEWPORT_NAME_PATTERN.pattern)
    device: DevicePreset = DevicePreset.DESKTOP


class EnvironmentOptions(StrictModel):
    device: DevicePreset = DevicePreset.DESKTOP
    color_scheme: ColorScheme | None = None
    reduced_motion: ReducedMotion | None = None
    locale: str | None = Field(default=None, min_length=2, max_length=64)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, value: str | None) -> str | None:
        if value is not None and not LOCALE_PATTERN.fullmatch(value):
            raise ValueError("locale must be a BCP 47-style language tag")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("timezone must be a valid IANA time zone") from exc
        return value


class GeolocationOptions(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float = Field(default=0, ge=0, le=100_000)


class ProxyOptions(StrictModel):
    server: str = Field(min_length=1, max_length=2_048)
    username: str | None = Field(default=None, max_length=1_024)
    password: str | None = Field(default=None, max_length=4_096)
    bypass: str | None = Field(default=None, max_length=4_096)

    @field_validator("server")
    @classmethod
    def validate_server(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError as exc:
            raise ValueError("proxy server authority is invalid") from exc
        if (
            parsed.scheme.lower() not in {"http", "https", "socks4", "socks5"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("proxy server must use http, https, socks4, or socks5")
        return value


class CookieOptions(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=4_096)
    domain: str = Field(min_length=1, max_length=253)
    path: str = Field(default="/", min_length=1, max_length=1_024)
    expires: float | None = None
    http_only: bool = False
    secure: bool = False
    same_site: str = Field(default="Lax", pattern=r"^(?:Strict|Lax|None)$")

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        domain = value.removeprefix(".")
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            labels = domain.split(".")
            if any(
                not label
                or len(label) > 63
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                for label in labels
            ):
                raise ValueError("cookie domain must be a hostname or IP address")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("cookie path must begin with /")
        return value


class NetworkOptions(StrictModel):
    user_agent: str | None = Field(default=None, min_length=1, max_length=1_024)
    geolocation: GeolocationOptions | None = None
    proxy: ProxyOptions | None = None
    cookies: list[CookieOptions] = Field(default_factory=list, max_length=MAX_COOKIES)
    block_url_patterns: list[str] = Field(
        default_factory=list,
        max_length=MAX_BLOCK_PATTERNS,
    )
    block_resource_types: set[ResourceType] = Field(default_factory=set)
    bypass_csp: bool = False
    ignore_https_errors: bool = False

    @field_validator("block_url_patterns")
    @classmethod
    def validate_block_patterns(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 2_048 for value in values):
            raise ValueError("block URL patterns must contain 1 through 2048 characters")
        if len(set(values)) != len(values):
            raise ValueError("block URL patterns must be unique")
        return values


class Action(StrictModel):
    type: ActionType
    selector: str | None = Field(default=None, min_length=1, max_length=MAX_SELECTOR_CHARS)
    value: str | None = Field(default=None, max_length=MAX_ACTION_VALUE_CHARS)
    values: list[str] | None = Field(default=None, min_length=1, max_length=32)
    key: str | None = Field(default=None, min_length=1, max_length=128)
    x: float | None = Field(default=None, ge=-100_000, le=100_000)
    y: float | None = Field(default=None, ge=-100_000, le=100_000)
    delay_ms: int = Field(default=0, ge=0, le=15_000)
    timeout_ms: int = Field(default=15_000, ge=1, le=30_000)

    @model_validator(mode="after")
    def validate_action(self) -> "Action":
        if self.values and any(
            len(value) > MAX_ACTION_VALUE_CHARS for value in self.values
        ):
            raise ValueError(
                f"select values may not exceed {MAX_ACTION_VALUE_CHARS} characters"
            )
        selector_actions = {
            ActionType.CLICK,
            ActionType.HOVER,
            ActionType.FILL,
            ActionType.SELECT,
            ActionType.HIDE,
        }
        if self.type in selector_actions and self.selector is None:
            raise ValueError(f"{self.type.value} action requires selector")
        if self.type is ActionType.FILL and self.value is None:
            raise ValueError("fill action requires value")
        if self.type is ActionType.PRESS and self.key is None:
            raise ValueError("press action requires key")
        if self.type is ActionType.SELECT and not self.values:
            raise ValueError("select action requires values")
        if self.type is ActionType.SCROLL and self.selector is None and self.x is None and self.y is None:
            raise ValueError("scroll action requires selector, x, or y")
        if self.type is ActionType.WAIT and self.selector is None and self.value is None and not self.delay_ms:
            raise ValueError("wait action requires selector, text value, or delay_ms")
        if self.type is ActionType.JAVASCRIPT and self.value is None:
            raise ValueError("javascript action requires value")
        return self


class AssertionOptions(StrictModel):
    content_includes: list[str] = Field(default_factory=list, max_length=MAX_ASSERTIONS)
    content_excludes: list[str] = Field(default_factory=list, max_length=MAX_ASSERTIONS)
    request_failures: list[str] = Field(default_factory=list, max_length=MAX_ASSERTIONS)

    @field_validator("content_includes", "content_excludes", "request_failures")
    @classmethod
    def validate_assertions(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 4_096 for value in values):
            raise ValueError("assertions must contain 1 through 4096 characters")
        if len(set(values)) != len(values):
            raise ValueError("assertions must be unique")
        return values


class DeliveryOptions(StrictModel):
    webhook_url: HttpUrl | None = None


class DiagnosticsOptions(StrictModel):
    bundle: bool = False
    include_console: bool = True
    include_network: bool = True
    include_har: bool = False
    include_trace: bool = False
    include_warc: bool = False


class DeterministicOptions(StrictModel):
    enabled: bool = False
    timestamp_ms: int = Field(default=1_700_000_000_000, ge=0)
    random_seed: int = Field(default=1, ge=0, le=2**32 - 1)
    wait_for_fonts: bool = True


class CertificationOptions(StrictModel):
    enabled: bool = False


class SliceOptions(StrictModel):
    height: int = Field(ge=100, le=10_000)
    overlap: int = Field(default=0, ge=0, le=1_000)

    @model_validator(mode="after")
    def validate_overlap(self) -> "SliceOptions":
        if self.overlap >= self.height:
            raise ValueError("slice overlap must be smaller than slice height")
        return self


class VideoOptions(StrictModel):
    duration_ms: int = Field(default=5_000, ge=1_000, le=30_000)
    scroll: bool = False
    scroll_step: int = Field(default=500, ge=1, le=4_320)
    scroll_delay_ms: int = Field(default=250, ge=50, le=2_000)

class ClipOptions(StrictModel):
    x: float = Field(default=0, ge=0, le=100_000)
    y: float = Field(default=0, ge=0, le=100_000)
    width: float = Field(gt=0, le=100_000)
    height: float = Field(gt=0, le=100_000)


class ImageOptions(StrictModel):
    quality: int | None = Field(default=None, ge=1, le=100)
    width: int | None = Field(default=None, ge=1, le=65_535)
    height: int | None = Field(default=None, ge=1, le=65_535)
    transparent_background: bool = False
    optimize_for_speed: bool = Field(
        default=False,
        description="Prefer Chromium's faster PNG or WebP encoder over output size.",
    )


class PdfMargins(StrictModel):
    top: float = Field(default=0.4, ge=0, le=4)
    right: float = Field(default=0.4, ge=0, le=4)
    bottom: float = Field(default=0.4, ge=0, le=4)
    left: float = Field(default=0.4, ge=0, le=4)


class PdfOptions(StrictModel):
    mode: PdfMode = PdfMode.PRINT
    paper_size: PaperSize = PaperSize.A4
    orientation: Orientation = Orientation.PORTRAIT
    print_background: bool = True
    margins: PdfMargins = Field(default_factory=PdfMargins)
    header_template: str | None = Field(default=None, max_length=16_384)
    footer_template: str | None = Field(default=None, max_length=16_384)
    page_ranges: str | None = Field(
        default=None,
        max_length=256,
        pattern=r"^\s*\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*\s*$",
    )

    @field_validator("page_ranges")
    @classmethod
    def validate_page_ranges(cls, value: str | None) -> str | None:
        if value is None:
            return None
        ranges: list[tuple[int, int]] = []
        for item in value.split(","):
            bounds = [int(part.strip()) for part in item.split("-", 1)]
            start, end = (bounds[0], bounds[-1])
            if start < 1 or end < start:
                raise ValueError("page_ranges must contain ascending positive pages")
            ranges.append((start, end))
        selected = 0
        current_start, current_end = sorted(ranges)[0]
        for start, end in sorted(ranges)[1:]:
            if start <= current_end + 1:
                current_end = max(current_end, end)
            else:
                selected += current_end - current_start + 1
                current_start, current_end = start, end
        selected += current_end - current_start + 1
        if selected > MAX_PDF_PAGES:
            raise ValueError(f"page_ranges may select at most {MAX_PDF_PAGES} pages")
        return value

    @model_validator(mode="after")
    def validate_margins(self) -> "PdfOptions":
        if self.mode is PdfMode.SINGLE_PAGE:
            return self
        paper_width, paper_height = {
            PaperSize.A0: (33.1, 46.8),
            PaperSize.A1: (23.4, 33.1),
            PaperSize.A2: (16.5, 23.4),
            PaperSize.A3: (11.7, 16.5),
            PaperSize.A4: (8.27, 11.69),
            PaperSize.A5: (5.83, 8.27),
            PaperSize.A6: (4.13, 5.83),
            PaperSize.LEGAL: (8.5, 14.0),
            PaperSize.LETTER: (8.5, 11.0),
            PaperSize.TABLOID: (11.0, 17.0),
        }[self.paper_size]
        if self.orientation is Orientation.LANDSCAPE:
            paper_width, paper_height = paper_height, paper_width
        if self.margins.left + self.margins.right >= paper_width:
            raise ValueError("horizontal margins must leave printable page width")
        if self.margins.top + self.margins.bottom >= paper_height:
            raise ValueError("vertical margins must leave printable page height")
        return self


class WaitOptions(StrictModel):
    event: WaitEvent = WaitEvent.LOAD
    selector: str | None = Field(default=None, min_length=1, max_length=MAX_SELECTOR_CHARS)
    text: str | None = Field(default=None, min_length=1, max_length=MAX_WAIT_TEXT_CHARS)
    delay_ms: int = Field(default=0, ge=0, le=15_000)
    timeout_ms: int = Field(default=15_000, ge=1, le=30_000)


class CleanupOptions(StrictModel):
    consent_mode: ConsentMode = ConsentMode.NONE
    block_ads: bool = False
    block_trackers: bool = False
    block_chats: bool = False
    block_newsletters: bool = False


def _validate_headers(headers: dict[str, str]) -> dict[str, str]:
    if len(headers) > MAX_HEADERS:
        raise ValueError(f"headers may contain at most {MAX_HEADERS} entries")

    total = 0
    seen: set[str] = set()
    for name, value in headers.items():
        name_bytes = name.encode("utf-8")
        value_bytes = value.encode("utf-8")
        lowered = name.lower()
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
    url: HttpUrl | None = None
    html: str | None = None
    markdown: str | None = None
    base_url: HttpUrl | None = None
    engine: BrowserEngine = BrowserEngine.CHROMIUM
    output: OutputFormat = OutputFormat.PNG
    viewport: Viewport = Field(default_factory=Viewport)
    viewports: list[NamedViewport] | None = Field(
        default=None,
        min_length=2,
        max_length=MAX_MULTI_VIEWPORTS,
        description="Render two or three bounded image viewports into one ZIP archive.",
    )
    environment: EnvironmentOptions = Field(default_factory=EnvironmentOptions)
    network: NetworkOptions = Field(default_factory=NetworkOptions)
    actions: list[Action] = Field(default_factory=list, max_length=MAX_ACTIONS)
    assertions: AssertionOptions = Field(default_factory=AssertionOptions)
    delivery: DeliveryOptions = Field(default_factory=DeliveryOptions)
    diagnostics: DiagnosticsOptions = Field(default_factory=DiagnosticsOptions)
    deterministic: DeterministicOptions = Field(default_factory=DeterministicOptions)
    certification: CertificationOptions = Field(default_factory=CertificationOptions)
    slices: SliceOptions | None = None
    profile_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    save_profile: bool = False
    video: VideoOptions | None = None
    full_page: bool = True
    preserve_viewport_width: bool = Field(
        default=False,
        description=(
            "For full-page images, clip horizontal overflow to the requested "
            "viewport width while preserving the full document height."
        ),
    )
    lazy_load: LazyLoadMode = LazyLoadMode.THOROUGH
    selector: str | None = Field(default=None, min_length=1, max_length=MAX_SELECTOR_CHARS)
    clip: ClipOptions | None = None
    custom_css: str | None = None
    fail_on_status: list[int] = Field(default_factory=list, max_length=MAX_FAIL_STATUSES)
    image: ImageOptions = Field(default_factory=ImageOptions)
    pdf: PdfOptions | None = None
    extract_mode: ExtractMode = ExtractMode.DOCUMENT
    include_shadow_dom: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    wait_for: WaitOptions = Field(default_factory=WaitOptions)
    cleanup: CleanupOptions = Field(default_factory=CleanupOptions)
    cache: bool = Field(
        default=False,
        description=(
            "Reuse an exact account-scoped API image render for up to 15 minutes. "
            "Cached hits bypass Chromium and are metered at two hits per credit."
        ),
    )
    proceed_on_captcha: bool = Field(
        default=False,
        description="Capture a detected page-level CAPTCHA instead of returning captcha_detected.",
    )

    @model_validator(mode="after")
    def validate_contract(self) -> "RenderRequest":
        sources = (self.url is not None, self.html is not None, self.markdown is not None)
        if sum(sources) != 1:
            raise ValueError("exactly one of url, html, or markdown is required")
        if self.base_url is not None and self.url is not None:
            raise ValueError("base_url is accepted only with html or markdown input")
        if self.headers and self.url is None and self.base_url is None:
            raise ValueError("base_url is required for raw input custom headers")
        if self.preserve_viewport_width and not self.full_page:
            raise ValueError("preserve_viewport_width requires full_page=true")
        if self.save_profile and self.profile_id is None:
            raise ValueError("save_profile requires profile_id")
        for source in (self.html, self.markdown):
            if source is not None and len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise ValueError("HTML and Markdown input may not exceed 5242880 bytes")

        is_image = self.output in {
            OutputFormat.PNG,
            OutputFormat.JPEG,
            OutputFormat.WEBP,
            OutputFormat.AVIF,
        }
        is_video = self.output in {OutputFormat.WEBM, OutputFormat.MP4, OutputFormat.GIF}
        if self.preserve_viewport_width and not is_image:
            raise ValueError(
                "preserve_viewport_width requires an image output"
            )
        if self.custom_css is not None and len(self.custom_css.encode("utf-8")) > MAX_CUSTOM_CSS_BYTES:
            raise ValueError("custom_css may not exceed 65536 UTF-8 bytes")
        if len(set(self.fail_on_status)) != len(self.fail_on_status):
            raise ValueError("fail_on_status values must be unique")
        if any(status < 100 or status > 599 for status in self.fail_on_status):
            raise ValueError("fail_on_status values must be between 100 and 599")
        if self.selector is not None and (self.full_page or not is_image):
            raise ValueError("selector requires full_page=false and an image output")
        if self.clip is not None and (self.full_page or not is_image):
            raise ValueError("clip requires full_page=false and an image output")
        if self.clip is not None and self.selector is not None:
            raise ValueError("clip and selector are mutually exclusive")
        if self.viewports is not None:
            if not is_image:
                raise ValueError("viewports requires PNG, JPEG, WebP, or AVIF output")
            if self.full_page:
                raise ValueError("viewports requires full_page=false")
            if self.selector is not None or self.clip is not None:
                raise ValueError("viewports cannot be combined with selector or clip")
            names = [viewport.name for viewport in self.viewports]
            if len(set(names)) != len(names):
                raise ValueError("viewports names must be unique")
        if self.diagnostics.bundle and self.viewports is not None:
            raise ValueError("diagnostic bundles cannot be combined with multi-viewports")
        advanced_diagnostics = (
            self.diagnostics.include_har
            or self.diagnostics.include_trace
            or self.diagnostics.include_warc
        )
        if advanced_diagnostics and not self.diagnostics.bundle:
            raise ValueError("HAR, trace, and WARC require diagnostics.bundle=true")
        if self.certification.enabled and self.viewports is not None:
            raise ValueError("certification cannot be combined with multi-viewports")
        if is_video:
            if self.video is None:
                self.video = VideoOptions()
            if self.selector is not None or self.clip is not None or self.viewports is not None:
                raise ValueError("video cannot use selector, clip, or multi-viewports")
            if self.diagnostics.include_trace:
                raise ValueError("video diagnostics support console, network, HAR, and WARC")
        elif self.video is not None:
            raise ValueError("video settings require WebM, MP4, or GIF output")
        if self.slices is not None and (not is_image or not self.full_page or self.viewports is not None):
            raise ValueError("slices require a single full-page image output")
        if self.cache and (not is_image or self.viewports is not None):
            raise ValueError("cache is accepted only for a single PNG, JPEG, WebP, or AVIF output")
        if self.cache and self.diagnostics.bundle:
            raise ValueError("cache cannot be combined with a diagnostic bundle")
        if self.cache and self.profile_id is not None:
            raise ValueError("cache cannot be combined with a persistent profile")
        if self.image.quality is not None and self.output not in {
            OutputFormat.JPEG,
            OutputFormat.WEBP,
            OutputFormat.AVIF,
        }:
            raise ValueError("quality is accepted only for JPEG, WebP, or AVIF")
        if self.image.transparent_background and self.output not in {
            OutputFormat.PNG,
            OutputFormat.WEBP,
            OutputFormat.AVIF,
        }:
            raise ValueError("transparent_background is accepted only for PNG, WebP, or AVIF")
        if self.image.optimize_for_speed and self.output not in {
            OutputFormat.PNG,
            OutputFormat.WEBP,
        }:
            raise ValueError("optimize_for_speed is accepted only for PNG or WebP")
        if (
            self.image.optimize_for_speed
            and self.engine.value != BrowserEngine.CHROMIUM.value
        ):
            raise ValueError("optimize_for_speed requires the Chromium engine")
        if self.image.width is not None or self.image.height is not None:
            if not is_image or self.viewports is not None or self.slices is not None:
                raise ValueError("image width and height require a single image output")
        if self.pdf is not None and self.output is not OutputFormat.PDF:
            raise ValueError("pdf settings require PDF output")
        if self.output is OutputFormat.PDF and self.pdf is None:
            self.pdf = PdfOptions()
        if (
            self.output is OutputFormat.PDF
            and self.engine.value != BrowserEngine.CHROMIUM.value
        ):
            raise ValueError("PDF output requires the Chromium engine")
        if self.pdf is not None and self.pdf.mode is PdfMode.SINGLE_PAGE and self.pdf.page_ranges:
            raise ValueError("single-page PDF cannot use page_ranges")
        if self.extract_mode is not ExtractMode.DOCUMENT and self.output not in {
            OutputFormat.HTML,
            OutputFormat.MARKDOWN,
        }:
            raise ValueError("article extraction requires HTML or Markdown output")
        if self.include_shadow_dom and self.output not in {
            OutputFormat.HTML,
            OutputFormat.MARKDOWN,
        }:
            raise ValueError("include_shadow_dom requires HTML or Markdown output")
        self.headers = _validate_headers(self.headers)
        return self

    @property
    def source_type(self) -> str:
        if self.url is not None:
            return "url"
        if self.html is not None:
            return "html"
        return "markdown"

    @property
    def credit_cost(self) -> int:
        if self.viewports is not None:
            return len(self.viewports)
        return 2 if self.output is OutputFormat.PDF else 1

    @property
    def recorded_output_type(self) -> str:
        return "zip" if (
            self.viewports is not None
            or self.slices is not None
            or self.certification.enabled
            or self.diagnostics.bundle
        ) else self.output.value


def canonical_render_document(
    request: RenderRequest,
    **dump_options,
) -> dict[str, object]:
    """Serialize request collections deterministically across processes."""
    document = request.model_dump(mode="json", **dump_options)
    network = document.get("network")
    if isinstance(network, dict) and isinstance(
        network.get("block_resource_types"), list
    ):
        network["block_resource_types"] = sorted(
            network["block_resource_types"]
        )
    return document
