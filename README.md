# SHOT — Webpage Screenshot Tool

A fast, local tool that takes full-page screenshots of any webpage using a headless Chromium browser. No cloud, no API keys, no subscriptions — runs entirely on your machine.

## Features

- **Full-page capture** of any URL via headless Chromium
- **Lazy-content loading** — walks down the page before taking the final image
- **CAPTCHA warning** — asks before capturing a visible challenge page
- **Resolution presets** — Phone, Tablet, HD, Full HD, 2K, 4K (or type any custom size)
- **Image quality control** — 1× standard · 2× retina-sharp · 4× ultra
- **Custom filename templates** — `{host}`, `{date}`, `{time}` variables
- **Auto-save** to a local `captures/` folder on download *(local mode)*
- **Downloads shortcut** opens the browser's default Windows Downloads folder *(local mode)*
- **Capture history** filmstrip with one-click re-download
- **Responsive Viper-branded UI** for desktop and mobile
- **Hosted mode** for `scp.viperisuseful.cc` with device-only downloads
- **Smart launcher** — skips dependency installs after the first run

## Quick Start

**Windows:**
```
Double-click run.bat
```

**Any platform (Python 3.11+):**
```bash
python launch.py
```

The launcher will:
1. Create a Python virtual environment *(first run only)*
2. Install dependencies *(skipped on subsequent runs unless `requirements.txt` changes)*
3. Install Chromium *(first run only)*
4. Start the server and open `http://127.0.0.1:8000` in your browser

## Requirements

- **Python 3.11 or newer** — everything else is set up automatically

## Lightweight Oracle VM setup

Use one Uvicorn worker: each worker owns a Chromium process tree, so adding
workers multiplies memory use without making an individual capture faster.

```bash
bash install.sh
SHOT_HOSTED=1 .venv/bin/python -m uvicorn main:app \
  --host 127.0.0.1 --port 8000 --workers 1 \
  --limit-concurrency 4 --no-access-log
```

Put an authenticated, rate-limited reverse proxy in front of `127.0.0.1:8000`.
Hosted mode disables server-side file saving/folder opening and blocks private
network destinations. A VM egress rule **must** block private ranges and the
Oracle metadata address as the final SSRF boundary; DNS rebinding cannot be
safely contained inside Chromium request handlers. Run the service as a
dedicated non-root user and keep Chromium's sandbox enabled.

Apply an OS-level cap because a hostile page can allocate memory before its
final screenshot dimensions are known. For a systemd service on this VM, start
with `MemoryHigh=1500M`, `MemoryMax=2G`, `TasksMax=128`, and
`Restart=on-failure`, then adjust only from observed workloads.

Resource defaults are intentionally conservative:

- `SHOT_MAX_CONCURRENCY=1` — one active browser context
- `SHOT_MAX_PIXELS=50000000` — about 191 MiB per raw RGBA image
- `SHOT_HOSTED=1` — no VM disk writes or desktop process spawning

Machine-specific overrides can go in `.env.local`, which is git-ignored.
`SHOT_ENABLE_GPU=1` passes Chromium's single `--enable-gpu` switch and should
only be used after GPU diagnostics show a real hardware renderer. Oracle Always
Free standard shapes do not provide a GPU; Chromium flags cannot create one.
Set `SHOT_DOWNLOADS_DIR=C:\path\to\folder` only if the browser uses a custom
download location instead of the operating system default.

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) |
| Screenshots | [Playwright](https://playwright.dev/python/) (headless Chromium) |
| Frontend | Vanilla HTML / CSS / JS — no framework, no build step |

## Project Structure

```
screenshot-api/
├── SCPLogo.png          # Viper Screenshot API wordmark
├── main.py              # FastAPI app + screenshot endpoint
├── launch.py            # Smart launcher (venv + deps + server)
├── run.bat              # Windows one-click starter
├── requirements.txt     # Python dependencies
├── templates/
│   └── index.html       # UI
├── static/
│   ├── style.css        # Styles
│   ├── hero-preview.png # API-generated hero screenshot
│   └── app.js           # Frontend logic
└── captures/            # Screenshots saved here (git-ignored)
```

## API

The screenshot endpoint is also available directly:

```
GET /screenshot?url=https://example.com&width=1920&height=1080&device_scale_factor=2&wait=1
```

| Parameter | Default | Description |
|---|---|---|
| `url` | required | Page to capture |
| `width` | `1920` | Viewport width in pixels |
| `height` | `1080` | Viewport height in pixels |
| `device_scale_factor` | `2.0` | Pixel density multiplier (1–4) |
| `wait` | `1` | Extra seconds to wait after page load (0–15) |
| `proceed_on_captcha` | `false` | Capture a detected challenge page anyway |

Returns a PNG image. If a visible Cloudflare Turnstile, reCAPTCHA, or hCaptcha
challenge is detected, the endpoint returns `409` until
`proceed_on_captcha=true` is supplied. This captures the challenge as displayed;
the UI reloads the target once before doing so. It does not solve or bypass it.

Before measuring and capturing the full page, SCP performs a bounded scroll
pass to trigger content loaded by `IntersectionObserver` or native lazy images.

## License

MIT — see [LICENSE](LICENSE)
