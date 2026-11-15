"""Pictures and videos the agent makes for the user, whatever the subject: a diagram, plan
or drawing it writes in SVG, an animation (MP4 video or GIF) from drawings or kept images,
and, as one source among others, photos of exercises from a free, public-domain bank.

They are kept like the PDFs the agent writes, so create_pdf can show the pictures
(<img src=...>) and a tool that attaches files can take them; the user receives them as
pictures and videos.

Needs the ``documents`` extra (resvg-py); MP4 videos also need the ``video`` extra
(PyAV), without which animations are GIFs. The exercise bank is a setting, off by default.

An SVG is the model's own text, but it is rendered on the server: nothing in it may load a
file or an address. Its links may only point inside itself (#id) or name a kept image,
which is inlined before rendering.
"""

from __future__ import annotations

import base64
import re
from fractions import Fraction
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, ParseError, register_namespace, tostring  # noqa: S405

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.documents import FONT_DIR, IMAGE_TYPES, find_fonts

try:
    import resvg_py
    from PIL import Image, ImageFont, UnidentifiedImageError

    INSTALLED = True
except ImportError:  # no documents extra: build_illustration_tools returns []
    INSTALLED = False

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
MAX_SVG_CHARS = 200_000
MAX_FRAMES = 24
# A video plays its frames over and over for at least this long (chat apps show a few
# seconds' clip better than one that ends at once), at a steady rate players all accept.
VIDEO_FPS = 25
MIN_VIDEO_MS = 6_000
MAX_VIDEO_MS = 30_000
# The long side of a drawing, in pixels: sharp in a PDF and on a phone, light to send.
LONG_SIDE = 1200
DEFAULT_SIDE = 100  # what an SVG with neither size nor viewBox measures
FORBIDDEN_TAGS = frozenset({"script", "foreignObject", "iframe", "audio", "video"})
_URL = re.compile(r"url\(\s*['\"]?\s*([^)'\"\s]*)", re.IGNORECASE)
_LENGTH = re.compile(r"^\s*([0-9.]+)\s*(px)?\s*$")

# free-exercise-db: some 870 exercises with a photo of their start and end positions,
# released into the public domain (Unlicense). Pinned to a commit, so what it says can't
# change under the agent.
EXERCISE_DB = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/f00c92c7dcf1216a928a52c3706c7ce8e2f71ed5"
MAX_MATCHES = 10


class _Svg:
    """An SVG checked and made self-contained, with the size to render it at."""

    def __init__(self, text: str, width: int, height: int) -> None:
        self.text = text
        self.width = width
        self.height = height


def prepare_svg(svg: str, images: dict[str, Attachment]) -> _Svg:
    """Check svg loads nothing from outside, inline the kept images it names, and choose
    its pixel size; ToolError says what to change."""
    if len(svg) > MAX_SVG_CHARS:
        raise ToolError("This SVG is too long: simplify the drawing.")
    try:
        root = fromstring(svg)
    except (ParseError, DefusedXmlException) as exc:
        raise ToolError(f"This SVG is not valid XML: {exc}") from exc
    if _local(root.tag) != "svg":
        raise ToolError("The drawing must be one <svg> element.")
    for element in root.iter():
        name = _local(element.tag)
        if name in FORBIDDEN_TAGS:
            raise ToolError(f"<{name}> is not allowed in a drawing.")
        if name == "style":
            _check_css(element.text or "")
        for key, value in list(element.attrib.items()):
            local = _local(key)
            if local.lower().startswith("on"):
                del element.attrib[key]
            elif local == "href":
                element.attrib[key] = _link(value, images)
            elif "url(" in value.lower():
                _check_css(value)
    width, height = _size(root)
    register_namespace("", SVG_NS)
    register_namespace("xlink", XLINK_NS)
    return _Svg(tostring(root, encoding="unicode"), width, height)


