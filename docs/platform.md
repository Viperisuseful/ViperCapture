# Platform and operator features

All platform features are opt-in and preserve the single-operator behavior of
older ViperCapture installations.

## Projects, keys, quotas, and profiles

Set separate random `VIPERCAPTURE_ADMIN_TOKEN` and
`VIPERCAPTURE_CONTROL_SECRET` values of at least 32 bytes. The first
authenticates administrators; the stable second value encrypts profiles and
keys API-token fingerprints so administrator-token rotation does not corrupt
stored state. Every `/v1`,
`/take`, and `/compat` request then requires either that administrator token or
a project key. Create a project and its first key:

```bash
curl -H "Authorization: Bearer $VIPERCAPTURE_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"ci","requests_per_minute":120,"concurrency":4}' \
  http://127.0.0.1:8000/v1/admin/projects

curl -H "Authorization: Bearer $VIPERCAPTURE_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"github-actions"}' \
  http://127.0.0.1:8000/v1/admin/projects/PROJECT_ID/keys
```

Raw keys are returned once and only server-keyed digests are stored. Keys can be
restricted to any combination of the `render`, `jobs`, `schedules`, `profiles`,
and `baselines` scopes. Jobs, schedules,
profiles, and visual baselines are project-owned. Profile storage state is
AES-GCM encrypted at rest; profile IDs are unguessable and can be supplied as
`profile_id` on a render request.

RPM events and active-render leases are transactionally recorded in the
control database. Processes using that same database therefore share project
limits; abandoned concurrency leases expire after 15 minutes.

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
The built-in SQLite control plane is intentionally restricted to `role=all`;
split deployments must enforce shared authentication and quotas at their
gateway. Schedules in split mode require a shared
`VIPERCAPTURE_SCHEDULE_STORE_FACTORY`; disable them explicitly otherwise.
Running-job recovery is performed only by the exclusive `all` role so starting
a distributed replica cannot steal a live worker's claim.

`GET /metrics` exports Prometheus text, `GET /ready` reports role and browser
readiness, and `/v1/admin/status` exposes authenticated operator state. Set
`OTEL_EXPORTER_OTLP_ENDPOINT` to enable batched OpenTelemetry FastAPI traces.

## Clients and integrations

The dependency-free Python CLI is under `skills/vipercapture/scripts`, typed
TypeScript and Go clients are under `sdk/`, and the repository includes a
composite GitHub Action, an importable n8n workflow, and a minimal Terraform
Docker module. Compatibility adapters intentionally reject unknown vendor
options instead of silently changing render behavior.
