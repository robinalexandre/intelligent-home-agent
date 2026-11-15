import json
from io import BytesIO
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.tools import (
    ToolError,
    build_document_tools,
    build_illustration_tools,
    illustrations,
)
from intelligent_home_agent.tools.illustrations import (
    ExerciseBank,
    font_families,
    prepare_svg,
)

BANK = "https://bank.test"
FONTS = Path("/usr/share/fonts/truetype/dejavu")
SQUARE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">'
    '<rect x="10" y="10" width="30" height="30" fill="#c00"/>'
    '<text x="50" y="30" font-size="10">Start → end, déjà</text></svg>'
)


def png(color: str = "blue", size: tuple[int, int] = (60, 40), kind: str = "PNG") -> bytes:
    out = BytesIO()
    Image.new("RGB", size, color).save(out, kind)
    return out.getvalue()


def tools(http=None, bank=None, **kwargs):
    return {t.name: t for t in build_illustration_tools(http, FONTS, bank=bank, **kwargs)}


async def test_a_drawing_is_kept_as_png_at_its_proportions_and_sent(ctx):
    result = await tools()["draw_image"].handler({"svg": SQUARE, "file_name": "plan.svg"}, ctx)

    assert result["created"] == "plan.png" and result["sent"]
    file = ctx.conversation.files["plan.png"]
    assert file.mimetype == "image/png" and file.origin == "agent"
    image = Image.open(BytesIO(file.data))
    assert image.size == (1200, 600)
    assert image.getpixel((300, 300))[:3] == (204, 0, 0)  # the red square, drawn
    assert image.getpixel((5, 5))[:3] == (255, 255, 255)  # on white, not transparent
    assert ctx.actions == [{"type": "share_file", "name": "plan.png"}]


async def test_a_drawing_only_for_a_pdf_is_not_sent(ctx):
    await tools()["draw_image"].handler({"svg": SQUARE, "send": False}, ctx)
    assert "drawing.png" in ctx.conversation.files and ctx.actions == []
    await tools(language="fr")["draw_image"].handler({"svg": SQUARE, "send": False}, ctx)
    assert "schéma.png" in ctx.conversation.files


def test_a_drawings_text_is_set_in_the_fonts_of_the_folder(tmp_path):
    assert font_families(tmp_path) == {}  # no font: the renderer's own fallback
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        (tmp_path / name).write_bytes(b"")
    assert font_families(tmp_path)["serif_family"] == "DejaVu Serif"


def test_without_the_drawing_libraries_there_is_no_tool(monkeypatch):
    monkeypatch.setattr(illustrations, "INSTALLED", False)
    assert build_illustration_tools(None, FONTS) == []


@pytest.mark.parametrize(
    "svg",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///app/data/login.json"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink">'
        '<image x:href="/etc/passwd"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="http://intranet/a.png"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(http://x/a)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "http://x/a.css";</style></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
        '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
        "<html/>",
        "not xml",
    ],
)
def test_a_drawing_can_load_nothing_from_outside(svg):
    with pytest.raises(ToolError):
        prepare_svg(svg, {})


def test_a_drawing_links_to_its_own_parts_and_inlines_kept_images():
    photo = Attachment("photo.png", "image/png", png())
    svg = prepare_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="300" height="200"><defs><linearGradient id="g"/></defs>'
        '<rect fill="url(#g)" onclick="x()"/><use xlink:href="#g"/>'
        '<image href="photo.png" width="300" height="200"/></svg>',
        {"photo.png": photo},
    )
    assert "data:image/png;base64," in svg.text and 'href="#g"' in svg.text
    assert "onclick" not in svg.text
    assert (svg.width, svg.height) == (1200, 800)


def test_a_huge_or_sizeless_drawing_renders_at_a_bounded_size():
    huge = prepare_svg('<svg xmlns="http://www.w3.org/2000/svg" width="90000" height="45000"/>', {})
    assert (huge.width, huge.height) == (1200, 600)
    bare = prepare_svg('<svg xmlns="http://www.w3.org/2000/svg"/>', {})
    assert (bare.width, bare.height) == (1200, 1200)
    with pytest.raises(ToolError, match="long and thin"):
        prepare_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10000 1"/>', {})


async def test_an_animation_mixes_drawings_and_kept_photos_into_a_video(ctx):
    pytest.importorskip("av")
    ctx.conversation.files["photo.jpg"] = Attachment(
        "photo.jpg", "image/jpeg", png("green", (400, 300), "JPEG")
    )
    result = await tools()["make_animation"].handler(
        {"frames": [SQUARE, "photo.jpg"], "frame_ms": 500, "file_name": "étapes"},
        ctx,
    )

    assert result["created"] == "étapes.mp4"
    file = ctx.conversation.files["étapes.mp4"]
    assert file.mimetype == "video/mp4" and file.data[4:8] == b"ftyp"
    import av

    with av.open(BytesIO(file.data)) as video:
        stream = video.streams.video[0]
        assert (stream.width, stream.height) == (1200, 600)
        assert float(stream.duration * stream.time_base) >= 5.9  # looped to a few seconds
    assert ctx.actions == [{"type": "share_file", "name": "étapes.mp4"}]


