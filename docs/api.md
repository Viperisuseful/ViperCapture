# API and workflow guide

All request models reject unknown fields. Exactly one of `url`, `html`, or
`markdown` is required. The canonical schema is `RenderRequest` in
`render_contract.py`; FastAPI also exposes interactive OpenAPI documentation at
`/docs` when the service is running.

## Outputs

| `output` | Response | Notes |
| --- | --- | --- |
| `png`, `jpeg`, `webp` | image | Full page, viewport, selector, clip, or multi-viewport |
| `pdf` | PDF | Print or single-page mode, paper/orientation/margins |
| `html` | UTF-8 HTML | Fully rendered document or article extraction |
| `markdown` | UTF-8 Markdown | Document or readability-based article extraction |
| `metadata` | JSON | Title, description, canonical URL, headings, links, images |
| `webm` | video | 1–30 second post-preparation window, stationary or stepped scrolling |

`viewports` accepts two or three named image viewports and returns a ZIP with a
manifest. `diagnostics.bundle` returns a ZIP containing the normal artifact,
manifest, and optionally bounded console and network event files.
WebM setup/navigation frames are trimmed from the recording with Playwright's
FFmpeg runtime, and `duration_ms` reports the verified encoded duration rather
than merely echoing the request.

## Browser preparation

- `environment`: device preset, color scheme, reduced motion, locale, timezone.
- `network`: user agent, geolocation, proxy, cookies, CSP/HTTPS controls, URL
  glob blocks, and resource-type blocks.
- `headers`: bounded target headers. Credential-bearing headers are stripped
  from cross-origin requests.
- `wait_for`: load event, visible selector, body text, delay, and timeout.
- `cleanup`: consent mode and ad/tracker/chat/newsletter blocking.
- `custom_css`: up to 64 KiB of injected CSS.

Hosted mode intentionally rejects per-request proxies, non-public targets and
subresources, cookies outside the target site, and unsafe redirects.

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

## Async, bulk, and schedules

`POST /v1/jobs` returns 202. Send a stable `X-Request-Id` to make retries
idempotent. Poll status and retrieve the result URL advertised in the job
document. Only queued jobs can be cancelled.

`POST /v1/jobs/bulk` accepts:

```json
{
  "items": [
    {"id": "desktop", "request_id": "release-42-desktop", "render": {
      "url": "https://example.com", "full_page": false
    }},
    {"id": "mobile", "render": {
      "url": "https://example.com", "full_page": false,
      "viewport": {"width": 390, "height": 844}
    }}
  ]
}
```

Each item is submitted independently. HTTP 207 means some items were rejected;
inspect every result.

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

## Signed links

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
Markdown inputs must use POST because signed links are intentionally URL-sized.
Signing authenticates the payload; it does not encrypt it. Do not place secret
headers, cookies, HTML, or Markdown in a URL that will be shared or logged.

## Webhook verification

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

## S3, R2, and MinIO

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
rule as crash-safe defense in depth.

## Visual differences

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

## Configuration index

See [self-hosting](self-hosting.md) for the security boundary and
[async jobs](async-jobs.md) for queue/provider durability. Important feature
flags include `VIPERCAPTURE_ASYNC_JOBS`, `VIPERCAPTURE_SCHEDULES`,
`VIPERCAPTURE_ALLOW_SCRIPTS`, `VIPERCAPTURE_WEBHOOK_SECRET`,
`VIPERCAPTURE_SIGNING_SECRET`, `VIPERCAPTURE_CACHE_TTL_SECONDS`, and
`VIPERCAPTURE_CACHE_MAX_ENTRIES`.
