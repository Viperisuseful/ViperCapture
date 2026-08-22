# API guide

The API rejects unknown request fields. Each render request requires exactly
one of `url`, `html`, or `markdown`. `RenderRequest` in
`vipercapture/render_contract.py` is the canonical schema. When the service is
running, the interactive OpenAPI reference is available at `/docs`.

## Outputs

| `output` | Response | Notes |
| --- | --- | --- |
| `png`, `jpeg`, `webp`, `avif` | image | Full page, viewport, selector, clip, resize, slices, or multi-viewport |
| `pdf` | PDF | Print or single-page mode, paper/orientation/margins, optional tags |
| `html` | UTF-8 HTML | Fully rendered document or article extraction |
| `markdown` | UTF-8 Markdown | Document or readability-based article extraction |
| `metadata` | JSON | Title, description, canonical URL, headings, links, images, and optional CSS-selector extraction |
| `webm`, `mp4`, `gif` | video | Full-page top-to-bottom animation, or a 1–30 second viewport recording |

`viewports` accepts two or three named image viewports and returns a ZIP with a
manifest. `diagnostics.bundle` returns a ZIP containing the normal artifact,
manifest, and optionally bounded console and network event files.
WebM setup/navigation frames are trimmed from the recording with Playwright's
FFmpeg runtime, and `duration_ms` reports the verified encoded duration rather
than merely echoing the request.

Full-page GIF and WebM output pans from the top to the bottom of the prepared
document over `video.duration_ms`. Set `video.transparent_background=true` to
keep transparent page and side padding; opaque output uses black padding. MP4
does not support transparency. With `full_page=false`, video records the live
viewport and `video.scroll` optionally adds stepped scrolling.
`video.fps` selects 1–60 FPS and `video.bitrate_mbps` selects a 1–100 Mbps
target for WebM and MP4; defaults are 60 FPS and 20 Mbps. GIF output uses the
selected frame rate and keeps the requested viewport size with a generated
palette. When GPU mode is enabled, ViperCapture probes FFmpeg's NVIDIA, AMD,
Intel, Apple, Windows Media Foundation, and Linux VA-API encoders with the
installed driver before using one. Hardware video uses the requested bitrate
and falls back to the software encoder if the probe or the real encode fails.
Transparent WebM and GIF remain software encoded because portable hardware
alpha-video and GIF encoders are not available.

Set `engine` to `chromium` (default), `firefox`, or `webkit`. Browsers start on
first use and each request still receives a fresh isolated context. PDF and
`image.optimize_for_speed` are Chromium-only; unsupported combinations fail
validation instead of silently changing engines. WebP and AVIF on Firefox or
WebKit are captured losslessly and converted by the bounded image pipeline.

`image.width` and `image.height` constrain the final image while preserving its
aspect ratio. HTML and Markdown requests may set `include_shadow_dom` to embed
open shadow roots as declarative shadow DOM. PDF options include A0–A6, Legal,
Letter, and Tabloid paper, optional page ranges, and bounded header/footer
templates. Metadata includes icons, loaded fonts, forms, and JSON-LD samples.
For structured extraction, set `output` to `metadata` and pass up to 32 unique
`elements` selectors. The response reports every selector's total match count
and includes up to 100 results across the request, with bounded text, inner
HTML, attributes, and viewport-relative geometry:

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

Set `pdf.tagged` explicitly to request or suppress Chromium's accessible PDF
structure tags. Omitting it preserves Chromium's default. Tags alone do not
guarantee PDF/UA conformance; source semantics and independent validation still
matter.

## Configure the browser

- `environment`: device preset, color scheme, reduced motion, CSS media, locale,
  and timezone. Set `media` to `screen` or `print` to apply it before the target
  loads. When omitted, screenshots keep browser screen media and PDFs use print.
- `network`: target-page JavaScript, user agent, geolocation, proxy, cookies,
  CSP/HTTPS controls, URL glob blocks, and resource-type blocks. Set
  `java_script_enabled: false` to prevent target-provided scripts from running.
- `headers`: bounded target headers. Credential-bearing headers are stripped
  from cross-origin requests.
- `wait_for`: load event, selector plus `visible`, `attached`, `hidden`, or
  `detached` state, optional complete-image readiness, body text, delay, and
  timeout. Image readiness eagerly requests current lazy images and repeats
  after full-page lazy loading.
- `cleanup`: consent mode and ad/tracker/chat/newsletter blocking.
- `custom_css`: up to 64 KiB of injected CSS.
- `stealth`: applies balanced, request-aware automation evasions by default;
  set it to `false` for debugging or strict browser-parity tests.
- `captcha`: chooses `error` (default), `capture`, or an operator-provided
  `external` handler when a blocking challenge is detected. `solver` is a
  non-secret operator routing alias, never a provider credential.

