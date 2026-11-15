"""Voice messages turned into text on the server, with faster-whisper: no model is sent
any audio, and nothing the user says leaves the machine. Optional: it needs the "voice"
extra (pyproject.toml) and WHISPER_MODEL; without them a voice message gets a line asking
the user to write instead.

The model is downloaded once to the data folder and loaded on first use (or at startup
by ``warm_up``); one voice message is transcribed at a time, in a worker thread, as the
CPU is shared with the rest of the agent. With several languages allowed, the likeliest
of them is picked for each message: Whisper left free can misread a short message as
some other language. A short prompt in that language, where the code has one, sets the
register (a message to an assistant), which keeps everyday words whole.

Only the first minutes are transcribed: a voice message can last most of an hour, and
would hold the CPU, and every voice message behind it, for many minutes.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import io
import logging
from pathlib import Path
from typing import Any

from intelligent_home_agent.i18n import LANGUAGES

log = logging.getLogger(__name__)

# faster-whisper decodes to 16 kHz mono.
SAMPLE_RATE = 16_000
MAX_MINUTES = 5
CUT_NOTE = f"[cut: only the first {MAX_MINUTES} minutes were transcribed]"


def available() -> bool:
    """Whether the "voice" extra is installed."""
    return importlib.util.find_spec("faster_whisper") is not None


def prompt_for(language: str | None) -> str | None:
    """The line that sets the register, in the languages the code speaks."""
    known = LANGUAGES.get(language or "")
    return known.voice_prompt if known else None


class Transcriber:
    def __init__(self, model: str, download_dir: str, languages: list[str] | None = None) -> None:
        self._model_name = model
        self._download_dir = download_dir
        self._languages = [lang for lang in languages or [] if lang]
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def warm_up(self) -> None:
        try:
            async with self._lock:
                await asyncio.to_thread(self._load)
        except Exception:
            log.exception("Could not load the %s speech model", self._model_name)

    async def transcribe(self, audio: bytes) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._run, audio)

    def _load(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel

            Path(self._download_dir).mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(
                self._model_name,
                device="cpu",
                compute_type="int8",
                download_root=self._download_dir,
            )
        return self._model

    def _language(self, model: Any, samples: Any) -> str | None:
        if len(self._languages) <= 1:
            return self._languages[0] if self._languages else None
        _, _, scores = model.detect_language(samples)
        likely = {lang: p for lang, p in scores if lang in self._languages}
        return max(likely, key=likely.__getitem__) if likely else self._languages[0]

    def _run(self, audio: bytes) -> str:
        from faster_whisper import decode_audio

        model = self._load()
        samples = decode_audio(io.BytesIO(audio))
        kept = MAX_MINUTES * 60 * SAMPLE_RATE
        cut = len(samples) > kept
        samples = samples[:kept]
        language = self._language(model, samples)
        segments, _ = model.transcribe(
            samples,
            language=language,
            initial_prompt=prompt_for(language),
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return f"{text} {CUT_NOTE}" if cut and text else text


@functools.cache
def shared_transcriber(model: str, download_dir: str, languages: tuple[str, ...]) -> Transcriber:
    """One per model in the process: the chat and the chat-reading tool share its memory."""
    return Transcriber(model, download_dir, list(languages))
