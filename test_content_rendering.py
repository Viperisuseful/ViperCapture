import unittest
from io import BytesIO

from pypdf import PdfWriter

from content_rendering import (
    MAX_PRINT_PAGES,
    input_document,
    render_document_output,
)
from render_contract import RenderRequest
from render_engine import RenderLimits
from render_errors import RenderError


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
        height=900,
        forced_breaks=0,
    ):
        self.html = html
        self.pdf_bytes = pdf or pdf_with_pages(1)
        self.height = height
        self.forced_breaks = forced_breaks
        self.pdf_options = None
        self.emulated_media = None

    async def content(self):
        return self.html

    async def evaluate(self, _script):
        return {
            "width": 800,
            "height": self.height,
            "forcedBreaks": self.forced_breaks,
        }

    async def pdf(self, **options):
        self.pdf_options = options
        return self.pdf_bytes

    async def emulate_media(self, *, media):
        self.emulated_media = media


class ContentRenderingTest(unittest.IsolatedAsyncioTestCase):
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
        printed = await render_document_output(
            print_page,
            RenderRequest.model_validate({"url": "https://example.com", "output": "pdf"}),
            LIMITS,
        )
        self.assertEqual(printed.media_type, "application/pdf")
        self.assertEqual(print_page.pdf_options["format"], "A4")
        self.assertEqual(print_page.pdf_options["page_ranges"], "1-51")
        self.assertEqual(print_page.emulated_media, "print")

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

        forced_breaks = FakePage(height=100, forced_breaks=50)
        with self.assertRaises(RenderError) as fragmented:
            await render_document_output(
                forced_breaks,
                RenderRequest.model_validate(
                    {"url": "https://example.com", "output": "pdf"}
                ),
                LIMITS,
            )
        self.assertEqual(fragmented.exception.code, "pdf_page_limit_exceeded")
        self.assertIsNone(forced_breaks.pdf_options)

    def test_pdf_costs_two_credits(self):
        request = RenderRequest.model_validate({"html": "Hello", "output": "pdf"})
        self.assertEqual(request.credit_cost, 2)


if __name__ == "__main__":
    unittest.main()
