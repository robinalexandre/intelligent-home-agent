"""Documents the agent writes itself: a PDF from simple HTML, with the kept images it names,
handed to the user and kept in the conversation so another tool can attach it. Also the two
tools that list and open the conversation's files.

Writing a PDF needs the ``documents`` extra (fpdf2); without it only the file tools are
there. The paper size and the fonts are settings, so nothing here assumes
a country: the letter's layout and its date format are the model's, told the language and
the country in the tool's description.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

if TYPE_CHECKING:
    from fpdf import FPDF

MAX_HTML_CHARS = 60_000
# The Creator of every PDF render_pdf writes, so a receipt can't be one of them.
WRITTEN_HERE = "intelligent-home-agent"
ALLOWED_TAGS = frozenset(
    {"h1", "h2", "h3", "p", "b", "strong", "i", "em", "u", "br", "hr",
     "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td"}
)  # fmt: skip
VOID_TAGS = frozenset({"br", "hr", "img"})
CELL_TAGS = frozenset({"td", "th"})
EMPHASIS = {"strong": "b", "em": "i", "b": "b", "i": "i", "u": "u"}
# fpdf lays a table cell out from plain text, or text wholly in one emphasis: inside one,
# these break the line and everything else is dropped.
CELL_BREAKS = frozenset({"br", "p", "li", "h1", "h2", "h3", "hr"})
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_IMAGES = 40
MARGIN_MM = 20
# Room left under an image for a heading on the same page, in points.
HEADING_ROOM_PT = 125
# DejaVu covers Latin with accents, Greek, Cyrillic, currency signs and most symbols; the
# Docker image installs it (fonts-dejavu-core). PDF_FONT_DIR names another folder.
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
FONT_SUFFIXES = (".ttf", ".otf")

# Paper sizes in millimetres. "auto" picks Letter where it is the usual paper.
PAPER_SIZES = {"a4": (210.0, 297.0), "letter": (215.9, 279.4)}
PAPER_NAMES = {"a4": "A4", "letter": "US Letter"}
LETTER_COUNTRIES = frozenset(
    {"us", "usa", "united states", "ca", "canada", "mx", "mexico", "ph", "philippines",
     "co", "colombia", "cl", "chile", "ve", "venezuela", "cr", "costa rica", "gt",
     "guatemala", "pa", "panama", "do", "dominican republic", "pr", "puerto rico"}
)  # fmt: skip


def paper_for(setting: str, country: str | None = None) -> str:
    """ "a4" or "letter": the setting, or with "auto" the country's usual paper."""
    setting = (setting or "auto").strip().casefold()
    if setting in PAPER_SIZES:
        return setting
    return "letter" if (country or "").strip().casefold() in LETTER_COUNTRIES else "a4"


@dataclass(frozen=True)
class Page:
    """A paper size and what fits between its margins, in points as fpdf reads an
    <img>'s width."""

    paper: str = "a4"

    @property
    def size_mm(self) -> tuple[float, float]:
        return PAPER_SIZES[self.paper]

    @property
    def width_pt(self) -> int:
        return _points(self.size_mm[0] - 2 * MARGIN_MM)

    @property
    def max_image_height_pt(self) -> int:
        return _points(self.size_mm[1] - 2 * MARGIN_MM) - HEADING_ROOM_PT


def _points(mm: float) -> int:
    """Millimetres in points, rounded down to a multiple of 5."""
    return int(mm * 72 / 25.4) // 5 * 5


def can_write_pdfs() -> bool:
    return all(find_spec(module) for module in ("fpdf", "PIL", "pypdf"))


@dataclass(frozen=True)
class Fonts:
    """The files of one font family; italics fall back to the upright files."""

    family: str
    regular: Path
    bold: Path
    italic: Path
    bold_italic: Path


_DEJAVU = {"regular": "", "bold": "-Bold", "italic": "-Oblique", "bold_italic": "-BoldOblique"}
_USUAL = {
    "regular": "-Regular",
    "bold": "-Bold",
    "italic": "-Italic",
    "bold_italic": "-BoldItalic",
}


