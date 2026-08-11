import unittest

from pydantic import ValidationError

from vipercapture.render_contract import (
    BrowserEngine,
    DevicePreset,
    LazyLoadMode,
    OutputFormat,
    RenderRequest,
    canonical_render_document,
)


class RenderContractTest(unittest.TestCase):
    def test_resource_type_serialization_is_canonical(self):
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "network": {
                    "block_resource_types": ["script", "font", "image"]
                },
            }
        )
        document = canonical_render_document(request)
        self.assertEqual(
            document["network"]["block_resource_types"],
            ["font", "image", "script"],
        )

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
        self.assertFalse(request.proceed_on_captcha)
        self.assertFalse(request.preserve_viewport_width)
        self.assertEqual(request.lazy_load, LazyLoadMode.THOROUGH)
        self.assertFalse(request.image.optimize_for_speed)
        self.assertFalse(request.cache)

    def test_cross_browser_and_parity_options_are_validated(self):
        request = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "engine": "firefox",
                "output": "webp",
                "image": {"width": 800, "height": 600, "quality": 82},
            }
        )
        self.assertEqual(request.engine, BrowserEngine.FIREFOX)
        self.assertEqual(request.image.width, 800)

        shadow = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "html",
                "include_shadow_dom": True,
            }
        )
        self.assertTrue(shadow.include_shadow_dom)

        for payload in (
            {"url": "https://example.com", "engine": "webkit", "output": "pdf"},
            {
                "url": "https://example.com",
                "engine": "firefox",
                "image": {"optimize_for_speed": True},
            },
            {"url": "https://example.com", "include_shadow_dom": True},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                RenderRequest.model_validate(payload)

        for pdf in (
            {"page_ranges": "1-51"},
            {"page_ranges": "5-2"},
            {
                "paper_size": "A6",
                "margins": {"top": 0, "right": 3, "bottom": 0, "left": 3},
            },
        ):
            with self.subTest(pdf=pdf), self.assertRaises(ValidationError):
                RenderRequest.model_validate(
                    {"url": "https://example.com", "output": "pdf", "pdf": pdf}
                )

        bounded_ranges = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "pdf",
                "pdf": {"page_ranges": "1-25,20-50"},
            }
        )
        self.assertEqual(bounded_ranges.pdf.page_ranges, "1-25,20-50")

        single_page = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "pdf",
                "pdf": {
                    "mode": "single_page",
                    "paper_size": "A6",
                    "margins": {"top": 0, "right": 3, "bottom": 0, "left": 3},
                },
            }
        )
        self.assertEqual(single_page.pdf.paper_size.value, "A6")

    def test_viewport_width_can_be_preserved_for_full_page_images(self):
        request = RenderRequest.model_validate(
            {"url": "https://example.com", "preserve_viewport_width": True}
        )
        self.assertTrue(request.preserve_viewport_width)
        with self.assertRaises(ValidationError):
            RenderRequest.model_validate(
                {
                    "url": "https://example.com",
                    "output": "pdf",
                    "full_page": True,
                    "preserve_viewport_width": True,
                }
            )

    def test_captcha_capture_can_be_explicitly_requested(self):
        request = RenderRequest.model_validate(
            {"url": "https://example.com", "proceed_on_captcha": True}
        )
        self.assertTrue(request.proceed_on_captcha)

    def test_exactly_one_source_payload_is_required(self):
        with self.assertRaises(ValidationError):
            RenderRequest.model_validate(
                {
                    "url": "https://example.com",
                    "html": "<p>x</p>",
                    "output": "png",
                }
            )

    def test_hosted_sources_and_outputs_are_explicit(self):
        for source in (
            {"url": "https://example.com"},
            {"html": "<main>Hello</main>", "base_url": "https://example.com"},
            {"markdown": "# Hello", "base_url": "https://example.com"},
        ):
            request = RenderRequest.model_validate(source)
            self.assertIn(request.source_type, {"url", "html", "markdown"})
        for output in OutputFormat:
            request = RenderRequest.model_validate(
                {"url": "https://example.com", "output": output.value}
            )
            self.assertEqual(request.credit_cost, 2 if output is OutputFormat.PDF else 1)

    def test_base_url_and_source_size_are_bounded(self):
        with self.assertRaises(ValidationError):
            RenderRequest.model_validate(
                {"url": "https://example.com", "base_url": "https://example.org"}
            )
        with self.assertRaises(ValidationError):
            RenderRequest.model_validate({"html": "x" * (5 * 1024 * 1024 + 1)})

    def test_selector_image_pdf_and_extraction_conflicts_are_rejected(self):
        invalid = (
            {"url": "https://example.com", "selector": "main"},
            {
                "url": "https://example.com",
                "selector": "main",
                "full_page": False,
                "output": "pdf",
            },
            {"url": "https://example.com", "output": "png", "image": {"quality": 90}},
            {
                "url": "https://example.com",
                "output": "jpeg",
                "image": {"transparent_background": True},
            },
            {"url": "https://example.com", "output": "png", "pdf": {"mode": "print"}},
            {"url": "https://example.com", "output": "png", "extract_mode": "article"},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                RenderRequest.model_validate(payload)

    def test_wait_header_and_cleanup_limits_are_enforced(self):
        accepted = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "webp",
                "lazy_load": "adaptive",
                "image": {
                    "quality": 85,
                    "transparent_background": True,
                    "optimize_for_speed": True,
                },
                "headers": {"Authorization": "Bearer local-test"},
                "wait_for": {
                    "event": "networkidle",
                    "selector": "main",
                    "text": "Ready",
                    "delay_ms": 15000,
                    "timeout_ms": 30000,
                },
                "cleanup": {"consent_mode": "reject", "block_ads": True},
            }
        )
        self.assertEqual(accepted.headers["Authorization"], "Bearer local-test")
        self.assertEqual(accepted.lazy_load, LazyLoadMode.ADAPTIVE)
        self.assertTrue(accepted.image.optimize_for_speed)
        for headers in (
            {"Host": "evil.example"},
            {"X-Forwarded-For": "127.0.0.1"},
            {"X-Test": "line\nfeed"},
            {f"X-{index}": "x" for index in range(33)},
        ):
            with self.subTest(headers=headers), self.assertRaises(ValidationError):
                RenderRequest.model_validate({"url": "https://example.com", "headers": headers})

    def test_environment_clip_status_css_metadata_and_pack_contracts(self):
        metadata = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "metadata",
                "environment": {
                    "device": "pixel_7",
                    "color_scheme": "dark",
                    "reduced_motion": "reduce",
                    "locale": "en-US",
                    "timezone": "America/New_York",
                },
                "custom_css": "header { display: none }",
                "fail_on_status": [404, 429, 500],
            }
        )
        self.assertEqual(metadata.environment.device, DevicePreset.PIXEL_7)
        self.assertEqual(metadata.credit_cost, 1)
        self.assertEqual(metadata.recorded_output_type, "metadata")

        clip = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "full_page": False,
                "clip": {"x": 10, "y": 20, "width": 300, "height": 200},
            }
        )
        self.assertEqual(clip.clip.width, 300)

        pack = RenderRequest.model_validate(
            {
                "url": "https://example.com",
                "output": "webp",
                "full_page": False,
                "viewports": [
                    {"name": "desktop", "width": 1280, "height": 720},
                    {
                        "name": "mobile",
                        "width": 390,
                        "height": 720,
                        "device": "iphone_14",
                    },
                ],
            }
        )
        self.assertEqual(pack.credit_cost, 2)
        self.assertEqual(pack.recorded_output_type, "zip")

    def test_cookie_domain_and_path_syntax_is_validated(self):
        for cookie in (
            {"name": "x", "value": "1", "domain": "https://example.com"},
            {
                "name": "x",
                "value": "1",
                "domain": "example.com",
                "path": "relative",
            },
        ):
            with self.subTest(cookie=cookie), self.assertRaises(ValidationError):
                RenderRequest.model_validate(
                    {
                        "url": "https://example.com",
                        "network": {"cookies": [cookie]},
                    }
                )

    def test_proxy_requires_a_valid_authority(self):
        for server in (
            "http://",
            "http://proxy.example:bad",
            "http://user:pass@proxy.example",
            "http://proxy.example/path",
        ):
            with self.subTest(server=server), self.assertRaises(ValidationError):
                RenderRequest.model_validate(
                    {
                        "url": "https://example.com",
                        "network": {"proxy": {"server": server}},
                    }
                )

    def test_new_feature_conflicts_and_bounds_are_rejected(self):
        invalid = (
            {
                "url": "https://example.com",
                "full_page": False,
                "selector": "main",
                "clip": {"width": 10, "height": 10},
            },
            {
                "url": "https://example.com",
                "full_page": True,
                "clip": {"width": 10, "height": 10},
            },
            {
                "url": "https://example.com",
                "full_page": False,
                "viewports": [
                    {"name": "same", "width": 10, "height": 10},
                    {"name": "same", "width": 20, "height": 20},
                ],
            },
            {"url": "https://example.com", "fail_on_status": [99]},
            {"url": "https://example.com", "fail_on_status": [404, 404]},
            {"url": "https://example.com", "environment": {"timezone": "Mars/Olympus"}},
            {"url": "https://example.com", "custom_css": "x" * (64 * 1024 + 1)},
            {"url": "https://example.com", "output": "pdf", "cache": True},
            {
                "url": "https://example.com",
                "full_page": False,
                "cache": True,
                "viewports": [
                    {"name": "one", "width": 10, "height": 10},
                    {"name": "two", "width": 20, "height": 20},
                ],
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                RenderRequest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
