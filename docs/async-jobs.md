# Async jobs

Use an async job when the caller cannot keep an HTTP connection open while a render runs. Use `POST /v1/render` for synchronous captures.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/jobs` | Submit a `RenderRequest` JSON payload |
| `GET /v1/jobs/{id}` | Read queue, running, or terminal status |
| `GET /v1/jobs/{id}/result` | Download the artifact for a succeeded job |
| `DELETE /v1/jobs/{id}` | Cancel a job that is still `queued` |

`POST /v1/jobs` returns HTTP 202 with `Location` and `Retry-After` headers. `X-Request-Id` is only for correlation. Sending the same valid `Idempotency-Key` and normalized request body returns the original job; changing the body returns HTTP 409. Omitting `Idempotency-Key` creates a new job on every submission.

Job statuses are `queued`, `running`, `succeeded`, `failed`, `cancelled`, and `expired`.

## Use the local providers

The bundled providers need no external database or object store:

- **Queue state.** SQLite in WAL mode at `~/.vipercapture/async-jobs.sqlite3`.
- **Result artifacts.** Private files and metadata under `~/.vipercapture/job-results`.
- **Encryption key.** `~/.vipercapture/async-jobs.key`, created with owner-only permissions for AES-GCM encryption of queued request payloads.

### Encryption, permissions, and Windows behavior

The job database stores only ciphertext for request bodies, so URLs and custom headers never sit in plaintext on disk. Terminal transitions erase that ciphertext. A keyed request fingerprint enforces idempotency without keeping the raw request.

When a job requests a webhook, ViperCapture encrypts the callback URL separately with AES-GCM and writes an outbox event atomically during the terminal transition. A dedicated notification worker drains that outbox so slow callbacks cannot block render workers, API requests, or startup maintenance. Only that encrypted URL stays stored until delivery succeeds, and pending callbacks are exempt from metadata expiry. Failed callback rows back off exponentially so one unreachable destination cannot starve newer jobs.

The bundled providers create the SQLite database, WAL sidecars, key file, and artifact files with owner-only permissions, and `fsync` both file data and parent directories before reporting durable writes. SQLite enables secure deletion and truncates the WAL after clearing queued payloads so coordinated backups do not hold decryptable historical request bodies.

Because Python file modes do not create private Windows ACLs, the bundled SQLite and filesystem providers refuse to start on Windows. The launcher disables async jobs by default on Windows. To run async jobs on Windows, configure external job and artifact providers with an explicit `VIPERCAPTURE_JOB_SECRET`, or run the Docker image.

### Backups, recovery, and retention

To take a consistent local backup, stop ViperCapture and copy the entire `~/.vipercapture` directory before restarting it. Include the database, any `-wal` and `-shm` files, `async-jobs.key`, and `job-results`. Copying only the database file while the server runs can miss committed queue rows and downloadable files. External providers need an equivalent coordinated snapshot procedure.

On first startup after upgrading, the bundled SQLite provider adds nullable `correlation_id` and `idempotency_key` columns and copies legacy `request_id` values into both. Existing jobs stay readable, and clients can migrate by sending their old `X-Request-Id` value in `Idempotency-Key`. New rows keep the legacy unique column as an internal compatibility slot, and the SQLite adapter recognizes migrated legacy rows for per-item bulk retries without reopening that namespace to new jobs. Custom database adapters must add equivalent columns and implement `get_by_idempotency_key` because ViperCapture does not migrate external schemas.

During shutdown, ViperCapture leaves claimed work in `running`. On restart, it requeues interrupted jobs that have not reached the attempt limit while preserving any success committed right before shutdown. Default lifecycle limits are:

- **Attempts.** Up to 3 attempts for retryable render failures.
- **Queue TTL.** 15 minutes for a queued job to be claimed.
- **Result TTL.** 1 hour for local artifacts (PNG, JPEG, WebP, AVIF, PDF, HTML, Markdown, JSON metadata, ZIP bundles, and video).
- **Metadata TTL.** 24 hours for terminal job metadata.

Routine maintenance removes expired and orphaned local artifacts. For succeeded jobs, queue timing measures creation through first claim, and render timing measures first claim through final settlement, including failed attempts, retry backoff, and result persistence.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `VIPERCAPTURE_ASYNC_JOBS` | `1` (`0` on Windows) | Set to `0` to disable async jobs |
| `VIPERCAPTURE_DATA_DIR` | `~/.vipercapture` | Root directory for local database, key, and results |
| `VIPERCAPTURE_JOB_SECRET` | generated local key | Shared encryption secret for portable or external state |
| `VIPERCAPTURE_JOB_QUEUE_LIMIT` | `30` | Maximum combined `queued` and `running` jobs |
| `VIPERCAPTURE_JOB_WORKERS` | render concurrency | Background job workers |
| `VIPERCAPTURE_JOB_QUEUE_TTL_SECONDS` | `900` | Time allowed for a queued job to be claimed |
| `VIPERCAPTURE_JOB_RESULT_TTL_SECONDS` | `3600` | Successful result lifetime |
| `VIPERCAPTURE_JOB_METADATA_TTL_SECONDS` | `86400` | Terminal metadata lifetime |
| `VIPERCAPTURE_JOB_MAX_ATTEMPTS` | `3` | Maximum attempts for retryable render failures |
| `VIPERCAPTURE_ASYNC_RESULT_CONCURRENCY` | `2` | Maximum simultaneous async result streams |
| `VIPERCAPTURE_JOB_STORE_FACTORY` | bundled SQLite | `module:function` database adapter factory |
| `VIPERCAPTURE_ARTIFACT_STORE_FACTORY` | bundled filesystem | `module:function` artifact adapter factory |
| `VIPERCAPTURE_S3_BUCKET` | unset | Enable the built-in S3-compatible artifact provider |
| `VIPERCAPTURE_S3_ENDPOINT_URL` | AWS default | Endpoint URL for Cloudflare R2, MinIO, Backblaze B2, or compatible stores |
| `VIPERCAPTURE_S3_REGION` | `us-east-1` | S3 signing region |
| `VIPERCAPTURE_S3_ADDRESSING_STYLE` | `auto` | `auto`, `virtual`, or `path` bucket addressing |
| `VIPERCAPTURE_S3_PREFIX` | `vipercapture` | Result object key prefix |

Workers share `VIPERCAPTURE_MAX_CONCURRENCY` with synchronous renders, so raising `VIPERCAPTURE_JOB_WORKERS` does not bypass the browser semaphore. Chromium can run as a small pool (`VIPERCAPTURE_BROWSER_POOL_SIZE`), and browser recycling replaces one process at a time without draining the queue.

## Implement a database adapter

`async_jobs.JobStore` defines the database provider contract. Export a factory that takes `JobStoreConfig` and returns an adapter implementing the async methods:

```python
from async_jobs import JobStoreConfig

