<p align="center">
  <img src="static/vipercapture-mark.svg" width="112" height="112" alt="ViperCapture logo">
</p>

<h1 align="center">ViperCapture</h1>

<p align="center">
  Capture public webpages as PNG, JPEG, or WebP images with Chromium.
</p>

<p align="center">
  <a href="https://capture.viperisuseful.cc">Hosted service</a>
  ·
  <a href="https://capture.viperisuseful.cc/docs">API documentation</a>
  ·
  <a href="docs/self-hosting.md">Self-hosting guide</a>
</p>

ViperCapture is a webpage capture engine with a browser interface and a JSON
API. It handles full-page, viewport, and element captures without requiring you
to manage browser automation code.

This repository contains the URL-to-image engine and local interface. The
hosted account, billing, document rendering, and deployment systems are kept in
separate private services.

## Features

- PNG, JPEG, and WebP output
- Full-page, viewport, and CSS selector capture
- Phone, desktop, and 4K presets with custom viewport support
- Image quality, transparent background, and device scale controls
- Wait conditions for page events, selectors, text, and fixed delays
- Same-origin request headers for authenticated or customized pages
- Bounded scrolling for lazy-loaded content
- Page-level challenge detection with an explicit capture-as-displayed choice

## Getting started

Install [Python 3.11 or newer](https://www.python.org/downloads/), then run:

```bash
git clone https://github.com/Viperisuseful/ViperCapture.git
cd ViperCapture
python launch.py
```

The launcher creates a virtual environment, installs the required packages and
Chromium, starts ViperCapture, and opens `http://127.0.0.1:8000`.

Windows users can double-click `run.bat` after cloning the repository.

For a manual installation:

```bash
bash install.sh
.venv/bin/python -m uvicorn main:app \
  --host 127.0.0.1 --port 8000 --workers 1 \
  --limit-concurrency 4 --no-access-log
```

## API

Send a JSON request to `POST /v1/render`:

```bash
curl 'http://127.0.0.1:8000/v1/render' \
  --header 'Content-Type: application/json' \
  --data '{
    "url": "https://www.wikipedia.org",
    "output": "webp",
    "viewport": {
      "width": 1280,
      "height": 720,
      "device_scale_factor": 1
    },
    "full_page": false,
    "selector": "main",
    "image": {
      "quality": 82,
      "transparent_background": true
    },
    "wait_for": {
      "event": "networkidle",
      "selector": "main",
      "timeout_ms": 15000
    },
    "headers": {
      "X-Render-Mode": "docs"
    }
  }' \
  --output wikipedia.webp
```

A successful request returns the image bytes with the matching media type.
Every response includes `X-Request-Id`. Errors use a consistent JSON object
with a stable code, message, request ID, retryable flag, and details.

### Request options

| Field | Default | Purpose |
| --- | --- | --- |
| `url` | required | Public webpage to capture |
| `output` | `png` | `png`, `jpeg`, or `webp` |
| `viewport` | `1280 × 720 × 1` | Width, height, and device scale factor |
| `full_page` | `true` | Capture the full document or current viewport |
| `selector` | empty | Capture one visible element when `full_page` is `false` |
| `image` | defaults | JPEG/WebP quality and PNG/WebP transparency |
| `wait_for` | load | Page event, selector, text, delay, and timeout |
| `headers` | `{}` | Headers sent only to same-origin target requests |
| `proceed_on_captcha` | `false` | Capture a detected page-level challenge as displayed instead of returning HTTP 409 |

Detected page-level challenges return `captcha_detected` by default. Setting
`proceed_on_captcha` to `true` captures the visible challenge; it does not solve
or bypass the CAPTCHA.

## Self-hosting

Run one Uvicorn worker because each worker owns a Chromium process tree. Start
with `SHOT_MAX_CONCURRENCY=1`, apply memory and CPU limits, and measure the host
before raising browser concurrency.

When `SHOT_HOSTED=1` is enabled, place ViperCapture behind a rate-limited
reverse proxy. Keep an operating-system or network egress rule that blocks
private address ranges and cloud metadata endpoints. This is the final SSRF
boundary.

See the [self-hosting guide](docs/self-hosting.md) for the full production
boundary and supported capability set.

## Development

Run the test suite with:

```bash
.venv/bin/python -m unittest discover
```

The main components are `main.py` for the FastAPI application,
`render_contract.py` for request validation, and `render_engine.py` for
Playwright rendering. The local interface is in `templates/` and `static/`.

## License

[MIT](LICENSE)
