# Same-host real-site rendering benchmark

## Evidence

- GitHub Actions run: [31533142388](https://github.com/Viperisuseful/ViperCapture/actions/runs/31533142388)
- ViperCapture commit: `9f25fcb5fff5a0a8a9d8ac666e9436ecb171073c`
- Runner: GitHub-hosted `ubuntu-22.04`, with both containers and the benchmark
  client on that one runner
- ViperCapture image ID: `sha256:7d27b520e23b01b0be7b56eb9b0bdc11e40ba15b87418aa930c440f5852b4521`
- Browserless: `v2.55.1`, OCI index
  `sha256:611b88859cb367a0bfd8d34fbd510d9783b7cede6729cfa8e7f6e19677084fbf`
- All cross-provider scenarios use `lazy_load: none`; provider-specific scrolling
  is excluded so both engines perform comparable work.
- Raw samples, output hashes, dimensions, inputs, and failures:
  [JSON](2026-08-11-viper-browserless-same-host.json)

## Summary

| Scenario | Provider | Success | Median | p95 | Median bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| wikipedia-home-viewport | viper | 10/10 | 441.74 ms | 460.32 ms | 103,137 |
| python-home-full-page | viper | 10/10 | 625.99 ms | 832.22 ms | 428,645 |
| rust-home-full-page | viper | 10/10 | 497.42 ms | 800.41 ms | 416,288 |
| github-home-viewport | viper | 10/10 | 2,597.47 ms | 2,863.37 ms | 158,188 |
| mdn-web-docs-viewport | viper | 10/10 | 1,302.03 ms | 1,335.35 ms | 113,802 |
| vite-home-full-page | viper | 10/10 | 1,695.16 ms | 1,764.15 ms | 2,881,579.5 |
| wikipedia-home-viewport | browserless | 10/10 | 1,000.30 ms | 1,099.90 ms | 102,380 |
| python-home-full-page | browserless | 10/10 | 997.64 ms | 1,133.61 ms | 474,056 |
| rust-home-full-page | browserless | 10/10 | 844.08 ms | 907.09 ms | 443,727 |
| github-home-viewport | browserless | 10/10 | 2,610.70 ms | 2,913.16 ms | 120,874 |
| mdn-web-docs-viewport | browserless | 10/10 | 1,583.52 ms | 1,649.50 ms | 118,989 |
| vite-home-full-page | browserless | 10/10 | 1,653.81 ms | 1,792.43 ms | 2,324,211 |

## Method

- Generated at `2026-08-11T20:31:59.440564+00:00`.
- Python `3.11.15` on `Linux-6.8.0-1062-azure-x86_64-with-glibc2.35` (`x86_64`).
- `10` measured runs after `2` warm-up run(s) for every provider and scenario.
- Providers were invoked sequentially from the same benchmark process and host.
- Caches must be disabled or cold-equivalent; provider credentials are read from the environment.

## Limitations

Real sites and provider fleets change. These measurements are a dated engineering snapshot, not a universal latency or rendering-quality ranking. Review the JSON artifact for raw samples, hashes, dimensions, failures, and exact inputs.
