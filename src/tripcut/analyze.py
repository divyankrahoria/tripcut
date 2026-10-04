"""Stage 2: understand each clip.

Two providers:
  * `gemini` - Gemini watches and listens to a small proxy of each clip (best results).
  * `local`  - no API key, no upload: audio-loudness heuristics. Good for a quick
               rough cut and for trying tripcut out.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol

from . import ffmpeg
from .models import ClipAnalysis, ClipInfo, Moment, TranscriptLine
from .prompts import CLIP_PROMPT

DEFAULT_MODEL = os.environ.get("TRIPCUT_MODEL", "gemini-2.5-flash")


class Provider(Protocol):
    name: str

    def analyze(self, clip: ClipInfo, workdir: Path) -> ClipAnalysis: ...


# --------------------------------------------------------------------------- parsing


_TS = re.compile(r"^\s*(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\s*$")


def to_seconds(value: Any) -> float:
    """Accept 12.5, "12.5", "0:12", "00:00:12.5" - models are not always consistent."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().rstrip("s")
    m = _TS.match(s)
    if m:
        h, mnt, sec = m.groups()
        return int(h or 0) * 3600 + int(mnt) * 60 + float(sec)
    return float(s)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response (tolerates ```json fences)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model response: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def analysis_from_json(raw: dict[str, Any], clip: ClipInfo, provider: str) -> ClipAnalysis:
    def clamp(t: Any) -> float:
        return min(max(0.0, to_seconds(t)), clip.duration)

    moments = []
    for m in raw.get("moments") or []:
        try:
            s, e = clamp(m["start"]), clamp(m["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if e - s < 0.5:
            continue
        kind = "speech" if str(m.get("kind", "")).lower().startswith("speech") else "broll"
        try:
            score = float(m.get("score", 5))
        except (TypeError, ValueError):
            score = 5.0
        moments.append(Moment(s, e, kind, str(m.get("description", "")), max(1.0, min(10.0, score))))

    transcript = []
    for t in raw.get("transcript") or []:
        try:
            transcript.append(TranscriptLine(clamp(t["start"]), clamp(t["end"]), str(t["text"]).strip()))
        except (KeyError, ValueError, TypeError):
            continue

    scene = str(raw.get("scene_type") or "broll").lower()
    return ClipAnalysis(
        clip=clip.name,
        summary=str(raw.get("summary") or ""),
        location_guess=raw.get("location_guess") or None,
        scene_type=scene if scene in {"talking", "broll", "mixed"} else "broll",
        tags=[str(t) for t in raw.get("tags") or []][:12],
        transcript=transcript,
        moments=sorted(moments, key=lambda m: m.start),
        quality_issues=[str(q) for q in raw.get("quality_issues") or []],
        provider=provider,
    )


# --------------------------------------------------------------------------- providers


class GeminiProvider:
    """Uploads a 480p proxy of each clip to Gemini and asks for a structured log."""

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        try:
            from google import genai
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Install the Gemini SDK: pip install google-genai") from e
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "No Gemini API key. Get a free one at https://aistudio.google.com/apikey "
                "then `export GEMINI_API_KEY=...` (or use --provider local)."
            )
        from google import genai

        self.client = genai.Client(api_key=key)
        self.model = model
        self.name = f"gemini:{model}"

    def _wait_active(self, f, timeout: float = 600):
        deadline = time.time() + timeout
        while getattr(f.state, "name", str(f.state)) in {"PROCESSING", "STATE_UNSPECIFIED", "None"}:
            if time.time() > deadline:
                raise TimeoutError(f"Gemini took too long to process {f.name}")
            time.sleep(3)
            f = self.client.files.get(name=f.name)
        if getattr(f.state, "name", str(f.state)) != "ACTIVE":
            raise RuntimeError(f"Gemini could not process the upload (state={f.state})")
        return f

    def generate_json(self, contents: list, retries: int = 3) -> dict[str, Any]:
        from google.genai import types

        last: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4),
                )
                return extract_json(resp.text or "")
            except Exception as e:  # rate limits, transient 5xx, malformed JSON
                last = e
                time.sleep(min(60, 5 * 2**attempt))
        raise RuntimeError(f"Gemini request failed after {retries} attempts: {last}")

    def analyze(self, clip: ClipInfo, workdir: Path) -> ClipAnalysis:
        proxy = workdir / "proxies" / (Path(clip.name).stem + ".mp4")
        if not proxy.exists():
            ffmpeg.make_proxy(Path(clip.path), proxy)
        uploaded = self._wait_active(self.client.files.upload(file=str(proxy)))
        try:
            prompt = CLIP_PROMPT.format(duration=clip.duration, created=clip.created_at or "unknown")
            raw = self.generate_json([uploaded, prompt])
        finally:
            try:
                self.client.files.delete(name=uploaded.name)
            except Exception:
                pass
        return analysis_from_json(raw, clip, self.name)


class LocalProvider:
    """No-AI fallback: loud stretches become "speech" moments, the rest is sampled as b-roll."""

    name = "local"

    def analyze(self, clip: ClipInfo, workdir: Path) -> ClipAnalysis:
        moments: list[Moment] = []
        loud: list[tuple[float, float]] = []
        if clip.has_audio:
            loud = _merge(ffmpeg.loud_intervals(Path(clip.path), clip.duration), gap=0.8)
        talky = [(s, e) for s, e in loud if e - s >= 2.0]
        talk_time = sum(e - s for s, e in talky)
        is_talking = talk_time >= 0.35 * clip.duration and len(talky) <= max(3, clip.duration / 8)

        if is_talking:
            for s, e in talky:
                moments.append(Moment(round(s, 2), round(e, 2), "speech", "audio activity", 6.0))
        else:
            # Evenly spaced b-roll windows, skipping the first/last second (camera handling).
            usable = max(0.0, clip.duration - 2.0)
            n = max(1, min(4, int(usable // 6) + 1))
            win = min(4.0, max(1.5, 0.7 * usable / n if usable else clip.duration))
            for i in range(n):
                center = 1.0 + usable * (i + 0.5) / n if usable else clip.duration / 2
                s = max(0.0, center - win / 2)
                e = min(clip.duration, s + win)
                if e - s >= 1.0:
                    moments.append(Moment(round(s, 2), round(e, 2), "broll", "sampled shot", 5.0))

        return ClipAnalysis(
            clip=clip.name,
            summary="talking to camera" if is_talking else "b-roll footage",
            scene_type="talking" if is_talking else "broll",
            moments=moments,
            provider=self.name,
        )


def _merge(intervals: list[tuple[float, float]], gap: float) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for s, e in sorted(intervals):
        if out and s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


def make_provider(name: str, model: str = DEFAULT_MODEL) -> Provider:
    if name == "gemini":
        return GeminiProvider(model=model)
    if name == "local":
        return LocalProvider()
    raise ValueError(f"Unknown provider {name!r} (choose gemini or local)")


# --------------------------------------------------------------------------- caching


def cache_key(clip: ClipInfo, provider_name: str) -> str:
    st = os.stat(clip.path)
    h = hashlib.sha1(f"{clip.path}|{st.st_size}|{int(st.st_mtime)}|{provider_name}".encode()).hexdigest()
    return f"{Path(clip.name).stem}-{h[:10]}.json"


def analyze_clip(provider: Provider, clip: ClipInfo, workdir: Path, force: bool = False) -> tuple[ClipAnalysis, bool]:
    """Returns (analysis, was_cached)."""
    cache = workdir / "analysis" / cache_key(clip, provider.name)
    if cache.exists() and not force:
        return ClipAnalysis.from_dict(json.loads(cache.read_text())), True
    result = provider.analyze(clip, workdir)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result.to_dict(), indent=2))
    return result, False
