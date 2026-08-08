# Self-hosting ViperCapture

The public ViperCapture repository contains the MIT-licensed rendering engine,
browser interface, orchestration APIs, local/S3-compatible storage, schedules,
signed delivery, diagnostics, and video. The managed ViperCapture Cloud account,
billing, credits, referrals, deployment configuration, and production secrets
remain separate.

## Local install

Use Python 3.11 or newer, then run `python launch.py`. This is the supported
setup and startup method. The launcher creates a virtual environment, installs
Playwright Chromium, starts the application, and opens the local interface.

## Production boundaries

- Put hosted mode behind a rate-limited reverse proxy.
- Run one application process; every process owns a Chromium process tree.
- Keep `VIPERCAPTURE_MAX_CONCURRENCY=1` until memory and swap pressure are measured.
- Apply container or systemd memory, PID, and CPU limits.
- Enforce network egress rules that block private ranges and cloud metadata endpoints.
- Do not place credentials in the repository or browser-facing JavaScript.
- Put all `/v1/jobs` routes behind the same reverse-proxy authentication as
  `/v1/render`; opaque job IDs are not an authorization mechanism.

## Optional GPU rendering

GPU acceleration is off by default. Self-hosters with a compatible GPU and
driver can set:

```bash
VIPERCAPTURE_GPU_MODE=auto
```

Use `required` instead of `auto` to fail startup unless Chromium reports
hardware GPU compositing through the Chrome DevTools Protocol. On headless
Linux systems where normal graphics autodetection does not work, also try:

```bash
VIPERCAPTURE_GPU_BACKEND=vulkan
```

The host or container must expose the GPU device and its drivers to Chromium.
The Vulkan backend is workload- and driver-dependent, so benchmark it against
the default backend before enabling it permanently. GPU acceleration primarily
helps rasterization, compositing, canvas, and WebGL-heavy pages; navigation,
JavaScript, scrolling waits, and image encoding remain CPU- or network-bound.
The older `VIPERCAPTURE_ENABLE_GPU=1` setting remains supported as an alias for
`VIPERCAPTURE_GPU_MODE=auto`.

The local interface also exposes a **GPU rendering** switch. It drains active
captures, restarts Chromium in `auto` or `off` mode, and reports whether
Chromium verified hardware compositing. For safety, this runtime switch accepts
only same-origin requests from the loopback interface; remotely hosted
instances should continue to configure GPU mode through environment variables
and restart the service normally.

## Capability boundary

The public engine implements the feature set documented in [API and workflows](api.md).
It blocks detected page-level challenges by default. Callers may set
`proceed_on_captcha: true` to capture the visible challenge as displayed;
ViperCapture never solves or bypasses CAPTCHAs.

Polling-based jobs are enabled by default and use the same rendering contract,
SSRF controls, concurrency semaphore, and pixel limits as `/v1/render`. The
local defaults keep encrypted state and expiring results under
`~/.vipercapture`. Set `VIPERCAPTURE_ASYNC_JOBS=0` to disable the subsystem, or
follow the [async jobs guide](async-jobs.md) to change retention, capacity, and
providers.

Schedules are enabled by default on Unix hosts. The bundled SQLite schedule
store cannot enforce private ACLs on Windows, so `VIPERCAPTURE_SCHEDULES`
defaults to `0` there and the bundled store refuses direct startup. Use an
external scheduler on Windows or keep the feature disabled.

## Selectors and waits

`selector` captures the first visible matching element and requires
`full_page: false`. `wait_for.selector` waits for a matching element to become
visible before capture. Both fields accept standard CSS selectors, are limited
to 2,048 characters, and do not support pseudo-elements such as `::before`.

Use `wait_for.event` for `load`, `domcontentloaded`, or `networkidle`.
`wait_for.text` waits for text in the document body, `delay_ms` adds a final
settle delay, and `timeout_ms` bounds page and selector waits. The local
interface displays the active wait plan and elapsed time while a render runs.
Cancelling the interface request also cancels queued or active server work.

For full-page captures of unusually wide documents, set
`preserve_viewport_width: true` to clip horizontal overflow to the requested
viewport while retaining the full document height.

## Custom headers

`headers` must be a JSON object whose values are strings. At most 32 headers
and 16 KiB serialized data are accepted; each name is limited to 128 bytes and
each value to 4 KiB. Hop-by-hop, proxy, `Host`, `Content-Length`, `Sec-*`, and
`X-Forwarded-*` headers are managed by ViperCapture and cannot be overridden.

Custom headers are sent only to requests matching the target URL's exact
scheme, hostname, and port. They are stripped from cross-origin subresources
and redirects.

If a Cloudflare, CDN, WAF, or origin rule blocks captures of a site you
administer, use the scoped pattern in [site access](site-access.md): fixed
renderer address, exact host and path, and an origin-only secret header. It
does not disable or evade challenges on third-party sites.

## Diagnostic response headers

Successful `POST /v1/render` responses include:

- `X-Request-Id`
- `X-ViperCapture-Queue-Ms`
- `X-ViperCapture-Render-Ms`
- `X-ViperCapture-Width`
- `X-ViperCapture-Height`
- `X-ViperCapture-Navigation-Status`, when navigation returned a response

Dimensions are output pixels after applying the device scale factor.
