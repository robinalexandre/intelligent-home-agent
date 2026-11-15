"""Pictures and videos made by an AI model, on any subject.

A language model writes no pictures (draw_image renders the SVG it writes): photo-like
images and real video clips come from an image or video model, billed on that provider's
account, through its REST API. Two providers are known: Google (Gemini image, Veo) and
OpenAI (gpt-image, Sora), or any server that speaks OpenAI's images API at another
address. Each is used for what it has a model for: one with no video model makes no video.
Another provider is a class with the same two methods (see Provider).

What goes out is the model's prompt and, when asked, kept images the user sent or the agent
made: the prompt is checked like a web search's query, so it can't carry out what the
request read. A few generations per request at most, so a loop can't run up the bill.
"""

from __future__ import annotations

import asyncio
import base64
import time
import weakref
from io import BytesIO
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.documents import IMAGE_TYPES

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_URL = "https://api.openai.com/v1"
ASPECTS = ("1:1", "16:9", "9:16", "4:3", "3:4")
MAX_PROMPT_CHARS = 4_000
MAX_REFERENCES = 3
# A clip takes one to a few minutes; past this the request gives up and says so.
VIDEO_DEADLINE_SECONDS = 600.0
POLL_SECONDS = 10.0
IMAGE_TIMEOUT = 180.0
VEO_SECONDS = (4, 6, 8)
SORA_SECONDS = (4, 8, 12)
# gpt-image's sizes for each shape asked.
OPENAI_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "4:3": "1536x1024",
    "9:16": "1024x1536",
    "3:4": "1024x1536",
}


class GenerationError(Exception):
    """The provider refused or failed: its reason, meant for the model."""


class Provider(Protocol):
    name: str
    # Empty: this provider makes no picture, or no video.
    image_model: str
    video_model: str

    async def image(
        self, prompt: str, aspect: str, references: list[Attachment]
    ) -> tuple[bytes, str]: ...

    async def video(
        self, prompt: str, seconds: int, portrait: bool, start: Attachment | None
    ) -> bytes: ...


def _nearest(seconds: int, allowed: tuple[int, ...]) -> int:
    return min(allowed, key=lambda a: (abs(a - seconds), a))


def _reason(resp: httpx.Response) -> str:
    try:
        error = resp.json().get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
    except ValueError:
        message = ""
    return (message or resp.reason_phrase or "error")[:300]


async def _checked(resp: httpx.Response, provider: str) -> httpx.Response:
    if resp.status_code >= 400:
        if resp.status_code in (401, 403):
            raise GenerationError(f"{provider} refused the API key ({resp.status_code}).")
        raise GenerationError(f"{provider}: {_reason(resp)} ({resp.status_code})")
    return resp


