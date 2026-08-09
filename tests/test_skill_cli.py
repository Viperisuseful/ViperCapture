import argparse
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "skills" / "vipercapture" / "scripts" / "capture.py"
SPEC = importlib.util.spec_from_file_location("vipercapture_skill_capture", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CAPTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPTURE)


class SkillCliTests(unittest.TestCase):
    def test_diagnostic_bundle_preserves_explicit_output_path(self):
        requested = Path("report.custom")
        args = argparse.Namespace(
            output_path=requested,
            diagnostic_bundle=True,
        )
        self.assertEqual(CAPTURE.output_path(args), requested)


if __name__ == "__main__":
    unittest.main()