def create_job_store(config: JobStoreConfig):
    return PostgreSQLJobStore(
        database_url=os.environ["DATABASE_URL"],
        metadata_ttl=config.metadata_ttl,
    )
```

Load the factory with:

```bash
VIPERCAPTURE_JOB_STORE_FACTORY=my_vipercapture_pg:create_job_store
```

Your adapter owns its client driver, schema, connection pool, and migrations. ViperCapture encrypts the serialized request before calling `create` and decrypts it only after `claim`, so adapters must treat `JobRecord.payload` as opaque bytes. Set the same `VIPERCAPTURE_JOB_SECRET` across every process or host that shares queue state.

### Required `JobStore` invariants

- **`create` and `get_by_idempotency_key`.** `create` must enforce the active queue limit and idempotency-key uniqueness atomically. `get_by_idempotency_key` handles replay lookup, compares `JobRecord.request_fingerprint` in constant time, and raises `IdempotencyConflictError` when an existing key has a different or legacy-missing fingerprint. Adapters migrating old per-item `request_id` records may also implement `get_legacy_by_idempotency_key`.
- **`claim_bulk_idempotency(key, fingerprint, now)`.** Required for request-level bulk idempotency. Persist the normalized envelope fingerprint atomically, accept an exact replay, raise `IdempotencyConflictError` on mismatch, and retain claims for at least `JobStoreConfig.metadata_ttl`. If an adapter omits this method, ViperCapture returns HTTP 503 for request-level bulk `Idempotency-Key` headers while keeping per-item keys available.
- **`claim`.** Move one eligible job from `queued` to `running` atomically using row locking or compare-and-swap. Retrying the same `claim_token` after an ambiguous acknowledgement must return the same row without incrementing the attempt count again. `claim` receives `max_attempts` and must fail any queued job that has already reached that cap before picking work.
- **`succeed` and `fail`.** Erase the encrypted payload on any terminal transition. Both methods receive `expected_attempt` and must never settle a different attempt. `succeed` must return an identical previously committed success first when retried, and reject a result whose `result_expires_at` has already passed at commit time with `ArtifactExpiredError`. Other commit errors are treated as ambiguous acknowledgements and retried even after result expiry. `fail` must accept an identical retry of an existing failed record. Conflicting terminal transitions must raise `JobConflictError`; ViperCapture then reads the winning terminal record and deletes only its unreferenced upload. Assign `completed_at` at commit time and derive `render_ms` from the preserved first `started_at` so state-transition retries stay idempotent. During graceful shutdown, a non-retryable failure gets one final bounded settlement attempt before the worker exits.
- **`get` and `cancel`.** Normalize overdue queued jobs to `expired` in both methods, and normalize overdue successful results in `get` so route call order cannot alter terminal state.
- **`requeue`, `defer`, `requeue_running`, and `recover_stale`.** `requeue` uses `expected_attempt` as a compare-and-swap token, accepts an identical retry of a committed requeue, and rejects stale retries against newer attempts or terminal jobs. ViperCapture resolves `JobConflictError` from `fail` and `requeue` by reading the stored record and stopping retries once the attempt has moved or settled. `defer` moves the claimed attempt from `running` back to `queued` atomically, clears its claim, and decrements the attempt counter because capacity deferral does not consume an attempt. `requeue_running` handles single-process restart recovery without exceeding `max_attempts`. Adapters used by split worker processes must also implement `recover_stale(now, max_attempts)` with durable claim leases or heartbeats so replicas recover only expired claims and never steal live work.
- **`maintain`, `expire_result`, and `acknowledge_artifact_deletion`.** `maintain` returns expired artifact keys for deletion and preserves succeeded metadata and artifacts until `result_expires_at`, even when `metadata_ttl` is shorter. `expire_result` moves a succeeded job with a given artifact key to `expired` atomically when storage confirms the artifact is missing or invalid, so later polls stop advertising a dead result URL. `acknowledge_artifact_deletion` clears an artifact key only after storage confirms idempotent deletion; until then, `maintain` must keep returning that key.
- **`pending_notifications` and `acknowledge_notification`.** Terminal settlement and encrypted webhook outbox insertion must commit in one atomic transaction. Delivery is at least once, so webhook receivers should deduplicate by the stable event ID.

ViperCapture throttles maintenance to one shared run per process per polling interval (at least 1 second), so status polling does not scan the provider on every request. The result endpoint holds one bounded download slot until streaming finishes to cap memory held by concurrent downloads.

## Implement a storage adapter

`async_jobs.ArtifactStore` defines the binary storage contract. Export a factory that accepts `ArtifactStoreConfig`:

```python
from async_jobs import ArtifactStoreConfig

