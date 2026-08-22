<p align="center">
  <img src="static/vipercapture-mark.svg" width="112" height="112" alt="ViperCapture logo">
</p>

<h1 align="center">ViperCapture</h1>

<p align="center"><strong>Self-hosted browser rendering API.</strong></p>

ViperCapture is an MIT-licensed browser renderer for infrastructure you control.
Send a URL, HTML, or Markdown and receive screenshots,
PDFs, AVIF images, WebM/MP4/GIF video, hydrated HTML, Markdown, or structured
metadata through a JSON API.

Version 1.0 is the first stable release. The documented JSON contract follows
semantic versioning; breaking API changes require a new major version. If you
are moving from another rendering service, start with the
[compatibility matrix](docs/compatibility.md) and
[migration guide](docs/migration-screenshotone-urlbox.md).

## Features

- Chromium, Firefox, and WebKit rendering; browsers start only when needed
- PNG, JPEG, WebP, AVIF, PDF with explicit structure-tag control, HTML,
  Markdown, metadata, and WebM/MP4/GIF output
- URL, raw HTML, and Markdown input; full-page, viewport, element, clip, and
  multi-viewport ZIP captures
- Typed click, hover, fill, select, key, scroll, wait, hide, and opt-in
  JavaScript actions
- Selector-state and image-readiness waits, target JavaScript control,
  assertions, custom CSS, devices, locale/timezone,
  geolocation, cookies, user agent, proxy, resource blocking, and cleanup
- Request-aware stealth controls, operator-controlled residential/datacenter
  proxies, and structured detection for common CAPTCHA and bot interstitials
- Ad, tracker, chat, newsletter, and consent-banner cleanup backed by the
  vendored, license-preserved AutoConsent rule set
- Cleanup and deterministic controls in the browser UI
- Explicit screen or print CSS media emulation applied before page load
- Timed, full-page GIF, WebM, and MP4 capture with higher-quality encoding,
  transparent padding where supported, and optional GPU acceleration with a
  safe software fallback
- Default local limits: 500 megapixels, 16,384-pixel viewports, and 100,000-pixel
  full-page height, all configurable for remote hosting
- Durable encrypted async jobs, idempotency, retries, polling, cancellation,
  bulk submission, cron schedules, and signed webhook callbacks
- Private local result storage or built-in S3-compatible storage for AWS S3,
  Cloudflare R2, MinIO, Backblaze B2, and compatible providers
- Expiring HMAC-signed render URLs and a 15-minute exact-request image cache
- Visual regression ZIPs with pixel counts, pass/fail thresholds, bounds, and
  highlighted changes
- Diagnostic ZIPs with console/network data and optional HAR,
  redacted Playwright trace, and WARC; Ed25519-certified artifact bundles
- Deterministic capture controls, sectioned slice ZIPs, project-owned visual
  baselines, and reproducible comparison reports
- Optional projects, hashed API keys, quotas, resource ownership, encrypted
  persistent profiles, portable browser-session imports, audit logs,
  Prometheus metrics, and OTLP tracing
- Separate API/worker roles for horizontally scaled provider-backed queues,
  plus ScreenshotOne and Urlbox compatibility adapters
- Bounded concurrency, output/pixel/deadline limits, client-disconnect
  cancellation, consistent error envelopes, and hosted-mode SSRF defenses
- Browser UI, Docker Compose, an n8n workflow, a Terraform module, and a
  hardened public-API deployment example

## Before you begin

