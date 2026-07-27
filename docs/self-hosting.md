# Self-hosting ViperCapture

The public ViperCapture repository contains the MIT-licensed URL-to-image engine and browser interface only. Hosted accounts, billing, credits, referrals, document/PDF rendering, managed cleanup, deployment configuration, and secrets are deliberately excluded.

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

The public engine supports a public URL source, PNG/JPEG/WebP output, viewport/full-page/selector capture, image quality, transparency, wait conditions, and same-origin target headers. It blocks detected page-level challenges by default. Callers may set `proceed_on_captcha: true` to capture the visible challenge as displayed; ViperCapture never solves or bypasses CAPTCHAs.
