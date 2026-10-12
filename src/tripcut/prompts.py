"""Prompts for the vision model (per clip) and the planner (whole trip)."""

# Bump when CLIP_PROMPT or CLIP_SCHEMA changes, so cached analyses are redone in the new format.
PROMPT_VERSION = 5

SHOTS = ("wide", "medium", "close", "detail")
MOTIONS = ("static", "pan", "tilt", "zoom", "walking", "handheld")
AUDIO = ("speech", "chatter", "ambient", "music", "plain", "noisy")
FRAMINGS = ("upright", "tilted", "sideways", "upside_down")
STABILITY = ("steady", "slight_shake", "shaky")
EXPOSURES = ("good", "bright", "dark", "backlit")
ENERGIES = ("calm", "lively", "intense")
TONES = ("scenic", "fun", "adventure", "chill", "warm")
TRANSITIONS = ("whip_pan", "wipe", "reveal")
TIMES_OF_DAY = ("dawn", "morning", "midday", "afternoon", "golden_hour", "dusk", "night", "indoors")

CLIP_PROMPT = """\
You are the assistant editor on a travel video. Watch AND listen to this whole clip.
It is {duration:.1f} seconds long, recorded {created} (camera's local time).

An editor will cut this clip on a timeline straight from your log, so timestamps
must be exact: seconds from the start of THIS clip, numbers with one decimal (12.5).

moments: the shots an editor could cut to, in time order, never overlapping.
- 2-8 per clip, each at least 1.5 s. Start where the shot settles; end before the
  camera drifts away or moves on. Leave out fumbling, blur, heavy shake and dead air.
  Keep a good shot even if it is crooked or sideways: an editor can rotate it, so
  report it in framing instead of dropping it.
- kind: "speech" only when the words are the point of the shot and said clearly:
  someone talking to the camera, or explaining something the viewer needs to hear.
  Start and end on sentence boundaries and keep the whole thought. Scenery with
  passing remarks ("that's the river", "let's go") is "broll".
- shot: wide (landscape, establishing), medium (a person or part of a scene),
  close (a face, one object), detail (texture, food, hands).
- motion (the camera's): static, pan, tilt, zoom, walking (moving forward with
  the filmer), handheld (small wobble).
- subject: 2-5 words naming what is in frame, e.g. "Watchman peak",
  "companion on footbridge", "Virgin River".
- audio: what the editor should do with the sound.
  speech: clear words worth hearing in the finished video (a complete thought, a
  funny line, a real reaction). Keep the voice.
  chatter: talk not worth keeping: fumbling, trailing off ("that's the..."), "um",
  half sentences, mumbling, people talking in the background. The editor mutes it
  and lays music over the picture. When in doubt between speech and chatter,
  choose chatter.
  ambient: natural sound worth keeping (river, birds, crowd).
  music: a song playing nearby; it must be muted for copyright.
  plain: nothing special. noisy: wind, shouting, handling.
- framing: upright; tilted (horizon visibly crooked); sideways (camera turned about
  90 degrees, picture on its side); upside_down.
- stability: steady; slight_shake (fine at normal speed); shaky (distracting).
- exposure: good; bright (blown-out sky or highlights); dark; backlit (subject
  dark against a bright background).
- energy: calm (still, scenic); lively (people moving, chatter); intense (action,
  cheering, fast movement). Used to pace the edit and match music.
- tone: scenic (the beauty of the place); fun (laughs, playfulness, silly
  moments); adventure (hiking, wading, effort); chill (resting, relaxing); warm
  (affection, people enjoying it together). The edit is a fun trip video, so
  mixing tones matters as much as picking pretty views.
- peak: the single best instant in the moment, in seconds. An editor who only
  uses two seconds of the shot will keep this instant.
- description: one line on what makes the shot worth using.
- score 1-10, judged against travel footage in general:
  9-10 trailer or opening shot (rare); 7-8 one of the strong shots; 5-6 good and
  usable; 3-4 filler only; 1-2 avoid. Most moments are 5-7. Give 8 or more to at
  most two moments in a clip.

For the whole clip:
- summary: 1-2 sentences on what happens.
- location_guess: the most specific place you can tell, e.g. "Zion Visitor Center,
  Zion National Park"; "" if you can't tell.
- time_of_day: judged from the light ("indoors" if inside).
- people: who appears or speaks, e.g. "filmer (off camera, narrating)",
  "companion, called Sam". Use a name only if it is said aloud.
- usable: false only if nothing is worth cutting to (lens covered, pocket recording).
- tags: short lowercase keywords.
- transitions: stretches that make good cut points between scenes, even when they
  are not usable as shots: whip_pan (the camera swings fast, the picture smears),
  wipe (something passes close in front of the lens and fills the frame), reveal
  (from a close, dark or blocked frame the view opens up, e.g. stepping out of a
  doorway). Give start, end and a short note such as "fast swing to the right".
  [] if none. Whip pans belong here, not in moments.
- visible_text: readable signs or text in frame with their time range, e.g.
  "Zion National Park sign 12.0-17.0"; [] if none.
- transcript: every spoken line with start and end; [] if nobody speaks.
- quality_issues: each problem with its time range, including things that spoil a
  shot, e.g. "heavy shake 0.0-6.0", "camera turned sideways 14.0-18.0",
  "wind noise throughout", "parked cars in frame 0.0-7.0".
"""