def find_fonts(font_dir: Path = FONT_DIR) -> Fonts | None:
    """The fonts of a folder: DejaVu Sans when it is there, else the first family that has
    a Regular and a Bold file (NotoSans-Regular.ttf, NotoSans-Bold.ttf...)."""
    if not font_dir.is_dir():
        return None
    files = {f.name: f for f in sorted(font_dir.iterdir()) if f.suffix.lower() in FONT_SUFFIXES}

    def family(stem: str, marks: dict[str, str]) -> Fonts | None:
        def pick(style: str) -> Path | None:
            names = (f"{stem}{marks[style]}{ext}" for ext in FONT_SUFFIXES)
            return next((files[n] for n in names if n in files), None)

        regular, bold = pick("regular"), pick("bold")
        if regular is None or bold is None:
            return None
        return Fonts(stem, regular, bold, pick("italic") or regular, pick("bold_italic") or bold)

    if (dejavu := family("DejaVuSans", _DEJAVU)) is not None:
        return dejavu
    for name in files:
        stem, dash, style = Path(name).stem.rpartition("-")
        if dash and style == "Regular" and (found := family(stem, _USUAL)) is not None:
            return found
    return None


def add_fonts(pdf: FPDF, font_dir: Path = FONT_DIR) -> str:
    """The font family to write with: the folder's when it has one, else Helvetica
    (Latin-1 only)."""
    fonts = find_fonts(font_dir)
    if fonts is None:
        return "Helvetica"
    pdf.add_font(fonts.family, "", str(fonts.regular))
    pdf.add_font(fonts.family, "B", str(fonts.bold))
    pdf.add_font(fonts.family, "I", str(fonts.italic))
    pdf.add_font(fonts.family, "BI", str(fonts.bold_italic))
    return fonts.family


def printable(text: str, family: str) -> str:
    if family != "Helvetica":
        return text
    return text.replace("€", "EUR").encode("latin-1", "replace").decode("latin-1")


def written_here(file: Attachment) -> bool:
    """A PDF render_pdf wrote: the agent's own words, never a vendor's invoice or receipt."""
    if file.origin != "agent" or file.mimetype != "application/pdf":
        return False
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        metadata = PdfReader(BytesIO(file.data)).metadata or {}
    except (PyPdfError, ValueError):
        return False
    return metadata.get("/Creator") == WRITTEN_HERE


def render_pdf(
    title: str,
    html: str,
    font_dir: Path = FONT_DIR,
    images: dict[str, Attachment] | None = None,
    page: Page | None = None,
) -> bytes:
    """The PDF of html; an <img src="name"> shows the kept image of that name, and nothing
    else: no address or server path ever reaches the renderer."""
    page = page or Page()
    with TemporaryDirectory() as tmp:
        placed = _place_images(images or {}, Path(tmp), page)
        return _render(title, html, font_dir, placed, page)


def _render(title: str, html: str, font_dir: Path, placed: dict[str, _Placed], page: Page) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(unit="mm", format=page.size_mm)
    pdf.set_creator(WRITTEN_HERE)
    pdf.set_margins(MARGIN_MM, MARGIN_MM, MARGIN_MM)
    pdf.set_auto_page_break(auto=True, margin=MARGIN_MM)
    pdf.set_title(title)
    family = add_fonts(pdf, font_dir)
    html = printable(html, family)
    pdf.add_page()
    pdf.set_font(family, size=11)
    try:
        clean = sanitize(html, placed)
        allowed = {p.path for p in placed.values()}
        pdf.write_html(clean, font_family=family, image_map=lambda src: _only(src, allowed))
    except NotImplementedError as exc:  # fpdf names what it can't lay out, e.g. <b> in a <td>
        raise ToolError(f"The PDF could not be laid out ({exc}): simplify its HTML.") from exc
    except Exception as exc:
        raise ToolError("The PDF could not be laid out: simplify its HTML.") from exc
    return bytes(pdf.output())


def _only(src: str, allowed: set[str]) -> str:
    if src not in allowed:
        raise ValueError("only kept images can be shown")
    return src


class _Placed:
    """A kept image copied as PNG or JPEG into the render's own folder, with its width."""

    def __init__(self, path: str, width_pt: int) -> None:
        self.path = path
        self.width_pt = width_pt


