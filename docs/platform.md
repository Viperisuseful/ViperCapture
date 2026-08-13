# Platform configuration

This page covers project controls, artifact options, distributed roles, and
observability. Existing single-operator installations can continue without
enabling the project control plane or distributed roles.

## Configure projects, keys, quotas, and profiles

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

Native routes use `Authorization: Bearer vcp_...`; Bearer authentication
failures include `WWW-Authenticate`. Query-string credentials are accepted
only by the ScreenshotOne-compatible `/take` route and can be disabled with
`VIPERCAPTURE_ALLOW_QUERY_AUTH=0`.

RPM events and active-render leases are transactionally recorded in the
control database. Processes using that same database therefore share project
limits; abandoned concurrency leases expire after 15 minutes.
Each project may retain at most 100 schedules and 512 MiB of encrypted schedule
payloads. Creation reserves quota before the durable schedule row is written;
updates resize that reservation and deletion releases it.

## Configure artifact options

`deterministic.enabled` fixes `Date`, `Math.random`, Web Crypto random values
and UUIDs, and the browser performance clock; it also waits for fonts and
combines with the existing animation stabilization. `slices` emits a ZIP
of bounded-height full-page sections. A diagnostic bundle can add redacted HAR,
Playwright trace, and WARC files. `certification.enabled` produces an
Ed25519-signed manifest when `VIPERCAPTURE_CERTIFICATION_SECRET` is set to at
least 32 bytes. Certification proves bundle integrity; it does not by itself
make a legal-admissibility claim.

## Configure distributed roles and observability

`VIPERCAPTURE_ROLE=api` starts queue providers without consumers;
`VIPERCAPTURE_ROLE=worker` consumes jobs and rejects public render traffic;
`all` remains the default. Split roles require a shared job-store factory,
shared artifact storage (S3/R2 or a factory), and `VIPERCAPTURE_JOB_SECRET`.
This makes accidental split deployment with local SQLite/files impossible.
The built-in SQLite control plane is restricted to `role=all`;
split deployments must enforce shared authentication and quotas at their
gateway. Schedules in split mode require a shared
`VIPERCAPTURE_SCHEDULE_STORE_FACTORY`; disable them explicitly otherwise.
Its paginated `list` operation must accept a `project_id` filter and apply it
inside indexed storage rather than scanning other tenants' rows.
The combined `all` role recovers its own interrupted claims on restart. Split
workers require an external job store with lease-based `recover_stale()` so a
replica can recover only expired claims and cannot steal live work.

`GET /metrics` exports Prometheus text. When the control plane is enabled it
requires the administrator Bearer token unless
`VIPERCAPTURE_METRICS_PUBLIC=1` is explicitly set. The supplied public gateway
continues to block `/metrics` while Prometheus scrapes the internal listener.
`GET /health` and `GET /ready` remain public; `/v1/admin/status` exposes
authenticated operator state. Set
`OTEL_EXPORTER_OTLP_ENDPOINT` to enable batched OpenTelemetry FastAPI traces.

## Use clients and integrations

The dependency-free Python client is under `skills/vipercapture/scripts`, typed
TypeScript and Go clients are under `sdk/`, and the repository includes a
composite GitHub Action, an importable n8n workflow, and a minimal Terraform
Docker module. Compatibility adapters reject unknown vendor
options instead of silently changing render behavior.
