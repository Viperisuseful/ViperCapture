# Migrating from ScreenshotOne or Urlbox

ViperCapture uses one strict nested JSON contract rather than duplicating every
vendor query-parameter name. Start by migrating server-side POST calls; signed
embed URLs and async workflows can follow independently.

## Common option mapping

| Vendor-style concept | ViperCapture field |
| --- | --- |
| `url`, `html`, `markdown` | same top-level source field |
| `format` | `output` |
| viewport width/height/DPR | `viewport.width`, `.height`, `.device_scale_factor` |
| full-page capture | `full_page` |
| selector | `selector` with `full_page: false` |
| clip x/y/width/height | `clip` with `full_page: false` |
| image quality | `image.quality` |
| transparent background | `image.transparent_background` |
| wait event/selector/text/delay | `wait_for` |
| styles/custom CSS | `custom_css` |
| block ads/cookie banners/chats | `cleanup` |
| user agent/proxy/cookies/geolocation | `network` |
| click/type/scroll/scripts | ordered `actions` |
| fail if text/request condition | `assertions` and `fail_on_status` |
| async | submit the same body to `/v1/jobs` |
| webhook URL | `delivery.webhook_url` on an async job |
| cache | `cache: true` for a single image |

Example translation:

```json
{
  "url": "https://example.com",
  "output": "webp",
  "viewport": {"width": 1440, "height": 900, "device_scale_factor": 2},
  "full_page": true,
  "image": {"quality": 85},
  "cleanup": {"block_ads": true, "consent_mode": "reject"},
  "wait_for": {"event": "networkidle", "timeout_ms": 15000}
}
```

## Rollout checklist

1. Pin the ViperCapture commit and Chromium version in a staging deployment.
2. Copy real requests with secrets removed into a private scenario suite.
3. Compare dimensions and visual differences, not only HTTP success.
4. Validate fonts, locale/timezone, authentication, lazy content, and consent
   behavior on representative pages.
5. Run the benchmark from the same client region with caching disabled.
6. Exercise async restart recovery, webhook signature verification, object
   expiry, and schedule timezone transitions.
7. Apply reverse-proxy authentication, request limits, Chromium isolation,
   egress filtering, backups, and provider lifecycle rules.
8. Move traffic gradually and retain the managed provider as rollback until
   workload-specific acceptance criteria pass.

There is intentionally no silent compatibility endpoint that accepts arbitrary
vendor query strings. Strict validation exposes unsupported options during
migration instead of quietly producing the wrong artifact.
