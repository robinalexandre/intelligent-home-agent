import re
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.tools import ToolError, ToolRegistry, build_document_tools, documents
from intelligent_home_agent.tools.documents import (
    ALLOWED_TAGS,
    Page,
    _place_images,
    _Placed,
    find_fonts,
    paper_for,
    render_pdf,
    sanitize,
    written_here,
)

SYSTEM_FONTS = [
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/System/Library/Fonts/Supplemental"),
]


def tools(font_dir: Path, **kwargs):
    return {t.name: t for t in build_document_tools(font_dir, **kwargs)}


def unicode_fonts(tmp_path: Path) -> Path | None:
    """A folder with a Unicode font named as DejaVu, from what this machine has."""
    for folder, regular, bold in [
        (SYSTEM_FONTS[0], "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
        (SYSTEM_FONTS[1], "Arial.ttf", "Arial Bold.ttf"),
    ]:
        if (folder / regular).exists() and (folder / bold).exists():
            (tmp_path / "DejaVuSans.ttf").write_bytes((folder / regular).read_bytes())
            (tmp_path / "DejaVuSans-Bold.ttf").write_bytes((folder / bold).read_bytes())
            return tmp_path
    return None


async def test_creates_a_pdf_kept_by_name_and_shared(ctx, tmp_path):
    create = tools(tmp_path)["create_pdf"]  # no fonts here: the Latin-1 fallback
    result = await create.handler(
        {
            "title": "Packing list",
            "html": "<h1>Statement</h1><p>For the trip, Sam packs… 1 200 €</p>"
            "<ul><li>No outstanding debt</li></ul>",
            "file_name": "statement",
        },
        ctx,
    )

    assert result["created"] == "statement.pdf"
    file = ctx.conversation.files["statement.pdf"]
    assert file.data.startswith(b"%PDF") and file.origin == "agent"
    assert ctx.actions == [{"type": "share_file", "name": "statement.pdf"}]
    assert written_here(file)
    assert not written_here(Attachment("invoice.pdf", "application/pdf", file.data, "mail"))


def test_unicode_text_renders_with_a_ttf_font(tmp_path):
    fonts = unicode_fonts(tmp_path)
    if fonts is None:
        pytest.skip("needs a Unicode font on this machine")
    data = render_pdf("Quote", "<h1>Quote n° 12</h1><p>Total: 1 200 € — thanks, Zoë</p>", fonts)
    assert data.startswith(b"%PDF") and len(data) > 1000
    assert "1 200 €" in PdfReader(BytesIO(data)).pages[0].extract_text()


def test_the_paper_is_a_setting_and_follows_the_country_when_left_to_it():
    assert paper_for("auto", "US") == "letter" and paper_for("auto", "Canada") == "letter"
    assert paper_for("auto", "FR") == "a4" and paper_for("auto", None) == "a4"
    assert paper_for("A4", "US") == "a4" and paper_for("letter", "DE") == "letter"
    assert (Page("a4").width_pt, Page("a4").max_image_height_pt) == (480, 600)
    assert (Page("letter").width_pt, Page("letter").max_image_height_pt) == (495, 550)


def test_a_pdf_is_laid_out_on_the_paper_asked(tmp_path):
    def size(paper: str) -> tuple[int, int]:
        data = render_pdf("x", "<p>Hello</p>", tmp_path, page=Page(paper))
        box = PdfReader(BytesIO(data)).pages[0].mediabox
        return round(box.width), round(box.height)

    assert size("a4") == (595, 842)
    assert size("letter") == (612, 792)


def test_the_tool_tells_the_model_the_paper_the_language_and_the_country(tmp_path):
    here = tools(tmp_path, paper="letter", language="en", country="US")["create_pdf"]
    assert "495 wide on US Letter paper" in here.description
    assert "English unless asked otherwise" in here.description
    assert "the way it is done in US" in here.description
    there = tools(tmp_path, language="fr")["create_pdf"]
    assert "480 wide on A4 paper" in there.description
    assert "French unless asked otherwise" in there.description
    assert "where the user lives" in there.description


async def test_a_pdf_without_a_title_is_named_in_the_users_language(ctx, tmp_path):
    result = await tools(tmp_path, language="fr")["create_pdf"].handler(
        {"title": " ", "html": "<p>Bonjour</p>"}, ctx
    )
    assert result["created"] == "Document.pdf"


def test_fonts_are_found_by_family_in_the_folder_given(tmp_path):
    assert find_fonts(tmp_path) is None and find_fonts(tmp_path / "absent") is None
    for name in ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "NotoSans-Italic.ttf", "x.txt"):
        (tmp_path / name).write_bytes(b"")
    noto = find_fonts(tmp_path)
    assert noto.family == "NotoSans" and noto.italic.name == "NotoSans-Italic.ttf"
    assert noto.bold_italic.name == "NotoSans-Bold.ttf"  # no bold italic: the bold file
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        (tmp_path / name).write_bytes(b"")
    dejavu = find_fonts(tmp_path)
    assert dejavu.family == "DejaVuSans" and dejavu.italic.name == "DejaVuSans.ttf"