Self-hosted mode accepts an HTTP, HTTPS, SOCKS4, or SOCKS5 proxy in
`network.proxy`. Credentials are separate fields and are never embedded in the
proxy URL:

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

The proxy is attached to the isolated browser context, so navigation and page
subresources use it. Operators can disable it with
`VIPERCAPTURE_ALLOW_CUSTOM_PROXIES=0`. Hosted mode defaults to disabled and
must opt in with `VIPERCAPTURE_ALLOW_CUSTOM_PROXIES=1`. Proxy access does not
replace container/host egress controls.

CAPTCHA detection recognizes blocking interstitials from Cloudflare,
reCAPTCHA, hCaptcha, Arkose Labs, DataDome, AWS WAF, GeeTest, Friendly Captcha,
MTCaptcha, Imperva, and HUMAN/PerimeterX, including widgets inside open shadow
roots. An ordinary embedded widget does not fail a capture until it becomes a
blocking challenge. Detection is heuristic and returns the provider, kind,
confidence, and signals in the error details; ViperCapture does not solve or
bypass CAPTCHAs.

An authorized caller can also complete an access flow with an independent
external tool, then submit a fresh render using short-lived, target-scoped
cookies, a profile, or exact-origin headers. Keep external-service credentials
outside render payloads. This does not invoke the operator handler or relax
exact-origin header routing or, in hosted mode, the target-domain check for
`network.cookies` and public-address/redirect validation. Imported profiles are
not target-filtered. Self-host mode allows internal targets, so its operator
must enforce private and metadata-network blocks with an egress policy.
ViperCapture has no affiliation with or built-in integration for those tools;
see [site access](site-access.md).

Hosted mode also rejects non-public targets and subresources, cookies outside
the target site, and unsafe redirects.

## Import browser sessions

With the project control plane enabled, `POST /v1/profiles/import` accepts up
to 5 MiB of pasted/exported session data and encrypts the normalized Playwright
storage state with `VIPERCAPTURE_CONTROL_SECRET`. Supported formats are:

- `playwright`: a Playwright `storageState` JSON object, including localStorage
- `cookies_json`: the common Cookie-Editor-style JSON array exported by Chrome,
  Edge, Brave, Firefox, and compatible browsers
- `netscape`: a Netscape `cookies.txt` export
- `cookie_header`: a pasted `Cookie` request header; this requires `origin`
- `auto`: detects one of the above from its content

For example, import an exported file without putting its secrets on the command
line, then use the returned `id` as `profile_id`:

```bash
jq -Rs '{format:"auto", content:.}' browser-cookies.txt |
  curl --fail-with-body http://127.0.0.1:8000/v1/profiles/import \
    -H "Authorization: Bearer $VIPERCAPTURE_API_KEY" \
    -H 'Content-Type: application/json' --data-binary @-
```

For a pasted header, send `{"format":"cookie_header",
"origin":"https://example.com","content":"Cookie: sid=..."}`. Session exports
are credentials: avoid shell history, logs, shared folders, and unencrypted
transport. ViperCapture intentionally does not read or decrypt live browser
profile databases on a user's machine.

## Actions and assertions

Actions run in array order after navigation, waits, CSS, and initial cleanup:

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

Supported action types are `click`, `hover`, `fill`, `press`, `select`,
`scroll`, `wait`, `hide`, and `javascript`. JavaScript requires the operator to
set `VIPERCAPTURE_ALLOW_SCRIPTS=1`.

## Submit async, bulk, and scheduled work

`X-Request-Id` is a correlation value for logs and support. ViperCapture
preserves a valid caller value or generates one; reusing it never deduplicates
work. `POST /v1/jobs` returns 202 with a `Location` header. Send a stable
`Idempotency-Key` to make creation retries safe:

```bash
curl -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Authorization: Bearer $VIPERCAPTURE_API_KEY" \
  -H "Idempotency-Key: homepage-2026-08-12" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","output":"png"}'
```

The same key and normalized request replay the original job. The same key with
a changed request returns 409 `idempotency_key_conflict`. Without the header,
each submission creates a new job. Keys are project-scoped. Poll status and
retrieve the result URL advertised in the job document. Only queued jobs can
be cancelled.

`POST /v1/jobs/bulk` accepts:

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

Each processed envelope returns HTTP 200, including mixed or all-item failure.
Every result contains `status`, `accepted`, `job`, and `error`; malformed,
oversized, unauthenticated, or unavailable requests still use the normal
request-level 4xx/5xx status. A bulk `Idempotency-Key` derives stable keys by
item index, fingerprints the normalized whole envelope, and atomically claims
that fingerprint before creating any item. Alternatively use per-item
`idempotency_key` values; do not combine both forms. A retry reuses accepted
jobs and retries rejected items.

