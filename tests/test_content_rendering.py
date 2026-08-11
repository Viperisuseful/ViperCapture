import asyncio
import threading
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError
from pypdf import PdfWriter

from vipercapture.content_rendering import (
    MAX_PRINT_PAGES,
    _validate_pdf,
    input_document,
    render_document_output,
)
from vipercapture.render_contract import RenderRequest
from vipercapture.render_engine import RenderLimits
from vipercapture.render_errors import RenderError

LIMITS = RenderLimits(7680, 4320, 50_000_000)


def pdf_with_pages(count):
    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class FakePage:
    def __init__(
        self,
        *,
        html="<html><body><main>Hello document body with useful words.</main></body></html>",
        pdf=None,
        width=800,
        height=900,
        forced_breaks=0,
    ):
        self.html = html
        self.pdf_bytes = pdf or pdf_with_pages(1)
        self.width = width
        self.height = height
        self.forced_breaks = forced_breaks
        self.pdf_options = None
        self.emulated_media = None

    async def content(self):
        return self.html

    async def evaluate(self, script):
        if "TextEncoder" in script:
            return len(self.html.encode("utf-8"))
        return {
            "width": self.width,
            "height": self.height,
            "forcedBreaks": self.forced_breaks,
        }

    async def pdf(self, **options):
        self.pdf_options = options
        return self.pdf_bytes

    async def emulate_media(self, *, media):
        self.emulated_media = media


