# ViperCapture

ViperCapture is an MIT-licensed webpage capture engine and browser interface.
It loads a public URL in Chromium, performs a bounded lazy-content scroll, and
returns a full-page PNG, JPEG, or WebP image.

This public repository contains only the URL-to-image engine, local website,
launchers, and safety controls. It intentionally does not include hosted
accounts, API keys, quotas/credits, referrals, PayPal billing, PDF output,
HTML/Markdown input, content extraction, cleanup automation, proxy routing, or
deployment configuration.

## Features

- Full-page PNG, JPEG, and WebP capture with headless Chromium
- Bounded scrolling for lazy-loaded pages
- Phone through 4K presets plus custom viewport sizes
- Full-page, viewport, and selector capture
- Configurable density, JPEG/WebP quality, transparency, waits, and same-origin headers
- Page-level challenge detection with provider, kind, confidence, and signals
- Responsive light and dark interfaces
- Hosted-mode public-network validation and resource limits
- Local Windows launcher and cross-platform Python launcher

## Run locally

Requirements: Python 3.11 or newer.

On Windows, double-click `run.bat`. On any supported platform:

```bash
python launch.py
```

The launcher creates a virtual environment, installs dependencies and
Chromium when needed, starts the application, and opens
`http://127.0.0.1:8000`.

To install without opening the launcher:

```bash
bash install.sh
.venv/bin/python -m uvicorn main:app \
  --host 127.0.0.1 --port 8000 --workers 1 \
  --limit-concurrency 4 --no-access-log
```

## API

```bash
curl 'http://127.0.0.1:8000/v1/render' \
  --header 'Content-Type: application/json' \
  --data '{
    "url":"https://www.wikipedia.org",
    "output":"webp",
    "viewport":{"width":1280,"height":720,"device_scale_factor":1},
    "full_page":false,
    "selector":"main",
    "image":{"quality":82,"transparent_background":true},
    "wait_for":{"event":"networkidle","selector":"main","timeout_ms":15000},
    "headers":{"X-Render-Mode":"docs"}
  }' \
  --output wikipedia.webp
```

| Parameter | Default | Description |
| --- | --- | --- |
| `url` | required | Public page URL to capture |
| `output` | `png` | `png`, `jpeg`, or `webp` |
| `viewport` | `1280 × 720 × 1` | Width, height, and device scale factor |
| `full_page` | `true` | Capture the full document or current viewport |
| `selector` | empty | Capture one visible element; requires `full_page: false` |
| `image` | defaults | JPEG/WebP quality and PNG/WebP transparency |
| `wait_for` | load | Ready event, selector, text, delay, and timeout |
| `headers` | `{}` | Safe custom headers sent only to same-origin target requests |

A successful request returns the selected image media type. Detector v2 ignores
ordinary embedded CAPTCHA form widgets, but reports page-level challenges with
provider, kind, confidence, and detection signals. The engine does not solve or
bypass CAPTCHAs. Every response has `X-Request-Id`; errors return a stable
`error.code`, message, request ID, retryable flag, and details object.

## Open-source boundary

This repository deliberately stays useful and small: URL source only;
PNG/JPEG/WebP output only. ViperCapture's hosted account system, private key
storage, usage ledger, paid plans, referrals, document/PDF renderer, extraction,
and managed cleanup are separate proprietary service code. No secrets or hosted
database schema are required to run this project locally.

## Hosted-mode safety

Set `SHOT_HOSTED=1` only behind a rate-limited reverse proxy. Hosted mode
disables server-side file saving and desktop folder opening, and rejects
private-network targets. Keep an OS or network egress rule blocking private
ranges and cloud metadata endpoints as the final SSRF boundary.

Useful limits:

```bash
SHOT_HOSTED=1
SHOT_MAX_CONCURRENCY=1
SHOT_MAX_PIXELS=50000000
```

Run one Uvicorn worker because each worker owns a Chromium process tree. Apply
container or systemd memory/task limits and measure before increasing browser
concurrency.

Machine-only overrides can live in `.env.local`, which is ignored by Git.
Never commit credentials.

## Structure

```text
screenshot-api/
├── main.py              # FastAPI app and v1 route
├── render_contract.py   # Strict URL-to-image JSON schema
├── render_engine.py     # Isolated Playwright image renderer
├── render_errors.py     # Request IDs and stable errors
├── launch.py            # Local environment and browser launcher
├── run.bat              # Windows entry point
├── requirements.txt
├── templates/index.html # Browser interface
├── static/              # Styles, JavaScript, and media
└── test_boundary.py     # Public/private capability boundary guard
```

## Stack

- FastAPI and Uvicorn
- Playwright with headless Chromium
- Vanilla HTML, CSS, and JavaScript

## License

[MIT](LICENSE)
