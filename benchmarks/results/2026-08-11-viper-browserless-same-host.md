# Same-host real-site rendering benchmark

## Evidence

- GitHub Actions run: [31535398958](https://github.com/Viperisuseful/ViperCapture/actions/runs/31535398958)
- ViperCapture commit: `6abd0cd6f260b6790dffc6ec64e6a78f8579b8ec`
- Runner: GitHub-hosted `ubuntu-22.04`, with both containers and the benchmark
  client on that one runner
- ViperCapture image ID: `sha256:5d42983e60df1e0bfbe787be628155b4ecc8c2a8a1c086e43b8fce3497158ce0`
- Browserless: `v2.55.1`, OCI index
  `sha256:611b88859cb367a0bfd8d34fbd510d9783b7cede6729cfa8e7f6e19677084fbf`
- Provider order rotates on every attempt; all cross-provider scenarios use
  `lazy_load: none` so both engines perform comparable work.
- Raw samples, output hashes, dimensions, inputs, and failures:
  [JSON](2026-08-11-viper-browserless-same-host.json)

## Summary

| Scenario | Provider | Success | Median | p95 | Median bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| wikipedia-home-viewport | viper | 10/10 | 398.26 ms | 427.78 ms | 103,137 |
| python-home-full-page | viper | 10/10 | 641.87 ms | 677.85 ms | 421,021 |
| rust-home-full-page | viper | 10/10 | 565.23 ms | 734.79 ms | 416,288 |
| github-home-viewport | viper | 10/10 | 2,565.03 ms | 2,650.23 ms | 156,551 |
| mdn-web-docs-viewport | viper | 10/10 | 1,304.82 ms | 1,382.87 ms | 113,802 |
| vite-home-full-page | viper | 10/10 | 1,735.22 ms | 1,797.98 ms | 2,883,795 |
| wikipedia-home-viewport | browserless | 10/10 | 983.11 ms | 1,449.45 ms | 102,380 |
| python-home-full-page | browserless | 10/10 | 962.21 ms | 1,045.33 ms | 462,094 |
| rust-home-full-page | browserless | 10/10 | 918.85 ms | 1,015.26 ms | 443,727 |
| github-home-viewport | browserless | 10/10 | 2,451.99 ms | 2,527.39 ms | 120,635.5 |
| mdn-web-docs-viewport | browserless | 10/10 | 1,583.17 ms | 1,645.06 ms | 118,989 |
| vite-home-full-page | browserless | 10/10 | 1,648.88 ms | 1,840.90 ms | 2,781,330 |

## Method

- Generated at `2026-08-11T20:57:56.039272+00:00`.
- Python `3.11.15` on `Linux-6.8.0-1062-azure-x86_64-with-glibc2.35` (`x86_64`).
- `10` measured runs after `2` warm-up run(s) for every provider and scenario.
- Provider order was rotated for every attempt from the same benchmark process and host.
- Caches must be disabled or cold-equivalent; provider credentials are read from the environment.

## Limitations

Real sites and provider fleets change. These measurements are a dated engineering snapshot, not a universal latency or rendering-quality ranking. Review the JSON artifact for raw samples, hashes, dimensions, failures, and exact inputs.
