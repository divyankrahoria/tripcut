from tripcut.analyze import analysis_from_json, extract_json, to_seconds
from tripcut.export import to_ass, to_edl, to_srt, transcript_on_timeline
from tripcut.models import ClipAnalysis, ClipInfo, EditPlan, Moment, Segment, TranscriptLine
from tripcut.plan import ai_plan, heuristic_plan, validate_plan
from tripcut.probe import sort_chronologically, timestamp_from_filename


def clip(name, dur=20.0, when="2026-09-12T10:00:00", **kw):
    return ClipInfo(
        path=f"/x/{name}",
        name=name,
        duration=dur,
        width=1920,
        height=1080,
        fps=30,
        has_audio=True,
        created_at=when,
        **kw,
    )


def test_to_seconds_accepts_model_formats():
    assert to_seconds(12) == 12
    assert to_seconds("12.5") == 12.5
    assert to_seconds("0:12") == 12
    assert to_seconds("1:02.5") == 62.5
    assert to_seconds("01:00:03") == 3603
    assert to_seconds("4.2s") == 4.2


def test_extract_json_handles_fences_and_chatter():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('here you go {"b": [1, 2]} thanks') == {"b": [1, 2]}


def test_analysis_from_json_clamps_and_cleans():
    c = clip("a.mp4", dur=10)
    a = analysis_from_json(
        {
            "summary": "beach",
            "scene_type": "MIXED",
            "moments": [
                {"start": "0:08", "end": 30, "kind": "broll", "score": 99},  # end clamped, score clamped
                {"start": 2, "end": 2.1, "kind": "speech"},  # too short, dropped
                {"start": 1, "end": 4, "kind": "Speech", "score": "7"},
                {"bogus": True},
            ],
            "transcript": [{"start": 1, "end": 4, "text": " hi "}],
        },
        c,
        "test",
    )
    assert a.scene_type == "mixed"  # case-normalised
    assert analysis_from_json({"scene_type": "selfie"}, c, "t").scene_type == "broll"  # unknown -> default
    assert [(m.start, m.end, m.kind, m.score) for m in a.moments] == [(1, 4, "speech", 7), (8, 10, "broll", 10)]
    assert a.transcript[0].text == "hi"


def test_filename_timestamps_and_ordering():
    assert timestamp_from_filename("VID_20260913_181500_00_007.mp4").hour == 18
    assert timestamp_from_filename("IMG_1234.mp4") is None
    clips = [clip("b", when="2026-09-13T09:00:00"), clip("c", when=None), clip("a", when="2026-09-12T09:00:00")]
    assert [c.name for c in sort_chronologically(clips)] == ["a", "b", "c"]


def test_validate_plan_drops_clamps_merges_and_trims():
    clips = [clip("a.mp4", 10), clip("b.mp4", 60)]
    plan = EditPlan(
        "t",
        [
            Segment("nope.mp4", 0, 3),  # unknown -> dropped
            Segment("a", 2, 50),  # stem match + clamp to 10
            Segment("b.mp4", 5, 5.5),  # too short -> dropped
            Segment("b.mp4", 10, 14, "broll"),
            Segment("b.mp4", 14, 18, "broll"),  # contiguous -> merged
            Segment("b.mp4", 20, 50, "speech"),
        ],
    )
    out, warnings = validate_plan(plan, clips, target=0)
    assert [(s.clip, s.start, s.end) for s in out.segments] == [("a.mp4", 2, 10), ("b.mp4", 10, 18), ("b.mp4", 20, 50)]
    assert len(warnings) == 3

    long = EditPlan("t", [Segment("b.mp4", 0, 30, "broll"), Segment("b.mp4", 30.5, 40, "speech")])
    out, _ = validate_plan(long, clips, target=20)
    assert out.segments[1].duration == 9.5  # speech untouched
    assert out.duration < 40


def test_heuristic_plan_covers_every_clip_in_order():
    clips = [clip("a.mp4", 30, "2026-09-12T09:00:00"), clip("b.mp4", 30, "2026-09-13T09:00:00")]
    analyses = {
        "a.mp4": ClipAnalysis("a.mp4", moments=[Moment(1, 5, "broll", "x", 9), Moment(10, 14, "broll", "y", 4)]),
        "b.mp4": ClipAnalysis("b.mp4", moments=[Moment(2, 12, "speech", "talk", 6)]),
    }
    plan = heuristic_plan(clips, analyses, target=15, aspect="16:9")
    names = [s.clip for s in plan.segments]
    assert names[0] == "a.mp4" and "b.mp4" in names
    assert plan.segments[0].caption == "Sat 12 Sep"
    assert any(s.caption == "Sun 13 Sep" for s in plan.segments)


class FakeGemini:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def generate_json(self, contents):
        self.prompts.append(contents[0])
        return self.response


def test_ai_plan_parses_model_output_and_sends_clip_log():
    clips = [clip("a.mp4", 30)]
    analyses = {
        "a.mp4": ClipAnalysis(
            "a.mp4", summary="sunset over the bay", moments=[Moment(3, 8, "broll", "golden light", 9)]
        )
    }
    fake = FakeGemini(
        {
            "title": "Golden Hour",
            "notes": "n",
            "segments": [
                {"clip": "a.mp4", "start": "0:03", "end": 8, "role": "opener", "caption": "Lisbon"},
                {"clip": "a.mp4"},  # malformed -> skipped
            ],
        }
    )
    plan = ai_plan(fake, clips, analyses, 60, "9:16", "moody")
    assert plan.title == "Golden Hour" and plan.aspect == "9:16"
    assert [(s.start, s.end, s.caption) for s in plan.segments] == [(3, 8, "Lisbon")]
    assert "sunset over the bay" in fake.prompts[0] and "moody" in fake.prompts[0]


def test_exports():
    plan = EditPlan("Trip", [Segment("a.mp4", 1.0, 3.0, "opener", "Day 1"), Segment("b.mp4", 10.0, 12.5, "speech")])
    edl = to_edl(plan, fps=30)
    assert "001  AX       AA/V  C        00:00:01:00 00:00:03:00 01:00:00:00 01:00:02:00" in edl
    assert "002  AX       AA/V  C        00:00:10:00 00:00:12:15 01:00:02:00 01:00:04:15" in edl
    assert "* FROM CLIP NAME: b.mp4" in edl

    analyses = {
        "b.mp4": ClipAnalysis(
            "b.mp4", transcript=[TranscriptLine(9.0, 11.0, "we made it"), TranscriptLine(11.0, 20.0, "to the top")]
        )
    }
    cues = transcript_on_timeline(plan, analyses)
    assert cues == [(2.0, 3.0, "we made it"), (3.0, 4.5, "to the top")]
    assert "00:00:02,000 --> 00:00:03,000" in to_srt(cues)

    ass = to_ass(plan, 1080, 1920, cues)
    assert "Style: Title" in ass and "Day 1" in ass and "we made it" in ass