def _place_images(
    images: dict[str, Attachment], folder: Path, page: Page | None = None
) -> dict[str, _Placed]:
    from PIL import Image, UnidentifiedImageError

    page = page or Page()
    placed: dict[str, _Placed] = {}
    for index, (name, file) in enumerate(images.items()):
        try:
            with Image.open(BytesIO(file.data)) as image:
                image.seek(0)  # an animation shows its first frame
                width, height = image.size
                # Re-encoded here, so the renderer only ever opens a plain image of ours.
                jpeg = file.mimetype == "image/jpeg"
                frame = image.convert("RGB" if jpeg else "RGBA")
                path = folder / f"{index}.{'jpg' if jpeg else 'png'}"
                frame.save(path)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            continue
        fit = min(width, page.width_pt, page.max_image_height_pt * width / max(height, 1))
        placed[name] = _Placed(str(path), max(1, round(fit)))
    return placed


class _Allowlist(HTMLParser):
    """Rebuilds the HTML from allowed tags without any attribute, text escaped; an <img>
    only for a kept image, pointing at its copy in the render's folder. A table cell keeps
    only what fpdf can lay out in one: its text, its line breaks, its images, and an
    emphasis only when it covers the whole cell."""

    def __init__(self, images: dict[str, _Placed]) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.images = images
        self.tables = 0
        self.cell: _Cell | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if self.cell is not None and tag not in CELL_TAGS and tag not in {"tr", "table"}:
            if tag == "img":
                self.cell.image(self._image(dict(attrs)))
            else:
                self.cell.start(tag)
            return
        if tag in CELL_TAGS and self.tables:
            self._close_cell()
            self.cell = _Cell(tag)
            return
        if tag == "img":
            self.out.append(self._image(dict(attrs)))
        elif tag in ALLOWED_TAGS:
            self._close_cell()
            self.out.append(f"<{tag}>")
            self.tables += tag == "table"

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        self.handle_starttag(tag, attrs)

    def _image(self, attrs: dict[str, str | None]) -> str:
        image = self.images.get((attrs.get("src") or "").strip())
        if image is None:
            return ""
        if self.tables:  # a table's cell fits the image to its own width
            return f'<img src="{image.path}">'
        width = image.width_pt
        asked = (attrs.get("width") or "").strip().removesuffix("px")
        if asked.isdigit() and int(asked) > 0:
            width = min(int(asked), width)
        return f'<img src="{image.path}" width="{width}">'

    def handle_endtag(self, tag: str) -> None:
        if self.cell is not None and tag not in CELL_TAGS and tag not in {"tr", "table"}:
            self.cell.end(tag)
            return
        if tag in CELL_TAGS and self.cell is not None:
            self._close_cell()
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self._close_cell()
            self.out.append(f"</{tag}>")
            if tag == "table" and self.tables:
                self.tables -= 1

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.text(data)
        else:
            self.out.append(escape(data, quote=False))

    def close(self) -> None:
        super().close()
        self._close_cell()

    def _close_cell(self) -> None:
        if self.cell is not None:
            self.out.append(self.cell.html())
            self.cell = None


