# API guide

The ViperCapture API rejects unknown request fields and requires every render request to provide exactly one of `url`, `html`, or `markdown`. `RenderRequest` in `vipercapture/render_contract.py` defines the schema, and a running server serves the interactive OpenAPI reference at `/docs`.

`vipercapture capture` sends this contract from the shell. Run `vipercapture capture --help` to see the flag for each major field, including `--from-json`, `--preset`, `--cookie-file`, `--js-file`, `--action-wait-text`, `--urls-file`, `--output-dir`, `--output-template`, `--retries`, `--retry-delay-ms`, `--async --no-wait`, `--open`, `--copy`, `--stop-server`, and `--certify`. Defaults can live in `~/.vipercapture/config.toml` (pass `--no-config` to `capture` to ignore it).

Companion CLI subcommands cover the rest of the API: `version` (`--version`, `-V`), `status`, `logs`, `gpu`, `preset`, `diff`, `baseline`, `bulk`, `job`, `schedule`, `profile`, `sign`, `verify`, `admin`, and `completion`. See [self-hosting](self-hosting.md#local-commands) for the full command reference.

## Outputs

| `output` | Response | Notes |
| --- | --- | --- |
| `png`, `jpeg`, `webp`, `avif` | Image | Full page, viewport, selector, clip, resize, slices, or multi-viewport |
| `pdf` | PDF | Print or single-page mode, paper size, orientation, margins, and optional tags |
| `html` | UTF-8 HTML | Rendered document or article extraction |
| `markdown` | UTF-8 Markdown | Document or readability-based article extraction |
| `metadata` | JSON | Title, description, canonical URL, headings, links, images, and optional CSS-selector extraction |
| `webm`, `mp4`, `gif` | Video | Full-page top-to-bottom pan, or a 1 to 30 second viewport recording |

### Engines, profiles, and caching

Set `engine` to `chromium` (default), `firefox`, or `webkit`. Each request runs in a fresh isolated browser context. Chromium starts in a managed pool; Firefox and WebKit start on first request. PDF output and `image.optimize_for_speed` require Chromium, and unsupported engine combinations fail validation instead of switching engines silently. On Firefox and WebKit, ViperCapture captures lossless frames and converts WebP and AVIF through the bounded image pipeline.

`profile` selects speed or completeness defaults:

- **`preview` (default).** Uses adaptive full-page lazy loading and Chromium's faster PNG and WebP encoder.
- **`accurate`.** Uses thorough lazy scrolling and the slower lossless encoder.

Explicit `lazy_load` and `image.optimize_for_speed` fields override the profile. Deterministic captures leave the fast encoder off unless you set it explicitly. Successful image responses include `X-ViperCapture-Encode-Ms` and `X-ViperCapture-Profile`.

Set `cache: true` on a single-image request to reuse cached results matching the exact request fingerprint for `VIPERCAPTURE_CACHE_TTL_SECONDS` (24 hours by default). Set `cache_key` to store distinct cached variants of the same request.

### Images, HTML, Markdown, PDF, and metadata

`image.width` and `image.height` resize the output image while keeping its aspect ratio. For `html` and `markdown` outputs, set `include_shadow_dom: true` to serialize open shadow roots as declarative shadow DOM.

PDF output supports A0 to A6, Legal, Letter, and Tabloid paper sizes, optional page ranges, and bounded header and footer templates. Set `pdf.tagged` explicitly to enable or disable Chromium's accessible PDF structure tags, or omit it to keep Chromium's default. Structure tags alone do not guarantee PDF/UA conformance; source document semantics and independent validation still apply.

`metadata` output includes icons, loaded fonts, forms, and JSON-LD samples. For structured extraction, pass up to 32 unique `elements` selectors. ViperCapture reports each selector's total match count and returns up to 100 matched items per request, including bounded text, inner HTML, attributes, and viewport-relative geometry:

```json
{
  "url": "https://example.com/products",
  "output": "metadata",
  "elements": [
    {"selector": "h1"},
    {"selector": ".product-card"}
  ]
}
```

### Multi-viewport packs, artifact bundles, and diagnostics

- **Multi-viewport packs.** Pass two or three named entries in `viewports` for image output to receive a ZIP archive with a manifest.
- **Artifact bundles.** Request a primary image together with unique `side_outputs` (`html`, `markdown`, `metadata`, or Chromium-only `mhtml`) and up to five named `image.thumbnails`. ViperCapture returns a ZIP with the primary image, requested side artifacts, thumbnails, and a manifest containing media types, sizes, dimensions, and SHA-256 digests. Thumbnail dimensions range from 10 to 2,000 pixels and preserve aspect ratio within the requested bounds. Artifact bundles cannot be combined with viewport packs, slices, diagnostics, certification, or cache. Handle `mhtml` with the same care as the source page because it can contain embedded resources and authenticated content.

```json
{
  "url": "https://example.com/products",
  "output": "png",
  "side_outputs": ["html", "markdown", "metadata", "mhtml"],
  "elements": [{"selector": ".product-card"}],
  "image": {
    "thumbnails": [
      {"name": "card", "width": 640},
      {"name": "preview", "width": 320, "height": 180}
    ]
  }
}
```

- **Diagnostic bundles.** Set `diagnostics.bundle: true` to receive a ZIP with the primary artifact, a manifest, and optional bounded console and network logs.
- **Redacted HAR 1.2.** Set `diagnostics.include_har: true` to include `network.har` with observed HTTP versions, allowlisted headers, MIME types, and timings. ViperCapture omits query strings, cookies, credentials, and bodies, and emits header values only for an allowlist of non-credential names. It strips query strings, fragments, and matrix or path parameters such as `;jsessionid=` from URL-bearing headers like `Location`, `Link`, and `Refresh`, even when relative or unquoted. It redacts `Content-Disposition` filenames and keeps `ETag` values as opaque cache validators. `entry.time` excludes the HAR `ssl` subset of `connect`. Compressed responses keep `Content-Length` as transferred `bodySize` and leave decoded `content.size` unknown. Chromium records HTTP versions from CDP; Firefox and WebKit backfill `nextHopProtocol` from Resource Timing, and HTTPS entries without that metadata keep `httpVersion` as `unknown`.

### Video and GIF output

Full-page GIF and WebM renders pan from the top to the bottom of the prepared document over `video.duration_ms`. Set `video.transparent_background: true` to preserve transparent page and side padding; opaque video uses black padding. MP4 does not support transparency. With `full_page: false`, video records the live viewport, and `video.scroll` adds optional stepped scrolling.

`video.fps` accepts 1 to 60 FPS (default 60), and `video.bitrate_mbps` accepts 1 to 100 Mbps (default 20) for WebM and MP4. GIF uses the requested frame rate and viewport dimensions with a generated color palette. ViperCapture trims WebM setup and navigation frames with Playwright's FFmpeg runtime, and `duration_ms` reports the verified encoded duration rather than echoing the request value.

When GPU mode is enabled, ViperCapture probes FFmpeg's NVIDIA, AMD, Intel, Apple, Windows Media Foundation, and Linux VA-API encoders with the installed driver before selecting one. Hardware encoding honors the requested bitrate and falls back to software encoding if the probe or encode fails. Transparent WebM and GIF stay software-encoded because portable hardware alpha-video and GIF encoders are unavailable.

## Configure the browser

- **`environment`.** Sets device preset, color scheme, reduced motion, CSS media, locale, and timezone. A non-desktop `device` (`iphone_14`, `pixel_7`, `ipad`) applies the Playwright descriptor for user agent, viewport, screen, `device_scale_factor`, and touch. Explicit `viewport.width`, `viewport.height`, or `viewport.device_scale_factor` values override the descriptor, so `{"environment": {"device": "iphone_14"}}` captures at iPhone 14 metrics on its own. Set `media` to `screen` or `print` to apply CSS media before page load; when omitted, screenshots use screen media and PDFs use print.
- **`network`.** Controls target-page JavaScript, user agent, geolocation, proxy, cookies, CSP and HTTPS checks, URL glob blocks, and resource-type blocks. Set `java_script_enabled: false` to block scripts delivered by the target page.
- **`headers`.** Sends bounded headers to the target origin and strips credential-bearing headers from cross-origin requests.
- **`wait_for`.** Configures the navigation load event, a CSS selector with state `visible`, `attached`, `hidden`, or `detached`, complete-image readiness, body text, delay, and timeout. Image readiness requests current lazy images and repeats the check after full-page lazy scrolling.
- **`cleanup`.** Controls cookie consent handling and ad, tracker, chat, and newsletter blocking.
- **`custom_css`.** Injects up to 64 KiB of CSS.
- **`stealth`.** Applies balanced, request-aware automation evasions by default on standard Playwright. Set `stealth: false` for debugging or strict browser-parity tests. For Patchright and harder Turnstile challenge handling, see [ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth) (`0.1.0-beta`).
- **`captcha`.** Chooses `error` (default), `capture`, or an operator-provided `external` handler when ViperCapture detects a blocking challenge. `solver` is a non-secret operator routing label, never a provider credential.

### Proxies

Self-hosted mode accepts an HTTP, HTTPS, SOCKS4, or SOCKS5 proxy in `network.proxy`. Keep credentials in separate fields instead of embedding them in the proxy URL:

```json
{
  "url": "https://example.com",
  "network": {
    "proxy": {
      "server": "socks5://proxy.example:1080",
      "username": "account-zone-residential",
      "password": "secret",
      "bypass": ".internal.example"
    }
  }
}
```

ViperCapture attaches the proxy to the isolated browser context so navigation and subresources both use it. Operators can disable custom proxies with `VIPERCAPTURE_ALLOW_CUSTOM_PROXIES=0`. Hosted mode disables custom proxies by default; set `VIPERCAPTURE_ALLOW_CUSTOM_PROXIES=1` to opt in. Proxy routing does not replace container or host egress controls.

### CAPTCHA detection and authorized access

ViperCapture detects blocking interstitials from Cloudflare, reCAPTCHA, hCaptcha, Arkose Labs, DataDome, AWS WAF, GeeTest, Friendly Captcha, MTCaptcha, Imperva, and HUMAN/PerimeterX, including widgets inside open shadow roots. An ordinary embedded widget does not fail a capture until it blocks the page. Detection is heuristic and reports the provider, kind, confidence, and signals in the error details. ViperCapture does not solve or bypass CAPTCHAs and does not claim a Cloudflare bypass. For Patchright and harder Turnstile handling, see [ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth) (`0.1.0-beta`).

An authorized caller can complete a site's access flow outside ViperCapture with an independent tool, then submit a fresh render with short-lived, target-scoped cookies, a profile, or exact-origin headers. Keep external service credentials out of render payloads. This handoff does not call the operator CAPTCHA handler or relax exact-origin header routing. In hosted mode, ViperCapture keeps its target-domain check for `network.cookies` and rejects non-public targets, non-public subresources, and unsafe redirects. Imported profiles are not target-filtered, and self-host mode allows internal targets, so operators must block private and metadata networks with an egress policy. ViperCapture has no affiliation with or built-in integration for external tools. See [site access](site-access.md).

## Import browser sessions

When the project control plane is enabled, `POST /v1/profiles/import` accepts up to 5 MiB of exported session data and encrypts the normalized Playwright storage state with `VIPERCAPTURE_CONTROL_SECRET`.

| Format | Description |
| --- | --- |
| `playwright` | Playwright `storageState` JSON object, including `localStorage` |
| `cookies_json` | Cookie-Editor-style JSON array exported from Chrome, Edge, Brave, Firefox, or compatible browsers |
| `netscape` | Netscape `cookies.txt` export |
| `cookie_header` | Pasted `Cookie` request header; requires `origin` |
| `auto` | Detects one of the formats above from the input content |

Import a file through stdin so secrets stay off the command line, then pass the returned `id` as `profile_id`:

```bash
jq -Rs '{format:"auto", content:.}' browser-cookies.txt |
  curl --fail-with-body http://127.0.0.1:8000/v1/profiles/import \
    -H "Authorization: Bearer $VIPERCAPTURE_API_KEY" \
    -H 'Content-Type: application/json' --data-binary @-
```

For a pasted header, send `{"format":"cookie_header","origin":"https://example.com","content":"Cookie: sid=..."}`. Treat session exports as credentials and keep them out of shell history, logs, shared folders, and unencrypted transport. ViperCapture never reads or decrypts live browser profile databases on disk.

## Actions and assertions

ViperCapture runs `actions` in array order after navigation, waits, custom CSS, and initial cleanup:

```json
{
  "url": "https://example.com/form",
  "full_page": false,
  "actions": [
    {"type": "fill", "selector": "#email", "value": "person@example.com"},
    {"type": "select", "selector": "#plan", "values": ["starter"]},
    {"type": "click", "selector": "button[type=submit]"},
    {"type": "wait", "selector": "#success", "timeout_ms": 10000}
  ],
  "assertions": {
    "content_includes": ["Thank you"],
    "content_excludes": ["Server error"],
    "request_failures": ["*api.example.com/checkout*"]
  }
}
```

Supported action types are `click`, `hover`, `fill`, `press`, `select`, `scroll`, `wait`, `hide`, and `javascript`. The `javascript` action requires the operator to set `VIPERCAPTURE_ALLOW_SCRIPTS=1`.

## Submit async, bulk, and scheduled work

### Single async jobs

`X-Request-Id` is a correlation ID for logs and support. ViperCapture preserves a valid caller value or generates one, and reusing it never deduplicates jobs. `POST /v1/jobs` returns HTTP 202 with a `Location` header. Send a stable `Idempotency-Key` to make creation retries safe:

```bash
curl -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Authorization: Bearer $VIPERCAPTURE_API_KEY" \
  -H "Idempotency-Key: homepage-2026-08-12" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","output":"png"}'
```

Sending the same key and normalized body replays the original job. Sending the same key with a different body returns HTTP 409 `idempotency_key_conflict`. Without `Idempotency-Key`, every request creates a new job. Keys are scoped per project. Poll the job document and download the artifact from the result URL it advertises. You can cancel only jobs that are still `queued`.

### Bulk jobs

`POST /v1/jobs/bulk` accepts up to 100 items:

```json
{
  "items": [
    {"id": "primary", "idempotency_key": "release-42-primary", "render": {
      "url": "https://example.com", "full_page": false
    }},
    {"id": "mobile", "render": {
      "url": "https://example.com", "full_page": false,
      "viewport": {"width": 390, "height": 844}
    }}
  ]
}
```

ViperCapture returns HTTP 200 for any processed envelope, even when some or all items fail. Each item result contains `status`, `accepted`, `job`, and `error`. Malformed, oversized, unauthenticated, or unavailable requests still return normal 4xx or 5xx HTTP statuses.

A request-level `Idempotency-Key` derives stable keys by item index, fingerprints the normalized whole envelope, and atomically claims that fingerprint before creating any item. You can instead supply per-item `idempotency_key` values, but do not mix both forms. Retrying a bulk request reuses accepted jobs and retries rejected items.

### Rate limits, quotas, and baselines

- **Project RPM limit.** Returns HTTP 429 `rate_limit_exceeded` with `Retry-After` computed from the oldest event in the sliding 60-second window.
- **Render concurrency limit.** Returns HTTP 429 `concurrency_limit_exceeded` with a short heuristic delay.
- **Body and payload limits.** Returns HTTP 413.
- **Project resource quotas.** Persistent profile, baseline, and schedule quota violations return HTTP 403 with a specific error code and any applicable `limit_count`, `used_count`, `limit_bytes`, and `used_bytes` fields.
- **Baselines.** `PUT /v1/baselines/{name}` returns HTTP 201 with a `Location` header when creating a baseline, or HTTP 200 when replacing one.

### Schedules

Create a recurring capture with `POST /v1/schedules`:

```json
{
  "name": "Hourly home page",
  "cron": "0 * * * *",
  "timezone": "America/New_York",
  "render": {
    "url": "https://example.com",
    "full_page": true,
    "delivery": {"webhook_url": "https://hooks.example.com/vipercapture"}
  }
}
```

Schedules accept five-field cron expressions. ViperCapture calculates the next run in the chosen IANA timezone, stores it in UTC, and encrypts schedule inputs with the same key as async jobs. The scheduler advances the due timestamp before submitting work idempotently so a crash cannot trigger a duplicate loop.

## Create signed links

Set a random secret of at least 32 bytes in `VIPERCAPTURE_SIGNING_SECRET` and a separate token in `VIPERCAPTURE_SIGNING_ADMIN_TOKEN`, then mint a link:

```bash
curl http://127.0.0.1:8000/v1/signed-url?ttl_seconds=3600 \
  -H "Authorization: Bearer $VIPERCAPTURE_SIGNING_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","full_page":false}'
```

The returned GET URL carries a canonical base64url payload, an absolute expiry, and a versioned HMAC-SHA256 signature. ViperCapture rejects tampered signatures, expired links, and expiries longer than seven days. Use POST for large inline HTML or Markdown payloads because signed links must fit within URL length limits. Signing authenticates the payload without encrypting it, so never put secret headers, cookies, HTML, or Markdown in a URL that may be shared or logged.

## Verify webhooks

ViperCapture sends webhook bodies as canonical compact JSON. Verify `X-ViperCapture-Webhook-Signature` over `timestamp + "." + raw_body`:

```python
import hashlib, hmac

expected = "v1=" + hmac.new(
    WEBHOOK_SECRET.encode(),
    timestamp.encode() + b"." + raw_body,
    hashlib.sha256,
).hexdigest()
assert hmac.compare_digest(expected, signature_header)
```

Callbacks carry a stable event ID, never follow redirects, and retry bounded transport, HTTP 429, and 5xx errors. A failed callback never rerenders or alters a completed job. ViperCapture resolves DNS once per delivery attempt and pins the socket to the validated IP address while keeping the original `Host` header and TLS SNI. Each terminal job transition creates an encrypted outbox row atomically, so undelivered webhooks stay queued across restarts until acknowledged.

## Configure S3-compatible storage

Set `VIPERCAPTURE_S3_BUCKET` to enable S3-compatible artifact storage. Optional variables:

| Variable | Purpose |
| --- | --- |
| `VIPERCAPTURE_S3_ENDPOINT_URL` | Endpoint URL for Cloudflare R2, MinIO, Backblaze B2, or another compatible store |
| `VIPERCAPTURE_S3_REGION` | Provider region; defaults to `us-east-1` |
| `VIPERCAPTURE_S3_ADDRESSING_STYLE` | `auto`, `virtual`, or `path` |
| `VIPERCAPTURE_S3_PREFIX` | Object key prefix |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Standard AWS SDK credentials |

The adapter writes exclusive, unguessable object keys with ViperCapture owner and expiry metadata. Maintenance deletes only objects that match both the expected prefix and UUID key shape and the owner marker, so unrelated objects in a shared bucket are left untouched. Configure a bucket lifecycle rule as well so objects expire even if application maintenance does not run.

## Compare images

Upload a baseline and current image to `POST /v1/diff`:

```bash
curl http://127.0.0.1:8000/v1/diff \
  -F baseline=@baseline.png -F current=@current.png \
  -F pixel_threshold=8 -F max_difference_ratio=0.001 \
  --output visual-diff.zip
```

Both images must share the same dimensions, stay at or under 20 MiB each, and expand to at most 8 million pixels each. Diff work runs in a bounded worker lane (`VIPERCAPTURE_DIFF_CONCURRENCY`, default `1`) because decoded RGBA and mask buffers use far more memory than compressed files. The returned ZIP contains `report.json` (changed pixel count, total pixels, difference ratio, pass or fail status, and changed bounding box) and `diff.png` with changes highlighted in magenta.

## Configuration variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `VIPERCAPTURE_ASYNC_JOBS` | `1` (`0` on Windows) | Enable or disable async jobs |
| `VIPERCAPTURE_SCHEDULES` | `1` (`0` on Windows) | Enable or disable cron schedules |
| `VIPERCAPTURE_ALLOW_SCRIPTS` | `0` | Allow `javascript` render actions |
| `VIPERCAPTURE_WEBHOOK_SECRET` | unset | Secret for HMAC-SHA256 webhook signatures |
| `VIPERCAPTURE_SIGNING_SECRET` | unset | Secret for signed GET render URLs |
| `VIPERCAPTURE_CACHE_TTL_SECONDS` | `86400` | Exact-request image cache TTL in seconds |
| `VIPERCAPTURE_CACHE_MAX_ENTRIES` | `1000` | Maximum cached image entries |
| `VIPERCAPTURE_CACHE_MAX_BYTES` | `536870912` (512 MiB) | Maximum total cached image bytes |
| `VIPERCAPTURE_MAX_CONCURRENCY` | CPU-sized (2 to 8) | Maximum simultaneous renders |
| `VIPERCAPTURE_BROWSER_POOL_SIZE` | about half of concurrency | Chromium browser pool size |

## Related

- [Self-hosting and local setup](self-hosting.md)
- [Async jobs and queue providers](async-jobs.md)
- [Projects, API keys, and observability](platform.md)
- [Authorizing ViperCapture through Cloudflare or a WAF](site-access.md)
- [ScreenshotOne and Urlbox compatibility matrix](compatibility.md)
- [All docs](README.md)