def test_without_the_pdf_libraries_only_the_file_tools_are_there(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "can_write_pdfs", lambda: False)
    assert set(tools(tmp_path)) == {"list_saved_files", "read_saved_file"}


def test_images_never_load_a_server_file(tmp_path, monkeypatch):
    secret = tmp_path / "token.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    opened = []
    real_open = open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    data = render_pdf(
        "x", f'<p>Hello</p><IMG src="{secret}"><img src="http://intranet/a.png"/>', tmp_path
    )
    assert data.startswith(b"%PDF")
    assert str(secret) not in opened


def test_only_layout_tags_without_attributes_get_through():
    tricky = (
        '<h1 style="x">Title</h1><im<img>g src="/app/data/login.json">'
        '<table><tr><td><img src="http://intranet/a.png"> 1 200 €</td></tr></table>'
        '<a href="http://intranet/">link</a><script>x()</script><p onclick="y">a &lt; b</p>'
    )
    clean = sanitize(tricky)
    tags = re.findall(r"<\s*/?\s*([^\s>/]+)([^>]*)>", clean)
    assert {name for name, _ in tags} <= ALLOWED_TAGS
    assert all(attrs.strip() in ("", "/") for _, attrs in tags)  # no attribute at all
    assert clean.startswith("<h1>Title</h1>")
    assert "<table><tr><td>1 200 €</td></tr></table>" in clean
    assert "<p>a &lt; b</p>" in clean


def test_a_table_cell_keeps_only_what_fpdf_lays_out():
    clean = sanitize(
        "<table><tr><th>Item</th><th><b>Amount</b></th></tr>"
        "<tr><td>Tent<br>pegs included</td><td><b>2 300 €</b> a month</td></tr>"
        "<tr><td><p>Sam</p><p>Jo</p></td><td><ul><li>a</li><li><i>b</i></li></ul></td>"
        "</tr><tr><td><strong><em>Total</em></strong></td><td>x</td></tr></table>"
    )
    assert clean == (
        "<table><tr><th>Item</th><th><b>Amount</b></th></tr>"
        "<tr><td>Tent\npegs included</td><td>2 300 € a month</td></tr>"
        "<tr><td>Sam\nJo</td><td>a\nb</td></tr>"
        "<tr><td><b><i>Total</i></b></td><td>x</td></tr></table>"
    )


def test_line_breaks_and_mixed_emphasis_in_a_table_render(tmp_path):
    data = render_pdf(
        "Budget",
        "<table><tr><th>Who</th><th>Amount</th></tr>"
        "<tr><td>Sam<br/>tent + stove</td><td><b>2 300 EUR</b> / month<br>rounded</td></tr>"
        "</table>",
        tmp_path,
    )
    text = PdfReader(BytesIO(data)).pages[0].extract_text()
    assert "tent + stove" in text and "rounded" in text


async def test_listing_files_from_elsewhere_taints_the_request(ctx, tmp_path):
    listing = tools(tmp_path)["list_saved_files"]
    ctx.conversation.files["quote.pdf"] = Attachment("quote.pdf", "application/pdf", b"x")
    await listing.handler({}, ctx)
    assert not ctx.tainted

    ctx.conversation.files["page.pdf"] = Attachment("page.pdf", "application/pdf", b"x", "web")
    result = await listing.handler({}, ctx)
    assert ctx.tainted
    assert {f["name"] for f in result["files"]} == {"quote.pdf", "page.pdf"}


