# Self-host ViperCapture

This repository holds the MIT-licensed rendering engine, web UI, CLI, terminal menu, JSON API, local and S3-compatible storage, schedules, signed delivery, diagnostics, and video pipeline. Managed cloud accounts, billing, credits, referrals, deployment configs, and production secrets live outside this repository.

## Install locally

Install Python 3.11 or newer and a full FFmpeg build from your operating system package manager or the [FFmpeg download page](https://ffmpeg.org/download.html). Verify that `ffmpeg -encoders` lists `libvpx`, `libvpx-vp9`, and `libx264`. Hardware GPU video encoding also requires the matching vendor encoder and driver. The Docker image includes FFmpeg.

On Linux or macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/Viperisuseful/ViperCapture/master/scripts/install.sh | bash
```

On Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/Viperisuseful/ViperCapture/master/scripts/install.ps1 | iex
```

On Windows PowerShell, the installer updates `Path` in the current session. On Linux and macOS, open a new terminal first, then run:

```bash
vipercapture
```

The first launch creates a virtual environment, installs Playwright Chromium, Firefox, and WebKit, starts the server, and opens the local web UI. Press Esc twice or Ctrl+C to stop it.

### Local commands

| Command | Action |
| --- | --- |
| `vipercapture [--host HOST] [--port PORT]` | Start the local server and open the web UI (default `http://127.0.0.1:8000`). |
| `vipercapture --gui [URL]` | Open the full-screen terminal capture menu in the same window. |
| `vipercapture --one-window` | Keep the status view and request log in one terminal, useful over SSH. Windows always runs in one window. |
| `vipercapture capture URL --output FILE` | Capture a URL through the API and print the saved path, or pass multiple URLs or `--urls-file` with `--output-dir`. |
| `vipercapture version` (`--version`, `-V`) | Print the installed ViperCapture version (supports `--json`). |
| `vipercapture status` | Check server health, readiness, GPU state, engines, and async jobs. |
| `vipercapture logs` | Read or tail `~/.vipercapture/requests.log`. |
| `vipercapture gpu status\|off\|auto` | Show or switch local GPU mode. |
| `vipercapture preset list\|show\|save\|delete` | Manage saved local presets. |
| `vipercapture diff` | Compare two images through `/v1/diff` and save a visual-diff ZIP. |
| `vipercapture baseline save\|list\|delete\|compare` | Manage project visual baselines. |
| `vipercapture bulk` | Submit a batch of async jobs to `/v1/jobs/bulk`. |
| `vipercapture job status\|cancel\|download` | Inspect, cancel, or download async jobs under `/v1/jobs`. |
| `vipercapture schedule create\|list\|get\|update\|delete` | Manage cron schedules. |
| `vipercapture profile create\|import\|delete` | Manage encrypted browser profiles. |
| `vipercapture sign` | Mint an HMAC-signed render URL through `/v1/signed-url`. |
| `vipercapture verify BUNDLE` | Verify the Ed25519 signature, SHA-256 digests, and canonical manifest of a `--certify` ZIP bundle. |
| `vipercapture admin projects\|keys\|audit\|status` | Manage control-plane projects, API keys, audit logs, and operator status. |
| `vipercapture completion bash\|zsh\|fish\|powershell` | Print shell completion scripts. |
| `vipercapture update [--check]` | Download the latest release while keeping your virtualenv, `.env.local`, saved jobs, and presets (`--check` exits 10 when an update is available). |
| `vipercapture kill` | Stop all running ViperCapture API servers, menus, and background workers. |

Binding the launcher to a non-loopback `--host` fails unless `VIPERCAPTURE_ADMIN_TOKEN` is set in the environment or `.env.local`. `--host` and `--port` cannot be combined with `--gui`.

`vipercapture update` runs the same Python updater on Linux, macOS, and Windows without sudo or a system package manager. On Windows, stop the running server before updating.

Store default `vipercapture capture` flags (such as `server`, `format`, `engine`, `full_page`, `output_dir`, and `preset`) and client credentials (`api_key`, `admin_token`, and `signing_token`) in `~/.vipercapture/config.toml`. `vipercapture capture` applies those defaults and accepts `--no-config` to ignore the file. Server-talking companion subcommands read `server` and their applicable token keys (`api_key`, `admin_token`, or `signing_token`) from `config.toml` and do not accept `--no-config`; `version`, `logs`, and `completion` do not read `config.toml`.

### Terminal capture menu (`--gui`)

`vipercapture --gui` opens a full-screen capture menu in the current terminal for all 11 output formats (`png`, `jpeg`, `webp`, `avif`, `pdf`, `gif`, `mp4`, `webm`, `html`, `markdown`, and `metadata`). It accepts an optional positional `URL` (or `--url URL`), `--viewport WIDTH HEIGHT` (default `1920 1080`), `--output-dir DIR`, `--server URL` to target an existing or remote server, and `--preset NAME` to load a saved preset at startup.

The URL bar supports cursor movement, word jumps, clipboard paste, and persisted URL history stored in `~/.vipercapture/gui_history.json`.

| Keybind | Action |
| --- | --- |
| `Left` / `Right` | Move the URL cursor |
| `Ctrl+Left` / `Ctrl+Right` | Jump by word or URL segment |
| `Home` / `Ctrl+A`, `End` / `Ctrl+E` | Move to the start or end of the URL |
| `Backspace` / `Delete` | Delete before or at the cursor |
| `Ctrl+W` / `Ctrl+U`, `Ctrl+V` | Delete word, clear URL, or paste from clipboard |
| `Up` / `Down` | Cycle persisted URL history (`gui_history.json`) |
| `Tab` / `Shift+Tab` | Cycle next or previous output format |
| `Ctrl+P` | Toggle full page or viewport capture |
| `Ctrl+R` | Cycle viewport and device presets |
| `Ctrl+T` | Cycle color scheme (system, dark, light) |
| `Ctrl+B` | Cycle browser engine (Chromium, Firefox, WebKit) |
| `Ctrl+X` | Toggle page cleanup |
| `Ctrl+D` | Cycle video duration or settle delay |
| `Enter` | Capture the current URL |
| `Ctrl+O` / `Ctrl+Y` | Open the last capture or copy its file path |
| `Ctrl+G` | Open the Advanced and GPU drawer |
| `Ctrl+F` | Open the saved Presets drawer |
| `Ctrl+L` | Open the History drawer with inline PNG preview |
| `F1` / `Ctrl+/` | Toggle the Help drawer |
| `Esc` | Cancel an active capture, close a drawer, or quit |
| `Ctrl+C` | Quit immediately |

`--gui` reads `VIPERCAPTURE_ADMIN_TOKEN` and `VIPERCAPTURE_API_KEY` from its own process environment (unlike the child server, it does not load `.env.local`):

- **Server started by `--gui`.** Sends `VIPERCAPTURE_ADMIN_TOKEN` when set, preferring it over `VIPERCAPTURE_API_KEY`.
- **Already-running default local server (`http://127.0.0.1:8000` or loopback port `8000` over HTTP).** Sends `VIPERCAPTURE_API_KEY` when set and falls back to `VIPERCAPTURE_ADMIN_TOKEN`.
- **Other `--server` URLs.** Non-default local `--server` URLs receive `VIPERCAPTURE_API_KEY` only (no admin token). Remote `--server` URLs receive `VIPERCAPTURE_API_KEY` only over HTTPS, and no credential over plain HTTP.

`vipercapture capture` accepts the same flags on Linux, macOS, and Windows, and `vipercapture capture --help` lists them. On Linux and macOS, a server started by `capture` stores its data in `~/.vipercapture/data` with owner-only permissions, even if the repository checkout is group-writable.

### Stop running instances

`vipercapture kill` stops every local API server, terminal menu, and background worker on Windows, macOS, and Linux. It sends an authenticated graceful-shutdown request to the API server and waits 30 seconds for active captures and browser processes to exit before forcing termination. Set `VIPERCAPTURE_KILL_GRACE_SECONDS` to any value from 5 to 300 seconds to change that timeout.

### Terminal display

On Linux and macOS, Ghostty and Kitty open request logs in a second window and show a full-screen status view in the current window. ViperCapture renders the graphics logo in Kitty, Ghostty, iTerm2, WezTerm, foot, mlterm, and Contour. WezTerm uses the Kitty graphics protocol when `enable_kitty_graphics = true`, and the iTerm2 image protocol otherwise.

Terminals without a reliably detectable graphics protocol, including Windows Terminal, show a two-tone block ASCII wordmark. Windows Terminal does not expose a supported Sixel capability or version variable. If your native or WSL terminal supports Sixel, set `VIPERCAPTURE_SIXEL=1` to opt in.

On Ubuntu and other apt-based Linux distributions, the first launch may prompt for your password so Playwright can install system browser libraries. Other Linux distributions download only the browser binaries and leave system packages untouched.

### Intel macOS (x86_64)

[GHSA-jwv3-5hgf-82ww](https://github.com/advisories/GHSA-jwv3-5hgf-82ww) is fixed in `cryptography` 49.0.0. PyCA dropped Intel macOS wheels in that release ([changelog](https://cryptography.io/en/latest/changelog/#v49-0-0)). PyPI releases for [49.0.0](https://pypi.org/project/cryptography/49.0.0/#files), [50.0.0](https://pypi.org/project/cryptography/50.0.0/#files), and [50.0.1](https://pypi.org/project/cryptography/50.0.1/#files) ship `macosx_11_0_arm64` wheels and a source distribution only, with no `x86_64` or `universal2` wheel for CPython 3.11 or newer. The last Intel and `universal2` wheels belong to vulnerable 48.0.1. Do not install them.

On Intel Macs, `launch.py` builds the official source distribution instead. Before installing dependencies, it checks for a C compiler, Rust 1.83.0 or newer, and a non-Apple OpenSSL prefix, then sets `OPENSSL_DIR` and `PKG_CONFIG_PATH` from Homebrew or MacPorts. If a build tool is missing, it exits with the [PyCA macOS build steps](https://cryptography.io/en/latest/installation/#building-cryptography-on-macos):

```bash
xcode-select --install
brew install openssl@3 rust
```

With MacPorts, run `sudo port install openssl rust`. Rust is required only during the build. Linux, Windows, and Apple Silicon continue to use binary wheels (`cryptography>=50.0.0`).

### Run from a clone

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you want faster environment setup:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows PowerShell, run `irm https://astral.sh/uv/install.ps1 | iex`. Then clone and launch:

```bash
git clone https://github.com/Viperisuseful/ViperCapture.git
cd ViperCapture
python launch.py
```

`python launch.py` runs the same startup path as `vipercapture`. When `uv` is on `PATH`, the launcher creates `.venv` and installs `requirements.txt` with `uv`. Otherwise it falls back to `venv` and `pip`. Set `VIPERCAPTURE_USE_UV=0` to force `pip` even when `uv` is installed.

### Run with Docker

Build and start the local Compose stack:

```bash
docker compose up --build
```

You can also pull the published container image from GitHub Container Registry:

```bash
docker pull ghcr.io/viperisuseful/vipercapture:1.1.1
```

By default, Compose binds only to loopback and stores queue state, encryption keys, schedules, cache entries, and local artifacts in a named volume. Review the production security section below before exposing the container to a network, and set separate `VIPERCAPTURE_ADMIN_TOKEN` and `VIPERCAPTURE_CONTROL_SECRET` values before serving multiple tenants.

## Send render requests

`vipercapture capture` prints each saved file path to stdout on success (or the job ID when using `--async --no-wait`) and one JSON object to stderr on failure. Exit code `0` means the command succeeded, `2` means it failed validation, and `1` means the capture or network call failed. `--full-page=false` and `--no-full-page` behave identically. Action flags (including `--action-wait-text` and `--js-file`) run in command-line order after `--wait` and `--delay-ms`.

Useful capture flags include `--from-json` (base request JSON or `-` for stdin), `--preset`, `--cookie-file` (Playwright JSON, Cookie-Editor JSON, Netscape `cookies.txt`, or `Cookie` header), `--urls-file` or multiple positional URLs, `--output-dir`, `--output-template`, `--retries` and `--retry-delay-ms`, `--async --no-wait`, `--open`, `--copy`, `--certify`, `--no-config`, and `--stop-server`. PowerShell and cmd accept the same flags; put them on one line if your shell does not treat a trailing backslash as a line continuation. When no local server is running, `capture` starts one and leaves it listening unless you pass `--stop-server`. Stop it later with `vipercapture kill`.

Synchronous `POST /v1/render` calls return the artifact directly along with `X-Request-Id` and `X-ViperCapture-*` headers for queue time, render time, dimensions, navigation status, and cache outcome. See [Read diagnostic response headers](#read-diagnostic-response-headers).

For captures that must survive a dropped connection, submit the same render payload to `POST /v1/jobs`, poll `GET /v1/jobs/{id}`, download `GET /v1/jobs/{id}/result`, or configure a signed callback with `delivery.webhook_url`.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/jobs/bulk` | Best-effort submission of up to 100 independently idempotent jobs |
| `POST/GET/PATCH/DELETE /v1/schedules` | Encrypted five-field cron schedules in any IANA timezone |
| `POST /v1/signed-url` | Mint an expiring HMAC render link |
| `GET /v1/render/signed` | Render through a verified signed link |
| `POST /v1/diff` | Compare two images and download a deterministic report ZIP |
| `PUT/GET/POST /v1/baselines` | Store project baselines and compare review bundles |
| `POST/DELETE /v1/profiles` | Manage encrypted Playwright storage-state profiles |
| `POST /v1/profiles/import` | Import pasted `Cookie` headers or browser-exported session files |
| `GET /take` | ScreenshotOne-compatible common-options adapter |
| `POST /compat/urlbox/v1/render/{sync,async}` | Urlbox-compatible sync and async adapters |

## Use proxies, sessions, and CAPTCHA hooks

Self-hosted renders can route an isolated browser context through an HTTP, HTTPS, SOCKS4, or SOCKS5 proxy. Pass credentials in separate fields instead of embedding them in the proxy URL:

```json
{
  "url": "https://example.com",
  "network": {
    "proxy": {
      "server": "socks5://proxy.example:1080",
      "username": "account-zone-residential",
      "password": "secret"
    }
  }
}
```

With the project control plane enabled, `POST /v1/profiles/import` normalizes Playwright storage state, Cookie-Editor JSON, Netscape `cookies.txt`, or a pasted `Cookie` header into an encrypted profile. Pass the returned `id` as `profile_id` on later renders. Imports keep `localStorage` and partitioned cookies when the source format provides them.

ViperCapture detects blocking CAPTCHA and bot interstitials, but it never solves or bypasses them. This repository runs standard Playwright and does not claim a Cloudflare bypass. For Patchright and harder Turnstile challenge handling, see the separate [ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth) fork (`0.1.0-beta`) and its [Stealth mode README section](https://github.com/Viperisuseful/ViperCapture-Stealth#stealth-mode--patchright). Stealth is not a universal Cloudflare bypass.

The default `captcha.action` is `error`. Set `captcha.action` to `capture` to render the challenge page as displayed. Operators can also register an approved async handler with `VIPERCAPTURE_CAPTCHA_HANDLER_FACTORY` and opt in per request with `captcha.action: "external"`. See the [API guide](api.md) for the handler contract and timeout behavior. An authorized caller can also complete an access flow outside ViperCapture with an independent tool and start a fresh render using short-lived, target-scoped session state. ViperCapture ships no provider integration, credentials, solver, or bypass service. If a site you control blocks the renderer, follow the least-privilege [Cloudflare and WAF authorization guide](site-access.md).

## Configure storage and webhooks

Set `VIPERCAPTURE_S3_BUCKET` to write job results through the built-in S3 adapter instead of local files. The adapter uses standard AWS credential resolution. Cloudflare R2 and MinIO also need an endpoint URL and, when required by the provider, path-style addressing. Use `docker-compose.s3.yml` for a local MinIO setup:

```bash
export MINIO_ROOT_PASSWORD='replace-with-at-least-a-long-random-secret'
docker compose -f docker-compose.yml -f docker-compose.s3.yml up --build
```

Set `VIPERCAPTURE_WEBHOOK_SECRET` to turn on webhook callbacks. ViperCapture signs canonical JSON bodies with HMAC-SHA256 in `X-ViperCapture-Webhook-Signature` and sends timestamp and event-ID headers for verification and deduplication. It rejects private callback targets unless the operator explicitly opts in, pins resolved public DNS addresses for the life of each callback connection to block DNS rebinding, and keeps pending callbacks in an encrypted outbox across restarts.

## Secure the service

Keep the server on loopback, set both `VIPERCAPTURE_ADMIN_TOKEN` and `VIPERCAPTURE_CONTROL_SECRET` (at least 32 bytes each), or put every route behind one authenticated, rate-limited reverse proxy. When the control plane is off, job and schedule UUIDs locate resources but do not authorize access.

Hosted mode rejects targets and redirects that resolve to private addresses during validation, along with unsafe subresources, cross-origin credential headers, custom proxies, and cross-site cookies. Because browser DNS can change after validation, pair hosted mode with host or container egress rules that block private ranges and cloud metadata endpoints. Self-host mode permits internal targets and proxies, so isolate Chromium accordingly.

JavaScript actions stay off unless you set `VIPERCAPTURE_ALLOW_SCRIPTS=1`. Render inputs can carry credentials and private page data. ViperCapture encrypts async and scheduled inputs with AES-GCM and erases them at terminal states, but diagnostic console logs and recorded browser video can still capture sensitive content. Protect those outputs with the same controls as the source page.

## Configure a production deployment

- Put hosted mode behind a rate-limited reverse proxy.
- Run one application process. Chromium starts at boot and scales to a small process pool controlled by `VIPERCAPTURE_BROWSER_POOL_SIZE` (about half of concurrency by default). Firefox and WebKit start lazily on first request.
- `VIPERCAPTURE_MAX_CONCURRENCY` defaults to a CPU-sized value from 2 to 8. Set it to `1` until you measure memory and swap use on large or full-page captures.
- Keep `VIPERCAPTURE_BROWSER_RECYCLE_RENDERS=1000` so ViperCapture drains and replaces one browser process every 1,000 render attempts without pausing the rest of the pool. Set it to `0` only for controlled diagnostics.
- Image cache entries expire after 24 hours by default (`VIPERCAPTURE_CACHE_TTL_SECONDS`).
- Apply container or systemd memory, PID, and CPU limits.
- Enforce host or container egress rules that block private IP ranges and cloud metadata endpoints.
- Keep credentials out of the repository and out of browser-facing JavaScript.
- Protect all `/v1/jobs` routes with the same reverse-proxy authentication as `/v1/render`, because job UUIDs are not access control.

## Local render limits

Local installs default to 500,000,000 output pixels, 16,384 CSS pixels for viewport width and height, 100,000 CSS pixels of full-page height, and 1 GiB of encoded output. These are upper safety bounds. Individual machines, pages, browser builds, or codecs can fail below them. A decoded RGBA image needs about four bytes per output pixel, and Chromium plus the encoder need extra working memory. Keep concurrency at `1` for very large captures.

Remote self-hosters should size these limits to their container or VM rather than exposing local defaults unchanged. These values set conservative hosted limits:

```bash
VIPERCAPTURE_MAX_PIXELS=50000000
VIPERCAPTURE_MAX_WIDTH=7680
VIPERCAPTURE_MAX_HEIGHT=4320
VIPERCAPTURE_MAX_FULL_PAGE_HEIGHT=20000
VIPERCAPTURE_MAX_OUTPUT_BYTES=52428800
VIPERCAPTURE_MAX_CONCURRENCY=1
VIPERCAPTURE_BROWSER_POOL_SIZE=1
VIPERCAPTURE_BROWSER_RECYCLE_RENDERS=1000
VIPERCAPTURE_CACHE_TTL_SECONDS=86400
```

Put them in the `.env` file beside `docker-compose.yml` or export them before running `python launch.py`. `GET /app-config` reports the active limits, and ViperCapture enforces them across direct renders, async jobs, schedules, bulk jobs, and viewport packs. Output pixels are roughly `width * height * device_scale_factor^2`, and full-page height is measured after load.

Never remove the pixel limit on an Internet-facing server. Combine it with an application memory cap, a request timeout, and low Chromium concurrency so one extreme page cannot exhaust the host.

## Configure page cleanup and rendering

The local web UI includes the cleanup controls: reject, accept, hide, or ignore cookie consent banners; block known ad, tracker, chat, and newsletter resources or overlays; and inject custom CSS. Cleanup is opt-in on the API and enabled by default only in the interactive UIs.

Advanced UI controls cover device presets, color scheme, reduced motion, locale and timezone, selector capture, rectangular crop, lazy-content loading, readiness waits, exact failure statuses, same-origin headers, image encoding, PDF layout, extraction mode, diagnostics, and video settings. The API also supports explicit screen or print CSS media, PDF structure tags, typed actions, cookies, proxies, resource blocks, geolocation, assertions, deterministic time and randomness, full-page slices, profiles, signed delivery, and certification. See [API and workflows](api.md).

## Configure GPU rendering

GPU acceleration is off by default. On hosts with a supported GPU and driver, enable it with:

```bash
VIPERCAPTURE_GPU_MODE=auto
```

Use `required` instead of `auto` to fail startup unless Chromium verifies hardware GPU compositing through the Chrome DevTools Protocol. On headless Linux hosts where graphics autodetection fails, try:

```bash
VIPERCAPTURE_GPU_BACKEND=vulkan
```

The host or container must expose the GPU device and drivers to both Chromium and FFmpeg. When GPU mode is on, ViperCapture also probes hardware video encoders for NVENC (NVIDIA), AMF (AMD), Quick Sync (Intel), VideoToolbox (Apple), Media Foundation (Windows), and VA-API (Linux). Any encoder that fails the probe or the encode falls back to software encoding. GIF, transparent WebM, and screenshot compression always run on CPU, though GPU compositing can still speed up page rasterization before encoding.

Benchmark `vulkan` against the default backend before adopting it permanently because results depend on the workload and driver. GPU acceleration helps rasterization, compositing, canvas, and WebGL-heavy pages, while navigation, JavaScript, scroll waits, and image compression stay CPU-bound or network-bound. The older `VIPERCAPTURE_ENABLE_GPU=1` flag still works as an alias for `VIPERCAPTURE_GPU_MODE=auto`.

The local web UI also provides a **GPU rendering** toggle. It drains active captures, restarts Chromium in `auto` or `off` mode, and reports whether Chromium verified hardware compositing. For safety, this runtime switch accepts only same-origin requests from loopback. Remote instances should configure GPU mode with environment variables and a normal restart.

## Feature limits

The open-source engine blocks detected page-level challenges by default. Set `proceed_on_captcha: true` to capture the visible challenge page as displayed. ViperCapture never solves or bypasses CAPTCHAs. For Patchright and harder Turnstile handling, use [ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth) (`0.1.0-beta`), which is also not a universal Cloudflare bypass.

Polling-based async jobs are on by default on Unix hosts and enforce the same rendering contract, SSRF checks, concurrency semaphore, and pixel ceilings as `/v1/render`. By default, ViperCapture stores encrypted job state and expiring artifacts under `~/.vipercapture`. Set `VIPERCAPTURE_ASYNC_JOBS=0` to disable them, or read [Async jobs](async-jobs.md) to configure retention, capacity, and external providers.

Schedules are on by default on Unix hosts. Because Python file modes do not create private Windows ACLs, `VIPERCAPTURE_SCHEDULES` defaults to `0` on Windows and the bundled SQLite schedule store refuses to start there. On Windows, use external providers or keep schedules disabled.

## Selectors and waits

`selector` captures the first visible matching element and requires `full_page: false`. `wait_for.selector` accepts `selector_state` values `visible` (default), `attached`, `hidden`, and `detached`. Both fields accept standard CSS selectors up to 2,048 characters and reject pseudo-elements like `::before`.

Use `wait_for.event` for `load`, `domcontentloaded`, or `networkidle`. `wait_for.text` waits for text in the document body, `delay_ms` adds a final settle wait, and `timeout_ms` bounds page and selector waits. Set `wait_for.images: true` to trigger current lazy images and wait until they finish loading; full-page renders repeat that bounded wait after lazy scrolling. The local web UI shows the active wait plan and elapsed time while a render runs, and cancelling a request in the UI cancels the queued or active server work.

For unusually wide pages, set `preserve_viewport_width: true` to clip horizontal overflow to the requested viewport width while keeping the full document height.

## Target-page JavaScript

Set `network.java_script_enabled` to `false` to stop scripts delivered by the target page from executing. It defaults to `true`. This setting is independent of `VIPERCAPTURE_ALLOW_SCRIPTS`, which gates caller-supplied JavaScript actions. Disabling one never enables the other.

## Custom headers

`headers` must be a JSON object mapping string names to string values. ViperCapture accepts up to 32 headers and 16 KiB of serialized header data, with limits of 128 bytes per name and 4 KiB per value. ViperCapture manages hop-by-hop, proxy, `Host`, `Content-Length`, `Sec-*`, and `X-Forwarded-*` headers and rejects attempts to override them.

ViperCapture sends custom headers only to requests matching the target URL's exact scheme, hostname, and port, and strips them from cross-origin subresources and redirects.

If a Cloudflare, CDN, WAF, or origin rule blocks captures of a site you administer, follow [site access](site-access.md) to allow the renderer by fixed source IP, exact host and path, and an origin-only secret header. That workflow authorizes your own site; it does not evade challenges on third-party sites.

## Read diagnostic response headers

Successful `POST /v1/render` responses include these headers:

- `X-Request-Id`
- `X-ViperCapture-Queue-Ms`
- `X-ViperCapture-Render-Ms`
- `X-ViperCapture-Encode-Ms`
- `X-ViperCapture-Profile`
- `X-ViperCapture-Cache` (`hit`, `miss`, or `disabled`)
- `X-ViperCapture-Width`
- `X-ViperCapture-Height`
- `X-ViperCapture-Navigation-Status`, present when navigation returned an HTTP response

Width and height report output pixels after applying the device scale factor.

## Verify an installation

Run the frontend build and cross-browser smoke check:

```bash
uv venv
uv pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium firefox webkit
npm ci --prefix frontend && npm run lint --prefix frontend && npm run build --prefix frontend
.venv/bin/python scripts/smoke.py
```

Without `uv`, create the environment with `python -m venv .venv` and install packages with `.venv/bin/python -m pip install -r requirements.txt`, then run the same Playwright, frontend, and smoke commands.

On macOS, omit `--with-deps`. On Windows, use `.venv\Scripts\python -m pip install -r requirements.txt` and `.venv\Scripts\python` for Playwright and smoke, and omit `--with-deps`.

`scripts/smoke.py` starts a temporary local server and checks Chromium, Firefox, WebKit, OpenAPI, health, and output dimensions. Pass `--base-url http://host:port` to verify an already running deployment. For an authenticated deployment, set `VIPERCAPTURE_SMOKE_TOKEN` to an API key or administrator token.

## Related

- [Full feature list](features.md)
- [API and workflows](api.md)
- [Async jobs and queue providers](async-jobs.md)
- [Projects, API keys, and observability](platform.md)
- [Public API deployment example](../deploy/public-api/README.md)
- [All docs](README.md)
