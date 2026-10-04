"""Prompts for the vision model (per clip) and the planner (whole trip)."""

CLIP_PROMPT = """\
You are an assistant video editor reviewing raw travel footage, one clip at a time.
Watch AND listen to this clip carefully. It is {duration:.1f} seconds long.
Recorded: {created}.

Return ONLY a JSON object with this exact shape (all times in SECONDS from the
start of this clip, as numbers, never "mm:ss"):

{{
  "summary": "1-2 sentences: what happens in this clip",
  "location_guess": "best guess of the place, e.g. 'Old Town Dubrovnik' or null",
  "scene_type": "talking" | "broll" | "mixed",
  "tags": ["short", "lowercase", "keywords"],
  "transcript": [{{"start": 0.0, "end": 3.2, "text": "what the person says"}}],
  "moments": [
    {{"start": 4.0, "end": 9.5, "kind": "speech" | "broll",
      "description": "what makes this moment worth using", "score": 1-10}}
  ],
  "quality_issues": ["e.g. heavy shake 0-6s", "wind noise", "lens covered"]
}}

Guidance for "moments" (this is what the editor will cut from):
- Give 2-8 moments, the best usable parts of the clip, in time order.
- "speech" moments: someone talking to camera. Start/end on sentence boundaries,
  never mid-word. Include the whole thought, even if it is 20+ seconds.
- "broll" moments: visually interesting shots (scenery, action, food, reactions),
  usually 2-6 seconds, starting where the shot is steady.
- score 9-10 = could open the video; 5 = fine filler; below 4 = only if desperate.
- Skip fumbling with the camera, blurry/shaky stretches, and dead air.
- transcript is [] if nobody speaks.
"""

PLAN_PROMPT = """\
You are an expert travel-video editor. Below is a log of every clip from one trip,
in the order they were recorded, with the best moments an assistant flagged in each.

Cut a {target:.0f}-second video (acceptable range {lo:.0f}-{hi:.0f}s), aspect {aspect}.
Style: {style}

Editing principles:
- Tell the story of the trip. Keep chronological order unless a strong opener
  from later in the trip is worth pulling forward as a cold open (max 1).
- The traveller's own talking moments are the spine: keep complete thoughts,
  never cut someone off mid-sentence. Trim rambling only at sentence boundaries.
- Use broll between talking parts to show what they are describing.
  Broll shots are usually 2-5 seconds. Vary them; avoid two near-identical shots in a row.
- Favour high-score moments. Skip moments with quality issues.
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
            + (f" | place: {e['location_guess']}" if e.get("location_guess") else "")
            + (f" | gps: {e['gps']}" if e.get("gps") else "")
        )
        if e.get("summary"):
            lines.append(f"summary: {e['summary']}")
        if e.get("quality_issues"):
            lines.append(f"issues: {'; '.join(e['quality_issues'])}")
        for m in e.get("moments", []):
            lines.append(f"- [{m['start']:.1f}-{m['end']:.1f}] {m['kind']} score={m['score']:g}: {m['description']}")
        speech = " ".join(t["text"] for t in e.get("transcript", []))
        if speech:
            lines.append(f'said: "{speech[:600]}"')
        lines.append("")
    return "\n".join(lines)