def create_artifact_store(config: ArtifactStoreConfig):
    return S3ArtifactStore(
        bucket=os.environ["S3_BUCKET"],
        result_ttl=config.result_ttl,
    )
```

Load it independently from the job store:

```bash
VIPERCAPTURE_ARTIFACT_STORE_FACTORY=my_vipercapture_s3:create_artifact_store
```

- **`put`.** Receives the job ID, validated bytes, media type, and download filename, and returns a `StoredArtifact` with an opaque key and absolute expiry. Start the result TTL after the bytes are durably stored, not before a slow upload. Every `put` must return an exclusive logical key owned by that job; deduplicating stores may share physical bytes internally but must expose a unique or reference-counted handle so deleting one job cannot affect another.
- **`get` and `delete`.** `get` returns an `Artifact`, and `delete` must be idempotent.
- **`maintain`.** Removes expired objects for local or database-backed stores. Managed object stores should also configure provider-side lifecycle expiry so a crash between upload and job settlement cannot leak objects permanently.

You can mix database and storage providers freely, such as pairing SQLite queue state with S3-compatible storage or PostgreSQL queue state with local files.

## Secure an async deployment

The open-source server includes no user accounts or per-job authorization unless you enable the project control plane. Keep it on loopback or put every render and job route behind the same reverse-proxy authentication, because job UUIDs are locators, not access control.

The bundled SQLite adapter supports only the default single-process `all` role. Split `api` and `worker` roles require an external job store with lease-based stale-claim recovery; each worker still owns its own Chromium pool. Custom adapters should enforce TLS, least-privilege credentials, bounded retries, and server-side expiry. Keep provider credentials out of browser JavaScript and out of the repository.

## Related

- [API and workflows](api.md)
- [Self-hosting and local setup](self-hosting.md)
- [Projects, API keys, and observability](platform.md)
- [Public API deployment example](../deploy/public-api/README.md)
- [All docs](README.md)
