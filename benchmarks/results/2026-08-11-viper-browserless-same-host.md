# Same-host real-site rendering benchmark

## Evidence

- GitHub Actions run: [31527269371](https://github.com/Viperisuseful/ViperCapture/actions/runs/31527269371)
- ViperCapture commit: `9decf73c9ec517a9c595db9d0b36f559edd21908`
- Runner: GitHub-hosted `ubuntu-22.04`, with both containers and the benchmark
  client on that one runner
- ViperCapture image ID: `sha256:e64de458a1d14721bdb21d19ccd1708516b08ebf9a26b49f3de2bdc06369c97e`
- Browserless: `v2.55.1`, OCI index
  `sha256:611b88859cb367a0bfd8d34fbd510d9783b7cede6729cfa8e7f6e19677084fbf`
- Raw samples, output hashes, dimensions, inputs, and failures:
  [JSON](2026-08-11-viper-browserless-same-host.json)

## Summary

| Scenario | Provider | Success | Median | p95 | Median bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| wikipedia-home-viewport | viper | 10/10 | 459.36 ms | 550.24 ms | 103,137 |
| python-home-full-page | viper | 10/10 | 1,177.61 ms | 1,265.39 ms | 442,962 |
| rust-home-full-page | viper | 10/10 | 1,073.65 ms | 1,175.41 ms | 416,288 |
| github-home-viewport | viper | 10/10 | 2,712.02 ms | 2,972.19 ms | 163,685 |
| mdn-web-docs-viewport | viper | 10/10 | 1,338.55 ms | 1,358.81 ms | 113,803 |
| vite-home-full-page | viper | 10/10 | 2,904.01 ms | 2,986.76 ms | 3,044,504 |
| wikipedia-home-viewport | browserless | 10/10 | 1,065.76 ms | 1,115.78 ms | 102,380 |
| python-home-full-page | browserless | 10/10 | 1,014.08 ms | 1,047.41 ms | 476,043 |
| rust-home-full-page | browserless | 10/10 | 894.52 ms | 940.40 ms | 443,727 |
| github-home-viewport | browserless | 10/10 | 2,581.90 ms | 2,898.82 ms | 119,773 |
| mdn-web-docs-viewport | browserless | 10/10 | 1,640.68 ms | 1,732.80 ms | 118,990 |
| vite-home-full-page | browserless | 10/10 | 1,906.24 ms | 2,026.66 ms | 2,781,385 |

## Method

- Generated at `2026-08-11T19:21:57.390754+00:00`.
- Python `3.11.15` on `Linux-6.8.0-1062-azure-x86_64-with-glibc2.35` (`x86_64`).
- `10` measured runs after `2` warm-up run(s) for every provider and scenario.
- Providers were invoked sequentially from the same benchmark process and host.
- Caches must be disabled or cold-equivalent; provider credentials are read from the environment.

## Limitations

Real sites and provider fleets change. These measurements are a dated engineering snapshot, not a universal latency or rendering-quality ranking. Review the JSON artifact for raw samples, hashes, dimensions, failures, and exact inputs.
