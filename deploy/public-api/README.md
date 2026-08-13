# Public API deployment

This stack defines the supported public-beta topology. It pins the released
GHCR image, excludes administrative and metrics routes from the public gateway,
applies global connection/request limits, uses project-level API keys and
quotas, stores results in S3-compatible storage, exports Prometheus metrics,
and gives the renderer a fixed Docker subnet for host-level egress filtering.

You must also configure TLS termination, host patching, alert delivery,
object-store durability, and an incident-response owner.

## Deploy the stack

1. Copy `.env.example` to `.env`, pin `VIPERCAPTURE_VERSION`, and replace every
   placeholder. Generate each secret independently with `openssl rand -hex 32`.
2. On the Linux Docker host, install the private-address egress policy:

   ```bash
   sudo ./egress-firewall.sh
   ```

   This inserts one isolated chain into `DOCKER-USER` for only the renderer's
   fixed `172.30.0.10/32` address; established replies to the gateway and
   Prometheus remain allowed. It does not flush other firewall rules. Re-run it
   after Docker/firewall replacement and verify it with
   `sudo iptables -S VIPERCAPTURE_EGRESS`.
3. Start and probe the pinned deployment:

   ```bash
   docker compose --env-file .env pull
   docker compose --env-file .env up -d
   curl --fail http://127.0.0.1:8080/ready
   ```
4. Terminate HTTPS in a host reverse proxy, load balancer, or tunnel that sends
   traffic only to `127.0.0.1:8080`. The front proxy must overwrite, not append,
   `X-Forwarded-For` with the connecting client address. Nginx trusts only the
   pinned Docker host gateway at `172.31.0.1` for that header. Do not expose the
   container port directly.
5. Create one project/key per customer or internal workload through an
   authenticated maintenance connection. Never give callers the admin token.

The application enforces project RPM/concurrency. Nginx adds a coarse per-IP
ceiling for unauthenticated floods. Put upstream DDoS filtering in front of the
host; neither layer replaces network-level abuse protection.

## Back up and restore the deployment

Enable bucket versioning and retention in S3/R2. The Docker volume contains
control-plane state, encrypted queued inputs, schedules, and cache metadata. It
does **not** contain the secrets needed to decrypt that state or the credentials
needed to retrieve S3/R2 artifacts. Keep a separately encrypted recovery copy of
the complete `.env`, including `VIPERCAPTURE_CONTROL_SECRET`,
`VIPERCAPTURE_JOB_SECRET`, `VIPERCAPTURE_SIGNING_SECRET`, admin and webhook
tokens, and every `VIPERCAPTURE_S3_*`/`AWS_*` value. For example, encrypt it to
an offline recovery key with [age](https://age-encryption.org/) and never store
the plaintext copy beside the volume archive:

```bash
umask 077
mkdir -p backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
age --encrypt --recipient "$VIPERCAPTURE_BACKUP_AGE_RECIPIENT" \
  --output "backups/vipercapture-env-${stamp}.age" .env
```

Store that encrypted file and the age identity in separate access-controlled
locations, and test that the object-store credentials still reach the retained
bucket. Then take an offline volume backup so its encrypted state is consistent:

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

At least monthly, restore the matching pair on a non-production host (replace
the timestamp and identity path):

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

Authenticate an admin status request and verify a retained
job/profile/baseline plus an S3-backed artifact. Delete the recovered plaintext
`.env` when the isolated drill is complete. A backup that has not passed this
full state, secret, and object-store restore drill is not release evidence.

## Upgrade or roll back

1. Record the current image digest and export the Compose config.
2. Complete a backup and restore drill.
3. Change only `VIPERCAPTURE_VERSION`, run `docker compose pull`, then
   `docker compose up -d`.
4. Probe `/ready`, render a controlled page, submit/poll/download an async job,
   inspect Prometheus alerts, and watch error/queue rates for one hour.
5. Roll back by restoring the previous version and running `up -d`. Restore the
   data snapshot only when release notes explicitly identify an incompatible
   migration; restoring data loses work accepted after the snapshot.

## Configure abuse controls and support

- Require scoped project keys; revoke a key immediately when leaked.
- Start customers at low RPM, concurrency, pixel, output, and queue limits.
- Retain gateway request IDs, status, timing, project audit events, and abuse
  decisions without logging authorization headers or render payloads.
- Publish acceptable-use, privacy, retention, takedown, vulnerability-report,
  and support-contact policies before onboarding external users.
- Maintain an operator kill switch: stop the gateway, revoke a project key, or
  block a destination/IP while preserving evidence needed for investigation.
- Do not promise an SLA until alert delivery, an on-call owner, recovery-time
  measurements, and scheduled restore drills exist.
