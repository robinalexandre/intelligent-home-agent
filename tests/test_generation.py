import base64
import json
from io import BytesIO

import httpx
import pytest
import respx
from PIL import Image

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.tools import (
    Gemini,
    OpenAI,
    ToolError,
    ToolRegistry,
    build_generation_tools,
)

G = "https://gemini.test/v1beta"
OPENAI = "https://openai.test/v1"
PNG = b"\x89PNG generated"
MP4 = b"\x00\x00\x00\x18ftypmp42 clip"


def photo(size=(300, 200)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, "green").save(out, "JPEG")
    return out.getvalue()


def tools(http, *names, **limits):
    providers = {
        "gemini": Gemini(http, "gkey", "img-model", "veo-model", G, poll_seconds=0),
        "openai": OpenAI(http, "okey", "gpt-image-x", "sora-x", OPENAI, poll_seconds=0),
    }
    built = build_generation_tools([providers[n] for n in names], **limits)
    return {t.name: t for t in built}


def test_without_a_key_there_is_no_generation_tool():
    assert build_generation_tools([]) == []


def test_a_provider_is_used_only_for_what_it_has_a_model_for():
    pictures = OpenAI(None, "", "local-image-model", "", "http://192.168.1.50:8080/v1")
    clips = Gemini(None, "gkey", "", "veo-model", G)
    built = {t.name: t for t in build_generation_tools([pictures, clips])}

    image, video = built["generate_image"], built["generate_video"]
    assert image.input_schema["properties"]["provider"]["enum"] == ["openai"]
    assert video.input_schema["properties"]["provider"]["enum"] == ["gemini"]
    assert [t.name for t in build_generation_tools([pictures])] == ["generate_image"]


@respx.mock
async def test_a_local_images_server_gets_no_key(ctx):
    local = "http://192.168.1.50:8080/v1"
    route = respx.post(f"{local}/images/generations").respond(
        json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
    )
    async with httpx.AsyncClient() as http:
        (image,) = build_generation_tools([OpenAI(http, "", "local-model", "", local)])
        result = await image.handler({"prompt": "A lighthouse", "send": False}, ctx)

    assert result["created"] == "image.png" and not result["sent"]
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
async def test_gemini_makes_an_image_from_a_prompt_and_a_kept_photo(ctx):
    route = respx.post(f"{G}/models/img-model:generateContent").respond(
        json={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "here"},
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": base64.b64encode(PNG).decode(),
                                }
                            },
                        ]
                    }
                }
            ]
        }
    )
    ctx.conversation.files["room.jpg"] = Attachment("room.jpg", "image/jpeg", photo())
    async with httpx.AsyncClient() as http:
        result = await tools(http, "gemini")["generate_image"].handler(
            {
                "prompt": "This living room with sage green walls",
                "aspect_ratio": "16:9",
                "reference_images": ["room.jpg"],
                "file_name": "green room",
            },
            ctx,
        )

    assert result["created"] == "green room.png" and result["by"] == "gemini"
    assert ctx.conversation.files["green room.png"].data == PNG
    assert ctx.actions == [{"type": "share_file", "name": "green room.png"}]
    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == "gkey"
    body = json.loads(request.content)
    assert body["generationConfig"] == {
        "responseModalities": ["IMAGE"],
        "imageConfig": {"aspectRatio": "16:9"},
    }
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "This living room with sage green walls"}
    assert parts[1]["inlineData"]["mimeType"] == "image/jpeg"


@respx.mock
async def test_a_blocked_image_says_why(ctx):
    respx.post(f"{G}/models/img-model:generateContent").respond(
        json={"promptFeedback": {"blockReason": "SAFETY"}}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="SAFETY"):
            await tools(http, "gemini")["generate_image"].handler({"prompt": "x"}, ctx)


@respx.mock
async def test_openai_makes_an_image_and_edits_a_photo(ctx):
    gen = respx.post(f"{OPENAI}/images/generations").respond(
        json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
    )
    edit = respx.post(f"{OPENAI}/images/edits").respond(
        json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
    )
    ctx.conversation.files["me.jpg"] = Attachment("me.jpg", "image/jpeg", photo())
    async with httpx.AsyncClient() as http:
        both = tools(http, "gemini", "openai")["generate_image"]
        await both.handler(
            {"prompt": "A lighthouse", "provider": "openai", "aspect_ratio": "9:16"}, ctx
        )
        await both.handler(
            {
                "prompt": "Same, in watercolor",
                "provider": "openai",
                "reference_images": ["me.jpg"],
            },
            ctx,
        )

    sent = json.loads(gen.calls[0].request.content)
    assert sent["model"] == "gpt-image-x" and sent["size"] == "1024x1536"
    assert gen.calls[0].request.headers["authorization"] == "Bearer okey"
    multipart = edit.calls[0].request.content
    assert b'name="image[]"' in multipart and b"watercolor" in multipart