Direct installation requires Python 3.11 or newer and a full FFmpeg build on
`PATH`. Video output needs the `libvpx`, `libvpx-vp9`, and `libx264` encoders;
GPU video also needs the matching hardware encoder and driver. Use your OS
package manager or the [FFmpeg download page](https://ffmpeg.org/download.html).
Run `ffmpeg -encoders` to confirm the encoders are available. The Docker image
already includes FFmpeg.

## Install locally

Run:

```bash
git clone https://github.com/Viperisuseful/ViperCapture.git
cd ViperCapture
python launch.py
```

The launcher creates a virtual environment, installs Chromium, Firefox, and
WebKit, starts the API, and opens `http://127.0.0.1:8000`.

To use Docker instead, run:

```bash
docker compose up --build
```

Stable container images are also published to GitHub Container Registry:

```bash
docker pull ghcr.io/viperisuseful/vipercapture:1.0.0
```

By default, Compose binds only to loopback. It keeps durable queue state,
encryption keys, schedules, cache entries, and local artifacts in a named
volume. Read [self-hosting](docs/self-hosting.md) before exposing the service to
a network. Set separate `VIPERCAPTURE_ADMIN_TOKEN` and
`VIPERCAPTURE_CONTROL_SECRET` values to enable the built-in project control
plane before exposing API routes to multiple tenants.

## Send a render request

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

A synchronous request returns the artifact directly. The
`X-ViperCapture-*` headers include the request ID, queue and render timing,
dimensions, navigation status, and cache outcome.

For work that must survive a dropped connection, send the same render object to
`POST /v1/jobs`. Poll
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
| `POST /v1/profiles/import` | Import pasted Cookie headers or browser-exported session files |
| `GET /take` | ScreenshotOne-compatible common-options adapter |
| `POST /compat/urlbox/v1/render/{sync,async}` | Urlbox-compatible adapters |

See the [API and workflows guide](docs/api.md), [async provider guide](docs/async-jobs.md),
[platform/operator guide](docs/platform.md),
[public API deployment](deploy/public-api), and
[migration guide](docs/migration-screenshotone-urlbox.md). If a site you
administer challenges the renderer, use the least-privilege
[Cloudflare/WAF authorization guide](docs/site-access.md).

## Use proxies, sessions, and CAPTCHA hooks

Self-hosted renders can route an isolated browser context through an HTTP,
HTTPS, SOCKS4, or SOCKS5 proxy. Keep credentials in separate fields instead of
embedding them in the proxy URL:

```json
{
  "url": "https://example.com",
  "network": {
    "proxy": {
      "server": "socks5://proxy.example:1080",
      "username": "account-zone-residential",
      "password": "secret"
    }
  }
}
```

With the project control plane enabled, `POST /v1/profiles/import` normalizes
Playwright storage state, Cookie-Editor JSON, Netscape `cookies.txt`, or a
pasted Cookie header into an encrypted profile. Pass its returned `id` as
`profile_id` on later renders. Imports preserve local storage and partitioned
cookies where the export format supports them.

ViperCapture detects common blocking CAPTCHA and bot interstitials but does not
solve or bypass them. The default `captcha.action` is `error`; use `capture` to
render the challenge as-is. Operators may configure their own approved async
handler with `VIPERCAPTURE_CAPTCHA_HANDLER_FACTORY` and opt in per request with
`captcha.action: "external"`. See the [API guide](docs/api.md) for the handler
contract and timeout behavior. Alternatively, an authorized caller can use an
external tool independently, then start a fresh render with short-lived,
target-scoped session state. ViperCapture ships no provider integration,
credentials, endorsement, solver, or bypass service.

## Configure storage and webhooks

Set `VIPERCAPTURE_S3_BUCKET` to store job results through the built-in S3
adapter instead of local files. It uses standard AWS credential resolution.
R2 and MinIO also need an endpoint URL and, where appropriate, path-style
addressing. The `docker-compose.s3.yml` overlay provides a complete local MinIO
example:

```bash
export MINIO_ROOT_PASSWORD='replace-with-at-least-a-long-random-secret'
docker compose -f docker-compose.yml -f docker-compose.s3.yml up --build
```

Set `VIPERCAPTURE_WEBHOOK_SECRET` to enable callbacks. Each callback contains
canonical JSON signed with HMAC-SHA256 in
`X-ViperCapture-Webhook-Signature`. Timestamp and event-ID headers support
verification and deduplication. Private callback targets are rejected unless
the operator explicitly opts in. Public DNS results stay pinned for the life
of the callback connection to prevent rebinding, and the encrypted delivery
outbox survives process restarts.

## Secure the service

Keep the service on loopback, enable `VIPERCAPTURE_ADMIN_TOKEN`, or put every
route behind the same authenticated, rate-limited reverse proxy. When the
control plane is disabled, job and schedule UUIDs identify resources but do
not provide access control. Hosted mode rejects targets and redirects that
resolve to private addresses during validation, along with unsafe subresources,
cross-origin credential headers, proxy use, and cross-site cookies. Browser DNS
can change after validation, so deployments also need host or container egress
rules to block rebinding. Self-host mode allows internal pages and
proxies; isolate Chromium with that boundary in mind.

JavaScript actions are disabled unless `VIPERCAPTURE_ALLOW_SCRIPTS=1`. Render
inputs can contain credentials and private page data. Async and scheduled inputs
are AES-GCM encrypted and erased at terminal job states, but diagnostic console
output and browser video can still record sensitive page content. Treat those
artifacts accordingly.

## Verify an installation

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium firefox webkit
npm ci --prefix frontend && npm run lint --prefix frontend && npm run build --prefix frontend
python scripts/smoke.py
```

The smoke command starts a temporary local server and verifies Chromium,
Firefox, WebKit, OpenAPI, health, and output dimensions. Pass
`--base-url http://host:port` to check an existing deployment instead. For an
authenticated deployment, set `VIPERCAPTURE_SMOKE_TOKEN` to an API or
administrator token.

## License

[MIT](LICENSE). AutoConsent assets under `vendor/autoconsent` retain their
upstream license and attribution.
