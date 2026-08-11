# ViperCapture and ScreenshotOne — 2026-08-11

This is a reproducible engineering snapshot, not a claim that one service is
universally faster. ViperCapture ran locally on the benchmark host while
ScreenshotOne was called over the public internet, so the remote results also
include network and provider queue latency.

## Summary

| Scenario | Provider | Success | Median | p95 | Output |
| --- | --- | ---: | ---: | ---: | ---: |
| 1280×720 viewport PNG | ViperCapture | 10/10 | 342.16 ms | 374.52 ms | 18,788 B |
| 1280×720 viewport PNG | ScreenshotOne | 10/10 | 2,718.92 ms | 3,561.01 ms | 17,705 B |
| 1440×900 full-page PNG | ViperCapture | 10/10 | 776.86 ms | 891.65 ms | 19,571 B |
| 1440×900 full-page PNG | ScreenshotOne | 10/10 | 5,854.20 ms | 8,367.85 ms | 18,553 B |

In this setup, ScreenshotOne's median end-to-end latency was 7.95× the local
ViperCapture latency for the viewport case and 7.54× for the full-page case.
ScreenshotOne's files were 5.76% and 5.20% smaller, respectively. All outputs
had the expected dimensions, and each provider produced a stable hash across
the ten measured runs.

## Method

- ViperCapture revision: `1148a79ed060fb2783e3f6fd245cf57c0975ba9f`
- Benchmark client: OVH VM, x86-64 Haswell-class CPU, 4 vCPUs, Linux
  `7.0.0-29`, Python `3.14.4`
- Harness: [`benchmarks/run.py`](../run.py)
- Scenarios: [`benchmarks/scenarios.json`](../scenarios.json)
- Execution: sequential, one warm-up followed by ten measured runs per case
- ScreenshotOne cache: disabled (the provider default)
- ViperCapture endpoint: local loopback
- ScreenshotOne endpoint: `https://api.screenshotone.com/take`

The command shape was:

```bash
export SCREENSHOTONE_ACCESS_KEY='...'
python benchmarks/run.py \
  --provider viper=http://127.0.0.1:8000 \
  --provider screenshotone=https://api.screenshotone.com/take \
  --runs 10 --warmups 1 --output benchmark-results.json
```

The access key was supplied through the environment and is not stored in this
repository or the benchmark report. The ScreenshotOne signing secret was not
needed.

## Raw latency samples

All values are milliseconds, in execution order.

| Scenario | Provider | Samples |
| --- | --- | --- |
| Viewport | ViperCapture | 362.64, 274.34, 316.50, 346.87, 347.28, 329.39, 337.46, 358.40, 318.94, 374.52 |
| Viewport | ScreenshotOne | 3,561.01, 2,727.71, 2,710.13, 2,596.13, 2,489.94, 2,975.08, 3,410.29, 2,894.06, 2,579.74, 2,510.03 |
| Full page | ViperCapture | 781.41, 768.66, 773.20, 790.38, 891.65, 780.52, 783.31, 712.84, 755.04, 716.66 |
| Full page | ScreenshotOne | 5,277.07, 5,651.97, 5,702.90, 6,078.67, 6,250.83, 5,157.23, 6,005.50, 6,273.62, 8,367.85, 5,170.51 |

## Limitations

- This compares local self-hosting with a remote managed API; it does not
  isolate browser-engine speed.
- The two public example pages are intentionally simple. Dynamic, authenticated,
  media-heavy, and geographically distant targets can behave differently.
- Runs were sequential at concurrency one and do not measure throughput or
  behavior under load.
- A single warm-up limits cold-start conclusions.
- Provider plan, region, routing, and service changes can alter later results.

Re-run the checked-in harness from the same client host with representative
pages before making a purchasing or capacity decision.
