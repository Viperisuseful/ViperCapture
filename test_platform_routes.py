import unittest

import main


class PlatformRouteTests(unittest.TestCase):
    def test_openapi_exposes_platform_workflows(self):
        paths = main.app.openapi()["paths"]
        expected = {
            "/v1/render": {"post"},
            "/v1/render/signed": {"get"},
            "/v1/signed-url": {"post"},
            "/v1/diff": {"post"},
            "/v1/jobs": {"post"},
            "/v1/jobs/bulk": {"post"},
            "/v1/schedules": {"get", "post"},
            "/v1/schedules/{schedule_id}": {"get", "patch", "delete"},
        }
        for path, methods in expected.items():
            self.assertIn(path, paths)
            self.assertTrue(methods.issubset(paths[path]), path)


if __name__ == "__main__":
    unittest.main()
