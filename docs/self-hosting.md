# Self-host ViperCapture

This repository contains the MIT-licensed rendering engine,
browser interface, orchestration APIs, local/S3-compatible storage, schedules,
signed delivery, diagnostics, and video. The managed ViperCapture Cloud account,
billing, credits, referrals, deployment configuration, and production secrets
remain separate.

## Install locally

Use Python 3.11 or newer and install a full FFmpeg build through the operating
system package manager or the [FFmpeg download page](https://ffmpeg.org/download.html). Confirm `ffmpeg -encoders` lists `libvpx`, `libvpx-vp9`,
and `libx264`; GPU video additionally requires the vendor encoder and driver.

End users install the `vipercapture` command and then run it (open a new terminal first on Linux and macOS; on Windows PowerShell the current session's `Path` is updated automatically).

Linux and macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/Viperisuseful/ViperCapture/master/scripts/install.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/Viperisuseful/ViperCapture/master/scripts/install.ps1 | iex
```

Then start it (on Linux or macOS, open a new terminal first):

```bash
vipercapture
```

The command creates a virtual environment, installs Playwright Chromium,
Firefox, and WebKit, starts the application, and opens the local interface.
Press Esc twice or Ctrl+C to stop it. `vipercapture update` downloads the
latest published version and keeps the virtualenv, `.env.local`, saved
jobs, and presets. It is the same Python command on Linux, macOS, and
Windows. On Windows, stop the running server before updating. The update does
not use sudo or a distro package manager. `vipercapture --gui` opens a
full-screen capture menu in the same terminal and does not open a log
window. Tab switches PNG, GIF, and MP4. Ctrl+P switches between the full
page and the 1920x1080 viewport. The menu sends `VIPERCAPTURE_ADMIN_TOKEN`
when it started the server. For a server that is already running it sends
`VIPERCAPTURE_API_KEY` when that project key is set, and the admin token
otherwise. `vipercapture --one-window` keeps the
status and request log in the same terminal, for SSH or any session where a
second window should not open. Windows always runs that way. From a git
checkout, `python launch.py` runs that same startup path. The launcher uses uv
when it is on `PATH` and otherwise falls back to pip. The Docker image already
includes FFmpeg.

`vipercapture capture https://example.com --output page.png` renders through
the local API and prints the saved path. The same flags work on Linux, macOS,
and Windows. `vipercapture capture --help` lists them. On Linux and macOS the
server this command starts stores its data in `~/.vipercapture/data`, which
it keeps owner-only, including when the checkout itself is group-writable.

### Intel macOS (x86_64)

[GHSA-jwv3-5hgf-82ww](https://github.com/advisories/GHSA-jwv3-5hgf-82ww) is
fixed in `cryptography` 49.0.0. PyCA dropped Intel macOS wheels in that
release ([changelog](https://cryptography.io/en/latest/changelog/#v49-0-0)).
PyPI file lists for
[49.0.0](https://pypi.org/project/cryptography/49.0.0/#files),
[50.0.0](https://pypi.org/project/cryptography/50.0.0/#files), and
[50.0.1](https://pypi.org/project/cryptography/50.0.1/#files) publish
`macosx_11_0_arm64` wheels and an sdist only — no `x86_64` or `universal2`
wheel for CPython 3.11+. The last Intel/universal2 wheels are on vulnerable
48.0.1; do not use them.

`launch.py` therefore source-builds the official sdist on Intel Macs. Before
installing dependencies it checks for a C compiler, Rust 1.83.0 or newer, and
a non-Apple OpenSSL prefix, then sets `OPENSSL_DIR` / `PKG_CONFIG_PATH` from
Homebrew or MacPorts. If anything is missing it exits with the
[PyCA macOS build steps](https://cryptography.io/en/latest/installation/#building-cryptography-on-macos):

```bash
xcode-select --install
brew install openssl@3 rust
```

MacPorts: `sudo port install openssl rust`. Rust is only needed at build time.
Linux, Windows, and Apple Silicon keep binary wheels (`cryptography>=50.0.0`).

### Stop running instances

`vipercapture kill` stops all running instances of ViperCapture (API server, GUI menu, and background workers) on Windows, macOS, and Linux. The API server receives an authenticated graceful-shutdown request and gets 30 seconds to close active work and browser processes before force escalation; set `VIPERCAPTURE_KILL_GRACE_SECONDS` to adjust that window (5–300 seconds).

### Terminal display

On Linux and macOS, Ghostty and Kitty open request logs in another window and
show a full-screen status view in this one. The terminal graphics logo is drawn for Kitty,
Ghostty, iTerm2, WezTerm, foot, mlterm, and Contour. Terminals without a reliably detectable graphics protocol, including Windows Terminal, display a styled two-tone block ASCII wordmark. Windows Terminal does not expose a supported Sixel capability/version variable; users who know their active native or WSL session supports Sixel can opt in with `VIPERCAPTURE_SIXEL=1`. WezTerm
uses the Kitty graphics protocol when `enable_kitty_graphics = true`, and the
iTerm2 image protocol otherwise. Override the `--gui` viewport with
`vipercapture --gui --viewport 1280 720`.
On Ubuntu and other apt-based systems, the first launch may ask for your
password so Playwright can install the libraries its browsers need. Other
Linux systems download the browsers and leave system packages alone.

### Run from a clone

To work from a clone instead, install uv if you can
([installation guide](https://docs.astral.sh/uv/getting-started/installation/)):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows, use `irm https://astral.sh/uv/install.ps1 | iex` in PowerShell.
Then run:

```bash
git clone https://github.com/Viperisuseful/ViperCapture.git
cd ViperCapture
python launch.py
```

`python launch.py` is the same startup path the `vipercapture` command runs.
The launcher prefers uv when it is on `PATH`: it creates `.venv` and installs
from `requirements.txt`. If uv is missing, it falls back to the standard
library `venv` module and pip. Set `VIPERCAPTURE_USE_UV=0` to force that pip
path even when uv is installed.

### Run with Docker

```bash
docker compose up --build
```

Stable container images are also published to GitHub Container Registry:

```bash
docker pull ghcr.io/viperisuseful/vipercapture:1.1.1
```

By default, Compose binds only to loopback. It keeps durable queue state,
encryption keys, schedules, cache entries, and local artifacts in a named
volume. Read the production section below before exposing the service to
a network. Set separate `VIPERCAPTURE_ADMIN_TOKEN` and
`VIPERCAPTURE_CONTROL_SECRET` values to enable the built-in project control
plane before exposing API routes to multiple tenants.

## Send render requests

`vipercapture capture` prints the saved path on stdout. A failure is one JSON object on stderr. Exit 0
means the file was written, 2 means the command was rejected, and 1 means
the capture or the network failed. `--full-page=false` and `--no-full-page`
are the same flag. Action flags run in the order you write them, after
`--wait` and `--delay-ms`. PowerShell and cmd take these same flags; put
them on one line if the shell does not continue a trailing backslash.
When the local server is not already running, `capture` starts it and leaves it
listening. Stop that process with `vipercapture kill`.

A synchronous request returns the artifact directly, with `X-Request-Id`
and `X-ViperCapture-*` headers for queue and render timing, dimensions,
navigation status, and cache outcome. See
[diagnostic response headers](#read-diagnostic-response-headers).

For work that must survive a dropped connection, send the same render object to
`POST /v1/jobs`. Poll
`GET /v1/jobs/{id}`, download `GET /v1/jobs/{id}/result`, or receive a signed
callback by setting `delivery.webhook_url`. Related orchestration endpoints:

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/jobs/bulk` | Best-effort submission of up to 100 independently idempotent jobs |
| `POST/GET/PATCH/DELETE /v1/schedules` | Encrypted five-field cron schedules with IANA time zones |
| `POST /v1/signed-url` | Mint an expiring HMAC render link |
| `GET /v1/render/signed` | Render through a verified signed link |
| `POST /v1/diff` | Compare two images and download a deterministic report ZIP |
| `PUT/GET/POST /v1/baselines` | Store project baselines and compare review bundles |
| `POST/DELETE /v1/profiles` | Manage encrypted Playwright storage-state profiles |
| `POST /v1/profiles/import` | Import pasted Cookie headers or browser-exported session files |
| `GET /take` | ScreenshotOne-compatible common-options adapter |
| `POST /compat/urlbox/v1/render/{sync,async}` | Urlbox-compatible adapters |

## Use proxies, sessions, and CAPTCHA hooks

Self-hosted renders can route an isolated browser context through an HTTP,
HTTPS, SOCKS4, or SOCKS5 proxy. Keep credentials in separate fields instead of
embedding them in the proxy URL:

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

With the project control plane enabled, `POST /v1/profiles/import` normalizes
Playwright storage state, Cookie-Editor JSON, Netscape `cookies.txt`, or a
pasted Cookie header into an encrypted profile. Pass its returned `id` as
`profile_id` on later renders. Imports preserve local storage and partitioned
cookies where the export format supports them.

ViperCapture detects common blocking CAPTCHA and bot interstitials but does not
solve or bypass them. This repository is the standard Playwright product. For
Patchright plus Turnstile complete-when-possible / harder challenge handling,
use the [ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth)
fork (0.1.0-beta). It is not a universal Cloudflare bypass; see that
repository's [README](https://github.com/Viperisuseful/ViperCapture-Stealth#stealth-mode--patchright)
for how-to. The default `captcha.action` is `error`; use `capture` to
render the challenge as-is. Operators may configure their own approved async
handler with `VIPERCAPTURE_CAPTCHA_HANDLER_FACTORY` and opt in per request with
`captcha.action: "external"`. See the [API guide](api.md) for the handler
contract and timeout behavior. Alternatively, an authorized caller can use an
external tool independently, then start a fresh render with short-lived,
target-scoped session state. ViperCapture ships no provider integration,
credentials, endorsement, solver, or bypass service. If a site you
administer challenges the renderer, use the least-privilege
[Cloudflare/WAF authorization guide](site-access.md).

## Configure storage and webhooks

Set `VIPERCAPTURE_S3_BUCKET` to store job results through the built-in S3
adapter instead of local files. It uses standard AWS credential resolution.
R2 and MinIO also need an endpoint URL and, where appropriate, path-style
addressing. The `docker-compose.s3.yml` overlay provides a complete local MinIO
example:

```bash
export MINIO_ROOT_PASSWORD='replace-with-at-least-a-long-random-secret'
docker compose -f docker-compose.yml -f docker-compose.s3.yml up --build
```

Set `VIPERCAPTURE_WEBHOOK_SECRET` to enable callbacks. Each callback contains
canonical JSON signed with HMAC-SHA256 in
`X-ViperCapture-Webhook-Signature`. Timestamp and event-ID headers support
verification and deduplication. Private callback targets are rejected unless
the operator explicitly opts in. Public DNS results stay pinned for the life
of the callback connection to prevent rebinding, and the encrypted delivery
outbox survives process restarts.

## Secure the service

Keep the service on loopback, set both `VIPERCAPTURE_ADMIN_TOKEN` and
`VIPERCAPTURE_CONTROL_SECRET` (each at least 32 bytes), or put every
route behind the same authenticated, rate-limited reverse proxy. When the
control plane is disabled, job and schedule UUIDs identify resources but do
not provide access control. Hosted mode rejects targets and redirects that
resolve to private addresses during validation, along with unsafe subresources,
cross-origin credential headers, proxy use, and cross-site cookies. Browser DNS
can change after validation, so deployments also need host or container egress
rules to block rebinding. Self-host mode allows internal pages and
proxies; isolate Chromium with that boundary in mind.

JavaScript actions are disabled unless `VIPERCAPTURE_ALLOW_SCRIPTS=1`. Render
inputs can contain credentials and private page data. Async and scheduled inputs
are AES-GCM encrypted and erased at terminal job states, but diagnostic console
output and browser video can still record sensitive page content. Treat those
artifacts accordingly.

## Verify an installation

```bash
uv venv
uv pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium firefox webkit
npm ci --prefix frontend && npm run lint --prefix frontend && npm run build --prefix frontend
.venv/bin/python scripts/smoke.py
```

Without uv, use `python -m venv .venv` and
`.venv/bin/python -m pip install -r requirements.txt` instead, then the same
Playwright, frontend, and smoke commands. On Windows, use
`.venv\Scripts\python -m pip install -r requirements.txt` and
`.venv\Scripts\python` for Playwright and smoke, and omit `--with-deps` from
the Playwright command. On macOS, omit `--with-deps`.
The smoke command starts a temporary local server and verifies Chromium,
Firefox, WebKit, OpenAPI, health, and output dimensions. Pass
`--base-url http://host:port` to check an existing deployment instead. For an
authenticated deployment, set `VIPERCAPTURE_SMOKE_TOKEN` to an API or
administrator token.

## Configure a production deployment

- Put hosted mode behind a rate-limited reverse proxy.
- Run one application process. Chromium starts immediately and can grow into a
  small process pool (`VIPERCAPTURE_BROWSER_POOL_SIZE`, default about half of
  concurrency). Firefox and WebKit still start lazily when requested.
- Default `VIPERCAPTURE_MAX_CONCURRENCY` is CPU-sized (2–8). Set it to `1`
  until memory and swap pressure are measured on large or full-page captures.
- Keep the default `VIPERCAPTURE_BROWSER_RECYCLE_RENDERS=1000`; it replaces one
  drained browser process after that many render attempts without pausing the
  rest of the pool. Set it to `0` only for controlled diagnostics.
- Image cache TTL defaults to 24 hours (`VIPERCAPTURE_CACHE_TTL_SECONDS`).
- Apply container or systemd memory, PID, and CPU limits.
- Enforce network egress rules that block private ranges and cloud metadata endpoints.
- Do not place credentials in the repository or browser-facing JavaScript.
- Put all `/v1/jobs` routes behind the same reverse-proxy authentication as
  `/v1/render`; opaque job IDs are not an authorization mechanism.

## Local render limits

Local installs default to a 500,000,000-pixel budget,
16,384 CSS pixels for either viewport dimension, and 100,000 CSS pixels of
full-page height. The encoded-output ceiling is 1 GiB. These are safety
ceilings. A machine, page, browser build, or image format can fail below these
limits. A decoded
RGBA surface alone needs roughly four bytes per output pixel, and Chromium plus
the encoder need additional working memory. Keep concurrency at one for very
large captures.

Remote self-hosters should size the limits to the container or VM instead of
exposing the local defaults unchanged. For example, this restores conservative
hosted ceilings:

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

Put these values in the `.env` file beside `docker-compose.yml`, or export them
before running `python launch.py`. The effective limits are returned by
`GET /app-config` and enforced for direct renders, async jobs, schedules, bulk
jobs, and viewport packs. Output pixels are approximately
`width × height × device_scale_factor²`; full-page height is measured after the
page finishes loading.

Do not remove the pixel ceiling on an Internet-facing service. Also set an
application memory limit, a request timeout, and low Chromium concurrency so
one extreme page cannot exhaust the host.

## Configure page cleanup and rendering

The local browser interface exposes the Cloud cleanup controls:
reject, accept, hide, or leave cookie consent unchanged; block known ad,
tracker, chat, and newsletter resources or overlays; and apply custom CSS.
Cleanup is opt-in at the API layer and enabled by default only in the
interactive interfaces.

Advanced controls include deterministic device signals, color scheme, reduced
motion, locale and timezone, selector capture, rectangular crop, lazy-content
loading, readiness waits, exact failure statuses, same-origin headers, image
encoding, PDF layout, extraction mode, diagnostics, and video settings. The
API additionally exposes explicit screen/print CSS media, a PDF structure-tag
setting, actions, cookies, proxies, resource patterns,
geolocation, assertions, deterministic time/randomness, slices, profiles,
signed delivery, and certification as documented in [API and workflows](api.md).

## Configure GPU rendering

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

The host or container must expose the GPU device and its drivers to Chromium
and FFmpeg. The same switch also tries runtime-tested hardware video encoding
through NVENC (NVIDIA), AMF (AMD), Quick Sync (Intel), VideoToolbox (Apple),
Media Foundation (Windows), or VA-API (Linux). Unsupported or unusable encoders
fall back to the existing software path. GIF, transparent WebM, and screenshot
file compression remain CPU encoded; GPU acceleration can still speed the
browser's page compositing and rasterization before those files are encoded.
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

## Feature limits

The public engine implements the feature set documented in [API and workflows](api.md).
It blocks detected page-level challenges by default. Callers may set
`proceed_on_captcha: true` to capture the visible challenge as displayed;
ViperCapture never solves or bypasses CAPTCHAs. This is the standard
Playwright product. For Patchright plus Turnstile complete-when-possible /
harder challenge handling, use
[ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth)
(0.1.0-beta); see that README for how-to. It is not a Cloudflare bypass.

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
`full_page: false`. `wait_for.selector` accepts `selector_state` values
`visible` (default), `attached`, `hidden`, and `detached`. Both selector fields
accept standard CSS selectors, are limited to 2,048 characters, and do not
support pseudo-elements such as `::before`.

Use `wait_for.event` for `load`, `domcontentloaded`, or `networkidle`.
`wait_for.text` waits for text in the document body, `delay_ms` adds a final
settle delay, and `timeout_ms` bounds page and selector waits. Set
`wait_for.images: true` to eagerly request current lazy images and wait until
all current images complete; full-page renders repeat the bounded image wait
after lazy-content scrolling. The local
interface displays the active wait plan and elapsed time while a render runs.
Cancelling the interface request also cancels queued or active server work.

For full-page captures of unusually wide documents, set
`preserve_viewport_width: true` to clip horizontal overflow to the requested
viewport while retaining the full document height.

## Target-page JavaScript

Set `network.java_script_enabled` to `false` to prevent scripts delivered by
the target page from running. It defaults to `true`. This browser-context
control is separate from `VIPERCAPTURE_ALLOW_SCRIPTS`, which gates caller-
supplied JavaScript actions; disabling either setting does not enable the
other.

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
does not disable or evade challenges on third-party sites. This engine does
not claim a Cloudflare bypass. For Patchright plus Turnstile
complete-when-possible / harder challenge handling, see
[ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth)
(0.1.0-beta).

## Read diagnostic response headers

Successful `POST /v1/render` responses include:

- `X-Request-Id`
- `X-ViperCapture-Queue-Ms`
- `X-ViperCapture-Render-Ms`
- `X-ViperCapture-Width`
- `X-ViperCapture-Height`
- `X-ViperCapture-Navigation-Status`, when navigation returned a response

Dimensions are output pixels after applying the device scale factor.