class _Cell:
    """One table cell's content, flattened to what fpdf lays out."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.parts: list[str] = []
        self.styles: list[str] = []
        self.styled: set[tuple[str, ...]] = set()

    def start(self, tag: str) -> None:
        if tag in EMPHASIS:
            self.styles.append(EMPHASIS[tag])
        elif tag in CELL_BREAKS:
            self._break()

    def end(self, tag: str) -> None:
        if tag in EMPHASIS and EMPHASIS[tag] in self.styles:
            del self.styles[len(self.styles) - 1 - self.styles[::-1].index(EMPHASIS[tag])]
        elif tag in CELL_BREAKS:
            self._break()

    def _break(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def text(self, data: str) -> None:
        if data.strip():
            self.styled.add(tuple(dict.fromkeys(self.styles)))
        self.parts.append(escape(" ".join(data.split("\n")), quote=False))

    def image(self, html: str) -> None:
        self.parts.append(html)

    def html(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).split("\n")]
        body = "\n".join(line for line in lines if line)
        if len(self.styled) == 1 and "<img" not in body:
            (styles,) = self.styled
            body = "".join(f"<{s}>" for s in styles) + body
            body += "".join(f"</{s}>" for s in reversed(styles))
        return f"<{self.tag}>{body}</{self.tag}>"


def sanitize(html: str, images: dict[str, _Placed] | None = None) -> str:
    """Only layout tags reach the renderer, and images of ours: no link, style or address
    can load anything."""
    parser = _Allowlist(images or {})
    parser.feed(html)
    parser.close()
    return "".join(parser.out)


def build_document_tools(
    font_dir: Path = FONT_DIR,
    *,
    paper: str = "a4",
    language: str = "en",
    country: str | None = None,
) -> list[Tool]:
    """list_saved_files and read_saved_file always; create_pdf when the documents extra
    is installed. ``paper`` is "a4" or "letter" (see paper_for)."""
    s = strings(language)
    page = Page(paper)

    async def create_pdf(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        title = str(args.get("title") or "").strip() or s.document_title
        html = str(args.get("html") or "").strip()
        if not html:
            raise ToolError("html is required.")
        if len(html) > MAX_HTML_CHARS:
            raise ToolError("This document is too long for one PDF.")
        name = str(args.get("file_name") or title).strip()
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        images = {
            name: f for name, f in ctx.conversation.files.items() if f.mimetype in IMAGE_TYPES
        }
        if len(images) > MAX_IMAGES:  # only those the HTML names
            images = {n: f for n, f in images.items() if n in html}
        data = render_pdf(title, html, font_dir, images, page)
        file = ctx.conversation.keep_file(Attachment(name, "application/pdf", data, "agent"))
        ctx.actions.append({"type": "share_file", "name": file.filename})
        return {"created": file.filename, "size_kb": round(len(data) / 1024)}

    async def list_saved_files(_args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        # Names of files that came from elsewhere were written by others: treat like an email.
        if any(f.origin not in {"user", "agent"} for f in ctx.conversation.files.values()):
            ctx.tainted = True
        return {
            "files": [
                {"name": f.filename, "from": f.origin, "size_kb": round(len(f.data) / 1024)}
                for f in ctx.conversation.files.values()
            ]
        }

    async def read_saved_file(args: dict[str, Any], ctx: ToolContext) -> Attachment:
        name = str(args.get("file_name") or "")
        file = ctx.conversation.files.get(name)
        if file is None:
            raise ToolError(f"No saved file named {name}: see list_saved_files.")
        # What the user sent is read with their message anyway; anything else, a copy you
        # made of it included, may carry others' words.
        if file.origin != "user":
            ctx.tainted = True
        return file

    tools = [
        Tool(
            name="list_saved_files",
            description=(
                "The files kept for this conversation (for days in a chat, earlier "
                "exchanges included): sent by the user, made by you, or fetched by a tool. "
                "Check here before asking the user for a file again."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=list_saved_files,
        ),
        Tool(
            name="read_saved_file",
            description=(
                "Open a kept file (see list_saved_files) to look at it as the user would: a "
                "PDF's pages with their stamps and signatures, an image, a text. Use it to "
                "find the right pages before copying some and to check what you made."
            ),
            input_schema={
                "type": "object",
                "properties": {"file_name": {"type": "string"}},
                "required": ["file_name"],
            },
            handler=read_saved_file,
            # Even a file the user sent was mostly written by others, like an email.
            taints=True,
        ),
    ]
    if not can_write_pdfs():
        return tools
    where = f"in {country}" if country else "where the user lives"
    tools.append(
        Tool(
            name="create_pdf",
            description=(
                "Write a PDF (letter, certificate, quote, summary, program...) from simple "
                "HTML: h1-h3, p, b, i, u, br, ul/ol/li, table/tr/th/td, hr, and "
                '<img src="file name" width="240"> for a kept image (one you drew, one the '
                f"user sent, a downloaded one; width in points, the page is {page.width_pt} "
                f"wide on {PAPER_NAMES[page.paper]} paper; in a table, the image fills its "
                "cell). A table cell holds plain text with <br> line breaks; bold or italic "
                "there only over the whole cell, else it is dropped. Write the document in "
                f"the language it is for ({s.name} unless asked otherwise) and lay a letter "
                f"out the way it is done {where}: where the sender, the recipient, the "
                "place and the date go, how dates, addresses, numbers and amounts are "
                "written, the usual opening and closing. The user receives the PDF, and it "
                "is kept by its file name for the tools that attach files. Never an invoice "
                "or a receipt standing for a vendor's: only the vendor issues those."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "html": {"type": "string"},
                    "file_name": {"type": "string", "description": "e.g. statement.pdf"},
                },
                "required": ["title", "html"],
            },
            handler=create_pdf,
        )
    )
    return tools
