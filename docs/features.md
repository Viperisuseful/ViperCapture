# Features

ViperCapture is a self-hosted browser rendering engine with a JSON API, CLI, terminal menu, and web UI. This page lists what the open-source engine supports.

## Rendering and outputs

- **Three browser engines.** Render with Chromium, Firefox, or WebKit in a fresh isolated context per request. Chromium starts in a managed pool; Firefox and WebKit start on first use.
- **Images, PDFs, HTML, Markdown, and metadata.** Output PNG, JPEG, WebP, AVIF, PDF with explicit structure-tag control, rendered HTML, Markdown, or JSON metadata with structured CSS-selector extraction.
- **Video and animations.** Record timed viewport or full-page WebM, MP4, and GIF output with configurable frame rate and bitrate, transparent padding where supported, and optional GPU encoding with software fallback.
- **Flexible inputs and framing.** Render a URL, raw HTML, or Markdown. Capture the full page, the viewport, a matched element, a rectangular clip, sectioned full-page slices, or two to three named viewports in one ZIP.
- **Multi-artifact bundles.** Return a primary image, side outputs (HTML, Markdown, metadata, and Chromium MHTML), up to five named thumbnails, and a SHA-256 manifest from one page load.
- **Default local ceilings.** Local installs default to 500 megapixels, 16,384-pixel viewport width and height, 100,000-pixel full-page height, and 1 GiB encoded output. Operators can lower or tune every ceiling through environment variables.

## Browser control and cleanup

- **Typed page actions.** Run ordered `click`, `hover`, `fill`, `press`, `select`, `scroll`, `wait`, `hide`, and opt-in `javascript` actions before capture.
- **Waits and assertions.** Wait for navigation events, selector states (`visible`, `attached`, `hidden`, `detached`), body text, completed images, or a fixed delay. Fail requests when text or network assertions do not pass.
- **Page cleanup.** Reject, accept, or hide cookie consent banners with the vendored, license-preserved AutoConsent rule set, and block ads, trackers, chat widgets, and newsletter overlays. The web UI exposes these cleanup controls by default.
- **Environment and network controls.** Emulate devices, screen or print CSS media, color schemes, reduced motion, locale, timezone, and geolocation. Inject custom CSS, disable target-page JavaScript, set same-origin headers, pass cookies, block resource types or URL globs, and route contexts through operator-controlled HTTP, HTTPS, SOCKS4, or SOCKS5 proxies.
- **Stealth and challenge detection.** Apply request-aware automation evasions on standard Playwright and detect blocking CAPTCHA and bot interstitials with structured provider, kind, confidence, and signal metadata.

## Jobs, storage, and verification

- **Async jobs, bulk batches, and schedules.** Submit durable encrypted jobs at `POST /v1/jobs`, batch up to 100 items at `POST /v1/jobs/bulk`, or run five-field cron schedules in any IANA timezone. Jobs support idempotency keys, bounded retries, polling, cancellation, and HMAC-SHA256 signed webhook callbacks.
- **Local and S3-compatible storage.** Store job results in private local files or write them to AWS S3, Cloudflare R2, MinIO, Backblaze B2, or another S3-compatible bucket.
- **Signed links and caching.** Mint expiring HMAC-SHA256 render URLs and cache exact image requests for 24 hours by default.
- **Visual diffs and baselines.** Compare two images at `POST /v1/diff` for a ZIP report with changed pixel counts, thresholds, bounding box, and a highlighted diff image. Store project baselines and compare reproducible review bundles.
- **Deterministic captures and diagnostics.** Freeze time and randomness for repeatable visual tests. Bundle console and network events with optional redacted HAR 1.2 logs, redacted Playwright traces, WARC archives, and Ed25519-signed manifests.

## Security, control plane, and operations

- **Projects, keys, and profiles.** Enable the optional control plane for hashed API keys with scoped permissions, per-project rate and concurrency limits, resource ownership, encrypted persistent browser profiles, portable browser-session imports, and audit logs.
- **Distributed roles and observability.** Run combined single-process installs or split `api` and `worker` roles over shared external providers. Export Prometheus metrics at `GET /metrics` and OpenTelemetry traces over OTLP.
- **Compatibility adapters.** Accept common ScreenshotOne requests at `GET /take` and Urlbox requests at `POST /compat/urlbox/v1/render/{sync,async}`.
- **Safety boundaries.** Enforce bounded concurrency, output and pixel limits, request deadlines, client-disconnect cancellation, consistent error envelopes, and hosted-mode SSRF, redirect, and subresource checks.
- **Full CLI and terminal menu.** Run `vipercapture capture` with `--from-json`, saved presets, multi-URL batches, cookie files, retries, `--async --no-wait`, and `~/.vipercapture/config.toml` defaults. Use 15 companion subcommands (`version`, `status`, `logs`, `gpu`, `preset`, `diff`, `baseline`, `bulk`, `job`, `schedule`, `profile`, `sign`, `verify`, `admin`, and `completion`) or the `--gui` terminal menu covering all 11 output formats with drawers, persisted history, and inline PNG previews.
- **Deployment assets.** Ships with a local web UI, Docker Compose, an n8n workflow, a Terraform module, and a hardened public API deployment example.

## Stability

Version 1.0 is the first stable release. The documented JSON contract follows semantic versioning, so breaking API changes require a new major version.

## Related

- [Self-hosting and local setup](self-hosting.md)
- [API and workflows](api.md)
- [Async jobs and queue providers](async-jobs.md)
- [Projects, API keys, and observability](platform.md)
- [All docs](README.md)
