import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Request

from vipercapture import main


def _preset_request(method: str, origin: str | None = "http://127.0.0.1:8000") -> Request:
    headers = [(b"host", b"127.0.0.1:8000")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "path": "/local/presets",
            "query_string": b"",
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 44000),
            "headers": headers,
        }
    )


class LocalControlGuardTests(unittest.TestCase):
    def test_allows_same_origin_get_without_origin_header(self):
        # Browsers omit Origin on same-origin GETs; preset reload must not 403.
        self.assertTrue(main._is_local_control_request(_preset_request("GET", origin=None)))

    def test_still_rejects_cross_origin(self):
        self.assertFalse(
            main._is_local_control_request(_preset_request("GET", origin="https://attacker.example"))
        )


class PresetStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        directory = Path(self.temp_dir.name) / "presets"
        patcher = patch.multiple(
            main,
            PRESETS_DIRECTORY=directory,
            PRESETS_FILE=directory / "presets.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.presets_file = directory / "presets.json"

    def test_roundtrip_with_owner_only_permissions(self):
        main._save_preset("blog", {"output": "png", "width": 1280})
        self.assertEqual(main._read_presets(), [{"name": "blog", "settings": {"output": "png", "width": 1280}}])
        if os.name == "posix":
            self.assertEqual(self.presets_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.presets_file.parent.stat().st_mode & 0o777, 0o700)

    def test_duplicate_name_rejected(self):
        main._save_preset("blog", {"output": "png"})
        with self.assertRaises(HTTPException) as context:
            main._save_preset("blog", {"output": "webp"})
        self.assertEqual(context.exception.status_code, 409)

    def test_limit_rejected_instead_of_silent_eviction(self):
        for index in range(main.MAX_PRESETS):
            main._save_preset(f"preset-{index}", {"output": "png"})
        with self.assertRaises(HTTPException) as context:
            main._save_preset("one-too-many", {"output": "png"})
        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(len(main._read_presets()), main.MAX_PRESETS)

    def test_malformed_entries_filtered(self):
        self.presets_file.parent.mkdir(parents=True, exist_ok=True)
        self.presets_file.write_text(
            json.dumps([{"name": "ok", "settings": {}}, {"name": 7}, "junk", {"settings": {}}]),
            encoding="utf-8",
        )
        self.assertEqual(main._read_presets(), [{"name": "ok", "settings": {}}])

    def test_corrupt_file_returns_empty(self):
        self.presets_file.parent.mkdir(parents=True, exist_ok=True)
        self.presets_file.write_text("not json", encoding="utf-8")
        self.assertEqual(main._read_presets(), [])

    def test_delete(self):
        main._save_preset("keep", {"output": "png"})
        main._save_preset("drop", {"output": "webp"})
        remaining = main._delete_preset("drop")
        self.assertEqual([preset["name"] for preset in remaining], ["keep"])


if __name__ == "__main__":
    unittest.main()
