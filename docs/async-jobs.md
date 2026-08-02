# Async jobs and provider adapters

ViperCapture can detach rendering from the client connection. The
synchronous `POST /v1/render` endpoint remains available; async clients use:

- `POST /v1/jobs` to submit the normal `RenderRequest` JSON.
- `GET /v1/jobs/{id}` to read queue, render, or terminal status.
- `GET /v1/jobs/{id}/result` to download a successful artifact.
- `DELETE /v1/jobs/{id}` to cancel work that is still queued.

Submission returns HTTP 202 with `Location` and `Retry-After` headers. Supplying
the same valid `X-Request-Id` returns the original job, making safe submission
retries idempotent. Reusing it with a different request returns HTTP 409.
Statuses are `queued`, `running`, `succeeded`, `failed`, `cancelled`, and
`expired`.

## Local defaults

The default providers require no external service:

- SQLite/WAL stores durable queue state in
  `~/.vipercapture/async-jobs.sqlite3`.
- Private files and metadata under `~/.vipercapture/job-results` store results.
- `~/.vipercapture/async-jobs.key` protects queued request payloads with
  AES-GCM and is created with owner-only permissions.

For a consistent local backup, stop ViperCapture and copy the complete
`~/.vipercapture` directory before restarting it. The snapshot must include the
database and any `-wal`/`-shm` sidecars, `async-jobs.key`, and the complete
`job-results` directory; copying only the database file while the service is
running can omit committed queue state and downloadable results. Provider
adapters need an equivalent coordinated snapshot procedure for live backups.
The job database receives only ciphertext for request bodies, so URLs and
custom target headers are not stored as plaintext. Terminal transitions erase
that request ciphertext. When webhook delivery is requested, its URL is stored
as separate AES-GCM ciphertext and the terminal transition atomically marks an
outbox event. Only that encrypted URL remains until successful delivery is
acknowledged; a keyed request fingerprint enforces idempotency without storing
the raw request.
The bundled providers create the database, WAL sidecars, key, and result files
with owner-only permissions. Key and result creation fsync both file contents
and directory entries before reporting durable success. SQLite enables secure
deletion and truncates the WAL after transitions that clear queued payloads so
coordinated backups do not retain decryptable historical request ciphertext.
Because Python file modes do not create private Windows ACLs, the bundled
SQLite and filesystem providers refuse to start on Windows; use external
providers plus an explicit `VIPERCAPTURE_JOB_SECRET` there. The packaged
Windows desktop sidecar and the regular Windows launcher disable async jobs by
default.

ViperCapture leaves claimed work `running` during shutdown. At startup it
requeues interrupted jobs unless they have already reached the configured
attempt limit; a success committed just before shutdown remains successful.
Retryable render failures are attempted up to three times by default. A queued
job has 15 minutes to be claimed. Local results expire after one hour and
terminal metadata after 24 hours. Expired and orphaned local artifacts are
removed during routine queue maintenance.

Successful job timings report queue time through the first claim and render
time from that first claim through final settlement. The render duration
therefore includes failed attempts, retry backoff, and result persistence.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `VIPERCAPTURE_ASYNC_JOBS` | `1` (`0` on Windows) | Set to `0` to disable async jobs |
| `VIPERCAPTURE_DATA_DIR` | `~/.vipercapture` | Local database, key, and result root |
| `VIPERCAPTURE_JOB_SECRET` | generated local key | Shared encryption secret for portable or external state |
| `VIPERCAPTURE_JOB_QUEUE_LIMIT` | `30` | Maximum queued plus running jobs |
| `VIPERCAPTURE_JOB_WORKERS` | render concurrency | Background workers |
| `VIPERCAPTURE_JOB_QUEUE_TTL_SECONDS` | `900` | Time allowed for a queued job to be claimed |
| `VIPERCAPTURE_JOB_RESULT_TTL_SECONDS` | `3600` | Successful result lifetime |
| `VIPERCAPTURE_JOB_METADATA_TTL_SECONDS` | `86400` | Terminal metadata lifetime |
| `VIPERCAPTURE_JOB_MAX_ATTEMPTS` | `3` | Maximum attempts for retryable render failures |
| `VIPERCAPTURE_ASYNC_RESULT_CONCURRENCY` | `2` | Maximum simultaneous async result streams |
| `VIPERCAPTURE_JOB_STORE_FACTORY` | bundled SQLite | `module:function` database adapter factory |
| `VIPERCAPTURE_ARTIFACT_STORE_FACTORY` | bundled filesystem | `module:function` artifact adapter factory |
| `VIPERCAPTURE_S3_BUCKET` | unset | Select the built-in S3-compatible artifact provider |
| `VIPERCAPTURE_S3_ENDPOINT_URL` | AWS default | R2, MinIO, B2, or compatible service endpoint |
| `VIPERCAPTURE_S3_REGION` | `us-east-1` | S3 signing region |
| `VIPERCAPTURE_S3_ADDRESSING_STYLE` | `auto` | `auto`, `virtual`, or `path` bucket addressing |
| `VIPERCAPTURE_S3_PREFIX` | `vipercapture` | Result object key prefix |

Workers share `VIPERCAPTURE_MAX_CONCURRENCY` with synchronous renders. Raising
the worker count does not bypass the Chromium semaphore.

## Database adapters

`async_jobs.JobStore` is the provider contract. A factory receives a
`JobStoreConfig` and returns an object implementing its async methods:

```python
from async_jobs import JobStoreConfig

def create_job_store(config: JobStoreConfig):
    return PostgreSQLJobStore(
        database_url=os.environ["DATABASE_URL"],
        metadata_ttl=config.metadata_ttl,
    )
```

