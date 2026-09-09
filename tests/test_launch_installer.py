from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import launch  # noqa: E402


class FindUvTests(unittest.TestCase):
    def test_find_uv_returns_which_result(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIPERCAPTURE_USE_UV", None)
            with mock.patch("launch.shutil.which", return_value="/opt/uv") as which:
                self.assertEqual(launch.find_uv(), "/opt/uv")
                which.assert_called_once_with("uv")

    def test_find_uv_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"VIPERCAPTURE_USE_UV": "0"}):
            with mock.patch("launch.shutil.which", return_value="/opt/uv") as which:
                self.assertIsNone(launch.find_uv())
                which.assert_not_called()


class InstallerCommandTests(unittest.TestCase):
    def test_venv_prefers_uv(self) -> None:
        venv_dir = Path("/tmp/.venv")
        command = launch.venv_command("/usr/bin/python3", venv_dir, "/opt/uv")
        self.assertEqual(
            command,
            ["/opt/uv", "venv", "--python", "/usr/bin/python3", str(venv_dir)],
        )

    def test_venv_falls_back_to_stdlib(self) -> None:
        venv_dir = Path("/tmp/.venv")
        command = launch.venv_command("/usr/bin/python3", venv_dir, None)
        self.assertEqual(
            command,
            ["/usr/bin/python3", "-m", "venv", str(venv_dir)],
        )

    def test_deps_prefer_uv_pip(self) -> None:
        requirements = Path("/app/requirements.txt")
        commands = launch.deps_commands("/venv/bin/python", requirements, "/opt/uv")
        self.assertEqual(
            commands,
            [(
                [
                    "/opt/uv",
                    "pip",
                    "install",
                    "--python",
                    "/venv/bin/python",
                    "-r",
                    str(requirements),
                ],
                "uv pip install",
            )],
        )

    def test_deps_fall_back_to_pip(self) -> None:
        requirements = Path("/app/requirements.txt")
        commands = launch.deps_commands("/venv/bin/python", requirements, None)
        self.assertEqual(
            [label for _command, label in commands],
            ["pip upgrade", "pip install"],
        )
        self.assertEqual(
            commands[1][0],
            [
                "/venv/bin/python",
                "-m",
                "pip",
                "install",
                "-r",
                str(requirements),
            ],
        )

    def test_deps_force_cryptography_sdist_on_intel_macos(self) -> None:
        requirements = Path("/app/requirements.txt")
        uv_commands = launch.deps_commands(
            "/venv/bin/python", requirements, "/opt/uv", intel_macos=True
        )
        pip_commands = launch.deps_commands(
            "/venv/bin/python", requirements, None, intel_macos=True
        )
        self.assertEqual(
            uv_commands[0][0][-2:],
            ["--no-binary", "cryptography"],
        )
        self.assertEqual(
            pip_commands[1][0][-2:],
            ["--no-binary", "cryptography"],
        )


class IntelMacosCryptographyTests(unittest.TestCase):
    def test_is_intel_macos_only_darwin_x86_64(self) -> None:
        self.assertTrue(launch.is_intel_macos("darwin", "x86_64"))
        self.assertFalse(launch.is_intel_macos("darwin", "arm64"))
        self.assertFalse(launch.is_intel_macos("linux", "x86_64"))
        self.assertFalse(launch.is_intel_macos("win32", "AMD64"))

    def test_parse_rustc_version(self) -> None:
        self.assertEqual(
            launch.parse_rustc_version("rustc 1.85.0 (hash 2026-01-01)"),
            (1, 85, 0),
        )
        self.assertLess(launch.parse_rustc_version("rustc 1.82.0"), launch.MIN_RUSTC)
        self.assertIsNone(launch.parse_rustc_version("not rustc"))

    def test_prepare_skips_non_intel_macos(self) -> None:
        missing = launch.prepare_intel_macos_cryptography_build(
            sys_platform="linux",
            machine="x86_64",
        )
        self.assertEqual(missing, [])

    def _openssl_prefix(self, root: Path) -> str:
        include = root / "include" / "openssl"
        include.mkdir(parents=True)
        (include / "ssl.h").write_text("/* test */\n", encoding="utf-8")
        (root / "lib" / "pkgconfig").mkdir(parents=True)
        return str(root)

    def test_preflight_reports_missing_source_build_tools(self) -> None:
        toolchain = launch.probe_intel_macos_cryptography_toolchain(
            which=lambda _name: None,
            run=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                [], 1, stdout="", stderr=""
            ),
            environ={},
        )
        missing = launch.intel_macos_cryptography_missing(toolchain)
        self.assertIn("Xcode command line tools (clang)", missing)
        self.assertIn("Rust 1.83.0+ (rustc)", missing)
        self.assertIn("Homebrew/MacPorts OpenSSL 3 (not Apple LibreSSL)", missing)
        message = launch.format_intel_macos_cryptography_error(missing)
        self.assertIn(launch.INTEL_MACOS_CRYPTOGRAPHY_DOCS, message)
        self.assertIn(launch.INTEL_MACOS_CRYPTOGRAPHY_CHANGELOG, message)
        self.assertIn("brew install openssl@3 rust", message)
        self.assertIn("xcode-select --install", message)
        self.assertIn("Do not pin cryptography <=48", message)

    def test_prepare_provisions_openssl_env_when_toolchain_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._openssl_prefix(Path(tmp) / "openssl@3")
            environ: dict[str, str] = {}

            def which(name: str) -> str | None:
                return {
                    "cc": "/usr/bin/cc",
                    "rustc": "/usr/local/bin/rustc",
                    "brew": "/usr/local/bin/brew",
                }.get(name)

            def run(cmd, **_kwargs):
                if cmd[:3] == ["/usr/local/bin/rustc", "--version"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="rustc 1.85.0 (hash)\n", stderr=""
                    )
                if cmd[:3] == ["/usr/local/bin/brew", "--prefix", "openssl@3"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=prefix + "\n", stderr=""
                    )
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

            missing = launch.prepare_intel_macos_cryptography_build(
                sys_platform="darwin",
                machine="x86_64",
                which=which,
                run=run,
                environ=environ,
            )
            self.assertEqual(missing, [])
            self.assertEqual(environ["OPENSSL_DIR"], prefix)
            self.assertTrue(
                environ["PKG_CONFIG_PATH"].startswith(str(Path(prefix) / "lib" / "pkgconfig"))
            )

    def test_prepare_rejects_old_rustc(self) -> None:
        def which(name: str) -> str | None:
            return {"cc": "/usr/bin/cc", "rustc": "/usr/bin/rustc"}.get(name)

        def run(cmd, **_kwargs):
            if cmd[:2] == ["/usr/bin/rustc", "--version"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="rustc 1.70.0\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        missing = launch.prepare_intel_macos_cryptography_build(
            sys_platform="darwin",
            machine="x86_64",
            which=which,
            run=run,
            environ={},
        )
        self.assertIn("Rust 1.83.0+ (rustc)", missing)

    def test_requirements_keep_patched_floors(self) -> None:
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn(
            'cryptography>=49.0.0; sys_platform == "darwin" and platform_machine == "x86_64"',
            text,
        )
        self.assertIn(
            'cryptography>=50.0.0; sys_platform != "darwin" or platform_machine != "x86_64"',
            text,
        )
        self.assertNotIn("<47.0.0", text)
        self.assertNotIn("<=48", text)


if __name__ == "__main__":
    unittest.main()
