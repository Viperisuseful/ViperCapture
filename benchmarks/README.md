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
