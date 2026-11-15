"""Filling a PDF form the user sent or the agent found.

A PDF with form fields gets them filled. Most official forms are "paper" PDFs without
any: their text comes with positions, dotted leaders and check boxes marked, so the model
can say where to write, and the text and check marks are drawn over the pages. The
filled copy goes back to the model as a document, so it can look at it and fix it.

Needs the ``documents`` extra (fpdf2, pdfminer.six): without it there is no tool.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.documents import FONT_DIR, add_fonts, printable

try:
    from fpdf import FPDF
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar, LTPage, LTTextLine
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.errors import PdfReadError

    INSTALLED = True
except ImportError:  # no documents extra: build_pdf_form_tools returns []
    INSTALLED = False

MAX_PAGES = 20


def too_long(pages: int) -> ToolError:
    """Says the way round, so the model doesn't try the same long PDF again."""
    return ToolError(
        f"Forms of more than {MAX_PAGES} pages are not handled (this one has {pages}). To "
        "read a long document, read_saved_file shows it; to fill or lay out some of its "
        "pages, take them out first with copy_pdf_pages and work on that copy."
    )


WORK_SECONDS = 60
MAX_ITEMS = 1_000
MAX_TEXT_CHARS = 300
LEADER = re.compile(r"[.…_]{3,}")
# Forms draw their boxes with a symbol font, whose glyphs land in the private use area.
BOXES = frozenset("☐☑☒□■▢◻◼❏❐❑❒")


def _is_box(char: str) -> bool:
    return char in BOXES or "" <= char <= ""


def _lines(item: Any) -> list[LTTextLine]:
    if isinstance(item, LTTextLine):
        return [item]
    if hasattr(item, "__iter__"):
        return [line for child in item for line in _lines(child)]
    return []


def _segments(line: LTTextLine, page: LTPage) -> list[dict[str, Any]]:
    """A line cut into text, blanks (dotted leaders) and boxes, each with where it sits
    from the page's top-left corner, in points."""
    chars = [(c.get_text(), c if isinstance(c, LTChar) else None) for c in line]
    text = "".join(c for c, _ in chars)
    kinds = ["box" if _is_box(c) else "text" for c, _ in chars]
    for match in LEADER.finditer(text):
        kinds[match.start() : match.end()] = ["blank"] * (match.end() - match.start())
    segments: list[dict[str, Any]] = []
    start = 0
    for i in range(1, len(chars) + 1):
        if i < len(chars) and kinds[i] == kinds[start] and kinds[i] != "box":
            continue
        boxes = [c for _, c in chars[start:i] if c is not None]
        words = text[start:i].strip()
        if boxes and (words or kinds[start] != "text"):
            x0, x1 = min(b.x0 for b in boxes), max(b.x1 for b in boxes)
            top, bottom = max(b.y1 for b in boxes), min(b.y0 for b in boxes)
            segment = {
                "x": round(x0 - page.x0, 1),
                "y": round(page.y1 - top, 1),
                "w": round(x1 - x0, 1),
                "h": round(top - bottom, 1),
            }
            if kinds[start] == "text":
                segment["text"] = words
            else:
                segment[kinds[start]] = True
            segments.append(segment)
        start = i
    return segments


def read_layout(data: bytes) -> dict[str, Any]:
    try:
        reader = PdfReader(BytesIO(data))
        fields = reader.get_fields() or {}
    except (PdfReadError, ValueError) as exc:
        raise ToolError("This PDF can't be read.") from exc
    if len(reader.pages) > MAX_PAGES:
        raise too_long(len(reader.pages))
    result: dict[str, Any] = {}
    if fields:
        result["fields"] = {
            name: {"type": str(f.get("/FT", "")).lstrip("/"), "value": str(f.get("/V") or "")}
            for name, f in fields.items()
        }
    pages, count = [], 0
    for number, page in enumerate(extract_pages(BytesIO(data), maxpages=MAX_PAGES), start=1):
        if count >= MAX_ITEMS:
            break
        items = [s for line in _lines(page) for s in _segments(line, page)]
        items.sort(key=lambda s: (s["y"], s["x"]))
        items = items[: max(0, MAX_ITEMS - count)]
        count += len(items)
        pages.append(
            {
                "page": number,
                "width": round(page.width),
                "height": round(page.height),
                "items": items,
            }
        )
    result["pages"] = pages
    if count >= MAX_ITEMS:
        result["note"] = "Cut short: too much text."
    return result


def fill(
    data: bytes,
    texts: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    fields: dict[str, str],
    font_dir: Path = FONT_DIR,
) -> bytes:
    try:
        writer = PdfWriter(clone_from=PdfReader(BytesIO(data)))
    except (PdfReadError, ValueError) as exc:
        raise ToolError("This PDF can't be read.") from exc
    if fields and "/AcroForm" not in writer.root_object:
        raise ToolError("This PDF has no form fields: write texts and checks instead.")
    if fields:
        for page in writer.pages:
            writer.update_page_form_field_values(page, fields, auto_regenerate=False)
        writer.set_need_appearances_writer(True)
    marks = [(t, t["text"], False) for t in texts] + [(c, "X", True) for c in checks]
    for number, page in enumerate(writer.pages, start=1):
        here = [m for m in marks if int(m[0]["page"]) == number]
        if not here:
            continue
        box = page.mediabox
        overlay = FPDF(unit="pt", format=(float(box.width), float(box.height)))
        overlay.set_auto_page_break(False)
        family = add_fonts(overlay, font_dir)
        overlay.add_page()
        for mark, text, bold in here:
            size = float(mark.get("size") or 10)
            overlay.set_font(family, "B" if bold else "", size)
            # The model gives the top-left corner, as read_pdf_layout does; fpdf wants the
            # baseline.
            overlay.text(float(mark["x"]), float(mark["y"]) + size * 0.8, printable(text, family))
        drawn = PdfReader(BytesIO(bytes(overlay.output()))).pages[0]
        page.merge_transformed_page(
            drawn, Transformation().translate(float(box.left), float(box.bottom))
        )
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def page_count(data: bytes) -> int:
    try:
        pages = len(PdfReader(BytesIO(data)).pages)
    except (PdfReadError, ValueError) as exc:
        raise ToolError("This PDF can't be read.") from exc
    if pages > MAX_PAGES:
        raise too_long(pages)
    return pages


