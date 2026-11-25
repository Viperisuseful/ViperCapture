# Public API deployment

This stack defines the supported topology for exposing ViperCapture on the public internet. It pins the released GHCR image, blocks administrative and metrics routes at the gateway, enforces global connection and request limits, authenticates callers with project API keys and quotas, stores results in S3-compatible storage, exports Prometheus metrics, and assigns the renderer a fixed Docker subnet for host egress filtering.

You also need to configure TLS termination, host patching, alert delivery, object-store durability, and an incident-response owner.

## Deploy the stack

1. Copy `.env.example` to `.env`, pin `VIPERCAPTURE_VERSION`, and replace every placeholder. Generate each secret independently with `openssl rand -hex 32`.
2. Install the private-address egress policy on the Linux Docker host:

   ```bash
   sudo ./egress-firewall.sh
   ```

   This script inserts one isolated chain into `DOCKER-USER` for only the renderer's fixed `172.30.0.10/32` address while allowing established replies to the gateway and Prometheus. It leaves other firewall rules untouched. Re-run it after any Docker or firewall restart and verify it with `sudo iptables -S VIPERCAPTURE_EGRESS`.

3. Pull, start, and probe the pinned stack:

   ```bash
   docker compose --env-file .env pull
   docker compose --env-file .env up -d
   curl --fail http://127.0.0.1:8080/ready
   ```

4. Terminate HTTPS in a host reverse proxy, load balancer, or tunnel that forwards traffic only to `127.0.0.1:8080`. The front proxy must overwrite `X-Forwarded-For` with the connecting client address rather than appending to it. Nginx trusts only the pinned Docker host gateway at `172.31.0.1` for that header. Never expose the container port directly.
5. Create one project and API key per customer or internal workload over an authenticated maintenance connection. Never share the admin token with callers.

ViperCapture enforces per-project RPM and concurrency limits. Nginx adds a coarse per-IP ceiling against unauthenticated floods. Put upstream DDoS filtering in front of the host, because neither layer replaces network-level abuse protection.

## Back up and restore the deployment

Turn on bucket versioning and retention in S3 or R2. The Docker volume holds control-plane state, encrypted queued inputs, schedules, and cache metadata, not the secrets required to decrypt that state or read S3 and R2 objects. Keep a separately encrypted recovery copy of the complete `.env` file, including `VIPERCAPTURE_CONTROL_SECRET`, `VIPERCAPTURE_JOB_SECRET`, `VIPERCAPTURE_SIGNING_SECRET`, admin and webhook tokens, and every `VIPERCAPTURE_S3_*` and `AWS_*` variable.

For example, encrypt `.env` to an offline recovery key with [age](https://age-encryption.org/) and never store plaintext beside the volume archive:

```bash
umask 077
mkdir -p backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
age --encrypt --recipient "$VIPERCAPTURE_BACKUP_AGE_RECIPIENT" \
  --output "backups/vipercapture-env-${stamp}.age" .env
```

Keep the encrypted `.env` file and the age identity in separate access-controlled locations, and verify that the object-store credentials still read the retained bucket. Then stop the container and archive the volume so its encrypted state is consistent:

```bash
mkdir -p backups
docker compose --env-file .env stop vipercapture
docker run --rm \
  -v vipercapture-public_vipercapture-data:/data:ro \
  -v "$PWD/backups:/backup" alpine:3.22.1 \
  tar -C /data -czf /backup/vipercapture-data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz .
docker compose --env-file .env start vipercapture
curl --fail http://127.0.0.1:8080/ready
```

At least monthly, restore both backups on a non-production host (replace `TIMESTAMP` and the identity path):

```bash
umask 077
age --decrypt --identity /secure/offline-recovery-key.txt \
  --output .env backups/vipercapture-env-TIMESTAMP.age
chmod 600 .env
export COMPOSE_PROJECT_NAME=vipercapture-restore
docker volume create "${COMPOSE_PROJECT_NAME}_vipercapture-data"
docker run --rm \
  -v "${COMPOSE_PROJECT_NAME}_vipercapture-data:/data" \
  -v "$PWD/backups:/backup:ro" alpine:3.22.1 \
  tar -C /data -xzf /backup/vipercapture-data-TIMESTAMP.tar.gz
docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file .env up -d
```

Send an authenticated admin status request and verify a retained job, profile, baseline, and S3-backed artifact. Delete the recovered plaintext `.env` once the drill finishes. A backup does not count as release evidence until it passes a full state, secret, and object-store restore drill.

## Upgrade or roll back

1. Record the running image digest and export the current Compose config.
2. Complete a backup and restore drill.
3. Update only `VIPERCAPTURE_VERSION`, run `docker compose pull`, and run `docker compose up -d`.
4. Probe `/ready`, render a controlled page, submit, poll, and download an async job, check Prometheus alerts, and watch error and queue rates for one hour.
5. Roll back by restoring the previous version and running `docker compose up -d`. Restore the data snapshot only when release notes document an incompatible migration, because restoring data drops work accepted after the snapshot.

## Abuse controls and support

- **Scoped keys.** Require project API keys and revoke leaked keys at once.
- **Header authentication.** This stack sets `VIPERCAPTURE_ALLOW_QUERY_AUTH: "0"` so `/take?access_key=...` fails and API keys travel only in the `Authorization` header. If a legacy client requires query-string keys, set the flag to `"1"` and rotate any key that may have landed in request logs.
- **Conservative defaults.** Start external callers with low RPM, concurrency, pixel, output, and queue limits.
- **Privacy-safe logs.** Keep gateway request IDs, statuses, timings, project audit events, and abuse decisions without logging authorization headers or render payloads.
- **Published policies.** Publish acceptable-use, privacy, retention, takedown, vulnerability-report, and support-contact policies before onboarding external users.
- **Kill switch.** Keep an operator procedure to stop the gateway, revoke a project key, or block a destination or IP while preserving investigation evidence.
- **SLAs.** Do not promise an SLA until alert delivery, an on-call owner, measured recovery times, and scheduled restore drills are in place.

## Related

- [Self-hosting and local setup](../../docs/self-hosting.md)
- [Projects, API keys, and observability](../../docs/platform.md)
- [Async jobs and queue providers](../../docs/async-jobs.md)
- [Security policy](../../SECURITY.md)
- [All docs](../../docs/README.md)
