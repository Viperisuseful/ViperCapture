---
name: viper-full-page-capture
description: Capture public webpages as full-page PNG, JPEG, WebP, or PDF files through the ViperCapture API. Use when the user asks for a full-page screenshot, website capture, visual reference, or rendered page artifact from a public URL.
---

# Viper Full Page Capture

## Overview

Use the bundled script to render a public URL through `https://capture.viperisuseful.cc/v1/render` with `full_page: true`. Save the returned artifact in the workspace and report its absolute path.

## Workflow

1. Identify the public URL and the requested format. Default to PNG when the user does not specify one.
2. Choose an output directory in the current workspace unless the user gives another path.
3. Run `scripts/capture.py`; never put the API key in the command line or in a generated file.
4. Return the saved file path and the ViperCapture request ID when available.

## Defaults and options

- Full-page capture is enabled by default.
- Default viewport is 1440x900 at device scale factor 1.
- Default cleanup rejects consent prompts and blocks ads, trackers, chats, and newsletters.
- Supported formats are `png`, `jpeg`, `webp`, and `pdf`.
- Use `--selector` for a specific element, or the `--wait-*` options for dynamic pages.
- Use PDF only when a document artifact is wanted; PDF output costs more credits than image output.

Example:

```powershell
python "C:\Users\<user>\.codex\skills\viper-full-page-capture\scripts\capture.py" `
  --url "https://example.com" --output png --output-dir .
```

## Safety and failures

- Only send public URLs unless the user explicitly confirms an authorized public target. Do not send session cookies, private headers, or personal files.
- CAPTCHA challenges are not solved or bypassed. `--proceed-on-captcha` only captures the challenge as displayed when that is explicitly requested.
- Retry only retryable failures and honor `Retry-After` for HTTP 429. Fix malformed requests, invalid selectors, private targets, and oversized dimensions instead of retrying them.
- If no key is found, tell the user to set `VIPERCAPTURE_API_KEY` or add it to the user-level `.env` file.

The API key is loaded from `VIPERCAPTURE_API_KEY`, then from `C:\Users\<user>\.env`, the current workspace `.env`, or `C:\Users\<user>\.codex\.env`. Never print or commit any of these secrets.
