# Contributing to ViperCapture

This guide covers how to set up a local development checkout and submit focused fixes, documentation updates, and small features to the open-source rendering engine.

## Scope

This repository holds the public rendering engine, JSON API, CLI, terminal menu, and local web UI. Accounts, authentication, billing, credits, and hosted infrastructure live in a separate product and are not accepted here.

Open an issue to discuss large features or architectural changes before writing code. Report security vulnerabilities privately per [SECURITY.md](SECURITY.md).

## Set up the repository

Install Python 3.11 or newer. [uv](https://docs.astral.sh/uv/getting-started/installation/) is the preferred package installer:

```bash
git clone https://github.com/YOUR_USERNAME/ViperCapture.git
cd ViperCapture
python launch.py
```

`python launch.py` is the development startup path; end users install the `vipercapture` CLI as described in [README.md](README.md). When `uv` is on `PATH`, the launcher uses it to create `.venv` and install `requirements.txt`, then installs Chromium, Firefox, and WebKit and starts the app. Without `uv`, it falls back to `python -m venv` and `pip`. Set `VIPERCAPTURE_USE_UV=0` to force `pip`.

Manual `pip` setup also works:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On Windows, run `.venv\Scripts\python -m pip install -r requirements.txt`.

## Prepare a change

Keep each pull request limited to one change and include:

- What changed and why
- How to reproduce the original problem
- How you tested the change

Keep the engine's security boundaries intact: public HTTP and HTTPS targets in hosted mode, redirect and DNS checks, same-origin routing for custom headers, strict request validation, and bounded browser work. Never commit secrets, cookies, private URLs, generated captures, virtual environments, or browser binaries.

### Repository map

| Path | Responsibility |
| --- | --- |
| `launch.py` | Local launcher, environment setup, `--host`/`--port` guard, and CLI dispatch |
| `gui.py` | Full-screen `--gui` terminal capture menu, drawers, history, and keybinds |
| `vipercapture/main.py` | FastAPI application and local web UI routes |
| `vipercapture/capture_cli.py` | `vipercapture capture` command |
| `vipercapture/cli_commands.py` | Companion CLI subcommands (`status`, `job`, `schedule`, `verify`, `admin`, and others) |
| `vipercapture/cli_config.py` | `~/.vipercapture/config.toml` defaults and version helpers |
| `vipercapture/render_contract.py` | Request schema and validation |
| `vipercapture/render_engine.py` | Playwright rendering and network controls |
| `vipercapture/render_errors.py` | Stable API error envelopes |
| `vipercapture/async_jobs.py` | Provider-neutral queue contracts and worker lifecycle |
| `vipercapture/async_job_providers.py` | Bundled SQLite and filesystem adapters |
| `templates/` and `static/` | Local browser interface |

## Check a change

Lint and build the frontend, run the CLI and GUI unit tests, and run the cross-browser smoke check before opening a pull request:

```bash
npm ci --prefix frontend
npm run lint --prefix frontend
npm run build --prefix frontend
.venv/bin/python -m unittest tests.test_launch_installer tests.test_capture_cli tests.test_cli_commands tests.test_gui
.venv/bin/python -m playwright install --with-deps chromium firefox webkit
.venv/bin/python scripts/smoke.py
```

On macOS, omit `--with-deps`. On Windows, use `.venv\Scripts\python`, omit `--with-deps`, and run the same Playwright and smoke commands. Keep checks deterministic and avoid depending on live third-party sites.

By submitting a contribution, you agree to license it under the repository's [MIT License](LICENSE).
