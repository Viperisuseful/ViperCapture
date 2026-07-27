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

## Desktop app

The Tauri 2 app is isolated in [`desktop/`](desktop), so desktop packaging and
releases do not change the web frontend in [`frontend/`](frontend). It bundles
the FastAPI renderer, Python runtime, and Playwright Chromium as a local,
authenticated sidecar; users do not need to install Python or a browser.

See the [desktop build guide](docs/desktop.md) for local development,
validation, package formats, and signing status.

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
    "lazy_load": "adaptive",
    "selector": "main",
    "image": {
      "quality": 82,
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
boundary and supported capability set.

## Project layout

The main components are `main.py` for the FastAPI application,
`render_contract.py` for request validation, and `render_engine.py` for
Playwright rendering. The hosted/self-hosted interface remains in `frontend/`;
the independently packaged Tauri application is in `desktop/`.

## License

[MIT](LICENSE)
