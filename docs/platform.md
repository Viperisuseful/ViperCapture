# Platform and operator features

All platform features are opt-in and preserve the single-operator behavior of
older ViperCapture installations.

## Projects, keys, quotas, and profiles

Set a random `VIPERCAPTURE_ADMIN_TOKEN` of at least 32 bytes. Every `/v1`,
`/take`, and `/compat` request then requires either that administrator token or
a project key. Create a project and its first key:

```bash
curl -H "Authorization: Bearer $VIPERCAPTURE_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"ci","requests_per_minute":120,"concurrency":4}' \
  http://127.0.0.1:8765/v1/admin/projects

curl -H "Authorization: Bearer $VIPERCAPTURE_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"github-actions"}' \
  http://127.0.0.1:8765/v1/admin/projects/PROJECT_ID/keys
```

Raw keys are returned once and only SHA-256 hashes are stored. Keys can be
restricted to any combination of the `render`, `jobs`, `schedules`, `profiles`,
and `baselines` scopes. Jobs, schedules,
profiles, and visual baselines are project-owned. Profile storage state is
AES-GCM encrypted at rest; profile IDs are unguessable and can be supplied as
`profile_id` on a render request.

## Deterministic, diagnostic, and certified artifacts

`deterministic.enabled` fixes JavaScript time and randomness, waits for fonts,
and combines with the existing animation stabilization. `slices` emits a ZIP
of bounded-height full-page sections. A diagnostic bundle can add redacted HAR,
Playwright trace, MHTML, and WARC files. `certification.enabled` produces an
Ed25519-signed manifest when `VIPERCAPTURE_CERTIFICATION_SECRET` is set to at
least 32 bytes. Certification proves bundle integrity; it does not by itself
make a legal-admissibility claim.

## Distributed roles and observability

`VIPERCAPTURE_ROLE=api` starts queue providers without consumers;
`VIPERCAPTURE_ROLE=worker` consumes jobs and rejects public render traffic;
`all` remains the default. Split roles require a shared job-store factory,
shared artifact storage (S3/R2 or a factory), and `VIPERCAPTURE_JOB_SECRET`.
This makes accidental split deployment with local SQLite/files impossible.

`GET /metrics` exports Prometheus text, `GET /ready` reports role and browser
readiness, and `/v1/admin/status` exposes authenticated operator state. Set
`OTEL_EXPORTER_OTLP_ENDPOINT` to enable batched OpenTelemetry FastAPI traces.

## Clients and integrations

The dependency-free Python CLI is under `skills/vipercapture/scripts`, typed
TypeScript and Go clients are under `sdk/`, and the repository includes a
composite GitHub Action, an importable n8n workflow, and a minimal Terraform
Docker module. Compatibility adapters intentionally reject unknown vendor
options instead of silently changing render behavior.