async def off_loop[T](work: Callable[..., T], *args: Any) -> T:
    """PDFs come from anyone: parse them away from the event loop, and give up on one
    that takes too long rather than keep the chat waiting."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(work, *args), WORK_SECONDS)
    except TimeoutError as exc:
        raise ToolError("This PDF takes too long to process.") from exc


def kept_pdf(ctx: ToolContext, name: str) -> Attachment:
    file = ctx.conversation.files.get(name)
    if file is None:
        raise ToolError(f"No saved file named {name}: see list_saved_files.")
    if file.mimetype != "application/pdf":
        raise ToolError(f"{name} is not a PDF.")
    return file


def _marks(args: dict[str, Any], key: str, pages: int, need_text: bool) -> list[dict[str, Any]]:
    marks = args.get(key) or []
    if not isinstance(marks, list) or len(marks) > MAX_ITEMS:
        raise ToolError(f"{key} must be a list of at most {MAX_ITEMS} items.")
    for mark in marks:
        try:
            page, x, y = int(mark["page"]), float(mark["x"]), float(mark["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError(f"Each of {key} needs page, x and y.") from exc
        if not 1 <= page <= pages or x < 0 or y < 0:
            raise ToolError(f"{key}: page {page} at {x},{y} is outside the document.")
        if need_text and not 0 < len(str(mark.get("text") or "")) <= MAX_TEXT_CHARS:
            raise ToolError(f"Each of {key} needs a text of 1 to {MAX_TEXT_CHARS} characters.")
    return marks


def build_pdf_form_tools(font_dir: Path = FONT_DIR, *, language: str = "en") -> list[Tool]:
    if not INSTALLED:
        return []
    suffix = strings(language).filled_suffix

    async def read_pdf_layout(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await off_loop(read_layout, kept_pdf(ctx, str(args.get("file_name") or "")).data)

    async def fill_pdf(args: dict[str, Any], ctx: ToolContext) -> Attachment:
        source = kept_pdf(ctx, str(args.get("file_name") or ""))
        pages = await off_loop(page_count, source.data)
        texts = _marks(args, "texts", pages, need_text=True)
        checks = _marks(args, "checks", pages, need_text=False)
        fields = {str(k): str(v) for k, v in (args.get("fields") or {}).items()}
        if not (texts or checks or fields):
            raise ToolError("Give texts, checks or fields to fill.")
        name = (
            str(args.get("output_name") or "").strip() or f"{Path(source.filename).stem} ({suffix})"
        )
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        data = await off_loop(fill, source.data, texts, checks, fields, font_dir)
        file = ctx.conversation.keep_file(Attachment(name, "application/pdf", data, "agent"))
        if args.get("send"):
            ctx.actions.append({"type": "share_file", "name": file.filename})
        return file

    mark = {
        "page": {"type": "integer", "description": "From 1."},
        "x": {"type": "number", "description": "Points from the page's left edge."},
        "y": {"type": "number", "description": "Points from the page's top edge."},
        "size": {"type": "number", "description": "Font size, default 10."},
    }
    return [
        Tool(
            name="read_pdf_layout",
            description=(
                "Read a kept PDF form (see list_saved_files) to fill it: its form fields if "
                "it has any, else each page's text with where it sits (x, y from the top-left "
                "corner, w, h, in points), the dotted blanks to write on (blank) and the "
                f"check boxes (box). At most {MAX_PAGES} pages: not for reading a long "
                "document (a contract, a loan offer), which read_saved_file shows."
            ),
            input_schema={
                "type": "object",
                "properties": {"file_name": {"type": "string"}},
                "required": ["file_name"],
            },
            handler=read_pdf_layout,
            taints=True,
        ),
        Tool(
            name="fill_pdf",
            description=(
                "Fill a kept PDF form into a new kept PDF, the original unchanged: `fields` "
                "for a PDF with form fields, else `texts` written at x, y (the top-left of "
                "the text: for a blank, its x + 3 and its y - 3) and `checks` (an X on a "
                "box: its x + 1.5 and y + 0.5). You get the result back: look at it and fill again "
                "with the same output_name to fix it. send: true once it is right, to send "
                "it to the user. Never sign for the user."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_name": {"type": "string"},
                    "output_name": {"type": "string", "description": "e.g. Form filled.pdf"},
                    "texts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {**mark, "text": {"type": "string"}},
                            "required": ["page", "x", "y", "text"],
                        },
                    },
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": mark,
                            "required": ["page", "x", "y"],
                        },
                    },
                    "fields": {"type": "object", "description": "Form field name: value."},
                    "send": {"type": "boolean"},
                },
                "required": ["file_name", "output_name"],
            },
            handler=fill_pdf,
            # The filled copy comes back whole, the form's own text included.
            taints=True,
        ),
    ]
