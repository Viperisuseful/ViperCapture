"""HTML, Markdown, extraction, and PDF output helpers."""

from __future__ import annotations

import math
from html import escape
from io import BytesIO

from lxml import html as lxml_html
from markdown_it import MarkdownIt
from markdownify import markdownify
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .render_contract import ExtractMode, OutputFormat, PdfMode, RenderRequest
from .render_engine import (
    PlaywrightError,
    RenderArtifact,
    RenderLimits,
    _settled_thread,
)
from .render_errors import RenderError

MAX_PRINT_PAGES = 50
MAX_SINGLE_PAGE_WIDTH = 20_000
MAX_SINGLE_PAGE_HEIGHT = 20_000
MAX_MARKDOWN_HTML_BYTES = 5 * 1024 * 1024
PAPER_INCHES = {
    "A0": (33.1, 46.8),
    "A1": (23.4, 33.1),
    "A2": (16.5, 23.4),
    "A3": (11.7, 16.5),
    "A4": (8.27, 11.69),
    "A5": (5.83, 8.27),
    "A6": (4.13, 5.83),
    "Legal": (8.5, 14.0),
    "Letter": (8.5, 11.0),
    "Tabloid": (11.0, 17.0),
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


async def _render_pdf(
    page, request: RenderRequest, limits: RenderLimits
) -> RenderArtifact:
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
    media = request.environment.media.value if request.environment.media else "print"
    if options.header_template is not None or options.footer_template is not None:
        common.update(
            {
                "display_header_footer": True,
                "header_template": options.header_template or "<span></span>",
                "footer_template": options.footer_template or "<span></span>",
            }
        )
    single_page = options.mode is PdfMode.SINGLE_PAGE
    if single_page:
        common["landscape"] = False
        await page.emulate_media(media=media)
        dimensions = await page.evaluate("""() => ({
            width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
            height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)
        })""")
        width = max(1, math.ceil(float(dimensions["width"])))
        height = max(1, math.ceil(float(dimensions["height"])))
        # Playwright treats width and height as the entire sheet. Include the
        # margins so the measured document remains the printable area instead
        # of silently overflowing past page one.
        sheet_width = width + math.ceil(
            (options.margins.left + options.margins.right) * 96
        )
        sheet_height = height + math.ceil(
            (options.margins.top + options.margins.bottom) * 96
        )
        if sheet_width > MAX_SINGLE_PAGE_WIDTH:
            raise RenderError(
                "pdf_page_too_wide",
                "Single-page PDF content exceeds 20000 CSS pixels.",
                413,
                False,
                {"max_width": MAX_SINGLE_PAGE_WIDTH},
            )
        if sheet_height > MAX_SINGLE_PAGE_HEIGHT:
            raise RenderError(
                "pdf_page_too_tall",
                "Single-page PDF content exceeds 20000 CSS pixels.",
                413,
                False,
                {"max_height": MAX_SINGLE_PAGE_HEIGHT},
            )
        if sheet_width * sheet_height > limits.max_pixels:
            raise RenderError(
                "pdf_area_limit_exceeded",
                "Single-page PDF content exceeds the pixel area limit.",
                413,
                False,
                {"max_pixels": limits.max_pixels},
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
        common["page_ranges"] = options.page_ranges or f"1-{MAX_PRINT_PAGES + 1}"
        await page.emulate_media(media=media)
        dimensions = await page.evaluate("""() => {
            return {
                width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
                height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)
            };
        }""")
        _paper_width, paper_height = PAPER_INCHES[options.paper_size.value]
        if options.orientation.value == "landscape":
            paper_height = _paper_width
        printable_height = max(
            1,
            (paper_height - options.margins.top - options.margins.bottom) * 96,
        )
        estimated_pages = math.ceil(
            max(1, float(dimensions["height"])) / printable_height
        )
        if options.page_ranges is None and estimated_pages > MAX_PRINT_PAGES:
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
    try:
        pdf = await page.pdf(**common)
    except PlaywrightError as exc:
        if options.page_ranges is not None and "page range exceeds page count" in str(exc).lower():
            raise RenderError(
                "pdf_page_range_invalid",
                "The requested PDF pages are outside the rendered document.",
                422,
                False,
            ) from exc
        raise
    pages = await _settled_thread(
        _validate_pdf, pdf, single_page=single_page
    )
    return RenderArtifact(pdf, "application/pdf", "vipercapture.pdf", {"pages": pages})


async def render_document_output(
    page,
    request: RenderRequest,
    limits: RenderLimits,
) -> RenderArtifact:
    if request.output is OutputFormat.PDF:
        return await _render_pdf(page, request, limits)
    if request.output not in {OutputFormat.HTML, OutputFormat.MARKDOWN}:
        raise RenderError(
            "unsupported_output", "The output format is not supported.", 422, False
        )

    try:
        maximum_html_bytes = (
            limits.output_bytes
            if request.output is OutputFormat.HTML
            else MAX_MARKDOWN_HTML_BYTES
        )
        serialized_bytes = await page.evaluate(
            """() => {
                const doctype = document.doctype
                    ? new XMLSerializer().serializeToString(document.doctype)
                    : "";
                const html = document.documentElement?.outerHTML || "";
                return new TextEncoder().encode(doctype + html).byteLength;
            }"""
        )
        if int(serialized_bytes) > maximum_html_bytes:
            code = "output_too_large" if request.output is OutputFormat.HTML else "document_too_large"
            raise RenderError(
                code,
                "The hydrated document is too large to serialize.",
                413,
                False,
                {"max_bytes": maximum_html_bytes},
            )
        if request.include_shadow_dom:
            serialized_bytes = await page.evaluate(
                """({baseBytes, maxBytes}) => {
                    const encoder = new TextEncoder();
                    let total = baseBytes;
                    const add = (value, expansion = 1) => {
                        total += encoder.encode(value || "").byteLength * expansion;
                    };
                    const countRoot = (root) => {
                        add('<template shadowrootmode="open"></template>');
                        const stack = [];
                        for (const child of root.childNodes) stack.push(child);
                        while (stack.length && total <= maxBytes) {
                            const node = stack.pop();
                            if (node.nodeType === Node.TEXT_NODE) {
                                add(node.data, 6);
                            } else if (node.nodeType === Node.COMMENT_NODE) {
                                add(node.data, 6);
                                total += 7;
                            } else if (node.nodeType === Node.ELEMENT_NODE) {
                                add(node.tagName, 2);
                                total += 5;
                                for (const attribute of node.attributes) {
                                    add(attribute.name);
                                    add(attribute.value, 6);
                                    total += 4;
                                }
                                for (const child of node.childNodes) stack.push(child);
                                if (node.shadowRoot) countRoot(node.shadowRoot);
                            }
                        }
                    };
                    for (const element of document.querySelectorAll('*')) {
                        if (total > maxBytes) break;
                        if (element.shadowRoot) countRoot(element.shadowRoot);
                    }
                    return total;
                }""",
                {
                    "baseBytes": int(serialized_bytes),
                    "maxBytes": maximum_html_bytes,
                },
            )
            if int(serialized_bytes) > maximum_html_bytes:
                code = (
                    "output_too_large"
                    if request.output is OutputFormat.HTML
                    else "document_too_large"
                )
                raise RenderError(
                    code,
                    "The hydrated document is too large to serialize.",
                    413,
                    False,
                    {"max_bytes": maximum_html_bytes},
                )
            document_html = await page.evaluate(
                """() => {
                    const visit = (source, clone) => {
                        for (let index = 0; index < source.children.length; index += 1) {
                            const sourceChild = source.children[index];
                            const cloneChild = clone.children[index];
                            if (!cloneChild) continue;
                            visit(sourceChild, cloneChild);
                            if (sourceChild.shadowRoot) {
                                const template = document.createElement("template");
                                template.setAttribute("shadowrootmode", sourceChild.shadowRoot.mode);
                                template.innerHTML = sourceChild.shadowRoot.innerHTML;
                                visit(sourceChild.shadowRoot, template.content);
                                cloneChild.prepend(template);
                            }
                        }
                    };
                    const clone = document.documentElement.cloneNode(true);
                    visit(document.documentElement, clone);
                    const doctype = document.doctype
                        ? new XMLSerializer().serializeToString(document.doctype)
                        : "";
                    return doctype + clone.outerHTML;
                }"""
            )
        else:
            document_html = await page.content()
        if len(document_html.encode("utf-8")) > maximum_html_bytes:
            code = (
                "output_too_large"
                if request.output is OutputFormat.HTML
                else "document_too_large"
            )
            raise RenderError(
                code,
                "The hydrated document is too large to serialize.",
                413,
                False,
                {"max_bytes": maximum_html_bytes},
            )
        selected_html = (
            await _settled_thread(_article_html, document_html)
            if request.extract_mode is ExtractMode.ARTICLE
            else document_html
        )
        if request.output is OutputFormat.HTML:
            body = selected_html.encode("utf-8")
            return RenderArtifact(body, "text/html; charset=utf-8", "vipercapture.html")
        markdown = await _settled_thread(
            markdownify,
            selected_html,
            heading_style="ATX",
        )
        body = markdown.encode("utf-8")
        return RenderArtifact(body, "text/markdown; charset=utf-8", "vipercapture.md")
    except RenderError:
        raise
    except PlaywrightError:
        raise
    except Exception as exc:
        raise RenderError(
            "document_parse_failed",
            "The rendered document could not be converted.",
            422,
            False,
        ) from exc
