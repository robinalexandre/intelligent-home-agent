import io
import zipfile

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from intelligent_home_agent.agent import file_blocks
from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.office import EXCEL, POWERPOINT, WORD, check_zip

ODT = "application/vnd.oasis.opendocument.text"
ODS = "application/vnd.oasis.opendocument.spreadsheet"


def zipped(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


def odf(body: str) -> bytes:
    content = (
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
        ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        f"<office:body>{body}</office:body></office:document-content>"
    )
    return zipped({"content.xml": content.encode()})


def text_of(blocks):
    return " ".join(b.get("text", "") for b in blocks)


def test_a_powerpoint_is_read_as_its_slides_text():
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Budget 2027"
    out = io.BytesIO()
    deck.save(out)

    blocks = file_blocks(Attachment("deck.pptx", POWERPOINT, out.getvalue()))

    assert "## Slide 1" in text_of(blocks)
    assert "Budget 2027" in text_of(blocks)


def test_a_word_document_is_read_as_text():
    doc = Document()
    doc.add_paragraph("Quote for the kitchen")
    out = io.BytesIO()
    doc.save(out)

    blocks = file_blocks(Attachment("quote.docx", WORD, out.getvalue()))

    assert "Quote for the kitchen" in text_of(blocks)


def test_opendocument_text_and_sheets_are_read():
    text = odf("<office:text><text:h>Title</text:h><text:p>Hello</text:p></office:text>")
    sheet = odf(
        "<office:spreadsheet><table:table><table:table-row>"
        "<table:table-cell><text:p>Rent</text:p></table:table-cell>"
        "<table:table-cell><text:p>950</text:p></table:table-cell>"
        "</table:table-row></table:table></office:spreadsheet>"
    )

    assert "Title\nHello" in text_of(file_blocks(Attachment("a.odt", ODT, text)))
    assert "Rent\t950" in text_of(file_blocks(Attachment("b.ods", ODS, sheet)))


def test_a_pages_file_shows_its_preview_and_says_it_is_only_that():
    pages = zipped({"Index/Document.iwa": b"\x00", "preview.jpg": b"\xff\xd8jpeg"})

    image, note = file_blocks(Attachment("letter.pages", "application/zip", pages))

    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/jpeg"
    assert "first page's preview" in note["text"]


def test_a_broken_office_file_is_only_an_attachment():
    (block,) = file_blocks(Attachment("x.docx", WORD, b"not a zip"))

    assert "can't be read" in block["text"]


def test_an_excel_sheet_is_read_row_by_row():
    book = Workbook()
    sheet = book.active
    sheet.title = "Budget"
    sheet.append(["Rent", 950])
    sheet.append([None, None])
    sheet.append(["Power", 60])
    out = io.BytesIO()
    book.save(out)

    text = text_of(file_blocks(Attachment("budget.xlsx", EXCEL, out.getvalue())))

    assert "## Sheet: Budget\nRent\t950\nPower\t60" in text


def test_a_zip_that_expands_too_much_is_refused():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("content.xml", b"0" * 5_000_000)
    bomb = out.getvalue()

    with pytest.raises(ValueError, match="expands to"):
        check_zip(bomb)
    (block,) = file_blocks(Attachment("big.odt", ODT, bomb))
    assert "can't be read" in block["text"]
