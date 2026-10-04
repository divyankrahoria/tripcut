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
        return SimpleNamespace(
            text='```json\n{"summary": "intro at the harbour", "scene_type": "talking", '
            '"transcript": [{"start": "0:01", "end": 3, "text": "Hi from Lisbon!"}], '
            '"moments": [{"start": 0.5, "end": 3.4, "kind": "speech", "description": "greeting", "score": 8}]}\n```'
        )


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
