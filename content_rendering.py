"""HTML, Markdown, extraction, and PDF output helpers."""

from __future__ import annotations

import asyncio
from html import escape
from io import BytesIO
import math

from lxml import html as lxml_html
from markdown_it import MarkdownIt
from markdownify import markdownify
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from render_contract import ExtractMode, OutputFormat, PdfMode, RenderRequest
from render_engine import RenderArtifact, RenderLimits
from render_errors import RenderError


MAX_PRINT_PAGES = 50
MAX_SINGLE_PAGE_HEIGHT = 20_000
MAX_MARKDOWN_HTML_BYTES = 5 * 1024 * 1024
PAPER_INCHES = {
    "A4": (8.27, 11.69),
    "Letter": (8.5, 11.0),
}
MARKDOWN = MarkdownIt("commonmark", {"html": True})


def _base_element(request: RenderRequest) -> str:
    if request.base_url is None:
        return ""
    return f'<base href="{escape(str(request.base_url), quote=True)}">'


def input_document(request: RenderRequest) -> str:
    """Return a complete UTF-8 document for caller-supplied content."""
    if request.url is not None:
        raise ValueError("URL inputs are navigated, not converted to a document")
    body = (
        request.html
        if request.html is not None
        else MARKDOWN.render(request.markdown or "")
    )
    base = _base_element(request)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"{base}</head><body>{body}</body></html>"
    )


def _article_html(document_html: str) -> str:
    try:
        from readability import Document

        article = Document(document_html).summary(html_partial=True)
        text = " ".join(lxml_html.fromstring(article).text_content().split())
    except Exception as exc:
        raise RenderError(
            "article_parse_failed", "The article could not be parsed.", 422, False
        ) from exc
    if len(text) < 20:
        raise RenderError(
            "article_not_found", "No readable article was found.", 422, False
        )
    return article


def _validate_pdf(pdf: bytes, *, single_page: bool) -> int:
    try:
        pages = len(PdfReader(BytesIO(pdf)).pages)
    except (PdfReadError, OSError, ValueError) as exc:
        raise RenderError(
            "invalid_pdf", "The renderer produced an invalid PDF.", 502, True
        ) from exc
    maximum = 1 if single_page else MAX_PRINT_PAGES
    if pages < 1 or pages > maximum:
        raise RenderError(
            "pdf_page_limit_exceeded",
            f"The PDF exceeds the {maximum}-page limit.",
            413,
            False,
            {"max_pages": maximum},
        )
    return pages


async def _render_pdf(page, request: RenderRequest) -> RenderArtifact:
    options = request.pdf
    assert options is not None
    common: dict[str, object] = {
        "print_background": options.print_background,
        "landscape": options.orientation.value == "landscape",
        "margin": {
            "top": f"{options.margins.top}in",
            "right": f"{options.margins.right}in",
            "bottom": f"{options.margins.bottom}in",
            "left": f"{options.margins.left}in",
        },
    }
    single_page = options.mode is PdfMode.SINGLE_PAGE
    if single_page:
        common["landscape"] = False
        dimensions = await page.evaluate("""() => ({
            width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
            height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)
        })""")
        width = max(1, math.ceil(float(dimensions["width"])))
        height = max(1, math.ceil(float(dimensions["height"])))
        if height > MAX_SINGLE_PAGE_HEIGHT:
            raise RenderError(
                "pdf_page_too_tall",
                "Single-page PDF content exceeds 20000 CSS pixels.",
                413,
                False,
                {"max_height": MAX_SINGLE_PAGE_HEIGHT},
            )
        # Playwright treats width and height as the entire sheet. Include the
        # margins so the measured document remains the printable area instead
        # of silently overflowing past page one.
        sheet_width = width + math.ceil(
            (options.margins.left + options.margins.right) * 96
        )
        sheet_height = height + math.ceil(
            (options.margins.top + options.margins.bottom) * 96
        )
        common.update(
            {
                "width": f"{sheet_width}px",
                "height": f"{sheet_height}px",
                "page_ranges": "1",
            }
        )
    else:
        common["format"] = options.paper_size.value
        # Render one sentinel page beyond the public limit. Chromium may
        # paginate due to fragmentation rules that scroll-height preflight
        # cannot predict, but never needs to emit the full document.
        common["page_ranges"] = f"1-{MAX_PRINT_PAGES + 1}"
        await page.emulate_media(media="print")
        dimensions = await page.evaluate("""() => {
            const forced = new Set(["always", "page", "left", "right", "recto", "verso"]);
            let forcedBreaks = 0;
            for (const element of document.querySelectorAll("*")) {
                const style = getComputedStyle(element);
                if (forced.has(style.breakBefore) || forced.has(style.breakAfter) ||
                    style.pageBreakBefore === "always" || style.pageBreakAfter === "always") {
                    forcedBreaks += 1;
                }
            }
            return {
                width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
                height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0),
                forcedBreaks
            };
        }""")
        _paper_width, paper_height = PAPER_INCHES[options.paper_size.value]
        if options.orientation.value == "landscape":
            paper_height = _paper_width
        printable_height = max(
            1,
            (paper_height - options.margins.top - options.margins.bottom) * 96,
        )
        estimated_pages = max(
            math.ceil(max(1, float(dimensions["height"])) / printable_height),
            int(dimensions.get("forcedBreaks", 0)) + 1,
        )
        if estimated_pages > MAX_PRINT_PAGES:
            raise RenderError(
                "pdf_page_limit_exceeded",
                f"The PDF exceeds the {MAX_PRINT_PAGES}-page limit.",
                413,
                False,
                {
                    "max_pages": MAX_PRINT_PAGES,
                    "estimated_pages": estimated_pages,
                },
            )
    pdf = await page.pdf(**common)
    pages = _validate_pdf(pdf, single_page=single_page)
    return RenderArtifact(pdf, "application/pdf", "vipercapture.pdf", {"pages": pages})


async def render_document_output(
    page,
    request: RenderRequest,
    limits: RenderLimits,
) -> RenderArtifact:
    del limits
    if request.output is OutputFormat.PDF:
        return await _render_pdf(page, request)
    if request.output not in {OutputFormat.HTML, OutputFormat.MARKDOWN}:
        raise RenderError(
            "unsupported_output", "The output format is not supported.", 422, False
        )

    try:
        document_html = await page.content()
        if (
            request.output is OutputFormat.MARKDOWN
            and len(document_html.encode("utf-8")) > MAX_MARKDOWN_HTML_BYTES
        ):
            raise RenderError(
                "document_too_large",
                "The hydrated document is too large for Markdown conversion.",
                413,
                False,
                {"max_bytes": MAX_MARKDOWN_HTML_BYTES},
            )
        selected_html = (
            await asyncio.to_thread(_article_html, document_html)
            if request.extract_mode is ExtractMode.ARTICLE
            else document_html
        )
        if request.output is OutputFormat.HTML:
            body = selected_html.encode("utf-8")
            return RenderArtifact(body, "text/html; charset=utf-8", "vipercapture.html")
        markdown = await asyncio.to_thread(
            markdownify,
            selected_html,
            heading_style="ATX",
        )
        body = markdown.encode("utf-8")
        return RenderArtifact(body, "text/markdown; charset=utf-8", "vipercapture.md")
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError(
            "document_parse_failed",
            "The rendered document could not be converted.",
            422,
            False,
        ) from exc