_TIME = {"type": "number", "minimum": 0}
_STRINGS = {"type": "array", "items": {"type": "string"}}

# Enforced by Gemini (response_json_schema), so every field arrives in a form Claude can cut from.
CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "location_guess": {"type": "string"},
        "scene_type": {"type": "string", "enum": ["talking", "broll", "mixed"]},
        "time_of_day": {"type": "string", "enum": list(TIMES_OF_DAY)},
        "people": _STRINGS,
        "usable": {"type": "boolean"},
        "tags": _STRINGS,
        "visible_text": _STRINGS,
        "transitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": _TIME,
                    "end": _TIME,
                    "type": {"type": "string", "enum": list(TRANSITIONS)},
                    "note": {"type": "string"},
                },
                "required": ["start", "end", "type", "note"],
            },
        },
        "transcript": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"start": _TIME, "end": _TIME, "text": {"type": "string"}},
                "required": ["start", "end", "text"],
            },
        },
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": _TIME,
                    "end": _TIME,
                    "kind": {"type": "string", "enum": ["speech", "broll"]},
                    "shot": {"type": "string", "enum": list(SHOTS)},
                    "motion": {"type": "string", "enum": list(MOTIONS)},
                    "subject": {"type": "string"},
                    "audio": {"type": "string", "enum": list(AUDIO)},
                    "framing": {"type": "string", "enum": list(FRAMINGS)},
                    "stability": {"type": "string", "enum": list(STABILITY)},
                    "exposure": {"type": "string", "enum": list(EXPOSURES)},
                    "energy": {"type": "string", "enum": list(ENERGIES)},
                    "tone": {"type": "string", "enum": list(TONES)},
                    "peak": _TIME,
                    "description": {"type": "string"},
                    "score": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": [
                    "start",
                    "end",
                    "kind",
                    "shot",
                    "motion",
                    "subject",
                    "audio",
                    "framing",
                    "stability",
                    "exposure",
                    "energy",
                    "tone",
                    "peak",
                    "description",
                    "score",
                ],
            },
        },
        "quality_issues": _STRINGS,
    },
    "required": [
        "summary",
        "location_guess",
        "scene_type",
        "time_of_day",
        "people",
        "usable",
        "tags",
        "visible_text",
        "transitions",
        "transcript",
        "moments",
        "quality_issues",
    ],
}

