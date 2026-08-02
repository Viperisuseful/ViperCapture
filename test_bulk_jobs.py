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
