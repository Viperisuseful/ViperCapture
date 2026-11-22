# Features

- Chromium, Firefox, and WebKit rendering; browsers start only when needed
- PNG, JPEG, WebP, AVIF, PDF with explicit structure-tag control, HTML,
  Markdown, metadata with structured CSS-selector extraction, and WebM/MP4/GIF
  output
- URL, raw HTML, and Markdown input; full-page, viewport, element, clip, and
  multi-viewport ZIP captures
- One-page-load artifact bundles with the primary image, HTML, Markdown,
  metadata, MHTML, and up to five named thumbnails
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
- Expiring HMAC-signed render URLs and a 24-hour exact-request image cache
- Visual regression ZIPs with pixel counts, pass/fail thresholds, bounds, and
  highlighted changes
- Diagnostic ZIPs with console/network data and optional redacted HAR
  (HTTP versions, safe headers, mime types, timings),
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

## Stability

Version 1.0 is the first stable release. The documented JSON contract follows
semantic versioning; breaking API changes require a new major version.
