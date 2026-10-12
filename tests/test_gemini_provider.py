"""Exercise the Gemini code path with a fake client (no network, no key)."""

from types import SimpleNamespace

from tripcut.analyze import GeminiProvider

from .conftest import make_talking_clip, needs_ffmpeg


class FakeFiles:
    def __init__(self):
        self.polls = 0
        self.deleted = []

    def upload(self, file):
        return SimpleNamespace(name="files/abc", state=SimpleNamespace(name="PROCESSING"), path=file)

    def get(self, name):
        self.polls += 1
        return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))

    def delete(self, name):
        self.deleted.append(name)


class FakeModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents))
        self.config = config
        return SimpleNamespace(
            text='```json\n{"summary": "intro at the harbour", "scene_type": "talking", '
            '"transcript": [{"start": "0:01", "end": 3, "text": "Hi from Lisbon!"}], '
            '"moments": [{"start": 0.5, "end": 3.4, "kind": "speech", "description": "greeting", "score": 8}]}\n```'
        )


class Overloaded(Exception):
    code = 503  # what google.genai's ServerError carries for "high demand"


class FlakyModels:
    """The main model is overloaded; everything else answers."""

    def __init__(self, busy="main", error=Overloaded):
        self.busy, self.error, self.calls = busy, error, []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        if model == self.busy:
            raise self.error("busy")
        return SimpleNamespace(text='{"summary": "ok"}')


def test_overload_switches_to_fallback_and_stays_there(monkeypatch):
    import time

    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    provider = GeminiProvider(model="main", api_key="fake", fallback_model="backup")
    provider.client = SimpleNamespace(models=FlakyModels())

    assert provider.generate_json(["clip 1"]) == {"summary": "ok"}
    assert provider.generate_json(["clip 2"]) == {"summary": "ok"}
    assert provider.client.models.calls == ["main", "backup", "backup"]  # clip 2 skips the busy model
    assert provider.last_model == "backup" and sleeps == []  # no waiting around


def test_other_errors_retry_without_switching(monkeypatch):
    import time

    import pytest

    monkeypatch.setattr(time, "sleep", lambda s: None)
    provider = GeminiProvider(model="main", api_key="fake", fallback_model="backup")
    provider.client = SimpleNamespace(models=FlakyModels(error=ValueError))

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        provider.generate_json(["clip"])
    assert provider.client.models.calls == ["main", "main", "main"]


@needs_ffmpeg
def test_analysis_records_the_model_that_answered(tmp_path, monkeypatch):
    import time

    from tripcut.probe import probe

    monkeypatch.setattr(time, "sleep", lambda s: None)
    clip = probe(make_talking_clip(tmp_path / "hello.mp4", seconds=3))
    provider = GeminiProvider(model="main", api_key="fake", fallback_model="backup")
    provider.client = SimpleNamespace(files=FakeFiles(), models=FlakyModels())

    assert provider.analyze(clip, tmp_path / ".tripcut").provider == "gemini:backup"
    assert provider.name == "gemini:main"  # cache identity unchanged, so the clip isn't redone


@needs_ffmpeg
def test_gemini_provider_flow(tmp_path, monkeypatch):
    import time

    from tripcut.probe import probe

    monkeypatch.setattr(time, "sleep", lambda s: None)
    clip = probe(make_talking_clip(tmp_path / "hello.mp4", seconds=5))

    provider = GeminiProvider(model="gemini-test", api_key="fake")
    provider.client = SimpleNamespace(files=FakeFiles(), models=FakeModels())

    a = provider.analyze(clip, tmp_path / ".tripcut")

    assert (tmp_path / ".tripcut" / "proxies" / "hello.mp4").exists()  # proxy made before upload
    assert provider.client.files.polls == 1
    assert provider.client.files.deleted == ["files/abc"]  # cleaned up after
    model, contents = provider.client.models.calls[0]
    assert model == "gemini-test" and "5.0 seconds" in contents[1]
    assert a.scene_type == "talking" and a.transcript[0].start == 1.0
    assert a.moments[0].kind == "speech" and a.moments[0].score == 8
    from tripcut.prompts import CLIP_SCHEMA

    assert provider.client.models.config.response_json_schema == CLIP_SCHEMA  # Gemini is held to the format
