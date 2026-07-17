import unittest

from pydantic import ValidationError

from render_contract import OutputFormat, RenderRequest


class RenderContractTest(unittest.TestCase):
    def test_url_image_request_is_supported(self):
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "png",
                "viewport": {
                    "width": 1280,
                    "height": 720,
                    "device_scale_factor": 1,
                },
            }
        )
        self.assertEqual(request.output, OutputFormat.PNG)

    def test_non_url_source_is_rejected(self):
        with self.assertRaises(ValidationError):
            RenderRequest.model_validate(
                {
                    "html": "<p>x</p>",
                    "output": "png",
                }
            )

    def test_public_formats_are_image_only(self):
        for output in OutputFormat:
            request = RenderRequest.model_validate(
                {"url": "https://example.com", "output": output.value}
            )
            self.assertEqual(request.credit_cost, 1)
        for output in ("pdf", "html", "markdown"):
            with self.subTest(output=output), self.assertRaises(ValidationError):
                RenderRequest.model_validate(
                    {"url": "https://example.com", "output": output}
                )

    def test_selector_image_wait_and_header_rules(self):
        accepted = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "webp",
                "full_page": False,
                "selector": "main",
                "image": {"quality": 80, "transparent_background": True},
                "headers": {"Authorization": "Bearer local-test"},
                "wait_for": {"event": "networkidle", "delay_ms": 100},
            }
        )
        self.assertEqual(accepted.selector, "main")
        invalid = (
            {"url": "https://example.com", "selector": "main"},
            {"url": "https://example.com", "output": "png", "image": {"quality": 80}},
            {
                "url": "https://example.com",
                "output": "jpeg",
                "image": {"transparent_background": True},
            },
            {"url": "https://example.com", "headers": {"Host": "evil.example"}},
            {"url": "https://example.com", "headers": {"X-Test": "line\rfeed"}},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                RenderRequest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