Project RPM exhaustion returns 429 `rate_limit_exceeded` with `Retry-After`
calculated from the oldest event in the sliding 60-second window. Render
concurrency exhaustion returns the distinct `concurrency_limit_exceeded` code
with a short heuristic delay. Payload/body limits use 413; persistent profile,
baseline, and schedule project quotas use 403 with their specific error code
and available `limit_count`, `used_count`, `limit_bytes`, and `used_bytes`
details.

`PUT /v1/baselines/{name}` returns 201 and `Location` when it creates a
baseline, or 200 when it replaces one.

Create a recurring job with `POST /v1/schedules`:

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

Schedules use exactly five cron fields. The next occurrence is calculated in
the selected IANA timezone and stored in UTC. Inputs are encrypted with the
same key as jobs. The scheduler advances the due time before idempotently
submitting work, preventing a crash from creating a tight duplicate loop.

## Create signed links

Set a random secret of at least 32 bytes in `VIPERCAPTURE_SIGNING_SECRET` and a
separate administrator token in `VIPERCAPTURE_SIGNING_ADMIN_TOKEN`. Then:

```bash
curl http://127.0.0.1:8000/v1/signed-url?ttl_seconds=3600 \
  -H "Authorization: Bearer $VIPERCAPTURE_SIGNING_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","full_page":false}'
```

The returned GET URL contains a canonical base64url payload, absolute expiry,
and versioned HMAC-SHA256 signature. The server rejects tampering, expiry, and
links whose expiry exceeds the seven-day maximum. Large embedded HTML or
Markdown inputs must use POST because signed links are limited to URL-sized payloads.
Signing authenticates the payload; it does not encrypt it. Do not place secret
headers, cookies, HTML, or Markdown in a URL that will be shared or logged.

## Verify webhooks

Webhook bodies use canonical compact JSON. Verify the signature over
`timestamp + "." + raw_body`:

```python
import hashlib, hmac

expected = "v1=" + hmac.new(
    WEBHOOK_SECRET.encode(),
    timestamp.encode() + b"." + raw_body,
    hashlib.sha256,
).hexdigest()
assert hmac.compare_digest(expected, signature_header)
```

Callbacks have a stable event ID, never follow redirects, and retry bounded
transport, 429, and server failures. Delivery failure does not rerender or
change a successfully completed job. DNS is resolved once per delivery and the
connection is pinned to the validated address while retaining the original
Host and TLS server name. A terminal transition atomically creates an encrypted
outbox entry; failed delivery remains pending across restarts until it is
acknowledged.

## Configure S3-compatible storage

Set `VIPERCAPTURE_S3_BUCKET`. Optional variables:

| Variable | Purpose |
| --- | --- |
| `VIPERCAPTURE_S3_ENDPOINT_URL` | R2, MinIO, B2, or other compatible endpoint |
| `VIPERCAPTURE_S3_REGION` | Provider region; defaults to `us-east-1` |
| `VIPERCAPTURE_S3_ADDRESSING_STYLE` | `auto`, `virtual`, or `path` |
| `VIPERCAPTURE_S3_PREFIX` | Object key prefix |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Standard SDK credentials |

The adapter writes exclusive, unguessable object keys plus ViperCapture owner
and expiry metadata. Maintenance deletes only objects with the expected
prefix/UUID key shape and owner marker; unrelated objects under a shared prefix
are never selected solely because of age. Also configure a provider lifecycle
rule so objects still expire if application maintenance does not run.

## Compare images

```bash
curl http://127.0.0.1:8000/v1/diff \
  -F baseline=@baseline.png -F current=@current.png \
  -F pixel_threshold=8 -F max_difference_ratio=0.001 \
  --output visual-diff.zip
```

Inputs must have identical dimensions, be no larger than 20 MiB each, and
contain no more than 8 million expanded pixels each. Diff computation runs in
a bounded worker lane (`VIPERCAPTURE_DIFF_CONCURRENCY`, default 1) because
decoded RGBA and mask buffers are substantially larger than compressed inputs.
`report.json` records changed/total pixels, difference ratio, pass/fail, and
the changed bounding box. `diff.png` highlights changes in magenta.

## Configuration variables

See [self-hosting](self-hosting.md) for the security boundary and
[async jobs](async-jobs.md) for queue/provider durability. Important feature
flags include `VIPERCAPTURE_ASYNC_JOBS`, `VIPERCAPTURE_SCHEDULES`,
`VIPERCAPTURE_ALLOW_SCRIPTS`, `VIPERCAPTURE_WEBHOOK_SECRET`,
`VIPERCAPTURE_SIGNING_SECRET`, `VIPERCAPTURE_CACHE_TTL_SECONDS`,
`VIPERCAPTURE_CACHE_MAX_ENTRIES`, and `VIPERCAPTURE_CACHE_MAX_BYTES` (default
512 MiB).
