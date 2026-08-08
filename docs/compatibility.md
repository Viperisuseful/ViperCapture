# ScreenshotOne and Urlbox compatibility matrix

Last verified against public vendor documentation: **2026-08-01**.

This matrix compares workflows, not marketing plan entitlements. “Yes” means
the repository implements and tests that capability. “Partial” identifies a
real functional gap. Vendor capabilities can change; source links are included
so corrections can be submitted as pull requests.

| Capability | ViperCapture OSS | ScreenshotOne | Urlbox |
| --- | --- | --- | --- |
| Self-host under an OSI license | **Yes — MIT** | No public OSS server | No public OSS server |
| URL and raw HTML input | Yes | Yes | Yes |
| Markdown input | Yes | Yes | Partial: Markdown is documented as an output |
| PNG / JPEG / WebP | Yes | Yes | Yes |
| PDF / hydrated HTML / Markdown / metadata | Yes | Yes | Yes |
| Video | **WebM, MP4, GIF** | GIF/WebM animations | MP4/WebM |
| Full page / viewport / selector / clip | Yes | Yes | Yes |
| Multiple viewports in one request | Yes, ZIP | Bulk API | Batch/API workflows |
| Typed browser actions | Yes | Scripts and page customization | Click/type/scroll and custom JS options |
| Custom CSS / JavaScript | Yes; JS operator opt-in | Yes | Yes |
| Waits and content/request assertions | Yes | Yes | Yes/partial depending on assertion |
| Ad, tracker, consent, chat cleanup | Yes | Yes | Yes |
| Headers, cookies, locale/timezone, geolocation | Yes | Yes | Yes |
| Per-request proxy | Self-host mode | Paid option | Paid option |
| Exact-request cache | Yes, local 15-minute default | Yes, CDN/R2 cache | Render-link cache; POST not cached |
| Sync and durable async polling | Yes | Async callback workflow | Yes |
| Signed webhooks | Yes | Yes | Yes |
| Expiring signed render links | Yes | Yes | Yes, tokenized render links |
| Native S3-compatible results | Yes: S3/R2/MinIO/B2 | Yes | Yes, plan-dependent |
| Bulk endpoint | Yes, up to 100 | Yes | Client/batch workflow |
| Cron schedules | Yes | External scheduler | Product/no-code workflows documented |
| Pixel visual diff reports | Yes | Not documented as core render API | Schedule/compare workflow documented |
| Diagnostic bundle | Yes: artifact, manifest, console, network, HAR, trace, MHTML, WARC | Partial: metadata/error options | Partial: request waterfall/metadata |
| Deterministic visual testing and baseline store | Yes | Managed service | Managed service |
| Project API keys, quotas, ownership, audit log | Yes, operator opt-in | Managed | Managed |
| Encrypted persistent browser profiles | Yes | Yes | Yes |
| Prometheus metrics and OTLP traces | Yes | Vendor-managed | Vendor-managed |
| Separate API and distributed worker roles | Yes, provider-backed | Vendor-managed | Vendor-managed |
| ScreenshotOne/Urlbox request adapters | Yes | Native | Native |
| Managed global proxies / stealth fleet | **No** | Yes | Yes |
| Owner-authorized Cloudflare/WAF access | **Yes: scoped header, profile, proxy, diagnostics, rule guide** | Managed | Managed |
| Vendor SLA and human render support | **No** | Managed offering | Managed offering |
| AVIF output | Yes | Yes | Yes |
| Certified/evidentiary archive | Yes, Ed25519 manifest | Not compared | Yes |

Vendor sources:

- ScreenshotOne: [options](https://screenshotone.com/docs/options/),
  [bulk](https://screenshotone.com/docs/bulk-screenshots/),
  [signed links](https://screenshotone.com/docs/signed-requests/), and
  [animated screenshots](https://screenshotone.com/docs/animated-screenshots/).
- Urlbox: [API reference](https://urlbox.com/docs/api),
  [render options](https://urlbox.com/docs/options),
  [render links](https://urlbox.com/docs/api/rest-api-vs-render-links), and
  [S3-compatible storage](https://urlbox.com/docs/guides/s3).

## Deliberate non-parity

ViperCapture does not claim a managed proxy network, anti-bot bypass, legal
admissibility opinion, SLA, or around-the-clock render support. Those are services,
not repository features, and can be valid reasons to buy a managed provider.
The open-source advantage is control: code, Chromium version, network, data,
storage, capacity, and cost are inspectable and operator-owned.

For a target the operator owns, the [site access guide](site-access.md) documents
a narrow Cloudflare/WAF exception using the renderer address, exact host and
path, and an origin-scoped secret header. This is authorization, not challenge
evasion.

## Adjacent alternatives

- [Browserless](https://docs.browserless.io/enterprise/open-source) also ships
  an open-source Docker deployment and is the strongest adjacent OSS option.
  Its focus is general browser infrastructure: REST screenshot/PDF/content
  endpoints plus Playwright/Puppeteer WebSocket sessions. ViperCapture's focus
  is the artifact workflow around rendering—typed actions, encrypted jobs,
  signed delivery, bulk/schedules, visual diffs, and diagnostic bundles.
- [ScreenshotAPI](https://www.screenshotapi.net/docs) documents image/PDF and
  scrolling video output with customization and geolocation as a managed API.
- [ApiFlash](https://apiflash.com/documentation) offers a narrower managed
  URL-to-image GET/POST API backed by AWS Lambda.

The existence of Browserless is why ViperCapture should not claim to be the
only open-source screenshot server. The defensible claim is a cohesive,
self-hosted ScreenshotOne/Urlbox-style rendering and delivery workflow under
MIT, with explicit gaps and tests.
