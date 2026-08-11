import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_release_manifest_matches_package_versions(self):
        versions = json.loads((ROOT / "release" / "versions.json").read_text("utf-8"))
        python_manifest = (ROOT / "sdk" / "python" / "pyproject.toml").read_text("utf-8")
        typescript_manifest = json.loads(
            (ROOT / "sdk" / "typescript" / "package.json").read_text("utf-8")
        )
        desktop_manifest = json.loads((ROOT / "desktop" / "package.json").read_text("utf-8"))
        android_manifest = json.loads(
            (ROOT / "desktop" / "src-tauri" / "tauri.android.conf.json").read_text("utf-8")
        )
        self.assertIn(f'version = "{versions["python_sdk"]}"', python_manifest)
        self.assertEqual(typescript_manifest["version"], versions["typescript_sdk"])
        self.assertEqual(desktop_manifest["version"], versions["desktop"])
        self.assertEqual(android_manifest["version"], versions["android"])
        self.assertTrue(versions["container"].endswith(versions["oss"]))

    def test_desktop_and_android_release_tags_are_checked_independently(self):
        script = ROOT / "desktop" / "scripts" / "check_release_version.py"
        for app, tag in (("desktop", "desktop-v0.1.9"), ("android", "android-v0.1.8")):
            completed = subprocess.run(
                [sys.executable, str(script), app],
                cwd=ROOT,
                env={**os.environ, "RELEASE_TAG": tag},
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


class ChecksumTests(unittest.TestCase):
    def test_checksum_script_uses_flat_published_asset_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            artifact = root / "nested" / "artifact.bin"
            artifact.write_bytes(b"release evidence")
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "write_checksums.py"), str(root)],
                check=True,
                cwd=ROOT,
            )
            checksum = (root / "SHA256SUMS.txt").read_text("ascii")
            self.assertEqual(
                checksum,
                f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  artifact.bin\n",
            )

    def test_checksum_script_rejects_duplicate_published_asset_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for nested in ("one", "two"):
                (root / nested).mkdir()
                (root / nested / "artifact.bin").write_bytes(nested.encode("ascii"))
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "write_checksums.py"), str(root)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("duplicate release asset name", completed.stderr)

    def test_repository_bundles_are_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / name for name in ("first", "second")]
            environment = os.environ.copy()
            environment.pop("SOURCE_DATE_EPOCH", None)
            for root in roots:
                subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "build_release.py"),
                        "--output-dir",
                        str(root),
                    ],
                    check=True,
                    cwd=ROOT,
                    env=environment,
                )
            first = {
                path.relative_to(roots[0]): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in roots[0].rglob("*")
                if path.is_file()
            }
            second = {
                path.relative_to(roots[1]): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in roots[1].rglob("*")
                if path.is_file()
            }
            self.assertEqual(first, second)


