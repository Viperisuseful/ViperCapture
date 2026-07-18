# Self-hosting ViperCapture

The public ViperCapture repository contains the MIT-licensed URL-to-image engine and browser interface only. Hosted accounts, billing, credits, referrals, document/PDF rendering, managed cleanup, deployment configuration, and secrets are deliberately excluded.

## Local install

Use Python 3.11 or newer, then run `python launch.py`. The launcher creates a virtual environment, installs Playwright Chromium, starts one Uvicorn worker, and opens the local interface.

For a manual installation:

```bash
bash install.sh
.venv/bin/python -m uvicorn main:app \
  --host 127.0.0.1 --port 8000 --workers 1 \
  --limit-concurrency 4 --no-access-log
```

## Production boundaries

- Put hosted mode behind a rate-limited reverse proxy.
- Run one Uvicorn worker; every worker owns a Chromium process tree.
- Keep `SHOT_MAX_CONCURRENCY=1` until memory and swap pressure are measured.
- Apply container or systemd memory, PID, and CPU limits.
- Enforce network egress rules that block private ranges and cloud metadata endpoints.
- Do not place credentials in the repository or browser-facing JavaScript.

## Capability boundary

The public engine supports a public URL source, PNG/JPEG/WebP output, viewport/full-page/selector capture, image quality, transparency, wait conditions, and same-origin target headers. It detects page-level challenges but never solves or bypasses CAPTCHAs.
