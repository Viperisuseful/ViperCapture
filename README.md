<p align="center">
  <img src="static/vipercapture-mark.svg" width="112" height="112" alt="ViperCapture logo">
</p>

<h1 align="center">ViperCapture</h1>

<p align="center"><strong>The open-source, self-hosted ScreenshotOne / Urlbox alternative.</strong></p>

ViperCapture is an MIT-licensed browser rendering platform. It turns URLs, HTML,
or Markdown into screenshots, PDFs, AVIF images, WebM/MP4/GIF video, hydrated HTML, Markdown, or
structured metadata—through a strict JSON API you can run on your own machines
and storage.

> [!IMPORTANT]
> ViperCapture OSS v0.1 is beta software. The engine and API are ready for
> developer self-hosting and evaluation, but the public API may still change
> before v1. The desktop and Android applications are also beta releases;
> desktop packages are not yet code-signed.

This project aims for practical workflow parity, not misleading one-for-one
option naming. See the dated, source-linked [compatibility matrix](docs/compatibility.md)
and run the [reproducible benchmark](benchmarks/README.md) on your workload.

## What ships

- Chromium, Firefox, and WebKit rendering with lazy browser startup
- PNG, JPEG, WebP, AVIF, PDF, HTML, Markdown, metadata, and WebM/MP4/GIF output
- URL, raw HTML, and Markdown input; full-page, viewport, element, clip, and
  multi-viewport ZIP captures
- Typed click, hover, fill, select, key, scroll, wait, hide, and opt-in
  JavaScript actions
- Wait conditions, assertions, custom CSS, devices, locale/timezone,
  geolocation, cookies, user agent, proxy, resource blocking, and cleanup
- Ad, tracker, chat, newsletter, and consent-banner cleanup backed by the
  vendored, license-preserved AutoConsent rule set
- Cleanup and deterministic advanced controls in both the browser UI and
  desktop app
- High local defaults: 500 megapixels, 16,384-pixel viewports, and 100,000-pixel
  full-page height, all configurable for remote hosting
- Durable encrypted async jobs, idempotency, retries, polling, cancellation,
  bulk submission, cron schedules, and signed webhook callbacks
- Private local result storage or built-in S3-compatible storage for AWS S3,
  Cloudflare R2, MinIO, Backblaze B2, and compatible providers
- Expiring HMAC-signed render URLs and a 15-minute exact-request image cache
- Visual regression ZIPs with pixel counts, pass/fail thresholds, bounds, and
  highlighted changes
- Privacy-aware diagnostic ZIPs with console/network data and optional HAR,
  redacted Playwright trace, and WARC; Ed25519-certified artifact bundles
- Deterministic capture controls, sectioned slice ZIPs, project-owned visual
  baselines, and reproducible comparison reports
- Optional projects, hashed API keys, quotas, resource ownership, encrypted
  persistent profiles, audit logs, Prometheus metrics, and OTLP tracing
- Separate API/worker roles for horizontally scaled provider-backed queues,
  plus ScreenshotOne and Urlbox compatibility adapters
- Bounded concurrency, output/pixel/deadline limits, client-disconnect
  cancellation, consistent error envelopes, and hosted-mode SSRF defenses
- Browser UI, Tauri desktop app, native Android app, Docker Compose, and a
  portable agent skill
- Reproducible sustained-load, forced restart-recovery, constrained-memory,
  same-host competitor, and longer real-site benchmark workflows

## Start in one command

Direct installs require Python 3.11 or newer and a full FFmpeg build on
`PATH`. Video output needs the `libvpx`, `libvpx-vp9`, and `libx264` encoders;
GPU video also needs the matching hardware encoder and driver. Use your OS
package manager or the [FFmpeg download page](https://ffmpeg.org/download.html),
then verify with `ffmpeg -encoders`. The Docker image already includes FFmpeg,
and desktop installers bundle it.

Then run:

```bash
git clone https://github.com/Viperisuseful/ViperCapture.git
cd ViperCapture
python launch.py
```

The launcher creates a virtual environment, installs Chromium, Firefox, and WebKit, starts the API,
and opens `http://127.0.0.1:8000`.

Or use Docker:

```bash
docker compose up --build
```

The Compose default binds only to loopback and stores durable queue state,
encryption keys, schedules, cache entries, and local artifacts in a named
volume. Read [self-hosting](docs/self-hosting.md) before exposing it to a
network. Set separate `VIPERCAPTURE_ADMIN_TOKEN` and
`VIPERCAPTURE_CONTROL_SECRET` values to enable the built-in project control
plane before exposing API routes to multiple tenants.

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
| `PUT/GET/POST /v1/baselines` | Store project baselines and compare review bundles |
| `POST/DELETE /v1/profiles` | Manage encrypted Playwright storage-state profiles |
| `GET /take` | ScreenshotOne-compatible common-options adapter |
| `POST /compat/urlbox/v1/render/{sync,async}` | Urlbox-compatible adapters |

See the [API and workflows guide](docs/api.md), [async provider guide](docs/async-jobs.md),
[platform/operator guide](docs/platform.md),
[public API deployment](deploy/public-api), [stable-v1 gates](docs/v1-readiness.md),
[release guide](docs/releasing.md), and [migration guide](docs/migration-screenshotone-urlbox.md). If a site you
administer challenges the renderer, follow the least-privilege
[Cloudflare/WAF authorization guide](docs/site-access.md).

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

Keep the service on loopback, enable `VIPERCAPTURE_ADMIN_TOKEN`, or place every
route behind the same authenticated, rate-limited reverse proxy. With the
control plane disabled, job and schedule UUIDs remain locators rather than
access control. Hosted mode rejects targets and redirects that resolve privately at
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

The beta Tauri 2 desktop app lives in [`desktop/`](desktop) and the beta Android
WebView renderer is documented in [docs/android.md](docs/android.md). Download
them from [GitHub Releases](https://github.com/Viperisuseful/ViperCapture/releases).
The portable
[`skills/vipercapture`](skills/vipercapture) skill works with Codex, Claude Code,
and Cursor and includes a dependency-free client.

## Development and proof

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium firefox webkit
python -m unittest -v
npm ci --prefix frontend && npm run lint --prefix frontend && npm run build --prefix frontend
```

CI runs the renderer suite and both web/desktop builds. The benchmark emits raw
samples and never invents competitor results; use your own provider credentials
to compare from the same host and scenario file.

## License

[MIT](LICENSE). AutoConsent assets under `vendor/autoconsent` retain their
upstream license and attribution.
