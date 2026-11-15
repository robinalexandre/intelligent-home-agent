from io import BytesIO

import pytest
from fpdf import FPDF
from pypdf import PdfReader, PdfWriter

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.tools import ToolError, build_pdf_page_tools
from intelligent_home_agent.tools.pdf_pages import page_numbers


def numbered(label: str, n: int) -> bytes:
    pdf = FPDF(unit="pt", format="A4")
    pdf.set_font("Helvetica", size=12)
    for i in range(1, n + 1):
        pdf.add_page()
        pdf.text(60, 100, f"{label} {i}")
    return bytes(pdf.output())


def texts(data: bytes) -> list[str]:
    return [page.extract_text().strip() for page in PdfReader(BytesIO(data)).pages]


def locked(data: bytes) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(BytesIO(data)))
    writer.encrypt("secret")
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def keep(ctx, name: str, data: bytes, origin: str = "user") -> Attachment:
    mimetype = "application/pdf" if name.endswith(".pdf") else "text/plain"
    file = Attachment(name, mimetype, data, origin)
    ctx.conversation.files[name] = file
    return file


def copy():
    return build_pdf_page_tools()[0]


def test_pages_read_as_ranges_in_the_order_given():
    assert page_numbers("2", 3) == [1]
    assert page_numbers("1-3, 5", 5) == [0, 1, 2, 4]
    assert page_numbers("3,1", 3) == [2, 0]
    assert page_numbers("3-1", 3) == [2, 1, 0]
    assert page_numbers([2, 1], 2) == [1, 0]
    assert page_numbers("", 2) == [0, 1]
    assert page_numbers(None, 2) == [0, 1]
    with pytest.raises(ToolError, match="more than 200 pages"):
        page_numbers("1-10000000," * 1000, 10_000_000)
    with pytest.raises(ToolError, match="more than 200 pages"):
        page_numbers("", 201)
    with pytest.raises(ToolError, match="more than 200 pages"):
        page_numbers("1-2", 5, room=1)


async def test_one_page_goes_into_a_new_pdf_and_the_original_stays(ctx):
    original = keep(ctx, "Loan offer .pdf", numbered("Page", 2))

    result = await copy().handler(
        {
            "parts": [{"file_name": "Loan offer .pdf", "pages": "2"}],
            "output_name": "Loan offer stamped",
            "send": True,
        },
        ctx,
    )

    assert result["created"] == "Loan offer stamped.pdf"
    assert result["pages"] == 1
    made = ctx.conversation.files["Loan offer stamped.pdf"]
    assert texts(made.data) == ["Page 2"]
    assert made.origin == "agent"
    assert ctx.conversation.files["Loan offer .pdf"] is original
    assert texts(original.data) == ["Page 1", "Page 2"]
    assert ctx.actions == [{"type": "share_file", "name": "Loan offer stamped.pdf"}]


async def test_several_pdfs_merge_in_the_order_of_parts(ctx):
    keep(ctx, "a.pdf", numbered("A", 3))
    keep(ctx, "b.pdf", numbered("B", 2), origin="mail")

    result = await copy().handler(
        {
            "parts": [
                {"file_name": "b.pdf"},
                {"file_name": "a.pdf", "pages": "3,1"},
            ],
            "output_name": "Application.pdf",
        },
        ctx,
    )

    assert result["pages"] == 4
    assert texts(ctx.conversation.files["Application.pdf"].data) == ["B 1", "B 2", "A 3", "A 1"]
    assert ctx.actions == []
    assert not ctx.tainted and not copy().taints


async def test_a_password_protected_pdf_opens_when_only_its_owner_has_one(ctx):
    writer = PdfWriter(clone_from=PdfReader(BytesIO(numbered("P", 2))))
    writer.encrypt(user_password="", owner_password="owner")
    out = BytesIO()
    writer.write(out)
    keep(ctx, "restricted.pdf", out.getvalue())

    await copy().handler(
        {"parts": [{"file_name": "restricted.pdf", "pages": "1"}], "output_name": "p1"}, ctx
    )

    assert texts(ctx.conversation.files["p1.pdf"].data) == ["P 1"]


async def test_copy_refuses_what_it_cant_make(ctx):
    keep(ctx, "f.pdf", numbered("F", 2))
    keep(ctx, "locked.pdf", locked(numbered("L", 1)))
    keep(ctx, "broken.pdf", b"not a pdf")
    keep(ctx, "n.txt", b"hi")
    run = copy().handler

    with pytest.raises(ToolError, match="no page 3"):
        await run({"parts": [{"file_name": "f.pdf", "pages": "2-3"}], "output_name": "o"}, ctx)
    with pytest.raises(ToolError, match="read like"):
        await run({"parts": [{"file_name": "f.pdf", "pages": "two"}], "output_name": "o"}, ctx)
    with pytest.raises(ToolError, match="another name"):
        await run({"parts": [{"file_name": "f.pdf", "pages": "1"}], "output_name": "f"}, ctx)
    with pytest.raises(ToolError, match="password"):
        await run({"parts": [{"file_name": "locked.pdf"}], "output_name": "o"}, ctx)
    with pytest.raises(ToolError, match="broken.pdf can't be read"):
        await run({"parts": [{"file_name": "broken.pdf"}], "output_name": "o"}, ctx)
    with pytest.raises(ToolError, match="not a PDF"):
        await run({"parts": [{"file_name": "n.txt"}], "output_name": "o"}, ctx)
    with pytest.raises(ToolError, match="No saved file"):
        await run({"parts": [{"file_name": "nope.pdf"}], "output_name": "o"}, ctx)
    with pytest.raises(ToolError, match="parts must list"):
        await run({"parts": [], "output_name": "o"}, ctx)
    with pytest.raises(ToolError, match="output_name"):
        await run({"parts": [{"file_name": "f.pdf"}]}, ctx)
    with pytest.raises(ToolError, match="more than 200 pages"):
        await run({"parts": [{"file_name": "f.pdf", "pages": "1-2,1-2," * 50 + "1"}],
                   "output_name": "o"}, ctx)  # fmt: skip
    assert set(ctx.conversation.files) == {"f.pdf", "locked.pdf", "broken.pdf", "n.txt"}