Configure it with:

```bash
VIPERCAPTURE_JOB_STORE_FACTORY=my_vipercapture_pg:create_job_store
```

The adapter owns its client library, schema, connections, and migrations.
`create` must atomically enforce the active limit and request-ID idempotency.
It must compare `JobRecord.request_fingerprint` in constant time and raise
`IdempotencyConflictError` when an existing request ID has a different or
legacy-missing fingerprint.
`claim` must atomically move one eligible job from `queued` to `running`; use
the database's row-locking or compare-and-swap primitive. Retrying its
`claim_token` after an ambiguous acknowledgement must return the same row
without incrementing the attempt again. It receives `max_attempts` and must
terminally fail queued jobs that have already reached
that cap before selecting work. Terminal operations must erase the encrypted
payload. `succeed` and `fail` receive the claimed job's `expected_attempt` and
must never settle a different attempt. `succeed` must return the existing
successful record when retried with identical arguments, because a commit
acknowledgement can be lost. `fail` must likewise accept an identical retry of
an existing failed record; conflicting terminal transitions should raise
`JobConflictError`; the service then reads the winning terminal record and
discards only its unreferenced upload. The state provider assigns `completed_at`
when the terminal transition commits and derives successful `render_ms` from the preserved first
`started_at`, so state-transition retries are included without changing the
arguments used for an idempotent retry. During graceful shutdown, a
non-retryable failure gets one final bounded settlement attempt before its
worker exits, preventing a transient first write failure from becoming a retry
after restart.
`succeed` must reject a result whose `result_expires_at` has already passed at
commit time with `ArtifactExpiredError`, but it must check for and return an
identical previously committed success first. Other errors are treated as
ambiguous acknowledgements and retried even after result expiry.
`get` and `cancel` must normalize an overdue queued job to `expired`; `get`
must likewise normalize an overdue successful result so route order cannot
change the terminal state.
`requeue_running` supplies restart recovery without exceeding its
`max_attempts` argument. `maintain` returns expired artifact keys for deletion
and must preserve successful metadata and artifacts until `result_expires_at`,
even when the metadata TTL is shorter. `requeue` receives the claimed job's
`expected_attempt` and must use it as a compare-and-swap token. Retrying an
already committed requeue must be accepted, while a stale retry must never
change a newer running attempt or a terminal job.
The service reconciles `JobConflictError` from `fail` and `requeue` by reading
the stored attempt and stops retrying after it has moved or become terminal.
`expire_result` atomically moves a still-successful job with the supplied
artifact key to `expired` when the storage provider definitively reports that
result missing or invalid, so later status polls no longer advertise a dead
result URL.
`acknowledge_artifact_deletion` clears a returned artifact key only after the
storage provider confirms its idempotent deletion; until then, `maintain` must
continue returning the key.

The service throttles queue and artifact maintenance to one shared run per
process per polling interval, with a one-second minimum. Status polling
therefore does not trigger a full provider scan for every request.
The result endpoint holds a bounded download slot until its streaming response
finishes, limiting memory retained by concurrent local-file downloads.

The application encrypts the serialized request before calling `create` and
decrypts it only after `claim`. Adapters must treat `JobRecord.payload` as
opaque bytes. Set the same `VIPERCAPTURE_JOB_SECRET` whenever state can move
between machines or processes.

Durable job-store adapters also implement `pending_notifications` and
`acknowledge_notification`. Terminal success/failure and creation of the
encrypted webhook outbox record must be one atomic state transition. Delivery
is at least once, so consumers should deduplicate the stable webhook ID.

## Storage adapters

`async_jobs.ArtifactStore` is the binary provider contract. Its factory
receives `ArtifactStoreConfig`:

```python
from async_jobs import ArtifactStoreConfig

def create_artifact_store(config: ArtifactStoreConfig):
    return S3ArtifactStore(
        bucket=os.environ["S3_BUCKET"],
        result_ttl=config.result_ttl,
    )
```

Configure it independently:

```bash
VIPERCAPTURE_ARTIFACT_STORE_FACTORY=my_vipercapture_s3:create_artifact_store
```

`put` receives the job ID, validated artifact bytes, media type, and download
filename. It returns a `StoredArtifact` containing an opaque key and its
absolute expiry. The provider must start the configured result TTL after the
artifact is durably persisted, not before a potentially slow upload. `get`
returns an `Artifact`; `delete` must be idempotent. `maintain` lets local
or database-backed providers remove expired objects. Managed object stores
should also enforce provider-side lifecycle expiration so a process crash
between upload and state settlement cannot leave an object indefinitely.
Each successful `put` must return an exclusive logical key owned by that job.
Deduplicating providers may share physical bytes internally, but must expose a
unique handle or reference-counted key so deleting one job cannot race with or
remove another job's result.

Database and artifact providers are selected separately. For example, SQLite
state can be paired with S3-compatible storage, or PostgreSQL state with a
local filesystem.

## Security and deployment

The open-source server does not add user accounts or per-job authorization.
Keep it on loopback or protect every render and job endpoint with the same
reverse-proxy authentication. UUID job IDs are useful locators, not access
control.

Run one application process. The bundled SQLite adapter coordinates its
workers within that process; a custom distributed adapter still does not change
the repository's one-process-per-Chromium-tree deployment recommendation.
Custom providers should use TLS, least-privilege credentials, bounded retries,
and server-side expiry. Never put provider credentials in browser JavaScript or
the repository.