async def test_without_the_video_extra_an_animation_is_a_gif(ctx, monkeypatch):
    monkeypatch.setattr(illustrations, "find_spec", lambda name: None)
    make = tools()["make_animation"]
    assert make.input_schema["properties"]["format"]["enum"] == ["gif"]
    assert make.description.startswith("Make an animated GIF")
    result = await make.handler({"frames": [SQUARE, SQUARE], "send": False}, ctx)
    assert result["created"] == "animation.gif"


async def test_an_animation_can_be_a_gif(ctx):
    result = await tools()["make_animation"].handler(
        {"frames": [SQUARE, SQUARE.replace("#c00", "#00c")], "format": "gif", "send": False},
        ctx,
    )
    gif = Image.open(BytesIO(ctx.conversation.files[result["created"]].data))
    assert gif.format == "GIF" and gif.n_frames == 2 and gif.info["loop"] == 0
    assert ctx.actions == []


async def test_an_animation_needs_frames_it_can_read(ctx):
    make = tools()["make_animation"]
    with pytest.raises(ToolError, match="at least 2"):
        await make.handler({"frames": [SQUARE]}, ctx)
    with pytest.raises(ToolError, match="neither an SVG nor a kept image"):
        await make.handler({"frames": [SQUARE, "/etc/passwd"]}, ctx)


EXERCISES = [
    {
        "id": "Hammer_Curls",
        "name": "Hammer Curls",
        "equipment": "dumbbell",
        "level": "beginner",
        "primaryMuscles": ["biceps"],
        "category": "strength",
        "instructions": ["Stand up with the torso upright.", "Curl the weights."],
        "images": ["Hammer_Curls/0.jpg", "Hammer_Curls/1.jpg"],
    },
    {
        "id": "Cable_Hammer_Curls_-_Rope_Attachment",
        "name": "Cable Hammer Curls - Rope Attachment",
        "equipment": "cable",
        "primaryMuscles": ["biceps"],
        "instructions": [],
        "images": [],
    },
    {"id": "Plank", "name": "Plank", "equipment": "body only", "instructions": [], "images": []},
]


@respx.mock
async def test_exercise_photos_are_found_in_english_and_kept_under_the_users_name(ctx):
    french = tools(bank=ExerciseBank(None, BANK), language="fr")["save_exercise_photos"]
    assert "'<name> - départ.jpg'" in french.description
    db = respx.get(f"{BANK}/dist/exercises.json").respond(json=EXERCISES)
    respx.get(f"{BANK}/exercises/Hammer_Curls/0.jpg").respond(content=png("red", kind="JPEG"))
    respx.get(f"{BANK}/exercises/Hammer_Curls/1.jpg").respond(content=png("blue", kind="JPEG"))
    async with httpx.AsyncClient() as http:
        found = tools(bank=ExerciseBank(http, BANK))
        result = await found["find_exercise_photos"].handler({"query": "hammer curl"}, ctx)
        assert [e["id"] for e in result["exercises"]] == [
            "Hammer_Curls",
            "Cable_Hammer_Curls_-_Rope_Attachment",
        ]
        only = await found["find_exercise_photos"].handler(
            {"query": "hammer curl", "equipment": "dumbbell"}, ctx
        )
        assert [e["id"] for e in only["exercises"]] == ["Hammer_Curls"]

        saved = await found["save_exercise_photos"].handler(
            {"exercise_id": "Hammer_Curls", "name": "Curl marteau"}, ctx
        )
        with pytest.raises(ToolError, match="no photo"):
            await found["save_exercise_photos"].handler({"exercise_id": "Plank"}, ctx)
        with pytest.raises(ToolError, match="No such exercise"):
            await found["save_exercise_photos"].handler({"exercise_id": "../../x"}, ctx)

    assert db.call_count == 1  # read once
    assert saved["saved"] == ["Curl marteau - start.jpg", "Curl marteau - end.jpg"]
    assert ctx.conversation.files["Curl marteau - start.jpg"].mimetype == "image/jpeg"
    assert ctx.actions == []


@respx.mock
async def test_an_unreachable_bank_says_so(ctx):
    respx.get(f"{BANK}/dist/exercises.json").respond(500)
    async with httpx.AsyncClient() as http:
        find = tools(bank=ExerciseBank(http, BANK))["find_exercise_photos"]
        with pytest.raises(ToolError, match="can't be reached"):
            await find.handler({"query": "plank"}, ctx)


def test_without_a_bank_only_the_drawing_tools_exist():
    assert set(tools()) == {"draw_image", "make_animation"}


async def test_a_drawing_goes_into_a_pdf(ctx):
    await tools()["draw_image"].handler({"svg": SQUARE, "send": False}, ctx)
    create = {t.name: t for t in build_document_tools(FONTS)}["create_pdf"]
    await create.handler(
        {"title": "Program", "html": '<h1>Program</h1><img src="drawing.png">'}, ctx
    )
    assert b"/Subtype /Image" in ctx.conversation.files["Program.pdf"].data
    assert json.dumps(ctx.actions)  # the PDF is shared