class ContentRenderingTest(unittest.IsolatedAsyncioTestCase):
    async def test_document_transport_failure_is_not_reclassified(self):
        page = FakePage()
        page.evaluate = AsyncMock(side_effect=PlaywrightError("page closed"))

        with self.assertRaises(PlaywrightError):
            await render_document_output(
                page,
                RenderRequest(url="https://example.com", output="html"),
                LIMITS,
            )

    async def test_cancelled_markdown_conversion_settles_worker(self):
        started = threading.Event()
        release = threading.Event()

        def convert(*_args, **_kwargs):
            started.set()
            release.wait()
            return "markdown"

        request = RenderRequest(
            url="https://example.com",
            output="markdown",
        )
        with patch("vipercapture.content_rendering.markdownify", side_effect=convert):
            task = asyncio.create_task(
                render_document_output(FakePage(), request, LIMITS)
            )
            await asyncio.to_thread(started.wait)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

    def test_html_input_is_wrapped_with_utf8_and_escaped_base(self):
        request = RenderRequest.model_validate({
            "html": "<h1>Hello</h1>",
            "base_url": "https://example.com/a?x=1&y=2",
        })
        document = input_document(request)
        self.assertIn('charset="utf-8"', document)
        self.assertIn("https://example.com/a?x=1&amp;y=2", document)
        self.assertIn("<h1>Hello</h1>", document)

    def test_markdown_allows_approved_embedded_html(self):
        request = RenderRequest.model_validate({"markdown": "# Title\n\n<strong>Raw</strong>"})
        document = input_document(request)
        self.assertIn("<h1>Title</h1>", document)
        self.assertIn("<strong>Raw</strong>", document)

    async def test_document_html_and_markdown_outputs(self):
        page = FakePage()
        html = await render_document_output(
            page, RenderRequest.model_validate({"url": "https://example.com", "output": "html"}), LIMITS
        )
        markdown = await render_document_output(
            page, RenderRequest.model_validate({"url": "https://example.com", "output": "markdown"}), LIMITS
        )
        self.assertEqual(html.media_type, "text/html; charset=utf-8")
        self.assertIn(b"Hello document", html.body)
        self.assertEqual(markdown.media_type, "text/markdown; charset=utf-8")
        self.assertIn(b"Hello document", markdown.body)

    async def test_open_shadow_dom_is_serialized(self):
        page = FakePage()
        page.content = AsyncMock(side_effect=AssertionError("content must not load"))
        page.evaluate = AsyncMock(
            side_effect=[
                len(page.html.encode("utf-8")),
                len(page.html.encode("utf-8")) + 100,
                '<html><body><x-card><template shadowrootmode="open"><p>Inside</p></template></x-card></body></html>',
            ]
        )
        artifact = await render_document_output(
            page,
            RenderRequest.model_validate(
                {
                    "url": "https://example.com",
                    "output": "html",
                    "include_shadow_dom": True,
                }
            ),
            LIMITS,
        )
        self.assertIn(b'template shadowrootmode="open"', artifact.body)
        self.assertTrue(
            all(
                "new XMLSerializer().serializeToString(document.doctype)" in call.args[0]
                for call in (page.evaluate.await_args_list[0], page.evaluate.await_args_list[2])
            )
        )
        page.content.assert_not_awaited()

    async def test_open_shadow_dom_is_bounded_before_serialization(self):
        page = FakePage()
        page.content = AsyncMock(side_effect=AssertionError("content must not load"))
        page.evaluate = AsyncMock(side_effect=[50, 101])
        with self.assertRaises(RenderError) as raised:
            await render_document_output(
                page,
                RenderRequest.model_validate(
                    {
                        "url": "https://example.com",
                        "output": "html",
                        "include_shadow_dom": True,
                    }
                ),
                RenderLimits(output_bytes=100),
            )
        self.assertEqual(raised.exception.code, "output_too_large")
        self.assertEqual(page.evaluate.await_count, 2)
        page.content.assert_not_awaited()

    async def test_markdown_hydrated_dom_size_is_bounded(self):
        page = FakePage(html="x" * (5 * 1024 * 1024 + 1))
        with self.assertRaises(RenderError) as raised:
            await render_document_output(
                page,
                RenderRequest.model_validate(
                    {"url": "https://example.com", "output": "markdown"}
                ),
                LIMITS,
            )
        self.assertEqual(raised.exception.code, "document_too_large")

    async def test_html_preflight_avoids_materializing_oversized_dom(self):
        page = FakePage(html="x" * 101)
        page.content = AsyncMock(side_effect=AssertionError("content must not load"))
        with self.assertRaises(RenderError) as raised:
            await render_document_output(
                page,
                RenderRequest(url="https://example.com", output="html"),
                RenderLimits(output_bytes=100),
            )
        self.assertEqual(raised.exception.code, "output_too_large")
        page.content.assert_not_awaited()

    async def test_article_html_and_markdown_outputs(self):
        body = " ".join(["A substantial article sentence."] * 20)
        page = FakePage(html=f"<html><body><nav>Menu</nav><article><h1>Title</h1><p>{body}</p></article></body></html>")
        for output in ("html", "markdown"):
            artifact = await render_document_output(
                page,
                RenderRequest.model_validate({
                    "url": "https://example.com", "output": output, "extract_mode": "article"
                }),
                LIMITS,
            )
            self.assertIn(b"substantial article", artifact.body)

    async def test_article_not_found_is_typed(self):
        with self.assertRaises(RenderError) as raised:
            await render_document_output(
                FakePage(html="<html><body>Hi</body></html>"),
                RenderRequest.model_validate({
                    "url": "https://example.com", "output": "html", "extract_mode": "article"
                }),
                LIMITS,
            )
        self.assertIn(raised.exception.code, {"article_not_found", "article_parse_failed"})

    async def test_print_and_single_page_pdf(self):
        print_page = FakePage()
        with patch(
            "vipercapture.render_engine.asyncio.to_thread",
            new_callable=AsyncMock,
            wraps=asyncio.to_thread,
        ) as to_thread:
            printed = await render_document_output(
                print_page,
                RenderRequest.model_validate({"url": "https://example.com", "output": "pdf"}),
                LIMITS,
            )
        self.assertTrue(
            any(call.args and call.args[0] is _validate_pdf for call in to_thread.await_args_list)
        )
        self.assertEqual(printed.media_type, "application/pdf")
        self.assertEqual(print_page.pdf_options["format"], "A4")
        self.assertEqual(print_page.pdf_options["page_ranges"], "1-51")
        self.assertEqual(print_page.emulated_media, "print")

        custom_print = FakePage()
        await render_document_output(
            custom_print,
            RenderRequest.model_validate(
                {
                    "url": "https://example.com",
                    "output": "pdf",
                    "pdf": {
                        "paper_size": "Legal",
                        "page_ranges": "2-3",
                        "header_template": '<span class="title"></span>',
                    },
                }
            ),
            LIMITS,
        )
        self.assertEqual(custom_print.pdf_options["format"], "Legal")
        self.assertEqual(custom_print.pdf_options["page_ranges"], "2-3")
        self.assertTrue(custom_print.pdf_options["display_header_footer"])

        single_page = FakePage()
        single = await render_document_output(
            single_page,
            RenderRequest.model_validate({
                "url": "https://example.com", "output": "pdf", "pdf": {"mode": "single_page"}
            }),
            LIMITS,
        )
        self.assertEqual(single.metadata["pages"], 1)
        self.assertEqual(single_page.pdf_options["page_ranges"], "1")
        self.assertEqual(single_page.pdf_options["width"], "877px")
        self.assertEqual(single_page.pdf_options["height"], "977px")
        self.assertEqual(single_page.emulated_media, "print")

        landscape = FakePage()
        await render_document_output(
            landscape,
            RenderRequest.model_validate(
                {
                    "url": "https://example.com",
                    "output": "pdf",
                    "pdf": {
                        "mode": "single_page",
                        "orientation": "landscape",
                    },
                }
            ),
            LIMITS,
        )
        self.assertFalse(landscape.pdf_options["landscape"])

    async def test_pdf_page_and_height_limits(self):
        with self.assertRaises(RenderError) as pages:
            await render_document_output(
                FakePage(pdf=pdf_with_pages(MAX_PRINT_PAGES + 1)),
                RenderRequest.model_validate({"url": "https://example.com", "output": "pdf"}),
                LIMITS,
            )
        self.assertEqual(pages.exception.code, "pdf_page_limit_exceeded")
        with self.assertRaises(RenderError) as height:
            await render_document_output(
                FakePage(height=20_001),
                RenderRequest.model_validate({
                    "url": "https://example.com", "output": "pdf", "pdf": {"mode": "single_page"}
                }),
                LIMITS,
            )
        self.assertEqual(height.exception.code, "pdf_page_too_tall")

        with self.assertRaises(RenderError) as width:
            await render_document_output(
                FakePage(width=20_001),
                RenderRequest.model_validate({
                    "url": "https://example.com", "output": "pdf", "pdf": {"mode": "single_page"}
                }),
                LIMITS,
            )
        self.assertEqual(width.exception.code, "pdf_page_too_wide")

        with self.assertRaises(RenderError) as area:
            await render_document_output(
                FakePage(width=10_000, height=10_000),
                RenderRequest.model_validate({
                    "url": "https://example.com", "output": "pdf", "pdf": {"mode": "single_page"}
                }),
                LIMITS,
            )
        self.assertEqual(area.exception.code, "pdf_area_limit_exceeded")

        with self.assertRaises(RenderError) as margin_area:
            await render_document_output(
                FakePage(width=19_000, height=2_500),
                RenderRequest.model_validate(
                    {
                        "url": "https://example.com",
                        "output": "pdf",
                        "pdf": {
                            "mode": "single_page",
                            "margins": {
                                "top": 4,
                                "right": 4,
                                "bottom": 4,
                                "left": 4,
                            },
                        },
                    }
                ),
                LIMITS,
            )
        self.assertEqual(
            margin_area.exception.code, "pdf_area_limit_exceeded"
        )

        print_page = FakePage(height=100_000)
        with self.assertRaises(RenderError) as preflight:
            await render_document_output(
                print_page,
                RenderRequest.model_validate(
                    {"url": "https://example.com", "output": "pdf"}
                ),
                LIMITS,
            )
        self.assertEqual(preflight.exception.code, "pdf_page_limit_exceeded")
        self.assertIsNone(print_page.pdf_options)

        redundant_breaks = FakePage(
            height=100,
            forced_breaks=98,
            pdf=pdf_with_pages(MAX_PRINT_PAGES),
        )
        fragmented = await render_document_output(
            redundant_breaks,
            RenderRequest.model_validate(
                {"url": "https://example.com", "output": "pdf"}
            ),
            LIMITS,
        )
        self.assertEqual(fragmented.metadata["pages"], MAX_PRINT_PAGES)

    async def test_pdf_range_beyond_document_is_non_retryable(self):
        page = FakePage()
        page.pdf = AsyncMock(
            side_effect=PlaywrightError(
                "Page.pdf: Protocol error (Page.printToPDF): Page range exceeds page count"
            )
        )
        with self.assertRaises(RenderError) as raised:
            await render_document_output(
                page,
                RenderRequest.model_validate(
                    {
                        "url": "https://example.com",
                        "output": "pdf",
                        "pdf": {"page_ranges": "10"},
                    }
                ),
                LIMITS,
            )
        self.assertEqual(raised.exception.code, "pdf_page_range_invalid")
        self.assertFalse(raised.exception.retryable)

    def test_pdf_costs_two_credits(self):
        request = RenderRequest.model_validate({"html": "Hello", "output": "pdf"})
        self.assertEqual(request.credit_cost, 2)


if __name__ == "__main__":
    unittest.main()
