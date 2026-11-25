# Migrating from ScreenshotOne or Urlbox

ViperCapture uses a strict nested JSON contract and rejects unknown fields rather than accepting every vendor query parameter. Move server-side POST requests first, then migrate signed URLs and async workflows separately.

For a staged cutover, use the compatibility endpoints:

- **ScreenshotOne.** `GET /take` accepts common query options and authenticates with `access_key` or a Bearer token.
- **Urlbox.** `POST /compat/urlbox/v1/render/sync` and `POST /compat/urlbox/v1/render/async` accept common POST options.

Once traffic is stable, switch callers to native `/v1/*` routes to access every ViperCapture option.

`access_key` is a compatibility-only query credential for `/take` and starts enabled by default. Query parameters can leak secrets into proxy and access logs, browser history, analytics, debugging tools, and copied links. Prefer `Authorization: Bearer ...` and set `VIPERCAPTURE_ALLOW_QUERY_AUTH=0` once legacy callers finish migrating. Native `/v1/*` routes never accept query keys.

## Map request options

| Vendor concept | ViperCapture field |
| --- | --- |
| `url`, `html`, or `markdown` | Same top-level source field |
| `format` | `output` |
| Viewport width, height, and DPR | `viewport.width`, `viewport.height`, and `viewport.device_scale_factor` |
| Full-page capture | `full_page` |
| Element selector | `selector` with `full_page: false` |
| Clip x, y, width, and height | `clip` with `full_page: false` |
| Image quality | `image.quality` |
| Transparent background | `image.transparent_background` |
| Wait event, selector, text, or delay | `wait_for` |
| Styles or custom CSS | `custom_css` |
| Block ads, cookie banners, or chats | `cleanup` |
| User agent, proxy, cookies, or geolocation | `network` |
| Click, type, scroll, or script steps | Ordered `actions` |
| Fail on text, request-failure, or HTTP-status checks | `assertions` and `fail_on_status` |
| Async execution | Submit the same body to `POST /v1/jobs` |
| Webhook URL | `delivery.webhook_url` on an async job |
| Exact-request cache | `cache: true` for a single image |

Example native request:

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

## Migrate traffic

1. Pin the ViperCapture commit and Chromium version in a staging deployment.
2. Copy representative production requests with secrets stripped into a private test suite.
3. Compare output dimensions, pixel diffs, and HTTP status codes.
4. Check fonts, locale, timezone, authentication, lazy-loaded content, and cookie consent handling on real pages.
5. Replay representative traffic from the same client region with caching off, and record success rate, latency, and visual differences.
6. Test async restart recovery, webhook signature verification, artifact expiry, and schedule timezone transitions.
7. Set up reverse-proxy authentication, request limits, Chromium isolation, egress filtering, backups, and bucket lifecycle rules.
8. Shift traffic gradually and keep the managed vendor as a rollback target until your acceptance criteria pass.

Compatibility endpoints reject unknown vendor options. Resolve each validation error before moving that request to ViperCapture.

## Related

- [ScreenshotOne and Urlbox compatibility matrix](compatibility.md)
- [API and workflows](api.md)
- [Self-hosting and local setup](self-hosting.md)
- [Public API deployment example](../deploy/public-api/README.md)
- [All docs](README.md)