class Gemini:
    """Gemini's image model and Veo, with an AI Studio key."""

    name = "gemini"

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        image_model: str,
        video_model: str,
        base_url: str = GEMINI_URL,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._http = http
        self._headers = {"x-goog-api-key": api_key}
        self.image_model = image_model
        self.video_model = video_model
        self._base = base_url.rstrip("/")
        self._poll = poll_seconds

    async def image(
        self, prompt: str, aspect: str, references: list[Attachment]
    ) -> tuple[bytes, str]:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        parts += [
            {"inlineData": {"mimeType": f.mimetype, "data": base64.b64encode(f.data).decode()}}
            for f in references
        ]
        resp = await self._http.post(
            f"{self._base}/models/{self.image_model}:generateContent",
            headers=self._headers,
            json={
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "responseModalities": ["IMAGE"],
                    "imageConfig": {"aspectRatio": aspect},
                },
            },
            timeout=IMAGE_TIMEOUT,
        )
        data = (await _checked(resp, "Gemini")).json()
        for candidate in data.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    mimetype = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    return base64.b64decode(inline["data"]), mimetype
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        finish = next((c.get("finishReason") for c in data.get("candidates") or []), None)
        raise GenerationError(
            f"Gemini made no image ({blocked or finish or 'no reason given'}): reword the "
            "prompt, or try the other provider."
        )

    async def video(
        self, prompt: str, seconds: int, portrait: bool, start: Attachment | None
    ) -> bytes:
        instance: dict[str, Any] = {"prompt": prompt}
        if start is not None:
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(start.data).decode(),
                "mimeType": start.mimetype,
            }
        resp = await self._http.post(
            f"{self._base}/models/{self.video_model}:predictLongRunning",
            headers=self._headers,
            json={
                "instances": [instance],
                "parameters": {
                    "aspectRatio": "9:16" if portrait else "16:9",
                    "durationSeconds": _nearest(seconds, VEO_SECONDS),
                },
            },
            timeout=60,
        )
        operation = (await _checked(resp, "Veo")).json()
        name = str(operation.get("name") or "")
        if not name.startswith("models/"):
            raise GenerationError("Veo did not start the video.")
        deadline = time.monotonic() + VIDEO_DEADLINE_SECONDS
        while not operation.get("done"):
            if time.monotonic() > deadline:
                raise GenerationError("Veo took too long: ask again later.")
            await asyncio.sleep(self._poll)
            resp = await self._http.get(f"{self._base}/{name}", headers=self._headers, timeout=60)
            operation = (await _checked(resp, "Veo")).json()
        if operation.get("error"):
            raise GenerationError(f"Veo: {str(operation['error'].get('message'))[:300]}")
        result = (operation.get("response") or {}).get("generateVideoResponse") or {}
        samples = result.get("generatedSamples") or []
        uri = str(((samples[0] if samples else {}).get("video") or {}).get("uri") or "")
        if not uri:
            reasons = "; ".join(map(str, result.get("raiMediaFilteredReasons") or []))
            raise GenerationError(f"Veo made no video ({reasons or 'filtered'}): reword it.")
        # The key goes only to Google's own address.
        if urlsplit(uri).hostname != urlsplit(self._base).hostname:
            raise GenerationError("Veo gave its video at an unexpected address.")
        resp = await self._http.get(uri, headers=self._headers, timeout=120, follow_redirects=True)
        return (await _checked(resp, "Veo")).content


class OpenAI:
    """gpt-image and Sora with an OpenAI API key, or another server that speaks the same
    images API (``base_url``)."""

    name = "openai"

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        image_model: str,
        video_model: str,
        base_url: str = OPENAI_URL,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._http = http
        # A local server needs no key.
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.image_model = image_model
        self.video_model = video_model
        self._base = base_url.rstrip("/")
        self._poll = poll_seconds

    async def image(
        self, prompt: str, aspect: str, references: list[Attachment]
    ) -> tuple[bytes, str]:
        fields = {
            "model": self.image_model,
            "prompt": prompt,
            "size": OPENAI_SIZES.get(aspect, "1024x1024"),
            "quality": "medium",
            "output_format": "png",
            "n": 1,
        }
        if references:
            resp = await self._http.post(
                f"{self._base}/images/edits",
                headers=self._headers,
                data={k: str(v) for k, v in fields.items()},
                files=[("image[]", (f.filename, f.data, f.mimetype)) for f in references],
                timeout=IMAGE_TIMEOUT,
            )
        else:
            resp = await self._http.post(
                f"{self._base}/images/generations",
                headers=self._headers,
                json=fields,
                timeout=IMAGE_TIMEOUT,
            )
        data = (await _checked(resp, "OpenAI")).json()
        images = data.get("data") or []
        b64 = images[0].get("b64_json") if images else None
        if not b64:
            raise GenerationError("OpenAI made no image: reword the prompt.")
        return base64.b64decode(b64), "image/png"

    async def video(
        self, prompt: str, seconds: int, portrait: bool, start: Attachment | None
    ) -> bytes:
        size = "720x1280" if portrait else "1280x720"
        fields = {
            "model": self.video_model,
            "prompt": prompt,
            "seconds": str(_nearest(seconds, SORA_SECONDS)),
            "size": size,
        }
        if start is not None:
            # Sora wants the first frame at the clip's own size.
            reference = _fitted(start, size)
            resp = await self._http.post(
                f"{self._base}/videos",
                headers=self._headers,
                data=fields,
                files={"input_reference": ("start.jpg", reference, "image/jpeg")},
                timeout=60,
            )
        else:
            resp = await self._http.post(
                f"{self._base}/videos", headers=self._headers, json=fields, timeout=60
            )
        video = (await _checked(resp, "Sora")).json()
        video_id = str(video.get("id") or "")
        if not video_id or "/" in video_id:
            raise GenerationError("Sora did not start the video.")
        deadline = time.monotonic() + VIDEO_DEADLINE_SECONDS
        while video.get("status") not in ("completed", "failed"):
            if time.monotonic() > deadline:
                raise GenerationError("Sora took too long: ask again later.")
            await asyncio.sleep(self._poll)
            resp = await self._http.get(
                f"{self._base}/videos/{video_id}", headers=self._headers, timeout=60
            )
            video = (await _checked(resp, "Sora")).json()
        if video["status"] == "failed":
            message = (video.get("error") or {}).get("message") or "failed"
            raise GenerationError(f"Sora: {str(message)[:300]}")
        resp = await self._http.get(
            f"{self._base}/videos/{video_id}/content", headers=self._headers, timeout=120
        )
        return (await _checked(resp, "Sora")).content


