# Contributing to ViperCapture

ViperCapture welcomes focused fixes, tests, documentation improvements, and
small features for the open-source URL-to-image engine.

## Scope

This repository contains the public rendering engine, JSON API, and local
browser interface. Accounts, authentication, billing, credits, and hosted
infrastructure belong to a separate product and are not accepted here.

Discuss large features or architectural changes in an issue before writing
them. Report security vulnerabilities privately as described in
[`SECURITY.md`](SECURITY.md).

## Setup

Use Python 3.11 or newer:

```bash
git clone https://github.com/YOUR_USERNAME/ViperCapture.git
cd ViperCapture
python launch.py
```

`python launch.py` is the supported setup and startup method. It creates the
virtual environment, installs dependencies and Chromium, and starts the app.

## Changes

Keep pull requests narrow and explain:

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
- `tests/` — renderer and API tests
- `vipercapture/render_errors.py` — stable API errors
- `vipercapture/async_jobs.py` — provider-neutral queue contracts and worker lifecycle
- `vipercapture/async_job_providers.py` — bundled SQLite and filesystem adapters
- `templates/` and `static/` — local browser interface

## Tests

Run the automated suite before submitting:

```bash
.venv/bin/python -m unittest -v
```

On Windows, use `.venv\Scripts\python -m unittest -v`.

Include a regression test for behavior changes where practical. Keep tests
deterministic and avoid relying on live third-party websites.

By submitting a contribution, you agree that it is licensed under the
repository's [MIT License](LICENSE).
