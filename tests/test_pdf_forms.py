from io import BytesIO
from pathlib import Path

import pytest
from fpdf import FPDF
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextLine

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.tools import ToolError, build_pdf_form_tools, pdf_forms, pdf_pages
from intelligent_home_agent.tools.pdf_forms import _is_box, read_layout

NO_FONTS = Path("/nonexistent")


def paper_form() -> bytes:
    pdf = FPDF(unit="pt", format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    pdf.text(60, 100, "Name ........................ Surname ........................")
    pdf.text(60, 130, "Town ......................")
    pdf.add_page()
    pdf.text(60, 100, "Signed at ..................")
    return bytes(pdf.output())


def many_pages(n: int) -> bytes:
    pdf = FPDF(unit="pt", format="A4")
    for _ in range(n):
        pdf.add_page()
    return bytes(pdf.output())


def tools(**kwargs):
    return {t.name: t for t in build_pdf_form_tools(NO_FONTS, **kwargs)}


def lines(data: bytes) -> list[tuple[int, float, float, str]]:
    found = []
    for number, page in enumerate(extract_pages(BytesIO(data)), start=1):
        for box in page:
            for line in box if hasattr(box, "__iter__") else []:
                if isinstance(line, LTTextLine):
                    found.append((number, line.x0, page.y1 - line.y1, line.get_text().strip()))
    return found


def test_a_paper_form_reads_as_text_blanks_and_where_they_sit():
    layout = read_layout(paper_form())

    first = layout["pages"][0]
    assert "fields" not in layout
    assert (first["width"], first["height"]) == (595, 842)
    kinds = [("blank" if s.get("blank") else s["text"]) for s in first["items"]]
    assert kinds == ["Name", "blank", "Surname", "blank", "Town", "blank"]
    name, blank = first["items"][:2]
    assert name["x"] == pytest.approx(60, abs=1)
    assert name["y"] == pytest.approx(92, abs=2)  # the line's top, baseline at 100
    assert blank["x"] > name["x"] + name["w"] - 1
    assert layout["pages"][1]["items"][0]["text"] == "Signed at"


def test_boxes_are_the_symbol_fonts_glyphs():
    assert _is_box("") and _is_box("☐") and not _is_box("o")


async def test_fill_writes_over_the_pages_and_keeps_the_original(ctx):
    original = Attachment("Form.pdf", "application/pdf", paper_form())
    ctx.conversation.files["Form.pdf"] = original
    fill = tools()["fill_pdf"]

    filled = await fill.handler(
        {
            "file_name": "Form.pdf",
            "output_name": "Form filled",
            "texts": [
                {"page": 1, "x": 90, "y": 89, "text": "DOE"},
                {"page": 2, "x": 95, "y": 89, "text": "Springfield"},
            ],
            "checks": [{"page": 1, "x": 300, "y": 120}],
        },
        ctx,
    )
    again = await fill.handler(
        {
            "file_name": "Form.pdf",
            "output_name": "Form filled.pdf",
            "texts": [{"page": 1, "x": 90, "y": 89, "text": "ROE"}],
            "send": True,
        },
        ctx,
    )

    written = {(page, text) for page, _, _, text in lines(filled.data)}
    assert {(1, "DOE"), (2, "Springfield"), (1, "X")} <= written
    doe = next(line for line in lines(filled.data) if line[3] == "DOE")
    assert doe[1] == pytest.approx(90, abs=1) and doe[2] == pytest.approx(89, abs=2)
    assert (filled.filename, filled.origin) == ("Form filled.pdf", "agent")
    assert ctx.conversation.files["Form.pdf"] is original
    assert ctx.conversation.files["Form filled.pdf"] is again
    assert ctx.actions == [{"type": "share_file", "name": "Form filled.pdf"}]


async def test_a_filled_form_without_a_name_is_named_in_the_users_language(ctx):
    ctx.conversation.files["Form.pdf"] = Attachment("Form.pdf", "application/pdf", paper_form())
    args = {"file_name": "Form.pdf", "checks": [{"page": 1, "x": 300, "y": 120}]}

    assert (await tools()["fill_pdf"].handler(args, ctx)).filename == "Form (filled).pdf"
    french = await tools(language="fr")["fill_pdf"].handler(args, ctx)
    assert french.filename == "Form (rempli).pdf"


def acro_form() -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        NameObject,
        RectangleObject,
        TextStringObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(595, 842)
    field = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("name"),
            NameObject("/V"): TextStringObject(""),
            NameObject("/Rect"): RectangleObject((100, 700, 300, 720)),
            NameObject("/P"): page.indirect_reference,
        }
    )
    ref = writer._add_object(field)
    page[NameObject("/Annots")] = ArrayObject([ref])
    writer.root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject([ref])}
    )
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


async def test_a_pdf_with_form_fields_gets_them_filled(ctx):
    from pypdf import PdfReader

    ctx.conversation.files["f.pdf"] = Attachment("f.pdf", "application/pdf", acro_form())

    layout = await tools()["read_pdf_layout"].handler({"file_name": "f.pdf"}, ctx)
    assert layout["fields"] == {"name": {"type": "Tx", "value": ""}}
    filled = await tools()["fill_pdf"].handler(
        {"file_name": "f.pdf", "output_name": "done", "fields": {"name": "DOE"}}, ctx
    )

    assert PdfReader(BytesIO(filled.data)).get_fields()["name"]["/V"] == "DOE"


def test_without_the_pdf_libraries_there_is_no_pdf_tool(monkeypatch):
    monkeypatch.setattr(pdf_forms, "INSTALLED", False)
    assert build_pdf_form_tools(NO_FONTS) == []
    # Copying pages only needs pypdf, which the agent always has.
    assert [t.name for t in pdf_pages.build_pdf_page_tools()] == ["copy_pdf_pages"]


async def test_fill_refuses_what_it_cant_place(ctx):
    ctx.conversation.files["f.pdf"] = Attachment("f.pdf", "application/pdf", paper_form())
    ctx.conversation.files["n.txt"] = Attachment("n.txt", "text/plain", b"hi")
    fill = tools()["fill_pdf"]
    base = {"file_name": "f.pdf", "output_name": "out"}

    with pytest.raises(ToolError, match="outside"):
        await fill.handler({**base, "texts": [{"page": 3, "x": 1, "y": 1, "text": "a"}]}, ctx)
    with pytest.raises(ToolError, match="needs a text"):
        await fill.handler({**base, "texts": [{"page": 1, "x": 1, "y": 1, "text": ""}]}, ctx)
    with pytest.raises(ToolError, match="no form fields"):
        await fill.handler({**base, "fields": {"name": "DOE"}}, ctx)
    with pytest.raises(ToolError, match="Give texts"):
        await fill.handler(base, ctx)
    ctx.conversation.files["big.pdf"] = Attachment("big.pdf", "application/pdf", many_pages(21))
    with pytest.raises(ToolError, match="more than 20 pages"):
        await fill.handler({"file_name": "big.pdf", "output_name": "o", "checks": []}, ctx)
    with pytest.raises(ToolError, match="has 21.*read_saved_file.*copy_pdf_pages"):
        await tools()["read_pdf_layout"].handler({"file_name": "big.pdf"}, ctx)
    with pytest.raises(ToolError, match="not a PDF"):
        await tools()["read_pdf_layout"].handler({"file_name": "n.txt"}, ctx)
    assert tools()["read_pdf_layout"].taints and tools()["fill_pdf"].taints
