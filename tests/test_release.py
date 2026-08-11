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
    def test_checksum_script_covers_nested_artifacts_but_not_itself(self):
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
                f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  nested/artifact.bin\n",
            )


if __name__ == "__main__":
    unittest.main()