@respx.mock
async def test_veo_waits_for_the_clip_and_downloads_it_with_the_key(ctx):
    start = respx.post(f"{G}/models/veo-model:predictLongRunning").respond(
        json={"name": "models/veo-model/operations/op1"}
    )
    uri = "https://gemini.test/v1beta/files/abc:download?alt=media"
    respx.get(f"{G}/models/veo-model/operations/op1").mock(
        side_effect=[
            httpx.Response(200, json={"name": "op1", "done": False}),
            httpx.Response(
                200,
                json={
                    "done": True,
                    "response": {
                        "generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]}
                    },
                },
            ),
        ]
    )
    download = respx.get(uri).respond(content=MP4)
    ctx.conversation.files["pose.jpg"] = Attachment("pose.jpg", "image/jpeg", photo(), "agent")
    async with httpx.AsyncClient() as http:
        result = await tools(http, "gemini")["generate_video"].handler(
            {"prompt": "A man does a hammer curl", "seconds": 7, "start_image": "pose.jpg"}, ctx
        )

    assert result["created"] == "video.mp4"
    assert ctx.conversation.files["video.mp4"].mimetype == "video/mp4"
    body = json.loads(start.calls[0].request.content)
    assert body["parameters"] == {"aspectRatio": "16:9", "durationSeconds": 6}
    assert body["instances"][0]["image"]["mimeType"] == "image/jpeg"
    assert download.calls[0].request.headers["x-goog-api-key"] == "gkey"


@respx.mock
async def test_veo_never_sends_the_key_to_another_host(ctx):
    respx.post(f"{G}/models/veo-model:predictLongRunning").respond(
        json={
            "name": "models/veo-model/operations/op1",
            "done": True,
            "response": {
                "generateVideoResponse": {
                    "generatedSamples": [{"video": {"uri": "https://evil.example/v.mp4"}}]
                }
            },
        }
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="unexpected address"):
            await tools(http, "gemini")["generate_video"].handler({"prompt": "x"}, ctx)


@respx.mock
async def test_sora_polls_then_downloads_and_a_failure_says_why(ctx):
    create = respx.post(f"{OPENAI}/videos").mock(
        side_effect=[
            httpx.Response(200, json={"id": "video_1", "status": "queued"}),
            httpx.Response(200, json={"id": "video_2", "status": "queued"}),
        ]
    )
    respx.get(f"{OPENAI}/videos/video_1").mock(
        side_effect=[
            httpx.Response(200, json={"id": "video_1", "status": "in_progress"}),
            httpx.Response(200, json={"id": "video_1", "status": "completed"}),
        ]
    )
    respx.get(f"{OPENAI}/videos/video_1/content").respond(content=MP4)
    respx.get(f"{OPENAI}/videos/video_2").respond(
        json={"id": "video_2", "status": "failed", "error": {"message": "moderation"}}
    )
    async with httpx.AsyncClient() as http:
        video = tools(http, "openai")["generate_video"]
        result = await video.handler({"prompt": "Waves", "portrait": True, "send": False}, ctx)
        with pytest.raises(ToolError, match="moderation"):
            await video.handler({"prompt": "Other"}, ctx)

    assert ctx.conversation.files[result["created"]].data == MP4 and ctx.actions == []
    assert json.loads(create.calls[0].request.content) == {
        "model": "sora-x",
        "prompt": "Waves",
        "seconds": "8",
        "size": "720x1280",
    }


@respx.mock
async def test_a_bad_key_and_an_unknown_provider_are_named(ctx):
    respx.post(f"{G}/models/img-model:generateContent").respond(403, json={"error": {}})
    async with httpx.AsyncClient() as http:
        image = tools(http, "gemini")["generate_image"]
        with pytest.raises(ToolError, match="refused the API key"):
            await image.handler({"prompt": "x"}, ctx)
        with pytest.raises(ToolError, match="Not configured: openai"):
            await image.handler({"prompt": "x", "provider": "openai"}, ctx)


async def test_what_the_request_read_cant_go_out_in_a_prompt(ctx):
    ctx.leaks.add("IBAN GB33 BUKB 2020 1555 5555 55")
    async with httpx.AsyncClient() as http:
        registry = ToolRegistry()
        registry.extend(tools(http, "gemini").values())
        with pytest.raises(ToolError, match="can't go to an outside model"):
            await registry.run(
                "generate_image", {"prompt": "A card showing GB33BUKB20201555555555"}, ctx
            )
    assert ctx.conversation.files == {}


async def test_only_images_of_the_user_or_the_agent_go_to_the_model(ctx):
    ctx.conversation.files["att.jpg"] = Attachment("att.jpg", "image/jpeg", photo(), "mail")
    async with httpx.AsyncClient() as http:
        image = tools(http, "gemini")["generate_image"]
        with pytest.raises(ToolError, match="came from mail"):
            await image.handler({"prompt": "x", "reference_images": ["att.jpg"]}, ctx)
        with pytest.raises(ToolError, match="No kept image"):
            await image.handler({"prompt": "x", "reference_images": ["absent.png"]}, ctx)


@respx.mock
async def test_a_request_makes_only_a_few(ctx):
    respx.post(f"{G}/models/img-model:generateContent").respond(
        json={
            "candidates": [
                {"content": {"parts": [{"inlineData": {"data": base64.b64encode(PNG).decode()}}]}}
            ]
        }
    )
    async with httpx.AsyncClient() as http:
        image = tools(http, "gemini", max_images=2)["generate_image"]
        await image.handler({"prompt": "a"}, ctx)
        await image.handler({"prompt": "b"}, ctx)
        with pytest.raises(ToolError, match="limit"):
            await image.handler({"prompt": "c"}, ctx)
