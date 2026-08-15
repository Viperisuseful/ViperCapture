import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch


class LauncherTest(unittest.TestCase):
    def test_chromium_only_stamp_is_upgraded_once(self):
        with tempfile.TemporaryDirectory() as directory:
            stamp = Path(directory) / ".playwright_stamp"
            stamp.write_text("1.2.3", encoding="utf-8")
            with (
                patch.object(launch, "PLAYWRIGHT_STAMP", stamp),
                patch.object(launch, "version", return_value="1.2.3"),
                patch.object(launch, "run") as run,
            ):
                launch.ensure_playwright()
            run.assert_called_once()
            self.assertIn("--no-shell", run.call_args.args)
            self.assertEqual(
                stamp.read_text(encoding="utf-8"), "1.2.3:chromium,firefox,webkit"
            )


if __name__ == "__main__":
    unittest.main()
