# Reproducible render benchmark

This suite records raw samples, success rate, output hashes, dimensions, runtime
environment, and the complete scenario inputs. It makes no performance claim
without a checked-in result from the exact command that produced it.

Start ViperCapture, then run:

```bash
python benchmarks/run.py \
  --provider viper=http://127.0.0.1:8000 \
  --runs 10 \
  --output benchmark-results.json
```

For a network-independent local proof, serve the checked-in deterministic
fixtures and select `scenarios-local.json`:

```bash
python -m http.server 8099 --directory benchmarks/fixtures
python benchmarks/run.py --provider viper=http://127.0.0.1:8000 \
  --scenarios benchmarks/scenarios-local.json --runs 10
```

To compare managed APIs using the same cases, set the provider credential in
your environment and repeat `--provider`:

```bash
export SCREENSHOTONE_ACCESS_KEY='...'
export URLBOX_SECRET='...'
python benchmarks/run.py \
  --provider viper=http://127.0.0.1:8000 \
  --provider screenshotone=https://api.screenshotone.com/take \
  --provider urlbox=https://api.urlbox.com/v1/render/sync \
  --runs 10 --warmups 1 --output benchmark-results.json
```

Run providers from the same client host and at roughly the same time. Do not
compare cached runs with uncached runs. The public fixtures are intentionally
simple and stable; copy `scenarios.json` and add representative pages owned by
your organization before making a purchasing or capacity decision. Provider
credentials are read only from the process environment and never written to
the report.

For an open-source same-host comparison, run Browserless on the benchmark host
and add its endpoint. `BROWSERLESS_TOKEN` is optional for an unprotected local
instance:

```bash
python benchmarks/run.py \
  --provider viper=http://127.0.0.1:8000 \
  --provider browserless=http://127.0.0.1:3000 \
  --scenarios benchmarks/scenarios-real-sites.json \
  --runs 30 --warmups 2 --output benchmark-results.json
python benchmarks/report.py benchmark-results.json benchmark-results.md \
  --title "Same-host real-site rendering benchmark"
```

The manual **Same-host benchmark** GitHub workflow builds ViperCapture and
Browserless on one runner, executes the six-site scenario set, and publishes
the raw JSON, Markdown summary, container metadata, and logs as one Actions
artifact. ScreenshotOne and Urlbox can be added to the same run after adding
`SCREENSHOTONE_ACCESS_KEY` and `URLBOX_SECRET` repository secrets. A run fails
instead of silently omitting a requested managed provider when either secret is
missing. Cross-provider scenarios disable ViperCapture-specific lazy-loading
scroll behavior; the harness rejects non-Viper providers when a scenario tries
to enable it, rather than publishing unequal work as a comparison.

## Operational gates

`benchmarks.production_gate` contains real-process checks rather than mocked
throughput tests:

```bash
python -m benchmarks.production_gate load --requests 120 --concurrency 4 \
  --duration-seconds 300
python -m benchmarks.production_gate restart-recovery
```

The **Operational readiness** workflow records concurrency 1/2/4/8 saturation,
runs a five-minute sustained gate, and kills a server while an encrypted job is
rendering, then verifies that a replacement process recovers the job and
artifact. A separate job builds the Docker image, applies a 1 GiB cgroup
ceiling and PID limit, runs 40 real renders inside that cgroup, and rejects OOM
termination. Every gate uploads its raw JSON and logs. Use a 3,600-second gate
on the intended production host for the stable-v1 evidence set.

## Published results

- [ViperCapture and Browserless, same host, 2026-08-11](results/2026-08-11-viper-browserless-same-host.md)
- [ViperCapture and ScreenshotOne, 2026-08-11](results/2026-08-11-screenshotone.md)

Results are engineering snapshots, not universal vendor rankings. Read the
methodology and limitations before citing a latency ratio.
