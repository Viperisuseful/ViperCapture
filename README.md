<p align="center">
  <img src="static/vipercapture-mark.svg" width="112" height="112" alt="ViperCapture logo">
</p>

<h1 align="center">ViperCapture</h1>

<p align="center">
  Capture public webpages as PNG, JPEG, or WebP images with Chromium.
</p>

<p align="center">
  <a href="https://capture.viperisuseful.cc">Live Demo</a>
  ·
  <a href="docs/self-hosting.md">Self-hosting guide</a>
</p>

ViperCapture is a webpage capture engine with a browser interface and a JSON
API. It handles full-page, viewport, and element captures without requiring you
to manage browser automation code.

## Features

- PNG, JPEG, and WebP output
- Full-page, viewport, and CSS selector capture
- Optional viewport-width preservation for wide full-page captures
- Phone, desktop, and 4K presets with custom viewport support
- Image quality, transparent background, and device scale controls
- Wait conditions for page events, selectors, text, and fixed delays
- Same-origin request headers for authenticated or customized pages
- Bounded scrolling for lazy-loaded content
- Optional adaptive or disabled lazy-load scrolling for faster full-page captures
- Optional speed-first WebP encoding
- Optional hardware GPU rendering with startup verification
- A local-only GPU rendering switch that safely restarts Chromium after active captures finish
- The same Radix Nova shadcn/ui component system and theme used by ViperCapture Cloud
- Page-level challenge detection with an explicit capture-as-displayed choice
- Observable renders with elapsed time, active waits, cancellation, dimensions,
  file size, render duration, and request IDs
- Durable polling-based jobs with encrypted inputs, restart recovery, expiring
  results, and pluggable database and object-storage providers
- Inline validation for selectors, waits, and same-origin custom headers

## Getting started

