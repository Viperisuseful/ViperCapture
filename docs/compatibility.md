# ScreenshotOne and Urlbox compatibility matrix

Last verified against public vendor documentation on 2026-08-09.

This matrix compares documented workflows across ViperCapture OSS, ScreenshotOne, and Urlbox. "Yes" means this repository implements and tests the capability. "Partial" means the implementations differ. Vendor behavior can change; check the vendor links below for current details.

| Capability | ViperCapture OSS | ScreenshotOne | Urlbox |
| --- | --- | --- | --- |
| Self-host under an OSI license | Yes (MIT) | No public OSS server | No public OSS server |
| URL and raw HTML input | Yes | Yes | Yes |
| Markdown input | Yes | Yes | Partial, Markdown is documented as an output |
| PNG, JPEG, and WebP output | Yes | Yes | Yes |
| AVIF output | Yes | Yes | Yes |
| PDF (with explicit structure-tag control), HTML, Markdown, and metadata | Yes | Yes | Yes |
| Video output | WebM, MP4, and GIF | GIF and WebM animations | MP4 and WebM |
| Full page, viewport, selector, and clip | Yes | Yes | Yes |
| Multiple viewports in one request | Yes, ZIP | Bulk API | Batch and API workflows |
| Multiple artifacts from one page load | Yes: image, HTML, Markdown, metadata, MHTML, and thumbnails | Partial: screenshot plus content metadata | Yes: side renders and thumbnails |
| Typed browser actions | Yes | Scripts and page customization | Click, type, scroll, and custom JS options |
| Custom CSS and JavaScript | Yes, JS is operator opt-in | Yes | Yes |
| Waits and content or request assertions | Yes | Yes | Yes or partial depending on assertion |
| Ad, tracker, consent, and chat cleanup | Yes | Yes | Yes |
| Headers, cookies, locale, timezone, and geolocation | Yes | Yes | Yes |
| Screen and print CSS media emulation | Yes | Yes | Yes |
| Per-request proxy | Self-host mode | Paid option | Paid option |
| Exact-request image cache | Yes, 24-hour local default | Yes, CDN and R2 cache | Render-link cache; POST is not cached |
| Sync and durable async polling | Yes | Async callback workflow | Yes |
| Signed webhooks | Yes | Yes | Yes |
| Expiring signed render links | Yes | Yes | Yes, tokenized render links |
| Native S3-compatible results | Yes: S3, R2, MinIO, and B2 | Yes | Yes, plan-dependent |
| Chromium, Firefox, and WebKit rendering | Yes | Chromium | Chromium |
| Open shadow-DOM HTML extraction | Yes | Yes | Not documented |
| Output thumbnail resizing | Yes | Yes | Yes |
| Bulk endpoint | Yes, up to 100 items | Yes | Client and batch workflow |
| Cron schedules | Yes | External scheduler | Product and no-code workflows documented |
| Pixel visual diff reports | Yes | Not documented as core render API | Schedule and compare workflow documented |
| Diagnostic bundle | Yes: artifact, manifest, console, network, redacted HAR 1.2 (allowlisted headers, Chromium CDP and Resource Timing HTTP versions, Firefox and WebKit HTTPS unknown without Timing), redacted trace, and WARC | Partial: metadata and error options | Partial: request waterfall and metadata |
| Structured CSS-selector extraction | Yes: text, HTML, attributes, and geometry | Partial: single-selector content | Partial: predefined extracted content |
| Deterministic visual testing and baseline store | Yes | Managed service | Managed service |
| Project API keys, quotas, ownership, and audit log | Yes, operator opt-in | Managed | Managed |
| Encrypted persistent browser profiles | Yes | Yes | Yes |
| Prometheus metrics and OTLP traces | Yes | Vendor-managed | Vendor-managed |
| Separate API and distributed worker roles | Yes, provider-backed | Vendor-managed | Vendor-managed |
| ScreenshotOne and Urlbox request adapters | Yes | Native | Native |
| Certified artifact archive | Yes, Ed25519 manifest | Not compared | Yes |
| Owner-authorized Cloudflare or WAF access | Yes: scoped header, profile, proxy, diagnostics, and rule guide | Managed | Managed |
| Managed global proxies and stealth fleet | No | Yes | Yes |
| Vendor SLA and human render support | No | Managed offering | Managed offering |

Vendor documentation referenced for this comparison:

- ScreenshotOne: [options](https://screenshotone.com/docs/options/), [bulk screenshots](https://screenshotone.com/docs/bulk-screenshots/), [signed requests](https://screenshotone.com/docs/signed-requests/), and [animated screenshots](https://screenshotone.com/docs/animated-screenshots/).
- Urlbox: [API reference](https://urlbox.com/docs/api), [render options](https://urlbox.com/docs/options), [REST API vs render links](https://urlbox.com/docs/api/rest-api-vs-render-links), and [S3-compatible storage](https://urlbox.com/docs/guides/s3).

## What ViperCapture does not include

ViperCapture does not include a managed proxy network, anti-bot bypass, legal admissibility opinion, SLA, or continuous render support. The operator manages upgrades, browsers, networking, data retention, storage, capacity, and cost.

This repository runs standard Playwright and does not claim a Cloudflare bypass. For Patchright and harder Turnstile challenge handling, see the separate [ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth) fork (`0.1.0-beta`). It is neither a managed stealth fleet nor a universal Cloudflare bypass.

For a site you control, follow the [site access guide](site-access.md) to configure a narrow Cloudflare or WAF rule matching your renderer's IP, exact host and path, and an origin-scoped secret header. That workflow authorizes your own server rather than evading challenges.

## Other services

- **[Browserless](https://docs.browserless.io/enterprise/open-source).** Offers an open-source Docker server for general browser automation through REST screenshot, PDF, and content endpoints plus Playwright and Puppeteer WebSocket sessions. ViperCapture focuses on the rendering and delivery workflow: typed actions, encrypted async jobs, signed links, bulk and cron jobs, visual diffs, and diagnostic bundles.
- **[ScreenshotAPI](https://www.screenshotapi.net/docs).** Provides a managed API for images, PDFs, scrolling video, customization, and geolocation.
- **[ApiFlash](https://apiflash.com/documentation).** Provides a narrower managed URL-to-image GET and POST API backed by AWS Lambda.

## Related

- [Migrating from ScreenshotOne or Urlbox](migration-screenshotone-urlbox.md)
- [Full feature list](features.md)
- [API and workflows](api.md)
- [All docs](README.md)
