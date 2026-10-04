# Contributing to tripcut

Thanks for helping! tripcut is small on purpose; the pipeline is five plain modules:

| Stage | Module | What it does |
|---|---|---|
| Scan | `probe.py` | ffprobe metadata, recording dates, 360° detection |
| Watch | `analyze.py` + `prompts.py` | per-clip understanding (Gemini or local heuristics), cached |
| Plan | `plan.py` | AI planner, heuristic planner, and `validate_plan` safety net |
| Render | `render.py` | FFmpeg encode, captions, music ducking |
| Export | `export.py` | storyboard, EDL, SRT/ASS |

## Getting set up

```bash
pip install -e ".[dev]"
pytest -q                     # end-to-end tests build fake clips with FFmpeg, no API key needed
ruff check . && ruff format .
```

## Good first contributions

- **A new provider** (Claude, OpenAI, a local VLM): implement `analyze(clip, workdir) -> ClipAnalysis`
  and, for AI planning, `generate_json(contents) -> dict`. Register it in `make_provider`.
- **Better prompts**: if you get a noticeably better cut on real footage, open a PR with before/after notes.
- **Exports**: FCPXML or OpenTimelineIO next to `to_edl`.

Please keep PRs focused, add a test, and never commit real footage (`*.mp4` is git-ignored).