async def test_empty_html_is_refused(ctx, tmp_path):
    create = tools(tmp_path)["create_pdf"]
    with pytest.raises(ToolError, match="html is required"):
        await create.handler({"title": "x", "html": " "}, ctx)


async def test_a_kept_file_opens_tainting_unless_the_user_sent_it(ctx, tmp_path):
    read = tools(tmp_path)["read_saved_file"]
    sent = Attachment("quote.pdf", "application/pdf", b"x")
    copy = Attachment("quote p2.pdf", "application/pdf", b"y", "agent")
    ctx.conversation.files.update({"quote.pdf": sent, "quote p2.pdf": copy})

    assert await read.handler({"file_name": "quote.pdf"}, ctx) is sent
    assert not ctx.tainted
    assert await read.handler({"file_name": "quote p2.pdf"}, ctx) is copy
    assert ctx.tainted
    with pytest.raises(ToolError, match="No saved file"):
        await read.handler({"file_name": "nope.pdf"}, ctx)
    assert read.taints


async def test_reading_a_kept_file_taints_the_rest_of_the_request(ctx, tmp_path):
    registry = ToolRegistry()
    registry.extend(build_document_tools(tmp_path))
    ctx.conversation.files["quote.pdf"] = Attachment("quote.pdf", "application/pdf", b"%PDF-1.4")

    result = await registry.run("read_saved_file", {"file_name": "quote.pdf"}, ctx)

    assert result.filename == "quote.pdf"
    # The agent taints the request after the batch, even for a file the user sent: others
    # wrote most of it. Listing and writing a PDF read nothing.
    assert registry.taints("read_saved_file")
    assert not registry.taints("list_saved_files") and not registry.taints("create_pdf")


def _image(size=(800, 400), kind="PNG") -> bytes:
    out = BytesIO()
    Image.new("RGB", size, "red").save(out, kind)
    return out.getvalue()


def _images_in(pdf: bytes) -> int:
    return sum(len(page.images) for page in PdfReader(BytesIO(pdf)).pages)


async def test_kept_images_show_in_the_pdf_in_text_and_in_tables(ctx, tmp_path):
    create = tools(tmp_path)["create_pdf"]
    ctx.conversation.files.update(
        {
            "drawing.png": Attachment("drawing.png", "image/png", _image(), "agent"),
            "start.jpg": Attachment("start.jpg", "image/jpeg", _image(kind="JPEG"), "web"),
            "anim.gif": Attachment("anim.gif", "image/gif", _image(kind="GIF"), "agent"),
        }
    )
    await create.handler(
        {
            "title": "Program",
            "html": '<h1>Program</h1><img src="drawing.png" width="200">'
            "<table><tr><th>Exercise</th><th>Start</th></tr>"
            '<tr><td>Curl</td><td><img src="start.jpg"></td></tr></table>'
            '<p><img src="anim.gif"><img src="absent.png"></p>',
        },
        ctx,
    )
    assert _images_in(ctx.conversation.files["Program.pdf"].data) == 3


def test_an_image_is_sized_to_fit_the_page_and_never_names_a_server_path(tmp_path):
    placed = {"big.png": _Placed(str(tmp_path / "0.png"), 480)}
    clean = sanitize(
        '<img src="big.png" width="9000"><img src="big.png" width="120px">'
        '<img src="/etc/passwd"><table><tr><td><img src="big.png" width="50"></td></tr></table>',
        placed,
    )
    path = tmp_path / "0.png"
    assert clean == (
        f'<img src="{path}" width="480"><img src="{path}" width="120">'
        f'<table><tr><td><img src="{path}"></td></tr></table>'
    )


def test_a_tall_image_is_narrowed_to_fit_the_page_height(tmp_path):
    tall = Attachment("tall.png", "image/png", _image((1000, 3000)))
    broken = Attachment("broken.png", "image/png", b"\x89PNG not really")
    placed = _place_images({"tall.png": tall, "broken.png": broken}, tmp_path)
    assert set(placed) == {"tall.png"} and placed["tall.png"].width_pt == 200
    letter = _place_images({"tall.png": tall}, tmp_path, Page("letter"))
    assert letter["tall.png"].width_pt == 183