class OperationalPackagingTests(unittest.TestCase):
    def test_egress_policy_targets_only_renderer_and_allows_replies(self):
        policy = (ROOT / "deploy" / "public-api" / "egress-firewall.sh").read_text(
            "utf-8"
        )
        self.assertIn("RENDERER_CIDR=${VIPERCAPTURE_RENDERER_CIDR:-172.30.0.10/32}", policy)
        self.assertIn('--ctstate ESTABLISHED,RELATED -j RETURN', policy)
        self.assertIn('-s "$RENDERER_CIDR" -j "$CHAIN"', policy)
        self.assertLess(policy.index("ESTABLISHED,RELATED"), policy.index("10.0.0.0/8"))

    def test_gateway_trust_and_rate_limit_share_one_fixed_network(self):
        compose = (ROOT / "deploy" / "public-api" / "docker-compose.yml").read_text(
            "utf-8"
        )
        nginx = (ROOT / "deploy" / "public-api" / "nginx.conf").read_text("utf-8")
        self.assertIn("gateway: 172.31.0.1", compose)
        self.assertIn("set_real_ip_from 127.0.0.1;", nginx)
        self.assertIn("set_real_ip_from 172.31.0.1;", nginx)
        self.assertIn("real_ip_header X-Forwarded-For;", nginx)
        self.assertIn("limit_req_zone $binary_remote_addr", nginx)

    def test_queue_alert_aggregates_both_metric_families(self):
        alerts = (ROOT / "deploy" / "public-api" / "alerts.yml").read_text("utf-8")
        self.assertIn("sum(rate(vipercapture_queue_seconds_sum[10m]))", alerts)
        self.assertIn("sum(rate(vipercapture_renders_total[10m]))", alerts)

    def test_error_rate_alert_excludes_probe_routes_from_both_operands(self):
        alerts = (ROOT / "deploy" / "public-api" / "alerts.yml").read_text(
            "utf-8"
        )
        route_filter = 'route!~"/(health|ready|metrics)"'
        self.assertEqual(alerts.count(route_filter), 2)

    def test_operational_matrix_varies_renderer_and_client_concurrency(self):
        workflow = (
            ROOT / ".github" / "workflows" / "operational-readiness.yml"
        ).read_text("utf-8")
        matrix = workflow[workflow.index("Concurrency saturation matrix") :]
        self.assertIn("timeout-minutes: 45", workflow)
        self.assertIn('VIPERCAPTURE_MAX_CONCURRENCY="$concurrency"', matrix)
        self.assertIn('--concurrency "$concurrency"', matrix)
        self.assertIn("VIPERCAPTURE_MAX_CONCURRENCY=4", matrix)

    def test_constrained_memory_report_is_copied_before_failure_propagates(self):
        workflow = (
            ROOT / ".github" / "workflows" / "operational-readiness.yml"
        ).read_text("utf-8")
        gate = workflow[workflow.index("Constrained-memory render gate") :]
        self.assertIn("|| gate_status=$?", gate)
        self.assertLess(gate.index("docker cp"), gate.index('test "$gate_status" -eq 0'))

    def test_container_waits_for_validated_packages_and_go_tag_is_prefixed(self):
        workflow = (ROOT / ".github" / "workflows" / "oss-release.yml").read_text(
            "utf-8"
        )
        self.assertIn("container:\n    needs: package", workflow)
        self.assertIn('go_tag="sdk/go/v${version}"', workflow)
        self.assertIn("packages_dir: dist/python/", workflow)
        self.assertIn("skip_existing: true", workflow)
        self.assertIn('npm view "${package}@${version}" version', workflow)
        self.assertIn("npm publish dist/typescript/*.tgz --access public --tag beta", workflow)
        self.assertIn('gh release view "$RELEASE_TAG"', workflow)
        self.assertIn("--json assets --jq '.assets[].name'", workflow)
        self.assertIn('gh release upload "$RELEASE_TAG" "$asset"', workflow)
        self.assertIn("MANUAL_PUBLISH:", workflow)
        self.assertIn(
            'git ls-remote origin "refs/tags/${expected}" "refs/tags/${expected}^{}"',
            workflow,
        )
        self.assertIn('test "$tagged_commit" = "$GITHUB_SHA"', workflow)

    def test_gateway_streams_admitted_request_bodies(self):
        nginx = (ROOT / "deploy" / "public-api" / "nginx.conf").read_text("utf-8")
        self.assertIn("proxy_request_buffering off;", nginx)
        self.assertNotIn("proxy_request_buffering on;", nginx)

    def test_gateway_preserves_caller_request_id_with_generated_fallback(self):
        nginx = (ROOT / "deploy" / "public-api" / "nginx.conf").read_text("utf-8")
        self.assertIn("map $http_x_request_id $upstream_request_id", nginx)
        self.assertIn('"" $request_id;', nginx)
        self.assertIn("default $http_x_request_id;", nginx)
        self.assertIn("proxy_set_header X-Request-Id $upstream_request_id;", nginx)


if __name__ == "__main__":
    unittest.main()