def _fitted(file: Attachment, size: str) -> bytes:
    width, height = (int(n) for n in size.split("x"))
    with Image.open(BytesIO(file.data)) as image:
        frame = image.convert("RGB")
    scale = max(width / frame.width, height / frame.height)
    frame = frame.resize((round(frame.width * scale), round(frame.height * scale)))
    left, top = (frame.width - width) // 2, (frame.height - height) // 2
    out = BytesIO()
    frame.crop((left, top, left + width, top + height)).save(out, "JPEG", quality=92)
    return out.getvalue()


def _name(raw: Any, default: str, extension: str) -> str:
    name = str(raw or "").strip() or default
    known = (".png", ".jpg", ".jpeg", ".webp", ".mp4")
    stem = name.rsplit(".", 1)[0] if name.lower().endswith(known) else name
    return f"{stem}.{extension}"


def build_generation_tools(
    providers: list[Provider],
    max_images: int = 6,
    max_videos: int = 2,
    *,
    language: str = "en",
) -> list[Tool]:
    """generate_image when a provider has an image model, generate_video when one has a
    video model; the first provider listed is the default for each."""
    for_images = {p.name: p for p in providers if p.image_model}
    for_videos = {p.name: p for p in providers if p.video_model}
    s = strings(language)
    used: weakref.WeakKeyDictionary[ToolContext, dict[str, int]] = weakref.WeakKeyDictionary()

    def provider_for(args: dict[str, Any], by_name: dict[str, Provider]) -> Provider:
        asked = str(args.get("provider") or "").lower()
        if asked and asked not in by_name:
            raise ToolError(f"Not configured: {asked}. Available: {', '.join(by_name)}.")
        return by_name.get(asked) or next(iter(by_name.values()))

    def checked_prompt(args: dict[str, Any], ctx: ToolContext) -> str:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("prompt is required: describe the picture in detail.")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ToolError("This prompt is too long: describe it in fewer words.")
        if what := ctx.leaks.found(prompt):
            raise ToolError(
                f"This prompt repeats {what} from what this request holds (memory, earlier "
                "replies, what was read): it can't go to an outside model. Describe the "
                "picture in the user's own words."
            )
        return prompt

    def count(ctx: ToolContext, kind: str, limit: int) -> None:
        done = used.setdefault(ctx, {})
        if done.get(kind, 0) >= limit:
            raise ToolError(f"Already {limit} {kind}s made for this request: that's the limit.")
        done[kind] = done.get(kind, 0) + 1

    def kept_image(ctx: ToolContext, name: str) -> Attachment:
        file = ctx.conversation.files.get(name)
        if file is None or file.mimetype not in IMAGE_TYPES:
            raise ToolError(f"No kept image named {name!r} (see list_saved_files).")
        # Others' files (an email's, a web page's) don't go to an outside model.
        if file.origin not in ("user", "agent"):
            raise ToolError(
                f"{name} came from {file.origin}: only images the user sent or "
                "you made can be given to the model."
            )
        try:
            with Image.open(BytesIO(file.data)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise ToolError(f"{name} can't be read as an image.") from exc
        return file

    def keep(ctx: ToolContext, file: Attachment, send: bool) -> str:
        kept = ctx.conversation.keep_file(file)
        if send:
            ctx.actions.append({"type": "share_file", "name": kept.filename})
        return kept.filename

    async def generate_image(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        prompt = checked_prompt(args, ctx)
        provider = provider_for(args, for_images)
        aspect = str(args.get("aspect_ratio") or "1:1")
        if aspect not in ASPECTS:
            raise ToolError(f"aspect_ratio is one of {', '.join(ASPECTS)}.")
        names = args.get("reference_images") or []
        if not isinstance(names, list) or len(names) > MAX_REFERENCES:
            raise ToolError(f"At most {MAX_REFERENCES} reference images, by kept name.")
        references = [kept_image(ctx, str(n)) for n in names]
        count(ctx, "image", max_images)
        try:
            data, mimetype = await provider.image(prompt, aspect, references)
        except GenerationError as exc:
            raise ToolError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"{provider.name} can't be reached right now.") from exc
        extension = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mimetype, "png")
        send = args.get("send", True) is not False
        name = _name(args.get("file_name"), s.image_name, extension)
        file = Attachment(name, mimetype, data, "agent")
        return {
            "created": keep(ctx, file, send),
            "by": provider.name,
            "sent": send,
            "check": "read_saved_file shows it: look before relying on it (hands, text and "
            "anatomy can be wrong); generate again with a better prompt if needed.",
        }

    async def generate_video(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        prompt = checked_prompt(args, ctx)
        provider = provider_for(args, for_videos)
        start_name = str(args.get("start_image") or "").strip()
        start = kept_image(ctx, start_name) if start_name else None
        try:
            seconds = int(args.get("seconds") or 8)
        except (TypeError, ValueError) as exc:
            raise ToolError("seconds is a number, 4 to 12.") from exc
        count(ctx, "video", max_videos)
        try:
            data = await provider.video(prompt, seconds, args.get("portrait") is True, start)
        except GenerationError as exc:
            raise ToolError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"{provider.name} can't be reached right now.") from exc
        send = args.get("send", True) is not False
        name = _name(args.get("file_name"), s.video_name, "mp4")
        file = Attachment(name, "video/mp4", data, "agent")
        return {
            "created": keep(ctx, file, send),
            "by": provider.name,
            "size_kb": round(len(data) / 1024),
            "sent": send,
        }

    def choice(by_name: dict[str, Provider]) -> dict[str, Any]:
        return {
            "type": "string",
            "enum": list(by_name),
            "description": f"Default {next(iter(by_name))}.",
        }

    send = {"type": "boolean", "description": "Send it to the user now (default true)."}
    tools = []
    if for_images:
        tools.append(
            Tool(
                name="generate_image",
                description=(
                    "Have an AI image model make a picture from a description: a photo-like "
                    "scene, an illustration, a logo idea, a room redone, a product shot... "
                    "Billed per picture: only when the user wants a generated picture, not "
                    "for a diagram or a chart (draw_image, when you have it, does those "
                    "exactly and for free). Write a detailed prompt in English (subject, "
                    "setting, style, framing, light). reference_images (kept images the "
                    "user sent or you made) are edited or followed: 'this photo of my "
                    "living room, with green walls'. The picture is kept with the "
                    "conversation's files, for a PDF or to send to the user."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "aspect_ratio": {"type": "string", "enum": list(ASPECTS)},
                        "reference_images": {"type": "array", "items": {"type": "string"}},
                        "provider": choice(for_images),
                        "file_name": {"type": "string"},
                        "send": send,
                    },
                    "required": ["prompt"],
                },
                handler=generate_image,
            )
        )
    if for_videos:
        tools.append(
            Tool(
                name="generate_video",
                description=(
                    "Have an AI video model make a short clip (4 to 12 s) from a "
                    "description, optionally starting from a kept image (start_image): a "
                    "movement shown by a person, a scene, an animated photo. Takes one to a "
                    "few minutes and is billed per second: only when the user asks for a "
                    "generated video (make_animation, when you have it, turns pictures into "
                    "an animation for free). Write the prompt in English: subject, action, "
                    "camera, setting."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "seconds": {"type": "integer", "description": "4 to 12; default 8."},
                        "portrait": {"type": "boolean", "description": "Vertical, for a phone."},
                        "start_image": {"type": "string"},
                        "provider": choice(for_videos),
                        "file_name": {"type": "string"},
                        "send": send,
                    },
                    "required": ["prompt"],
                },
                handler=generate_video,
            )
        )
    return tools
