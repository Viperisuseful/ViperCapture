---
name: vipercapture
description: Render public URLs, HTML, or Markdown as PNG, JPEG, WebP, PDF, HTML, or Markdown artifacts with ViperCapture. Use for webpage screenshots, full-page captures, element captures, PDFs, rendered documents, or article extraction through the hosted or self-hosted ViperCapture API.
---

# ViperCapture

Use the bundled `scripts/capture.py` client. Resolve the script path relative to
this `SKILL.md`; do not assume the current working directory is the skill
directory.

## Capture workflow

1. Determine whether the source is a public URL, an HTML file, or a Markdown
   file. Never upload a local file unless the user asked to render that file.
2. Choose the output. Default to `png`; use `pdf` only for a document artifact,
   and use `html` or `markdown` only when the user asks for extracted content.
3. Save the artifact in the current workspace unless the user provides a path.
4. Run the client with Python 3.11 or newer.
5. Report the absolute output path and request ID from the client's JSON result.

Example:

```bash
python /path/to/skills/vipercapture/scripts/capture.py \
  --url https://example.com \
  --output png \
  --output-dir .
```

Render local content:

```bash
python /path/to/skills/vipercapture/scripts/capture.py \
  --markdown-file ./report.md \
  --output pdf \
  --output-path ./report.pdf
```

## Options

- Captures are full-page by default. Use `--viewport-only` for the initial
  viewport. Supplying `--selector` automatically uses element capture.
- Use `--viewport-width`, `--viewport-height`, and `--device-scale-factor` only
  when the user needs a specific viewport. For full-page captures these values
  set the initial viewport; the final artifact may be taller than that viewport.
- Use `--wait-event`, `--wait-selector`, `--wait-text`, or `--delay-ms` for
  dynamic pages.
- Use `--cleanup` to reject consent prompts and block ads, trackers, chat
  widgets, and newsletter overlays.
- Use `--quality` only for JPEG or WebP. Use `--transparent-background` only
  for PNG or WebP.
- Use `--extract-mode article` with `--output html` or `--output markdown` to
  isolate article content.
- Use `--api-url http://127.0.0.1:8000/v1/render` for a self-hosted engine.
  Self-hosted feature support may be narrower than the hosted API.
- Run the client with `--help` for the complete option list.

## Authentication and safety

- The hosted API reads `VIPERCAPTURE_API_KEY` from the environment. Never put a
  key in a command, prompt, output filename, committed file, or chat response.
- A custom self-hosted API URL may run without a key. If
  `VIPERCAPTURE_API_KEY` is present, the client sends it to that endpoint.
- Capture only public or explicitly authorized targets. ViperCapture blocks
  private-network targets and unsafe redirects.
- Do not solve or bypass CAPTCHAs. `--proceed-on-captcha` captures the visible
  challenge exactly as displayed and requires an explicit user request.
- Do not overwrite an existing artifact unless the user explicitly approves
  replacement and `--force` is supplied.
- Retry only errors marked retryable. The client handles bounded retries and
  honors both forms of `Retry-After`.
