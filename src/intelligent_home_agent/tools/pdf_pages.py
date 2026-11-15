"""New PDFs made from the pages of kept ones: a page taken out of a form, pages dropped or
put in another order, or several PDFs merged into one. The PDFs they come from never change.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

from intelligent_home_agent.conversations import Attachment, safe_filename
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.pdf_forms import kept_pdf, off_loop

MAX_PARTS = 20
MAX_PAGES = 200


def page_numbers(spec: Any, count: int, room: int = MAX_PAGES) -> list[int]:
    """'1-3, 5' as page indexes, in the order given; every page when empty. Refused past
    `room` pages, before the ranges are spelled out."""
    if isinstance(spec, list):
        spec = ",".join(str(n) for n in spec)
    spec = str(spec or "").replace(" ", "")
    if not spec:
        spec = f"1-{count}"
    indexes: list[int] = []
    for part in spec.split(","):
        first, _, last = part.partition("-")
        try:
            start, end = int(first), int(last or first)
        except ValueError as exc:
            raise ToolError(f"Pages read like 2 or 1-3,5, not {spec}.") from exc
        for n in (start, end):
            if not 1 <= n <= count:
                raise ToolError(f"There is no page {n} in a document of {count} pages.")
        if len(indexes) + abs(end - start) + 1 > room:
            raise ToolError(f"A PDF of more than {MAX_PAGES} pages is not made.")
        step = 1 if end >= start else -1
        indexes.extend(range(start - 1, end - 1 + step, step))
    return indexes


def assemble(parts: list[tuple[str, bytes, Any]]) -> tuple[bytes, int]:
    writer = PdfWriter()
    for name, data, spec in parts:
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ToolError(f"{name} is protected by a password.")
            indexes = page_numbers(spec, len(reader.pages), MAX_PAGES - len(writer.pages))
            writer.append(reader, pages=indexes, import_outline=False)
        except (PyPdfError, ValueError) as exc:
            raise ToolError(f"{name} can't be read as a PDF.") from exc
    out = BytesIO()
    writer.write(out)
    return out.getvalue(), len(writer.pages)


def build_pdf_page_tools() -> list[Tool]:
    async def copy_pdf_pages(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        parts = args.get("parts")
        if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
            raise ToolError(f"parts must list 1 to {MAX_PARTS} PDFs.")
        if not all(isinstance(p, dict) for p in parts):
            raise ToolError("Each of parts needs a file_name.")
        sources = [(kept_pdf(ctx, str(p.get("file_name") or "")), p.get("pages")) for p in parts]
        name = str(args.get("output_name") or "").strip()
        if not name:
            raise ToolError("output_name is required.")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        if safe_filename(name) in {source.filename for source, _ in sources}:
            raise ToolError("Give the new PDF another name: the original stays as it is.")
        data, pages = await off_loop(
            assemble, [(source.filename, source.data, spec) for source, spec in sources]
        )
        file = ctx.conversation.keep_file(Attachment(name, "application/pdf", data, "agent"))
        if args.get("send"):
            ctx.actions.append({"type": "share_file", "name": file.filename})
        return {"created": file.filename, "pages": pages, "size_kb": round(len(data) / 1024)}

    return [
        Tool(
            name="copy_pdf_pages",
            description=(
                "Make a new kept PDF from pages of kept PDFs (see list_saved_files), the "
                "originals unchanged: take one or a few pages out of a PDF, drop pages (list "
                "the ones to keep) or reorder them, or merge several PDFs into one, in the "
                "order of parts. It is kept by its name for the tools that attach files. "
                "send: true to send it to the user."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "parts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_name": {"type": "string"},
                                "pages": {
                                    "type": "string",
                                    "description": "From 1, e.g. 2 or 1-3,5 or 3,1,2; "
                                    "empty for all of them.",
                                },
                            },
                            "required": ["file_name"],
                        },
                    },
                    "output_name": {"type": "string", "description": "e.g. Statement p2.pdf"},
                    "send": {"type": "boolean"},
                },
                "required": ["parts", "output_name"],
            },
            handler=copy_pdf_pages,
        ),
    ]