PLAN_PROMPT = """\
You are an expert travel-video editor. Below is a log of every clip from one trip,
in the order they were recorded, with the best moments an assistant flagged in each.

Cut a video of at most {target:.0f} seconds, aspect {aspect}. The footage decides the
length: include every distinct, strong moment that serves the story, then stop.
Never pad toward the maximum.
Style: {style}

Editing principles:
- Tell the story of the trip. Keep chronological order unless a strong opener
  from later in the trip is worth pulling forward as a cold open (max 1).
- The traveller's own talking moments are the spine: keep complete thoughts,
  never cut someone off mid-sentence. Trim rambling only at sentence boundaries.
- Use broll between talking parts to show what they are describing.
  Broll shots are usually 2-5 seconds. Vary shot size and camera motion; avoid two
  shots of the same subject in a row. Prefer audio=ambient shots where sound matters.
- Skip clips marked UNUSABLE, and moments with audio=music (copyrighted songs).
- Keep a voice only on audio=speech shots. audio=chatter is fumbling: use the shot
  for its picture with the voice muted under music, and never give it role "speech".
- Quality over coverage: use only strong shots (score 7+), and a 6 only when a
  place or story beat has nothing better. Leaving whole clips out is fine.
- Never repeat yourself. Travel footage has many look-alike shots (the same cliffs,
  the same river from similar angles). Anywhere in the video, use only the single
  best of shots that look alike: same subject, similar size and angle.
- Length follows the material: if there isn't enough distinct, strong footage for
  the target, finish shorter rather than repeat a look or use weak shots.
- Skip moments with quality issues, and prefer steady, upright, well-exposed shots.
  Use peak= to choose where to trim a long moment.
- A vertical clip loses most of its picture in a 16:9 cut (and a horizontal clip in
  9:16): use those only when the shot is worth it.
- Match the energy of neighbouring shots, building from calm to lively and back.
- Make it feel like a fun trip, not a nature documentary: mix tones, putting fun
  and warm moments with the people between the scenic shots.
- Cut like a professional: open on a hook (an 8+ shot inside the first 3 seconds)
  and end on a strong wide shot. Keep broll at 2-4 seconds, longer only for a
  real moment. Default to clean hard cuts and match cuts (similar shape or motion
  across the cut). Use an in-camera transition from a clip's transitions at most
  twice in the whole video, only where it is clearly good; a reveal is the safest.
  Overused whip pans look dated.
- Keep it natural, never try-hard: real moments and clear lines over showy shots.
  Captions are a few plain place names, nothing more.
- Each segment must use start/end times that lie inside that clip
  (you may tighten a flagged moment, but do not invent footage).
- Add a short on-screen "caption" ONLY when the location changes or for the opening
  title (e.g. "Day 2 — Lake Bled"). Most segments have caption null.

Return ONLY JSON:
{{
  "title": "a short, evocative title for the video",
  "notes": "one or two sentences on the story structure you chose",
  "segments": [
    {{"clip": "<exact clip name>", "start": 12.0, "end": 16.5,
      "role": "opener" | "speech" | "broll" | "closer",
      "caption": "Day 1 — Lisbon" or null,
      "reason": "why this shot, here"}}
  ]
}}

CLIP LOG
========
{clip_log}
"""


def clip_log(entries: list[dict]) -> str:
    """Compact, model-friendly text version of all analyses."""
    lines = []
    for e in entries:
        lines.append(
            f"## {e['name']}  | recorded {e['created'] or 'unknown'} | {e['duration']:.1f}s"
            + (f" | {e['orientation']}" if e.get("orientation", "horizontal") != "horizontal" else "")
            + (f" | {e['time_of_day']}" if e.get("time_of_day") else "")
            + (f" | place: {e['location_guess']}" if e.get("location_guess") else "")
            + (f" | gps: {e['gps']}" if e.get("gps") else "")
            + ("" if e.get("usable", True) else " | UNUSABLE")
        )
        if e.get("summary"):
            lines.append(f"summary: {e['summary']}")
        if e.get("people"):
            lines.append(f"people: {'; '.join(e['people'])}")
        if e.get("visible_text"):
            lines.append(f"signs: {'; '.join(e['visible_text'])}")
        if e.get("transitions"):
            lines.append(
                "transitions: "
                + "; ".join(f"{t['type']} {t['start']:.1f}-{t['end']:.1f} ({t['note']})" for t in e["transitions"])
            )
        if e.get("quality_issues"):
            lines.append(f"issues: {'; '.join(e['quality_issues'])}")
        for m in e.get("moments", []):
            look = "/".join(x for x in (m.get("shot"), m.get("motion")) if x)
            # Only flag what needs attention; upright/steady/good is the norm.
            flags = [
                v
                for k, ok in (("framing", "upright"), ("stability", "steady"), ("exposure", "good"))
                if (v := m.get(k)) and v != ok
            ]
            lines.append(
                f"- [{m['start']:.1f}-{m['end']:.1f}] {m['kind']}"
                + (f" {look}" if look else "")
                + (f" {m['energy']}" if m.get("energy") else "")
                + (f" {m['tone']}" if m.get("tone") else "")
                + (f" audio={m['audio']}" if m.get("audio") else "")
                + f" score={m['score']:g}"
                + (f" peak={m['peak']:.1f}" if m.get("peak") is not None else "")
                + (f" [{', '.join(flags)}]" if flags else "")
                + ": "
                + (f"{m['subject']}: " if m.get("subject") else "")
                + m["description"]
            )
        speech = " ".join(t["text"] for t in e.get("transcript", []))
        if speech:
            lines.append(f'said: "{speech[:600]}"')
        lines.append("")
    return "\n".join(lines)
