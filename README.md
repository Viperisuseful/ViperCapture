<p align="center">
  <img src="static/vipercapture-mark.svg" width="112" height="112" alt="ViperCapture logo">
</p>

<h1 align="center">ViperCapture</h1>

<p align="center"><strong>The open-source, self-hosted ScreenshotOne / Urlbox alternative.</strong></p>

ViperCapture is an MIT-licensed browser rendering platform. It turns URLs, HTML,
or Markdown into screenshots, PDFs, WebM video, hydrated HTML, Markdown, or
structured metadata—through a strict JSON API you can run on your own machines
and storage.

This project aims for practical workflow parity, not misleading one-for-one
option naming. See the dated, source-linked [compatibility matrix](docs/compatibility.md)
and run the [reproducible benchmark](benchmarks/README.md) on your workload.

## What ships

- PNG, JPEG, WebP, PDF, HTML, Markdown, metadata, and WebM output
- URL, raw HTML, and Markdown input; full-page, viewport, element, clip, and
  multi-viewport ZIP captures
- Typed click, hover, fill, select, key, scroll, wait, hide, and opt-in
  JavaScript actions
- Wait conditions, assertions, custom CSS, devices, locale/timezone,
  geolocation, cookies, user agent, proxy, resource blocking, and cleanup
- Ad, tracker, chat, newsletter, and consent-banner cleanup backed by the
  vendored, license-preserved AutoConsent rule set
- Durable encrypted async jobs, idempotency, retries, polling, cancellation,
  bulk submission, cron schedules, and signed webhook callbacks
- Private local result storage or built-in S3-compatible storage for AWS S3,
  Cloudflare R2, MinIO, Backblaze B2, and compatible providers
- Expiring HMAC-signed render URLs and a 15-minute exact-request image cache
- Visual regression ZIPs with pixel counts, pass/fail thresholds, bounds, and
  highlighted changes
- Privacy-aware diagnostic ZIPs with the artifact, manifest, console events,
  and a request waterfall stripped of credentials, headers, bodies, and query
  strings
- Bounded concurrency, output/pixel/deadline limits, client-disconnect
  cancellation, consistent error envelopes, and hosted-mode SSRF defenses
- Browser UI, Tauri desktop app, native Android app, Docker Compose, and a
  portable agent skill

## Start in one command

With Python 3.11 or newer:

```bash
git clone https://github.com/Viperisuseful/ViperCapture.git
cd ViperCapture
python launch.py
```

The launcher creates a virtual environment, installs Chromium, starts the API,
and opens `http://127.0.0.1:8000`.

Or use Docker:

```bash
docker compose up --build
```

The Compose default binds only to loopback and stores durable queue state,
encryption keys, schedules, cache entries, and local artifacts in a named
volume. Read [self-hosting](docs/self-hosting.md) before exposing it to a
network. The server has no built-in multi-tenant authorization layer.

## Render API

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/render \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com",
    "output": "png",
    "full_page": false,
    "viewport": {"width": 1280, "height": 720},
    "actions": [{"type": "hide", "selector": ".newsletter"}],
    "cleanup": {"block_ads": true, "consent_mode": "reject"},
    "assertions": {"content_includes": ["Example Domain"]}
  }' --output example.png
```

The synchronous response is the artifact itself. It includes a request ID,
queue/render timing, dimensions, navigation status, and cache outcome in
`X-ViperCapture-*` headers.

For durable work, send the same render object to `POST /v1/jobs`. Poll
`GET /v1/jobs/{id}`, download `GET /v1/jobs/{id}/result`, or receive a signed
callback by setting `delivery.webhook_url`. Related orchestration endpoints:

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/jobs/bulk` | Best-effort submission of up to 100 independently idempotent jobs |
| `POST/GET/PATCH/DELETE /v1/schedules` | Encrypted five-field cron schedules with IANA time zones |
| `POST /v1/signed-url` | Mint an expiring HMAC render link |
| `GET /v1/render/signed` | Render through a verified signed link |
| `POST /v1/diff` | Compare two images and download a deterministic report ZIP |

See the [API and workflows guide](docs/api.md), [async provider guide](docs/async-jobs.md),
and [migration guide](docs/migration-screenshotone-urlbox.md).

## Storage and webhooks

Set `VIPERCAPTURE_S3_BUCKET` to switch job results from local files to the
built-in S3 adapter. Standard AWS credential resolution is used. R2 and MinIO
add an endpoint URL and path-style addressing where appropriate. The
`docker-compose.s3.yml` overlay provides a complete local MinIO proof:

```bash
export MINIO_ROOT_PASSWORD='replace-with-at-least-a-long-random-secret'
docker compose -f docker-compose.yml -f docker-compose.s3.yml up --build
```

Set `VIPERCAPTURE_WEBHOOK_SECRET` to enable callbacks. Each body is canonical
JSON signed with HMAC-SHA256 in `X-ViperCapture-Webhook-Signature`, with timestamp and
event-ID headers for verification and deduplication. Private callback targets
are rejected unless the operator explicitly opts in. Public DNS results are
pinned through the callback connection to prevent rebinding, and the encrypted
delivery outbox survives process restarts.

## Security boundary

Keep the service on loopback or place every route behind the same authenticated,
rate-limited reverse proxy. Job and schedule UUIDs are locators, not access
control. Hosted mode rejects targets and redirects that resolve privately at
validation time, unsafe subresources, cross-origin credential headers, proxy
use, and cross-site cookies. Because browser DNS can change after validation,
deployments must also enforce host/container egress rules to block rebinding.
Self-host mode intentionally permits operators to reach internal pages and use
proxies; isolate Chromium accordingly.

JavaScript actions are disabled unless `VIPERCAPTURE_ALLOW_SCRIPTS=1`. Render
inputs can contain credentials and private page data. Async and scheduled inputs
are AES-GCM encrypted and erased at terminal job states, but diagnostic console
output and browser video can still capture sensitive page content—treat their
artifacts accordingly.

## Native apps and agent skill

The Tauri 2 desktop app lives in [`desktop/`](desktop) and the Android WebView
renderer is documented in [docs/android.md](docs/android.md). The portable
[`skills/vipercapture`](skills/vipercapture) skill works with Codex, Claude Code,
and Cursor and includes a dependency-free client.

## Development and proof

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m unittest -v
npm ci --prefix frontend && npm run lint --prefix frontend && npm run build --prefix frontend
```

CI runs the renderer suite and both web/desktop builds. The benchmark emits raw
samples and never invents competitor results; use your own provider credentials
to compare from the same host and scenario file.

## License

[MIT](LICENSE). AutoConsent assets under `vendor/autoconsent` retain their
upstream license and attribution.
