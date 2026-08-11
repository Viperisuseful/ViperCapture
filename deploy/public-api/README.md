# Public API deployment

This stack is the smallest supported public-beta topology. It pins the released
GHCR image, keeps administrative and metrics routes off the public gateway,
applies global connection/request limits, uses project-level API keys and
quotas, stores results in S3-compatible storage, exports Prometheus metrics,
and gives the renderer a fixed Docker subnet for host-level egress filtering.

It is not a substitute for TLS termination, host patching, alert delivery,
object-store durability, or an incident-response owner.

## Bring it up

1. Copy `.env.example` to `.env`, pin `VIPERCAPTURE_VERSION`, and replace every
   placeholder. Generate each secret independently with `openssl rand -hex 32`.
2. On the Linux Docker host, install the private-address egress policy:

   ```bash
   sudo ./egress-firewall.sh
   ```

   This inserts one isolated chain into `DOCKER-USER`; it does not flush other
   firewall rules. Re-run it after Docker/firewall replacement and verify it
   with `sudo iptables -S VIPERCAPTURE_EGRESS`.
3. Start and probe the pinned deployment:

   ```bash
   docker compose --env-file .env pull
   docker compose --env-file .env up -d
   curl --fail http://127.0.0.1:8080/ready
   ```
4. Terminate HTTPS in a host reverse proxy, load balancer, or tunnel that sends
   traffic only to `127.0.0.1:8080`. Do not expose the container port directly.
5. Create one project/key per customer or internal workload through an
   authenticated maintenance connection. Never give callers the admin token.

The application enforces project RPM/concurrency. Nginx adds a coarse per-IP
ceiling for unauthenticated floods. Put upstream DDoS filtering in front of the
host; neither layer replaces network-level abuse protection.

## Backups and restore drills

Enable bucket versioning and retention in S3/R2. The Docker volume contains
control-plane state, encrypted queued inputs, encryption keys, schedules, and
cache metadata. Take an offline volume backup so the SQLite files and keys are
from the same instant:

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

At least monthly, restore into a new named volume on a non-production host,
start the same pinned image, authenticate an admin status request, and verify a
retained job/profile/baseline. A backup that has not passed a restore drill is
not release evidence.

## Upgrade and rollback

1. Record the current image digest and export the Compose config.
2. Complete a backup and restore drill.
3. Change only `VIPERCAPTURE_VERSION`, run `docker compose pull`, then
   `docker compose up -d`.
4. Probe `/ready`, render a controlled page, submit/poll/download an async job,
   inspect Prometheus alerts, and watch error/queue rates for one hour.
5. Roll back by restoring the previous version and running `up -d`. Restore the
   data snapshot only when release notes explicitly identify an incompatible
   migration; restoring data loses work accepted after the snapshot.

## Abuse and support boundary

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
