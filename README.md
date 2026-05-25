# SHOT — Webpage Screenshot Tool

A fast, local tool that takes full-page screenshots of any webpage using a headless Chromium browser. No cloud, no API keys, no subscriptions — runs entirely on your machine.

## Features

- **Full-page capture** of any URL via headless Chromium
- **Resolution presets** — Phone, Tablet, HD, Full HD, 2K, 4K (or type any custom size)
- **Image quality control** — 1× standard · 2× retina-sharp · 4× ultra
- **Custom filename templates** — `{host}`, `{date}`, `{time}` variables
- **Auto-save** to a local `captures/` folder on download
- **Capture history** filmstrip with one-click re-download
- **Zero-scroll UI** — everything visible without scrolling
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

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) |
| Screenshots | [Playwright](https://playwright.dev/python/) (headless Chromium) |
| Frontend | Vanilla HTML / CSS / JS — no framework, no build step |

## Project Structure

```
screenshot-api/
├── main.py              # FastAPI app + screenshot endpoint
├── launch.py            # Smart launcher (venv + deps + server)
├── run.bat              # Windows one-click starter
├── requirements.txt     # Python dependencies
├── templates/
│   └── index.html       # UI
├── static/
│   ├── style.css        # Styles
│   └── app.js           # Frontend logic
└── captures/            # Screenshots saved here (git-ignored)
```

## API

The screenshot endpoint is also available directly:

```
GET /screenshot?url=https://example.com&width=1920&height=1080&device_scale_factor=2&wait=4
```

| Parameter | Default | Description |
|---|---|---|
| `url` | required | Page to capture |
| `width` | `1920` | Viewport width in pixels |
| `height` | `1080` | Viewport height in pixels |
| `device_scale_factor` | `2.0` | Pixel density multiplier (1–4) |
| `wait` | `4` | Extra seconds to wait after page load |

Returns a PNG image.

## License

MIT — see [LICENSE](LICENSE)
