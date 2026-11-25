# Platform configuration

This guide covers the optional project control plane, deterministic and certified artifacts, split API and worker roles, observability, and operator CAPTCHA handlers. Single-operator installs can leave the control plane and distributed roles disabled.

## Configure projects, keys, quotas, and profiles

Set two separate random secrets of at least 32 bytes each:

- **`VIPERCAPTURE_ADMIN_TOKEN`.** Authenticates administrator calls.
- **`VIPERCAPTURE_CONTROL_SECRET`.** Encrypts stored browser profiles and keys API-token digests so rotating the admin token does not invalidate stored state.

Once both are set, every `/v1`, `/take`, and `/compat` request requires either the admin token or a project API key. Create a project and its first key:

```bash
curl -H "Authorization: Bearer $VIPERCAPTURE_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"ci","requests_per_minute":120,"concurrency":4}' \
  http://127.0.0.1:8000/v1/admin/projects

curl -H "Authorization: Bearer $VIPERCAPTURE_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"github-actions"}' \
  http://127.0.0.1:8000/v1/admin/projects/PROJECT_ID/keys
```

ViperCapture returns raw API keys once and stores only server-keyed digests. You can scope each key to any subset of `render`, `jobs`, `schedules`, `profiles`, and `baselines`. Jobs, schedules, profiles, and baselines belong to the project that created them. ViperCapture encrypts profile storage state with AES-GCM at rest and issues unguessable IDs that callers pass as `profile_id` on render requests.

Use `POST /v1/profiles/import` to normalize Playwright storage state, Chrome, Edge, Brave, or Firefox cookie JSON exports, Netscape `cookies.txt` files, or a pasted `Cookie` header. ViperCapture does not read live browser profile databases on disk because that depends on OS credential decryption and would grant the server broader access than an explicit export.

Native routes authenticate with `Authorization: Bearer vcp_...` and return `WWW-Authenticate` on auth failure. Only the ScreenshotOne-compatible `/take` route accepts query-string keys, and you can turn that off with `VIPERCAPTURE_ALLOW_QUERY_AUTH=0`.

The control database records RPM events and active-render leases transactionally, so processes sharing that database share project quotas. Abandoned concurrency leases expire after 15 minutes. Each project can store up to 100 schedules and 512 MiB of encrypted schedule payloads. ViperCapture reserves quota before writing a schedule row, resizes the reservation on updates, and releases it on deletion.

## Configure artifact options

- **Deterministic captures.** `deterministic.enabled` fixes `Date`, `Math.random`, Web Crypto random values and UUIDs, and the browser performance clock. It also waits for fonts and combines with animation stabilization.
- **Full-page slices.** `slices` returns a ZIP of bounded-height sections covering a full page.
- **Diagnostic bundles.** Bundles can include a redacted HAR 1.2 log, a redacted Playwright trace, and WARC files. The HAR log records HTTP versions from Chromium CDP or Resource Timing, allowlisted headers, MIME types, and timings, while omitting query strings, matrix and path parameters, cookies, credentials, and bodies. Firefox and WebKit HTTPS entries stay `unknown` without Resource Timing.
- **Signed certification.** `certification.enabled` adds an Ed25519-signed manifest when `VIPERCAPTURE_CERTIFICATION_SECRET` is at least 32 bytes. Run `vipercapture verify <bundle>` to verify the Ed25519 signature, SHA-256 digests, and canonical manifest of a `--certify` ZIP bundle using `--public-key` or `/v1/certification/public-key`. Certification proves bundle integrity and does not make a legal-admissibility claim on its own.

## Configure distributed roles and observability

| `VIPERCAPTURE_ROLE` | Behavior |
| --- | --- |
| `all` (default) | Runs the API, local or external stores, and render workers in one process |
| `api` | Serves API routes and starts queue providers without running render consumers |
| `worker` | Consumes queued jobs and rejects public render traffic |

