from __future__ import annotations

import os
import sys
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


if __name__ == "__main__":
    unittest.main()
