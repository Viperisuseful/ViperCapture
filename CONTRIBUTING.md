# Contributing to ViperCapture

This guide explains how to submit focused fixes, documentation updates, and
small features to the open-source rendering engine.

## Scope

This repository contains the public rendering engine, JSON API, and local
browser interface. Accounts, authentication, billing, credits, and hosted
infrastructure belong to a separate product and are not accepted here.

Discuss large features or architectural changes in an issue before writing
them. Report security vulnerabilities privately as described in
[`SECURITY.md`](SECURITY.md).

## Set up the repository

Use Python 3.11 or newer. [uv](https://docs.astral.sh/uv/) is the preferred
installer
([installation guide](https://docs.astral.sh/uv/getting-started/installation/)):

```bash
git clone https://github.com/YOUR_USERNAME/ViperCapture.git
cd ViperCapture
python launch.py
```

`python launch.py` is the supported setup and startup method. It prefers uv
when uv is on `PATH` to create `.venv` and install from `requirements.txt`,
then installs Chromium, Firefox, and WebKit and starts the app. Without uv it
falls back to `python -m venv` and pip. Set `VIPERCAPTURE_USE_UV=0` to force
the pip path. Existing pip-only workflows stay valid:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On Windows, use `.venv\Scripts\python -m pip install -r requirements.txt`.

## Prepare a change

Keep the pull request limited to one change. Include:

- What changed and why
- How to reproduce the original problem
- How the change was tested

Preserve the public engine's main security boundaries: public HTTP(S) targets
only, redirect and DNS checks, same-origin routing for custom headers, strict
request validation, and bounded browser work. Never commit secrets, cookies,
private URLs, generated captures, virtual environments, or browser binaries.

The main files are:

- `vipercapture/main.py` — FastAPI application and local interface
- `vipercapture/render_contract.py` — request validation
- `vipercapture/render_engine.py` — Playwright rendering and network controls
- `vipercapture/render_errors.py` — stable API errors
- `vipercapture/async_jobs.py` — provider-neutral queue contracts and worker lifecycle
- `vipercapture/async_job_providers.py` — bundled SQLite and filesystem adapters
- `templates/` and `static/` — local browser interface

## Check a change

Build the frontend and run the cross-browser smoke check before submitting:

```bash
npm ci --prefix frontend
npm run lint --prefix frontend
npm run build --prefix frontend
.venv/bin/python -m playwright install --with-deps chromium firefox webkit
.venv/bin/python scripts/smoke.py
```

On Windows, use `.venv\Scripts\python`, omit `--with-deps`, and run the same
Playwright and smoke commands. Keep checks deterministic and avoid relying on
live third-party websites.

By submitting a contribution, you agree that it is licensed under the
repository's [MIT License](LICENSE).
