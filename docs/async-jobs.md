# Async jobs and provider adapters

ViperCapture can detach image rendering from the client connection. The
synchronous `POST /v1/render` endpoint remains available; async clients use:

- `POST /v1/jobs` to submit the normal `RenderRequest` JSON.
- `GET /v1/jobs/{id}` to read queue, render, or terminal status.
- `GET /v1/jobs/{id}/result` to download a successful image.
- `DELETE /v1/jobs/{id}` to cancel work that is still queued.

Submission returns HTTP 202 with `Location` and `Retry-After` headers. Supplying
the same valid `X-Request-Id` returns the original job, making safe submission
retries idempotent. Statuses are `queued`, `running`, `succeeded`, `failed`,
`cancelled`, and `expired`.

## Local defaults

The default providers require no external service:

- SQLite/WAL stores durable queue state in
  `~/.vipercapture/async-jobs.sqlite3`.
- Private files and metadata under `~/.vipercapture/job-results` store results.
- `~/.vipercapture/async-jobs.key` protects queued request payloads with
  AES-GCM and is created with owner-only permissions.

Preserve both the database and encryption key when backing up or moving an
instance. The job database receives only ciphertext for request bodies, so URLs
and custom target headers are not stored as plaintext. Terminal transitions
erase that ciphertext.

ViperCapture requeues interrupted `running` jobs at startup. Retryable render
failures are attempted up to three times by default. A queued job has 15
minutes to be claimed. Local results expire after one hour and terminal
metadata after 24 hours. Expired and orphaned local artifacts are removed
during routine queue maintenance.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `VIPERCAPTURE_ASYNC_JOBS` | `1` | Set to `0` to disable async jobs |
| `VIPERCAPTURE_DATA_DIR` | `~/.vipercapture` | Local database, key, and result root |
| `VIPERCAPTURE_JOB_SECRET` | generated local key | Shared encryption secret for portable or external state |
| `VIPERCAPTURE_JOB_QUEUE_LIMIT` | `30` | Maximum queued plus running jobs |
| `VIPERCAPTURE_JOB_WORKERS` | render concurrency | Background workers |
| `VIPERCAPTURE_JOB_QUEUE_TTL_SECONDS` | `900` | Time allowed for a queued job to be claimed |
| `VIPERCAPTURE_JOB_RESULT_TTL_SECONDS` | `3600` | Successful result lifetime |
| `VIPERCAPTURE_JOB_METADATA_TTL_SECONDS` | `86400` | Terminal metadata lifetime |
| `VIPERCAPTURE_JOB_MAX_ATTEMPTS` | `3` | Maximum attempts for retryable render failures |
| `VIPERCAPTURE_JOB_STORE_FACTORY` | bundled SQLite | `module:function` database adapter factory |
| `VIPERCAPTURE_ARTIFACT_STORE_FACTORY` | bundled filesystem | `module:function` artifact adapter factory |

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
`claim` must atomically move one eligible job from `queued` to `running`; use
the database's row-locking or compare-and-swap primitive. Terminal operations
must erase the encrypted payload. `requeue_running` supplies restart recovery,
and `maintain` returns expired artifact keys for deletion.

The application encrypts the serialized request before calling `create` and
decrypts it only after `claim`. Adapters must treat `JobRecord.payload` as
opaque bytes. Set the same `VIPERCAPTURE_JOB_SECRET` whenever state can move
between machines or processes.

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

`put` receives the job ID, validated image bytes, media type, download
filename, and absolute expiry. It returns an opaque key saved by the job store.
`get` returns an `Artifact`; `delete` must be idempotent. `maintain` lets local
or database-backed providers remove expired objects. Managed object stores
should also enforce provider-side lifecycle expiration so a process crash
between upload and state settlement cannot leave an object indefinitely.

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
