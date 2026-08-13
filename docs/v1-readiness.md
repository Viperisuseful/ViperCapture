# Stable v1 release gates

ViperCapture remains beta until each gate below has dated, reviewable evidence.
Unit tests and one successful package build do not satisfy these gates.

| Gate | Minimum evidence for v1 |
| --- | --- |
| API stability | Versioned OpenAPI snapshot, migration policy, two release candidates without an unannounced breaking change |
| Sustained load | At least 10,000 representative renders over one hour, 99.9% accepted-request success, declared p95 target, no monotonic memory growth |
| Concurrency | Runs at configured concurrency 1, 2, 4, and 8 with explicit CPU/RAM, queue, success, p95/p99, and saturation behavior |
| Restart recovery | Twenty forced process/container terminations covering queued, running, succeeded, webhook-pending, and scheduled work with no accepted job silently lost |
| Constrained memory | One-hour run under each supported memory profile; no OOM kill, corrupt state, orphan browser tree, or unbounded retry loop |
| Storage recovery | Encrypted local-volume and S3/R2 backup restored on a clean host; retained jobs, profiles, schedules, baselines, and keys verified |
| Egress | Private, loopback, link-local, carrier-grade NAT, cloud metadata, IPv6-private, and DNS-rebinding probes blocked at both application and network layers |
| Rate/abuse control | Project RPM/concurrency limits plus gateway flood limits, key revocation, audit retention, and an exercised incident runbook |
| Observability | Ready/metrics/trace signals, error and queue alerts, container CPU/RAM/PID monitoring, log retention, and alert delivery to an owned channel |
| Releases | Reproducible source/package/container builds, checksums, packaged-renderer smoke tests, signed mobile artifact, rollback instructions, and release provenance |

Use the operational and benchmark workflows to collect initial evidence:

- **Operational readiness**: concurrency 1/2/4/8 saturation checks, a five-minute
  sustained-load gate, real crash/restart job recovery, and 40 renders under a
  1 GiB cgroup.
- **Same-host benchmark**: 30 measured runs after two warmups across six public
  real sites for ViperCapture and Browserless, with optional ScreenshotOne and
  Urlbox calls from that exact runner.

The workflow defaults are regression checks. They are not the required
one-hour v1 evidence.
Before v1, run higher counts on the intended production machine type and commit
dated Markdown plus raw JSON under `benchmarks/results/`. Record provider image
digests, ViperCapture commit, region, CPU/RAM, concurrency, and all failures.
