"""Stage 6: visual and audio understanding per window, behind an adapter (spec section 3)."""

from __future__ import annotations

import os
import time
from importlib import resources
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from .models import Inference, ShotRole, TranscriptSegment
from .segment import shots_in_window

PROMPT_VERSION = "window-v1"
SCHEMA_VERSION = "moment-1"
MAX_OFFLINE_SHOT_MS = 8_000


class RawMoment(BaseModel):
    """What a model returns, in seconds relative to the window it saw."""

    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    observations: list[str]
    inferences: list[Inference] = []
    shot_role: ShotRole = "other"
    relevance: float = Field(default=0.5, ge=0, le=1)
    visible_text: list[str] = []
    location_evidence: str | None = None


class WindowResult(BaseModel):
    moments: list[RawMoment]


class WindowUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class VideoModel(Protocol):
    model_id: str

    def analyze_window(
        self,
        proxy: Path,
        window_ms: tuple[int, int],
        transcript: list[TranscriptSegment],
        cuts_ms: list[int],
    ) -> tuple[WindowResult, WindowUsage]: ...


def _window_transcript(
    transcript: list[TranscriptSegment], window_ms: tuple[int, int]
) -> list[TranscriptSegment]:
    start, end = window_ms
    return [s for s in transcript if s.end_ms > start and s.start_ms < end]


class OfflineVideoModel:
    """Deterministic stand-in used when no video model is configured.

    It never describes what it cannot see: moments come from shot cuts and the
    transcript, and observations say only that.
    """

    model_id = "offline-heuristic-2"

    def analyze_window(self, proxy, window_ms, transcript, cuts_ms):
        start, _ = window_ms
        speech = _window_transcript(transcript, window_ms)
        moments = []
        for a, b in shots_in_window(window_ms, cuts_ms):
            pieces = max(1, -(-(b - a) // MAX_OFFLINE_SHOT_MS))
            step = (b - a) / pieces
            for i in range(pieces):
                s, e = int(a + i * step), int(a + (i + 1) * step)
                said = [
                    t for t in speech if min(e, t.end_ms) - max(s, t.start_ms) > (t.end_ms - t.start_ms) / 2
                ]
                if said:
                    obs = [f'Someone says: "{t.text}"' for t in said]
                    role, relevance = "dialogue", 0.7
                else:
                    obs = ["Quiet shot, picked for picture quality"]
                    role, relevance = ("establishing" if i == 0 and s == a else "other"), 0.4
                moments.append(
                    RawMoment(
                        start_s=(s - start) / 1000,
                        end_s=(e - start) / 1000,
                        observations=obs,
                        shot_role=role,
                        relevance=relevance,
                    )
                )
        return WindowResult(moments=moments), WindowUsage()


class GeminiVideoModel:
    """Gemini over the proxy. The proxy is uploaded once per asset and each window
    is requested with clip offsets instead of re-uploading cut files."""

    def __init__(self, model_id: str | None = None, api_key: str | None = None, fps: float = 1.0):
        from google import genai

        self._genai = genai
        self._client = genai.Client(api_key=api_key or os.environ["GEMINI_API_KEY"])
        self.model_id = model_id or os.environ.get("BIDU_GEMINI_MODEL", "gemini-2.5-flash")
        self.fps = fps
        self._uploaded: dict[Path, object] = {}
        self._prompt = resources.files("bidu_analysis.prompts").joinpath("window_v1.md").read_text()

    def _file(self, proxy: Path):
        if proxy not in self._uploaded:
            f = self._client.files.upload(file=str(proxy))
            while getattr(f.state, "name", "") == "PROCESSING":
                time.sleep(2)
                f = self._client.files.get(name=f.name)
            if getattr(f.state, "name", "") == "FAILED":
                raise RuntimeError("Video model could not process this clip")
            self._uploaded[proxy] = f
        return self._uploaded[proxy]

    def release(self, proxy: Path) -> None:
        """Delete the provider copy as soon as analysis of this asset finishes."""
        f = self._uploaded.pop(proxy, None)
        if f is not None:
            self._client.files.delete(name=f.name)

    def analyze_window(self, proxy, window_ms, transcript, cuts_ms):
        types = self._genai.types
        start, end = window_ms
        lines = [
            f"[{(t.start_ms - start) / 1000:.1f}s-{(t.end_ms - start) / 1000:.1f}s] {t.text}"
            for t in _window_transcript(transcript, window_ms)
        ]
        prompt = self._prompt.format(
            window_start=f"{start / 1000:.1f}s",
            window_end=f"{end / 1000:.1f}s",
            transcript="\n".join(lines) or "(no speech detected)",
        )
        f = self._file(proxy)
        video = types.Part(
            file_data=types.FileData(file_uri=f.uri, mime_type="video/mp4"),
            video_metadata=types.VideoMetadata(
                start_offset=f"{start / 1000:.3f}s", end_offset=f"{end / 1000:.3f}s", fps=self.fps
            ),
        )
        resp = self._client.models.generate_content(
            model=self.model_id,
            contents=types.Content(role="user", parts=[video, types.Part(text=prompt)]),
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=WindowResult, temperature=0.2
            ),
        )
        result = (
            resp.parsed
            if isinstance(resp.parsed, WindowResult)
            else WindowResult.model_validate_json(resp.text)
        )
        meta = resp.usage_metadata
        usage = WindowUsage(
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        )
        return result, usage


def default_video_model() -> VideoModel:
    if os.environ.get("GEMINI_API_KEY") and os.environ.get("BIDU_VIDEO_MODEL", "auto") != "offline":
        return GeminiVideoModel()
    return OfflineVideoModel()