Split `api` and `worker` deployments require a shared `VIPERCAPTURE_JOB_STORE_FACTORY`, shared artifact storage (S3-compatible bucket or `VIPERCAPTURE_ARTIFACT_STORE_FACTORY`), and `VIPERCAPTURE_JOB_SECRET`. ViperCapture refuses to start split roles against local SQLite or local files.

The bundled SQLite control plane supports only `role=all`, so split deployments must enforce shared authentication and quotas at their gateway. Running schedules in split mode requires a shared `VIPERCAPTURE_SCHEDULE_STORE_FACTORY`; otherwise disable schedules explicitly. A shared schedule store's paginated `list` method must accept `project_id` and filter inside indexed storage instead of scanning other tenants' rows. While `role=all` recovers its own interrupted claims on restart, split workers require an external job store with lease-based `recover_stale()` so replicas recover only expired claims and never steal live work.

`GET /metrics` exports Prometheus metrics. When the control plane is on, `/metrics` requires the administrator Bearer token unless you set `VIPERCAPTURE_METRICS_PUBLIC=1`. The public deployment example blocks `/metrics` at the gateway while letting Prometheus scrape the internal listener. `GET /health` and `GET /ready` stay public, and `/v1/admin/status` returns authenticated operator state. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to export batched OpenTelemetry traces for FastAPI.

## Use integrations

This repository ships an importable n8n workflow and a small Terraform Docker module. Compatibility adapters reject unknown vendor options instead of silently changing render behavior.

## Configure proxies, stealth, and CAPTCHA callbacks

Per-request proxies under `network.proxy` (HTTP, HTTPS, SOCKS4, or SOCKS5) are on by default in self-host mode and off by default in hosted mode. Set `VIPERCAPTURE_ALLOW_CUSTOM_PROXIES=0` to disable them or `1` to opt in. Enabling custom proxies does not weaken URL validation or replace a network egress firewall.

ViperCapture applies request-aware stealth scripts to each isolated context by default on standard Playwright to align language, platform, and user-agent signals with the request. These are balanced defaults, not a guarantee against bot detection. Callers can pass `stealth: false` for debugging or compatibility checks. For Patchright and harder Turnstile handling, use [ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth) (`0.1.0-beta`), which is not a universal Cloudflare bypass; see its README for setup.

ViperCapture detects CAPTCHA and bot challenges without solving or bypassing them. To connect an operator-approved internal or third-party handler, set `VIPERCAPTURE_CAPTCHA_HANDLER_FACTORY=package.module:create_handler`. ViperCapture calls the factory once at startup and expects an async callable with this signature:

```python
async def handler(page, challenge, solver_name, timeout_ms) -> bool:
    # Dispatch to the operator's integration. Return True only after the page
    # is ready for ViperCapture to re-check it.
    return False

def create_handler():
    return handler
```

Callers opt in per request with `"captcha":{"action":"external","solver":"operator-alias","timeout_ms":120000}`. `solver` is a non-secret routing label, never a provider credential. Keep handler credentials inside the operator integration and out of render requests, cookies, headers, and profiles. ViperCapture invokes only the configured callable, enforces the timeout, re-runs challenge detection, and fails the render if the challenge persists. No provider integration, credentials, solver, or challenge-bypass code ships with this repository.

Callers can instead finish an authorized access flow independently and submit a fresh render with short-lived, target-scoped session state as described in [site access](site-access.md). That flow does not invoke the operator handler. Neither path relaxes same-origin header routing, hosted target-domain checks for `network.cookies`, public-address and redirect validation in hosted mode, or egress policy. Because imported profiles are not target-filtered and self-host mode permits internal targets, use dedicated target-scoped profiles and block private and metadata networks at the egress layer.

## Related

- [API and workflows](api.md)
- [Async jobs and queue providers](async-jobs.md)
- [Self-hosting and local setup](self-hosting.md)
- [Authorizing ViperCapture through Cloudflare or a WAF](site-access.md)
- [Public API deployment example](../deploy/public-api/README.md)
- [All docs](README.md)
