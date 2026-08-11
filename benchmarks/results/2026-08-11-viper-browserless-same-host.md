# Same-host real-site rendering benchmark

## Evidence

- GitHub Actions run: [31523434529](https://github.com/Viperisuseful/ViperCapture/actions/runs/31523434529)
- ViperCapture commit: `c607ee0a0146a213f1d85cc3ef7dee29eb70a837`
- Runner: GitHub-hosted `ubuntu-22.04`, with both containers and the benchmark
  client on that one runner
- ViperCapture image ID: `sha256:72619225e76fbf08480ce8422a124bdca07644309d0c001aea71033953c5dad5`
- Browserless: `v2.55.1`, OCI index
  `sha256:611b88859cb367a0bfd8d34fbd510d9783b7cede6729cfa8e7f6e19677084fbf`
- Raw samples, output hashes, dimensions, inputs, and failures:
  [JSON](2026-08-11-viper-browserless-same-host.json)

## Summary

| Scenario | Provider | Success | Median | p95 | Median bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| wikipedia-home-viewport | viper | 10/10 | 315.66 ms | 321.59 ms | 103,137 |
| python-home-full-page | viper | 10/10 | 1,025.68 ms | 1,042.69 ms | 450,369 |
| rust-home-full-page | viper | 10/10 | 916.63 ms | 1,077.37 ms | 416,288 |
| github-home-viewport | viper | 10/10 | 2,327.86 ms | 2,541.36 ms | 155,673 |
| mdn-web-docs-viewport | viper | 10/10 | 1,250.09 ms | 1,303.85 ms | 113,802 |
| vite-home-full-page | viper | 10/10 | 2,425.11 ms | 2,459.56 ms | 3,038,013 |
| wikipedia-home-viewport | browserless | 10/10 | 949.49 ms | 1,197.71 ms | 102,380 |
| python-home-full-page | browserless | 10/10 | 903.92 ms | 1,703.17 ms | 480,413 |
| rust-home-full-page | browserless | 10/10 | 773.79 ms | 1,336.08 ms | 443,727 |
| github-home-viewport | browserless | 10/10 | 1,290.35 ms | 1,530.01 ms | 107,727 |
| mdn-web-docs-viewport | browserless | 10/10 | 774.63 ms | 1,132.62 ms | 116,032 |
| vite-home-full-page | browserless | 10/10 | 1,631.38 ms | 1,819.50 ms | 1,867,087 |

## Method

- Generated at `2026-08-11T18:38:03.816706+00:00`.
- Python `3.11.15` on `Linux-6.8.0-1062-azure-x86_64-with-glibc2.35` (`x86_64`).
- `10` measured runs after `2` warm-up run(s) for every provider and scenario.
- Providers were invoked sequentially from the same benchmark process and host.
- Caches must be disabled or cold-equivalent; provider credentials are read from the environment.

## Limitations

Real sites and provider fleets change. These measurements are a dated engineering snapshot, not a universal latency or rendering-quality ranking. Review the JSON artifact for raw samples, hashes, dimensions, failures, and exact inputs.
