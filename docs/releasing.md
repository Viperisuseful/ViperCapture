# Release and distribution guide

`release/versions.json` is the release manifest. For this train it prepares:

| Deliverable | Version | Ship to | How users consume it |
| --- | --- | --- | --- |
| OSS developer beta | 0.1.0-beta.2 | GitHub Releases | source archive or repository tag |
| Container | 0.1.0-beta.2 | GitHub Container Registry | `docker pull ghcr.io/viperisuseful/vipercapture:v0.1.0-beta.2` |
| GitHub Action | 0.1.0-beta.2 | Repository tag | `uses: Viperisuseful/ViperCapture@v0.1.0-beta.2` |
| Agent skill | 0.1.0-beta.2 | GitHub Release asset | extract/install `vipercapture-skill-*.zip` |
| n8n/Terraform integrations | 0.1.0-beta.2 | GitHub Release asset | import/extract `vipercapture-integrations-*.zip` |
| Python SDK | 0.1.0b2 | PyPI | `pip install vipercapture==0.1.0b2` |
| TypeScript SDK | 0.1.0-beta.2 | npm | `npm install @vipercapture/sdk@0.1.0-beta.2` |
| Go SDK | 0.1.0-beta.2 source | GitHub | `go get github.com/Viperisuseful/ViperCapture/sdk/go@v0.1.0-beta.2` |
| Desktop beta | 0.1.9 | GitHub Releases | MSI/NSIS, Apple/Intel DMGs, Debian package |
| Android beta | 0.1.8 | GitHub Releases, then Play Console internal testing | signed universal APK/AAB |

Android 0.1.8 is intentionally unchanged and already has a signed APK/AAB with
checksums. Desktop advances independently to 0.1.9 so the Windows renderer fix
and the stacked browser-hardening work are rebuilt without falsely relabeling
the existing Android binary.

## Review build

Before tagging, build all repository artifacts without publishing:

```bash
python scripts/build_release.py --output-dir dist
python -m pip install build
python -m build sdk/python --outdir dist/python
npm ci --prefix sdk/typescript
npm pack ./sdk/typescript --pack-destination dist/typescript
python scripts/write_checksums.py dist
```

Run the **OSS release** workflow manually with `publish=false`. Download and
inspect `oss-release-packages`, verify `SHA256SUMS.txt`, install both SDK
packages in clean temporary environments, and test the source archive with
`python launch.py` or Compose.

## Registry setup required once

- Configure PyPI trusted publishing for repository
  `Viperisuseful/ViperCapture` and workflow `oss-release.yml`.
- Create an npm automation token with publish access to the `@vipercapture`
  scope and save it as repository secret `NPM_TOKEN`.
- GitHub's workflow token publishes the GHCR image and GitHub prerelease.
- Add `SCREENSHOTONE_ACCESS_KEY` and `URLBOX_SECRET` only if managed-provider
  benchmark evidence is desired. They are not release credentials.

## Publish order after the stacked PRs merge

1. Confirm PR #18 is merged before this stacked PR, then fast-forward `master`.
2. Run Validate, CodeQL, Operational readiness, and Same-host benchmark.
3. Run OSS release with `publish=false`; inspect and smoke-test its artifacts.
4. Create and push annotated tag `v0.1.0-beta.2`. The OSS workflow publishes
   GitHub assets, GHCR, PyPI, npm, Action tag, skill, integrations, and the
   required `sdk/go/v0.1.0-beta.2` Go submodule tag. It refuses to overwrite a
   Go tag that points at another commit.
5. Create and push annotated tag `desktop-v0.1.9`. The Desktop workflow builds
   four native packages and smoke-tests each packaged renderer before release.
6. Keep `android-v0.1.8` as the existing Android beta. Upload its AAB to Play
   Console internal testing only after completing store metadata and policy review.
7. Deploy the exact GHCR digest with `deploy/public-api`, run a restore drill,
   and start with a private/public-beta allowlist before general availability.

Do not reuse or replace existing public tags. If a workflow fails before a
release exists, fix forward and create the next version rather than mutating a
download users may already have cached.
