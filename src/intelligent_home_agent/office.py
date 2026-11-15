"""Office files turned into what the model can read: Word, Excel and PowerPoint (2007 and
later) and OpenDocument as text; Pages, Numbers and Keynote as the preview image Apple
stores in the file (their content is in a private format). Older binary formats (.doc,
.xls, .ppt) are kept as attachments only.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from typing import Any

from intelligent_home_agent.conversations import Attachment

WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
EXCEL = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
POWERPOINT = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
OPENDOCUMENT = "application/vnd.oasis.opendocument."
IWORK_EXTENSIONS = (".pages", ".numbers", ".key")
IWORK_TYPES = ("application/x-iwork-", "application/vnd.apple.")
IWORK_PREVIEWS = ("preview.jpg", "QuickLook/Thumbnail.jpg", "QuickLook/Preview.jpg")

# An office file is a zip: what it may expand to, and how much text is read from it.
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
MAX_ZIP_RATIO = 200
MAX_FILE_CHARS = 300_000
MAX_SHEET_ROWS = 2_000
MAX_SHEET_COLUMNS = 200

_ODF = "{urn:oasis:names:tc:opendocument:xmlns:%s:1.0}"
_PARAGRAPHS = {_ODF % "text" + "p", _ODF % "text" + "h"}
_ROW = _ODF % "table" + "table-row"
_PAGE = _ODF % "drawing" + "page"


def check_zip(data: bytes) -> None:
    """Refuse a zip that would expand past what a document can hold (a zip bomb)."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        size = sum(info.file_size for info in archive.infolist())
    if size > MAX_UNZIPPED_BYTES or size > MAX_ZIP_RATIO * max(len(data), 1):
        raise ValueError(f"expands to {size} bytes from {len(data)}")


def _cell(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


class _Text:
    """Lines gathered up to MAX_FILE_CHARS: past it, the readers stop reading."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.size = 0

    @property
    def full(self) -> bool:
        return self.size >= MAX_FILE_CHARS

    def add(self, line: str) -> None:
        self.lines.append(line)
        self.size += len(line) + 1

    def __str__(self) -> str:
        return "\n".join(self.lines)


def sheet_text(data: bytes) -> str:
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = _Text()
    try:
        for sheet in book.worksheets:
            out.add(f"## Sheet: {sheet.title}")
            rows = sheet.iter_rows(max_col=MAX_SHEET_COLUMNS, values_only=True)
            for n, row in enumerate(rows):
                if n >= MAX_SHEET_ROWS or out.full:
                    out.add(f"[cut after {n} rows]")
                    break
                cells = [_cell(v) for v in row]
                if any(cells):
                    out.add("\t".join(cells).rstrip())
            if out.full:
                break
    finally:
        book.close()
    return str(out)


def slides_text(data: bytes) -> str:
    from pptx import Presentation

    out = _Text()
    for n, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
        if out.full:
            break
        out.add(f"## Slide {n}")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                out.add(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    out.add("\t".join(_cell(c.text) for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            out.add(f"[Notes] {slide.notes_slide.notes_text_frame.text.strip()}")
    return str(out)


def document_text(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    out = _Text()
    for paragraph in doc.paragraphs:
        if out.full:
            return str(out)
        if paragraph.text.strip():
            out.add(paragraph.text)
    for table in doc.tables:
        for row in table.rows:
            if out.full:
                return str(out)
            out.add("\t".join(_cell(c.text) for c in row.cells))
    return str(out)


_READERS: dict[str, Callable[[bytes], str]] = {
    WORD: document_text,
    EXCEL: sheet_text,
    POWERPOINT: slides_text,
}


def _extension(file: Attachment) -> str:
    return "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""


def is_iwork(file: Attachment) -> bool:
    return file.mimetype.startswith(IWORK_TYPES) or _extension(file) in IWORK_EXTENSIONS


def office_text(file: Attachment) -> str | None:
    """The file's text, or None when it is no office format with a reader."""
    reader = _READERS.get(file.mimetype)
    if reader is None and not file.mimetype.startswith(OPENDOCUMENT):
        return None
    check_zip(file.data)
    return reader(file.data) if reader else _opendocument_text(file.data)


def iwork_preview(file: Attachment) -> bytes | None:
    """The JPEG preview of a Pages, Numbers or Keynote file, when it carries one."""
    check_zip(file.data)
    with zipfile.ZipFile(io.BytesIO(file.data)) as archive:
        names = set(archive.namelist())
        for name in IWORK_PREVIEWS:
            if name in names:
                return archive.read(name)
    return None


def _opendocument_text(data: bytes) -> str:
    from defusedxml.ElementTree import fromstring

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = fromstring(archive.read("content.xml"))
    lines: list[str] = []
    slides = 0

    def walk(element: Any) -> None:
        nonlocal slides
        if element.tag == _PAGE:
            slides += 1
            lines.append(f"## Slide {slides}")
        if element.tag == _ROW:
            cells = [" ".join("".join(cell.itertext()).split()) for cell in element]
            if any(cells):
                lines.append("\t".join(cells).rstrip())
            return
        if element.tag in _PARAGRAPHS:
            text = "".join(element.itertext()).strip()
            if text:
                lines.append(text)
            return
        for child in element:
            walk(child)

    walk(root)
    return "\n".join(lines)