def _local(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _check_css(css: str) -> None:
    if "@import" in css.lower():
        raise ToolError("@import is not allowed in a drawing.")
    for target in _URL.findall(css):
        if not target.startswith("#"):
            raise ToolError("url() may only point inside the drawing (url(#id)).")


def _link(value: str, images: dict[str, Attachment]) -> str:
    value = value.strip()
    if value.startswith("#"):
        return value
    file = images.get(value)
    if file is None:
        raise ToolError(
            f"{value!r} is not a kept image: a drawing links only to its own parts (#id) "
            "or to a kept image by its file name (see list_saved_files)."
        )
    return f"data:{file.mimetype};base64,{base64.b64encode(file.data).decode()}"


def _size(root: Element) -> tuple[int, int]:
    """The pixel size to render at: the SVG's own proportions, LONG_SIDE on its long side."""
    view = (root.get("viewBox") or "").replace(",", " ").split()
    width, height = _length(root.get("width")), _length(root.get("height"))
    if (width is None or height is None) and len(view) == 4:
        try:
            vw, vh = float(view[2]), float(view[3])
        except ValueError:
            vw = vh = 0
        if vw > 0 and vh > 0:
            if width is None and height is None:
                width, height = vw, vh
            elif width is None:
                width = height * vw / vh
            else:
                height = width * vh / vw
    width, height = width or DEFAULT_SIDE, height or DEFAULT_SIDE
    if max(width / height, height / width) > 20:
        raise ToolError("This drawing is too long and thin: give it a viewBox closer to square.")
    scale = LONG_SIDE / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _length(value: str | None) -> float | None:
    match = _LENGTH.match(value or "")
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    return number if number > 0 else None


def font_families(font_dir: Path = FONT_DIR) -> dict[str, str]:
    """The names a drawing's text is set in: DejaVu's three families when the folder is
    DejaVu's, else the folder's own family for all three; nothing without fonts."""
    fonts = find_fonts(font_dir)
    if fonts is None:
        return {}
    if fonts.family == "DejaVuSans":
        return {
            "font_family": "DejaVu Sans",
            "sans_serif_family": "DejaVu Sans",
            "serif_family": "DejaVu Serif",
            "monospace_family": "DejaVu Sans Mono",
        }
    try:
        name = ImageFont.truetype(str(fonts.regular)).getname()[0]
    except OSError:
        return {}
    if not name:
        return {}
    return dict.fromkeys(
        ("font_family", "sans_serif_family", "serif_family", "monospace_family"), name
    )


def render_svg(svg: _Svg, font_dir: Path = FONT_DIR) -> Image.Image:
    fonts = [str(font_dir)] if font_dir.is_dir() else None
    try:
        png = resvg_py.svg_to_bytes(
            svg_string=svg.text,
            width=svg.width,
            height=svg.height,
            background="#ffffff",
            font_dirs=fonts,
            **font_families(font_dir),
        )
    except ValueError as exc:
        raise ToolError(f"This drawing could not be rendered: {exc}") from exc
    image = Image.open(BytesIO(bytes(png)))
    image.load()
    return image


def png_bytes(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def gif_bytes(frames: list[Image.Image], frame_ms: int) -> bytes:
    """An animation that loops forever, every frame shown frame_ms."""
    palette = [f.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE) for f in frames]
    out = BytesIO()
    palette[0].save(
        out,
        "GIF",
        save_all=True,
        append_images=palette[1:],
        duration=frame_ms,
        loop=0,
        disposal=2,
        optimize=True,
    )
    return out.getvalue()


def mp4_bytes(frames: list[Image.Image], frame_ms: int) -> bytes:
    """An H.264 video showing every frame frame_ms, the whole repeated for a few seconds."""
    try:
        import av  # the video extra
    except ImportError as exc:
        raise ToolError("Videos can't be made on this server: make a GIF instead.") from exc
    width, height = (side - side % 2 for side in frames[0].size)  # H.264 wants even sides
    per_frame = max(1, round(frame_ms * VIDEO_FPS / 1000))
    once = len(frames) * per_frame * 1000 // VIDEO_FPS  # one pass, in ms
    rounds = max(1, min(-(-MIN_VIDEO_MS // once), MAX_VIDEO_MS // once or 1))
    out = BytesIO()
    with av.open(out, "w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=Fraction(VIDEO_FPS))
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        stream.options = {"crf": "23", "preset": "veryfast", "movflags": "+faststart"}
        video = [
            av.VideoFrame.from_image(f.convert("RGB").crop((0, 0, width, height))) for f in frames
        ]
        stream.codec_context.time_base = Fraction(1, VIDEO_FPS)
        shown = 0
        for _ in range(rounds):
            for frame in video:
                for _ in range(per_frame):
                    frame.pts, frame.time_base = shown, Fraction(1, VIDEO_FPS)
                    shown += 1
                    for packet in stream.encode(frame):
                        container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return out.getvalue()


def _name(raw: Any, default: str, extension: str) -> str:
    name = str(raw or "").strip() or default
    known = (".png", ".gif", ".svg", ".mp4", ".jpg", ".jpeg")
    stem = name.rsplit(".", 1)[0] if name.lower().endswith(known) else name
    return f"{stem}.{extension}"


def _kept_frame(name: str, images: dict[str, Attachment]) -> Image.Image:
    file = images.get(name)
    if file is None:
        raise ToolError(f"{name[:80]!r} is neither an SVG nor a kept image (see list_saved_files).")
    try:
        with Image.open(BytesIO(file.data)) as image:
            image.seek(0)
            frame = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ToolError(f"{name} can't be read as an image.") from exc
    scale = LONG_SIDE / max(frame.size)
    if scale < 1:
        frame = frame.resize((round(frame.width * scale), round(frame.height * scale)))
    return frame


def _fit(frames: list[Image.Image]) -> list[Image.Image]:
    """Every frame on a white canvas the size of the first, centred, proportions kept."""
    size = frames[0].size
    fitted = []
    for frame in frames:
        if frame.size == size:
            fitted.append(frame)
            continue
        copy = frame.copy()
        copy.thumbnail(size)
        canvas = Image.new("RGB", size, "white")
        canvas.paste(copy, ((size[0] - copy.width) // 2, (size[1] - copy.height) // 2))
        fitted.append(canvas)
    return fitted


def _kept_images(ctx: ToolContext) -> dict[str, Attachment]:
    return {n: f for n, f in ctx.conversation.files.items() if f.mimetype in IMAGE_TYPES}


def _keep(ctx: ToolContext, file: Attachment, send: bool) -> str:
    kept = ctx.conversation.keep_file(file)
    if send:
        ctx.actions.append({"type": "share_file", "name": kept.filename})
    return kept.filename


class ExerciseBank:
    """free-exercise-db, read once and kept in memory."""

    def __init__(self, http: httpx.AsyncClient, base_url: str = EXERCISE_DB) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._exercises: list[dict[str, Any]] | None = None

    async def exercises(self) -> list[dict[str, Any]]:
        if self._exercises is None:
            try:
                resp = await self._http.get(f"{self._base}/dist/exercises.json", timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ToolError("The exercise bank can't be reached right now.") from exc
            self._exercises = [e for e in data if isinstance(e, dict) and e.get("id")]
        return self._exercises

    async def find(self, query: str, equipment: str = "") -> list[dict[str, Any]]:
        words = [w for w in re.split(r"[\s_-]+", query.lower()) if w]
        found = []
        for exercise in await self.exercises():
            name = str(exercise.get("name") or "").lower()
            if equipment and str(exercise.get("equipment") or "") != equipment:
                continue
            text = " ".join(
                [name, *(exercise.get("primaryMuscles") or []), exercise.get("category") or ""]
            )
            hits = sum(w in text for w in words)
            if words and hits == len(words):
                # Whole words of the name first, then shorter (plainer) names.
                found.append((-sum(w in name.split() for w in words), len(name), exercise))
        found.sort(key=lambda t: (t[0], t[1]))
        return [e for *_, e in found[:MAX_MATCHES]]

    async def get(self, exercise_id: str) -> dict[str, Any] | None:
        return next((e for e in await self.exercises() if e["id"] == exercise_id), None)

    async def photo(self, path: str) -> bytes:
        if ".." in path or path.startswith("/"):
            raise ToolError("Not a photo of the bank.")
        try:
            resp = await self._http.get(f"{self._base}/exercises/{path}", timeout=30)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError("This exercise's photos can't be downloaded right now.") from exc
        return resp.content


def build_illustration_tools(
    http: httpx.AsyncClient | None = None,
    font_dir: Path = FONT_DIR,
    bank: ExerciseBank | None = None,
    *,
    language: str = "en",
) -> list[Tool]:
    """draw_image and make_animation; the exercise photo tools too when ``http`` (or a
    ``bank``) is given. Nothing without the documents extra."""
    if not INSTALLED:
        return []
    if bank is None and http is not None:
        bank = ExerciseBank(http)
    s = strings(language)
    videos = find_spec("av") is not None

    async def draw_image(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        svg = str(args.get("svg") or "").strip()
        if not svg:
            raise ToolError("svg is required.")
        image = render_svg(prepare_svg(svg, _kept_images(ctx)), font_dir)
        send = args.get("send", True) is not False
        name = _name(args.get("file_name"), s.drawing_name, "png")
        file = Attachment(name, "image/png", png_bytes(image), "agent")
        return {
            "created": _keep(ctx, file, send),
            "size_kb": round(len(file.data) / 1024),
            "sent": send,
            "check": "read_saved_file shows it: look at it before sending or putting it in a PDF.",
        }

    async def make_animation(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        frames = args.get("frames")
        if not isinstance(frames, list) or len(frames) < 2:
            raise ToolError("Give at least 2 frames: SVG drawings or kept images' names.")
        if len(frames) > MAX_FRAMES:
            raise ToolError(f"At most {MAX_FRAMES} frames.")
        images = _kept_images(ctx)
        rendered = [
            render_svg(prepare_svg(f, images), font_dir)
            if f.lstrip().startswith("<")
            else _kept_frame(f.strip(), images)
            for f in map(str, frames)
        ]
        frame_ms = min(max(int(args.get("frame_ms") or 800), 80), 10_000)
        send = args.get("send", True) is not False
        if str(args.get("format") or ("mp4" if videos else "gif")).lower() == "gif":
            name = _name(args.get("file_name"), s.animation_name, "gif")
            data, mimetype = gif_bytes(_fit(rendered), frame_ms), "image/gif"
        else:
            name = _name(args.get("file_name"), s.animation_name, "mp4")
            data, mimetype = mp4_bytes(_fit(rendered), frame_ms), "video/mp4"
        file = Attachment(name, mimetype, data, "agent")
        return {
            "created": _keep(ctx, file, send),
            "size_kb": round(len(data) / 1024),
            "sent": send,
        }

    async def find_exercise_photos(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        assert bank is not None
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required, in English (e.g. 'hammer curl').")
        matches = await bank.find(query, str(args.get("equipment") or ""))
        return {
            "exercises": [
                {
                    "id": e["id"],
                    "name": e.get("name"),
                    "equipment": e.get("equipment"),
                    "level": e.get("level"),
                    "muscles": e.get("primaryMuscles"),
                    "how": " ".join(e.get("instructions") or [])[:600],
                }
                for e in matches
            ],
            "note": "" if matches else "Nothing found: try other English words, or fewer.",
        }

    async def save_exercise_photos(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        assert bank is not None
        exercise = await bank.get(str(args.get("exercise_id") or ""))
        if exercise is None:
            raise ToolError("No such exercise: take its id from find_exercise_photos.")
        paths = [p for p in exercise.get("images") or [] if isinstance(p, str)][:2]
        if not paths:
            raise ToolError("This exercise has no photo in the bank.")
        label = str(args.get("name") or "").strip() or str(exercise.get("name") or exercise["id"])
        send = args.get("send") is True
        names = []
        for path, step in zip(paths, s.exercise_steps, strict=False):
            data = await bank.photo(path)
            try:
                with Image.open(BytesIO(data)) as image:
                    image.verify()
            except (UnidentifiedImageError, OSError) as exc:
                raise ToolError("This exercise's photos can't be read.") from exc
            file = Attachment(f"{label} - {step}.jpg", "image/jpeg", data, "agent")
            names.append(_keep(ctx, file, send))
        return {
            "saved": names,
            "sent": send,
            "license": "public domain (free-exercise-db): no credit needed",
            "next": "make_animation with these names shows the movement as a video.",
        }

    tools = [
        Tool(
            name="draw_image",
            description=(
                "Draw a picture by writing SVG, rendered to PNG on the server: a diagram, a "
                "flow, a plan of a room or a garden, a chart, a timeline, a body position "
                "(stick figure), an annotated photo... Text with accents is fine. "
                "Links may only point to the drawing's own parts (#id) or to a kept image by "
                "its file name, which you can draw over. The picture is kept with the files "
                "of the conversation: show it in a PDF with create_pdf "
                '(<img src="its name">) or '
                "send it to the user."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "svg": {"type": "string", "description": "The drawing, with a viewBox."},
                    "file_name": {"type": "string", "description": "e.g. planning.png"},
                    "send": {
                        "type": "boolean",
                        "description": "Send it to the user now (default true); false when "
                        "it only goes into a PDF.",
                    },
                },
            },
            handler=draw_image,
        ),
        Tool(
            name="make_animation",
            description=(
                (
                    "Make a short video (MP4, default) or an animated GIF"
                    if videos
                    else "Make an animated GIF"
                )
                + " from frames shown one after the other, in a loop: a movement, the steps "
                "of a process, a before and after, a change over time. Each frame is an SVG "
                "drawing (as for draw_image) or the file name of a kept image: a photo the "
                "user sent, one you downloaded or saved, a picture you drew. A GIF's first "
                "frame is what a PDF shows."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "frames": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2 to 24 frames: SVG text or kept images' names.",
                    },
                    "frame_ms": {"type": "integer", "description": "Per frame; default 800."},
                    "format": {"type": "string", "enum": ["mp4", "gif"] if videos else ["gif"]},
                    "file_name": {"type": "string"},
                    "send": {
                        "type": "boolean",
                        "description": "Send it to the user now (default true).",
                    },
                },
                "required": ["frames"],
            },
            handler=make_animation,
        ),
    ]
    if bank is not None:
        tools += [
            Tool(
                name="find_exercise_photos",
                description=(
                    "Search a free bank of ~870 gym and home exercises (public domain), each "
                    "with a photo of its start and end positions. Search with the exercise's "
                    "English name (push-up, hammer curl); equipment narrows it: body only, "
                    "dumbbell, barbell, kettlebells, bands, cable, machine, exercise ball, "
                    "medicine ball, foam roll, e-z curl bar, other."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "equipment": {"type": "string"},
                    },
                    "required": ["query"],
                },
                handler=find_exercise_photos,
            ),
            Tool(
                name="save_exercise_photos",
                description=(
                    "Keep an exercise's two photos from the bank: "
                    f"'<name> - {s.exercise_steps[0]}.jpg' and "
                    f"'<name> - {s.exercise_steps[1]}.jpg'. Put them in a PDF with "
                    "create_pdf, or turn them "
                    "into a video of the movement with make_animation. When the bank has "
                    "nothing close enough, draw the positions with draw_image instead."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "exercise_id": {"type": "string"},
                        "name": {
                            "type": "string",
                            "description": "What the user calls it, in their language.",
                        },
                        "send": {"type": "boolean", "description": "Default false."},
                    },
                    "required": ["exercise_id"],
                },
                handler=save_exercise_photos,
            ),
        ]
    return tools
