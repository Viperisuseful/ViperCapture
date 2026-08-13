# Release guide

`release/versions.json` is the release manifest. For this train it prepares:

| Deliverable | Version | Ship to | How users consume it |
| --- | --- | --- | --- |
| OSS developer beta | 0.2.0-beta.2 | GitHub Releases | source archive or repository tag |
| Container | 0.2.0-beta.2 | GitHub Container Registry | `docker pull ghcr.io/viperisuseful/vipercapture:v0.2.0-beta.2` |
| GitHub Action | 0.2.0-beta.2 | Repository tag | `uses: Viperisuseful/ViperCapture@v0.2.0-beta.2` |
| Agent skill | 0.2.0-beta.2 | GitHub Release asset | extract/install `vipercapture-skill-*.zip` |
| n8n/Terraform integrations | 0.2.0-beta.2 | GitHub Release asset | import/extract `vipercapture-integrations-*.zip` |
| Python SDK | 0.2.0b2 | GitHub Releases; PyPI when enabled | release wheel/sdist or `pip install vipercapture==0.2.0b2` |
| TypeScript SDK | 0.2.0-beta.2 | GitHub Releases; npm when enabled | release tarball or `npm install @vipercapture/sdk@0.2.0-beta.2` |
| Go SDK | 0.2.0-beta.2 source | GitHub | `go get github.com/Viperisuseful/ViperCapture/sdk/go@v0.2.0-beta.2` |
| Desktop beta | 0.2.1 | GitHub Releases | MSI/NSIS, Apple/Intel DMGs, Debian package |
| Android beta | 0.1.8 | GitHub Releases, then Play Console internal testing | signed universal APK/AAB |

Android remains at 0.1.8 and already has a signed APK/AAB with
checksums. Desktop advances independently to 0.2.1 so PR #21's animated
full-page output, quality, GPU encoding, and release-tooling changes ship in
fresh native packages without relabeling the existing Android binary.

## Build release artifacts

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

## Configure registries

- Configure PyPI trusted publishing for repository
  `Viperisuseful/ViperCapture` and workflow `oss-release.yml`.
- Set repository variable `PUBLISH_PYPI=true` after the trusted publisher is
  configured. Until then, the wheel and source distribution remain available
  from the GitHub release.
- Create an npm automation token with publish access to the `@vipercapture`
  scope, save it as repository secret `NPM_TOKEN`, and set repository variable
  `PUBLISH_NPM=true`. Until then, the npm tarball remains available from the
  GitHub release.
- GitHub's workflow token publishes the GHCR image and GitHub prerelease.
- Add `SCREENSHOTONE_ACCESS_KEY` and `URLBOX_SECRET` only if managed-provider
  benchmark evidence is desired. They are not release credentials.

## Publish a release

1. Confirm PR #21 is merged, then fast-forward `master`.
2. Run Validate, CodeQL, Operational readiness, and Same-host benchmark.
3. Run OSS release with `publish=false`; inspect and smoke-test its artifacts.
4. Create and push annotated tag `v0.2.0-beta.2`. The OSS workflow publishes
   GitHub assets, GHCR, PyPI, npm, Action tag, skill, integrations, and the
   required `sdk/go/v0.2.0-beta.2` Go submodule tag. PyPI and npm publish only
   when their repository variables are enabled. It refuses to overwrite a
   Go tag that points at another commit.
5. Create and push annotated tag `desktop-v0.2.1`. The Desktop workflow builds
   four native packages and smoke-tests each packaged renderer before release.
6. Keep `android-v0.1.8` as the existing Android beta. Upload its AAB to Play
   Console internal testing only after completing store metadata and policy review.
7. Deploy the exact GHCR digest with `deploy/public-api`, run a restore drill,
   and start with a private/public-beta allowlist before general availability.

Do not reuse or replace existing public tags. If a workflow fails before a
release exists, fix forward and create the next version rather than mutating a
download users may already have cached.
