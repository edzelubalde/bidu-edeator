"""Stage 5: timestamped transcription that keeps code-switched speech as spoken."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from .models import TranscriptSegment, Word

UNCERTAIN_WORD = 0.5


class Transcriber(Protocol):
    model_id: str

    def transcribe(self, audio: Path, asset_id: str, run_id: str) -> list[TranscriptSegment]: ...


class NoTranscriber:
    """Used when no speech model is installed; the footage is still analyzed visually."""

    model_id = "none"

    def transcribe(self, audio: Path, asset_id: str, run_id: str) -> list[TranscriptSegment]:
        return []


class FasterWhisperTranscriber:
    """faster-whisper with VAD and word timestamps.

    ``language=None`` lets the model detect per file; Taglish quality must be
    measured on the evaluation set before trusting it (spec section 11).
    """

    def __init__(self, model_size: str | None = None, device: str = "auto"):
        from faster_whisper import WhisperModel

        size = model_size or os.environ.get("BIDU_WHISPER_MODEL", "large-v3")
        compute = "int8" if device in ("cpu", "auto") else "float16"
        self._model = WhisperModel(size, device=device, compute_type=compute)
        self.model_id = f"faster-whisper/{size}"

    def transcribe(self, audio: Path, asset_id: str, run_id: str) -> list[TranscriptSegment]:
        segments, info = self._model.transcribe(
            str(audio),
            language=None,
            task="transcribe",
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        out = []
        for i, seg in enumerate(segments):
            words = [
                Word(
                    start_ms=int(w.start * 1000),
                    end_ms=int(w.end * 1000),
                    text=w.word.strip(),
                    probability=w.probability,
                )
                for w in seg.words or []
            ]
            out.append(
                TranscriptSegment(
                    id=f"{run_id}-t{i:04d}",
                    asset_id=asset_id,
                    analysis_run_id=run_id,
                    start_ms=int(seg.start * 1000),
                    end_ms=int(seg.end * 1000),
                    text=seg.text.strip(),
                    language=info.language,
                    words=words,
                    uncertain=any((w.probability or 1) < UNCERTAIN_WORD for w in words),
                )
            )
        return out


def default_transcriber() -> Transcriber:
    if os.environ.get("BIDU_TRANSCRIBER", "auto") == "none":
        return NoTranscriber()
    try:
        return FasterWhisperTranscriber()
    except ImportError:
        return NoTranscriber()
