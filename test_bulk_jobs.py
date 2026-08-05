import unittest

from pydantic import ValidationError

from bulk_jobs import BulkJobRequest


class BulkJobContractTests(unittest.TestCase):
    def test_bulk_request_accepts_named_render_items(self):
        request = BulkJobRequest.model_validate(
            {
                "items": [
                    {
                        "id": "homepage",
                        "request_id": "release-42-homepage",
                        "render": {"url": "https://example.com"},
                    },
                    {
                        "id": "mobile",
                        "render": {
                            "url": "https://example.com",
                            "viewport": {"width": 390, "height": 844},
                        },
                    },
                ]
            }
        )
        self.assertEqual(len(request.items), 2)
        self.assertEqual(request.items[0].id, "homepage")

    def test_bulk_request_is_bounded(self):
        with self.assertRaises(ValidationError):
            BulkJobRequest.model_validate({"items": []})
        with self.assertRaises(ValidationError):
            BulkJobRequest.model_validate(
                {"items": [{"render": {"url": "https://example.com"}}] * 101}
            )

    def test_bulk_aggregate_source_is_bounded(self):
        # 5 x 5 MiB embedded sources are each individually valid (per-item cap
        # is 5 MiB) but exceed the 20 MiB aggregate budget in one request.
        items = [
            {"render": {"html": "x" * (5 * 1024 * 1024)}}
            for _ in range(5)
        ]
        with self.assertRaises(ValidationError):
            BulkJobRequest.model_validate({"items": items})

        accepted = BulkJobRequest.model_validate(
            {"items": [{"render": {"html": "x" * (5 * 1024 * 1024)}} for _ in range(4)]}
        )
        self.assertEqual(len(accepted.items), 4)
