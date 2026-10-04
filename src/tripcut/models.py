"""Plain data structures passed between pipeline stages.

Everything here round-trips through JSON so each stage can be cached,
inspected, and hand-edited (looking at you, plan.json).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ClipInfo:
    path: str
    name: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    created_at: str | None = None  # ISO 8601
    created_source: str = "unknown"  # metadata | filename | mtime | unknown
    location: str | None = None  # ISO 6709 string if the camera recorded GPS
    is_360: bool = False

    @property
    def created_dt(self) -> datetime | None:
        if not self.created_at:
            return None
        try:
            return datetime.fromisoformat(self.created_at)
        except ValueError:
            return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ClipInfo:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Moment:
    start: float
    end: float
    kind: str = "broll"  # "speech" | "broll"
    description: str = ""
    score: float = 5.0  # 1-10, how good / important this moment is

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class TranscriptLine:
    start: float
    end: float
    text: str


@dataclass
class ClipAnalysis:
    clip: str  # ClipInfo.name
    summary: str = ""
    location_guess: str | None = None
    scene_type: str = "broll"  # "talking" | "broll" | "mixed"
    tags: list[str] = field(default_factory=list)
    transcript: list[TranscriptLine] = field(default_factory=list)
    moments: list[Moment] = field(default_factory=list)
    quality_issues: list[str] = field(default_factory=list)
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ClipAnalysis:
        return cls(
            clip=d["clip"],
            summary=d.get("summary", ""),
            location_guess=d.get("location_guess"),
            scene_type=d.get("scene_type", "broll"),
            tags=list(d.get("tags", [])),
            transcript=[TranscriptLine(**t) for t in d.get("transcript", [])],
            moments=[Moment(**m) for m in d.get("moments", [])],
            quality_issues=list(d.get("quality_issues", [])),
            provider=d.get("provider", ""),
        )


@dataclass
class Segment:
    clip: str  # ClipInfo.name
    start: float
    end: float
    role: str = "broll"  # "speech" | "broll" | "opener" | "closer"
    caption: str | None = None  # on-screen text for this segment, optional
    reason: str = ""  # why the planner picked it (for humans reviewing the plan)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class EditPlan:
    title: str
    segments: list[Segment]
    aspect: str = "16:9"
    notes: str = ""

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EditPlan:
        return cls(
            title=d.get("title", "My trip"),
            segments=[Segment(**s) for s in d.get("segments", [])],
            aspect=d.get("aspect", "16:9"),
            notes=d.get("notes", ""),
        )
