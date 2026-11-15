import sys
import types

import pytest

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.transcription import (
    CUT_NOTE,
    MAX_MINUTES,
    SAMPLE_RATE,
    Transcriber,
    prompt_for,
)


class FakeModel:
    def __init__(self, scores):
        self.scores = scores
        self.asked = []
        self.heard = 0

    def detect_language(self, samples):
        return self.scores[0][0], self.scores[0][1], self.scores

    def transcribe(self, samples, **options):
        self.asked.append(options)
        self.heard = len(samples)
        segment = types.SimpleNamespace(text=" Remind me tomorrow. ")
        return iter([segment]), None


@pytest.fixture(autouse=True)
def decoded(monkeypatch):
    samples = [0.0]
    module = types.ModuleType("faster_whisper")
    module.decode_audio = lambda audio: samples
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return samples


def transcriber(languages, scores):
    t = Transcriber("small", "unused", languages)
    t._model = FakeModel(scores)
    return t


async def test_the_likeliest_allowed_language_is_used_with_its_prompt():
    t = transcriber(["fr", "en"], [("it", 0.5), ("fr", 0.3), ("en", 0.2)])

    assert await t.transcribe(b"ogg") == "Remind me tomorrow."
    assert t._model.heard == 1
    assert t._model.asked == [
        {"language": "fr", "initial_prompt": strings("fr").voice_prompt, "vad_filter": True}
    ]


async def test_one_language_skips_the_detection():
    t = transcriber(["en"], [])

    await t.transcribe(b"ogg")

    assert t._model.asked[0]["language"] == "en"


async def test_a_long_voice_message_is_cut_and_says_so(decoded):
    kept = MAX_MINUTES * 60 * SAMPLE_RATE
    decoded[:] = [0.0] * (kept + SAMPLE_RATE)
    t = transcriber(["fr"], [])

    assert await t.transcribe(b"ogg") == f"Remind me tomorrow. {CUT_NOTE}"
    assert t._model.heard == kept


async def test_no_language_lets_whisper_detect_it():
    t = transcriber([], [])

    await t.transcribe(b"ogg")

    assert t._model.asked[0]["language"] is None
    assert t._model.asked[0]["initial_prompt"] is None


def test_a_language_the_code_has_no_words_for_gets_no_prompt():
    assert prompt_for("en") == strings("en").voice_prompt
    assert prompt_for("ja") is None and prompt_for(None) is None