Install [Python 3.11 or newer](https://www.python.org/downloads/), then run:

```bash
git clone https://github.com/Viperisuseful/ViperCapture.git
cd ViperCapture
python launch.py
```

The launcher creates a virtual environment, installs the required packages and
Chromium, starts ViperCapture, and opens `http://127.0.0.1:8000`.

Running `python launch.py` is the supported setup and startup method on every
platform.

## Native apps

The Tauri 2 app is isolated in [`desktop/`](desktop), so desktop packaging and
releases do not change the web frontend in [`frontend/`](frontend). It bundles
the FastAPI renderer, Python runtime, and Playwright Chromium as a local,
authenticated sidecar; users do not need to install Python or a browser.

See the [desktop build guide](docs/desktop.md) for local development,
validation, package formats, and signing status.

The Android build uses a native, offscreen Android WebView renderer instead of
the desktop Python sidecar. It supports Android 10 and newer and saves captures
through Android's Downloads collection. See the [Android build guide](docs/android.md)
for setup, local builds, supported controls, and release signing.

## API

Send a JSON request to `POST /v1/render`:

```bash
curl 'http://127.0.0.1:8000/v1/render' \
  --header 'Content-Type: application/json' \
  --data '{
    "url": "https://www.wikipedia.org",
    "output": "png",
    "viewport": {
      "width": 1280,
      "height": 720,
      "device_scale_factor": 1
    },
    "full_page": false,
    "lazy_load": "adaptive",
    "selector": "main",
    "image": {
      "transparent_background": true,
      "optimize_for_speed": true
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
  --output wikipedia.png
```

A successful request returns the image bytes with the matching media type.
Every response includes `X-Request-Id`. Successful renders also report queue
time, render time, and output dimensions in `X-ViperCapture-*` diagnostic
headers. Errors use a consistent JSON object with a stable code, message,
request ID, retryable flag, and details.

### Async jobs

For work that should survive a client disconnect or application restart, submit
the same request to `POST /v1/jobs`, poll its status, and download the result:

```bash
job_id="$(
  curl --fail-with-body --silent \
    --header 'Content-Type: application/json' \
    --header 'X-Request-Id: nightly-homepage' \
    --data '{"url":"https://example.com","full_page":false}' \
    http://127.0.0.1:8000/v1/jobs |
  python -c 'import json,sys; print(json.load(sys.stdin)["id"])'
)"

curl --fail-with-body "http://127.0.0.1:8000/v1/jobs/$job_id"
curl --fail-with-body "http://127.0.0.1:8000/v1/jobs/$job_id/result" \
  --output capture.png
```

The bundled provider uses SQLite for job state and private local files for
results. Queued request data is AES-GCM encrypted before it reaches the job
store. Database and storage adapters are independently replaceable through
Python factory hooks, so deployments can use PostgreSQL, Redis, S3-compatible
storage, or another provider without changing the API or renderer. See the
[async jobs and providers guide](docs/async-jobs.md).

### Request options

| Field | Default | Purpose |
| --- | --- | --- |
| `url` | required | Public webpage to capture |
| `output` | `png` | `png`, `jpeg`, or `webp` |
| `viewport` | `1280 × 720 × 1` | Width, height, and device scale factor |
| `full_page` | `true` | Capture the full document or current viewport |
| `preserve_viewport_width` | `false` | Clip wide full-page output to the requested viewport width |
| `lazy_load` | `thorough` | `thorough`, `adaptive`, or `none` full-page scrolling |
| `selector` | empty | Capture one visible element when `full_page` is `false` |
| `image` | defaults | JPEG/WebP quality, PNG/WebP transparency, and speed-first WebP encoding |
| `wait_for` | load | Page event, selector, text, delay, and timeout |
| `headers` | `{}` | Headers sent only to same-origin target requests |
| `proceed_on_captcha` | `false` | Capture a detected page-level challenge as displayed instead of returning HTTP 409 |

Detected page-level challenges return `captcha_detected` by default. Setting
`proceed_on_captcha` to `true` captures the visible challenge; it does not solve
or bypass the CAPTCHA.

For the lowest latency, combine `wait_for.event: "domcontentloaded"` with
`lazy_load: "none"`. This captures earlier and may omit resources or content
that appears later. `lazy_load: "adaptive"` is a middle ground, while the
default `thorough` mode preserves the existing scrolling behavior.

## Agent skill

The portable [Agent Skill](https://agentskills.io) in
[`skills/vipercapture`](skills/vipercapture) lets Codex, Claude Code, and
Cursor capture webpages or render HTML and Markdown through ViperCapture. It
includes a dependency-free Python client for ViperCapture instances.

Clone this repository, then copy the skill into the user-level directory for
your agent:

| Agent | Install location | Invocation |
| --- | --- | --- |
| [Codex](https://developers.openai.com/codex/skills) | `~/.agents/skills/vipercapture` | `$vipercapture` |
| [Claude Code](https://code.claude.com/docs/en/skills) | `~/.claude/skills/vipercapture` | `/vipercapture` |
| [Cursor](https://cursor.com/docs/skills) | `~/.agents/skills/vipercapture` or `~/.cursor/skills/vipercapture` | `/vipercapture` |

```bash
git clone https://github.com/Viperisuseful/ViperCapture.git
cd ViperCapture
```

On macOS or Linux, install for Codex and Cursor with:

```bash
mkdir -p ~/.agents/skills
cp -R skills/vipercapture ~/.agents/skills/
```

Install for Claude Code with:

```bash
mkdir -p ~/.claude/skills
cp -R skills/vipercapture ~/.claude/skills/
```

On Windows PowerShell, replace the destination with
`$HOME\.agents\skills\vipercapture`, `$HOME\.claude\skills\vipercapture`, or
`$HOME\.cursor\skills\vipercapture`:

```powershell
New-Item -ItemType Directory -Force "$HOME\.agents\skills" | Out-Null
Copy-Item -Recurse -Force ".\skills\vipercapture" "$HOME\.agents\skills"
```

## Self-hosting

Run one application process because each process owns a Chromium process tree.
Start with `VIPERCAPTURE_MAX_CONCURRENCY=1`, apply memory and CPU limits, and
measure the host before raising browser concurrency.

See the [self-hosting guide](docs/self-hosting.md) for the full production
boundary and supported capability set, and the
[async jobs guide](docs/async-jobs.md) for durable queue configuration and
custom providers.

## Project layout

The main components are `main.py` for the FastAPI application,
`render_contract.py` for request validation, and `render_engine.py` for
Playwright rendering. `async_jobs.py` defines the portable queue contracts and
`async_job_providers.py` supplies SQLite and filesystem defaults. The
hosted/self-hosted interface remains in `frontend/`;
the independently packaged Tauri application is in `desktop/`.

## License

[MIT](LICENSE)
