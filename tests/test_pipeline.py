"""SCALED — unit tests. Zero network, zero real keys.

Every test that would otherwise reach an API monkeypatches requests.post /
requests.get, so `pytest tests/ -q` is safe to run anywhere, including CI.
"""
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class FakeResp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status=200, payload=None, content=b"", text=None):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def chat(content):
    """An OpenAI-shaped chat completion carrying `content`."""
    return {"choices": [{"message": {"content": content}}]}


# ==========================================================================
# TASK 4 — pipeline/llm.py
# ==========================================================================
class TestLLM:
    def _mod(self, monkeypatch, keys=("NIM_KEY", "GROQ_KEY", "GEMINI_KEY")):
        for env in ("NIM_KEY", "GROQ_KEY", "GEMINI_KEY"):
            monkeypatch.setenv(env, "k-" + env if env in keys else "")
        from pipeline import llm
        return llm

    def test_happy_path_uses_first_provider(self, monkeypatch):
        llm = self._mod(monkeypatch)
        calls = []

        def fake_post(url, **kw):
            calls.append((url, kw["json"]["model"], kw["json"]["temperature"]))
            return FakeResp(payload=chat("Base stats first."))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("sys", "user") == "Base stats first."
        assert len(calls) == 1
        url, model, temp = calls[0]
        assert url == llm.NIM_BASE + "/chat/completions"
        assert model == "meta/llama-3.3-70b-instruct"
        assert temp == 0.85

    def test_code_chain_starts_at_gemini_with_low_temperature(self, monkeypatch):
        llm = self._mod(monkeypatch)
        seen = {}

        def fake_post(url, **kw):
            seen.update(url=url, **kw["json"])
            return FakeResp(payload=chat("tl.to('#s0-a',{opacity:1},0.5);"))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        llm.llm_code("sys", "user")
        assert seen["url"] == llm.GEMINI_BASE + "/chat/completions"
        assert seen["model"] == "gemini-2.5-flash"
        assert seen["temperature"] == 0.4
        assert seen["max_tokens"] == 12000

    def test_first_provider_fails_second_is_used(self, monkeypatch):
        llm = self._mod(monkeypatch)
        models = []

        def fake_post(url, **kw):
            models.append(kw["json"]["model"])
            if len(models) == 1:
                return FakeResp(status=500, text="upstream boom")
            return FakeResp(payload=chat("second answer"))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u") == "second answer"
        assert models == ["meta/llama-3.3-70b-instruct", "llama-3.3-70b-versatile"]

    def test_exception_also_falls_through(self, monkeypatch):
        llm = self._mod(monkeypatch)
        n = {"i": 0}

        def fake_post(url, **kw):
            n["i"] += 1
            if n["i"] == 1:
                raise RuntimeError("connection reset")
            return FakeResp(payload=chat("recovered"))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u") == "recovered"
        assert n["i"] == 2

    def test_empty_key_provider_is_skipped_not_called(self, monkeypatch):
        llm = self._mod(monkeypatch, keys=("GROQ_KEY",))
        models = []

        def fake_post(url, **kw):
            models.append(kw["json"]["model"])
            return FakeResp(payload=chat("groq only"))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u") == "groq only"
        assert models == ["llama-3.3-70b-versatile"]

    def test_all_down_raises(self, monkeypatch):
        llm = self._mod(monkeypatch)
        monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResp(status=503, text="down"))
        with pytest.raises(RuntimeError, match="all LLM providers down"):
            llm.llm("s", "u")

    def test_no_keys_at_all_raises(self, monkeypatch):
        llm = self._mod(monkeypatch, keys=())
        monkeypatch.setattr(llm.requests, "post", lambda *a, **k: pytest.fail("must not POST"))
        with pytest.raises(RuntimeError, match="all LLM providers down"):
            llm.llm("s", "u")

    def test_json_out_sets_response_format(self, monkeypatch):
        llm = self._mod(monkeypatch)
        seen = {}

        def fake_post(url, **kw):
            seen.update(kw["json"])
            return FakeResp(payload=chat('{"verdict":"APEX"}'))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u", json_out=True) == {"verdict": "APEX"}
        assert seen["response_format"] == {"type": "json_object"}

    @pytest.mark.parametrize("raw", [
        '{"a":1}',
        '```json\n{"a":1}\n```',
        '```JSON\r\n{"a":1}\r\n```',
        '```\n{"a":1}\n```',
        'Sure, here it is:\n{"a":1}\nHope that helps!',
        '   \n\n```json   \n\n{"a":1}\n\n```   \n',
    ])
    def test_fence_stripping_and_slicing(self, monkeypatch, raw):
        llm = self._mod(monkeypatch)
        monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResp(payload=chat(raw)))
        assert llm.llm("s", "u", json_out=True) == {"a": 1}

    def test_json_array_response_parses(self, monkeypatch):
        llm = self._mod(monkeypatch)
        monkeypatch.setattr(llm.requests, "post",
                            lambda *a, **k: FakeResp(payload=chat('```json\n[{"topic":"x"}]\n```')))
        assert llm.llm("s", "u", json_out=True) == [{"topic": "x"}]

    def test_unparseable_json_is_a_provider_failure(self, monkeypatch):
        llm = self._mod(monkeypatch)
        models = []

        def fake_post(url, **kw):
            models.append(kw["json"]["model"])
            if len(models) == 1:
                return FakeResp(payload=chat("I'm afraid I can't do that."))
            return FakeResp(payload=chat('{"ok":true}'))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u", json_out=True) == {"ok": True}
        assert len(models) == 2

    def test_providers_health_reflects_env(self, monkeypatch):
        monkeypatch.setenv("NIM_KEY", "x")
        monkeypatch.setenv("GROQ_KEY", "")
        monkeypatch.delenv("GEMINI_KEY", raising=False)
        import importlib
        from pipeline import llm as _llm
        llm = importlib.reload(_llm)
        assert llm.PROVIDERS_HEALTH == {"NIM_KEY": True, "GROQ_KEY": False, "GEMINI_KEY": False}

    def test_prompts_file_has_every_part_f_section(self):
        from pipeline import llm
        p = llm.load_prompts()
        for name in ("SCRIPT", "SCENE", "COMMENT", "EXTRA_CREDIT", "THUMBNAIL", "STRATEGIST", "TOPICS"):
            assert name in p, name
        assert p["SCENE"]["system"].rstrip().endswith("containing only tl.* lines.")
        assert "Two hundred million years of field research." in p["SCRIPT"]["user"]
        assert llm.prompt("COMMENT", "system").startswith("You are Professor Croc replying")


# ==========================================================================
# TASK 5 — pipeline/srt.py  (VERIFY-ON-FIRST-RUN #4: both whisper word shapes)
# ==========================================================================
SEGMENTS_SHAPE = {
    "text": "Your brain seals your strength.",
    "segments": [
        {"id": 0, "words": [{"word": "Your", "start": 0.12, "end": 0.34},
                            {"word": " brain", "start": 0.34, "end": 0.71}]},
        {"id": 1, "words": [{"word": "seals ", "start": 0.75, "end": 1.10},
                            {"word": "strength.", "start": 1.10, "end": 1.7123}]},
    ],
}
FLAT_SHAPE = {
    "text": "Your brain seals your strength.",
    "words": [{"word": "Your", "start": 0.12, "end": 0.34},
              {"word": "brain", "start": 0.34, "end": 0.71},
              {"word": "seals", "start": 0.75, "end": 1.10},
              {"word": "strength.", "start": 1.10, "end": 1.7123}],
}


class TestSrt:
    def test_segments_shape(self):
        from pipeline import srt
        words = srt.normalize_words(SEGMENTS_SHAPE)
        assert [w["w"] for w in words] == ["Your", "brain", "seals", "strength."]
        assert srt.WORD_SHAPE == "segments[].words[]"

    def test_flat_shape(self):
        from pipeline import srt
        words = srt.normalize_words(FLAT_SHAPE)
        assert [w["w"] for w in words] == ["Your", "brain", "seals", "strength."]
        assert srt.WORD_SHAPE == "flat words[]"

    def test_both_shapes_agree(self):
        from pipeline import srt
        assert srt.normalize_words(SEGMENTS_SHAPE) == srt.normalize_words(FLAT_SHAPE)

    def test_times_rounded_to_three_places(self):
        from pipeline import srt
        assert srt.normalize_words(FLAT_SHAPE)[-1]["e"] == 1.712

    def test_words_are_stripped(self):
        from pipeline import srt
        assert all(w["w"] == w["w"].strip() for w in srt.normalize_words(SEGMENTS_SHAPE))

    def test_unrecognised_shape_returns_empty(self):
        from pipeline import srt
        assert srt.normalize_words({"text": "no words here"}) == []
        assert srt.normalize_words({"words": []}) == []

    def test_blank_and_malformed_words_dropped(self):
        from pipeline import srt
        out = srt.normalize_words({"words": [
            {"word": "  ", "start": 0, "end": 1},
            {"word": "ok", "start": 1, "end": 2},
            "not-a-dict",
            {"word": "bad", "start": "x", "end": "y"},
        ]})
        assert [w["w"] for w in out] == ["ok"]

    def test_missing_key_raises(self, monkeypatch, tmp_path):
        from pipeline import srt
        monkeypatch.setenv("GROQ_KEY", "")
        f = tmp_path / "a.mp3"
        f.write_bytes(b"ID3fake")
        with pytest.raises(RuntimeError, match="GROQ_KEY missing"):
            srt.word_timeline(str(f))

    def test_empty_result_raises(self, monkeypatch, tmp_path):
        from pipeline import srt
        monkeypatch.setenv("GROQ_KEY", "k")
        f = tmp_path / "a.mp3"
        f.write_bytes(b"ID3fake")
        monkeypatch.setattr(srt.requests, "post", lambda *a, **k: FakeResp(payload={"text": ""}))
        with pytest.raises(RuntimeError, match="no words"):
            srt.word_timeline(str(f))

    def test_request_shape(self, monkeypatch, tmp_path):
        from pipeline import srt
        monkeypatch.setenv("GROQ_KEY", "k")
        f = tmp_path / "a.mp3"
        f.write_bytes(b"ID3fake")
        seen = {}

        def fake_post(url, **kw):
            seen.update(url=url, data=kw["data"], headers=kw["headers"])
            return FakeResp(payload=FLAT_SHAPE)

        monkeypatch.setattr(srt.requests, "post", fake_post)
        srt.word_timeline(str(f))
        assert seen["url"] == srt.ENDPOINT
        assert seen["data"]["model"] == "whisper-large-v3-turbo"
        assert seen["data"]["response_format"] == "verbose_json"
        assert seen["data"]["timestamp_granularities[]"] == "word"
        assert seen["headers"]["Authorization"] == "Bearer k"


# ==========================================================================
# TASK 6 — pipeline/adapters.py
# ==========================================================================
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP3 = b"ID3\x03" + b"\x00" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64


class TestAdapters:
    def test_default_config_matches_bible(self):
        from pipeline import adapters
        assert adapters.DEFAULT_CONFIG == {
            "voice": {"base": "", "path": "/tts",
                      "payload": {"text": "{text}", "voice_id": "{voice_id}"},
                      "returns": "mp3-bytes", "voice_id": ""},
            "image": {"base": "", "path": "/image",
                      "payload": {"prompt": "{prompt}", "size": "{size}"},
                      "returns": "jpeg-bytes"},
        }

    @pytest.mark.parametrize("data,expected", [
        (JPEG, "jpg"), (MP3, "mp3"), (WAV, "wav"), (WEBP, "webp"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "png"),
        (b"\xff\xfb\x90d" + b"\x00" * 8, "mp3"),
        (b"OggS" + b"\x00" * 8, "ogg"),
        (b"<html>error</html>", None),
        (b"", None), (b"ab", None),
    ])
    def test_sniff(self, data, expected):
        from pipeline import adapters
        assert adapters.sniff(data) == expected

    def test_substitution_replaces_placeholders_at_depth(self):
        from pipeline import adapters
        out = adapters._substitute(
            {"text": "[narration] {text}", "n": 2, "voice": "{voice_id}",
             "nested": {"p": "a {text} b"}, "list": ["{text}", 5]},
            {"text": "hello", "voice_id": "croc"})
        assert out == {"text": "[narration] hello", "n": 2, "voice": "croc",
                       "nested": {"p": "a hello b"}, "list": ["hello", 5]}

    def test_substitution_keeps_type_when_value_is_only_a_placeholder(self):
        from pipeline import adapters
        out = adapters._substitute({"size": "{size}", "w": "{width}"}, {"size": "1920x1080", "width": 1920})
        assert out == {"size": "1920x1080", "w": 1920}

    def test_dig_walks_dotted_and_indexed_paths(self):
        from pipeline import adapters
        doc = {"urls": ["a.webp", "b.webp"], "data": {"items": [{"u": "deep"}]}}
        assert adapters._dig(doc, "urls.0") == "a.webp"
        assert adapters._dig(doc, "urls.1") == "b.webp"
        assert adapters._dig(doc, "data.items.0.u") == "deep"
        assert adapters._dig(doc, "urls[0]") == "a.webp"

    def test_empty_base_raises_immediately_without_calling(self, monkeypatch):
        from pipeline import adapters
        monkeypatch.setattr(adapters.requests, "post", lambda *a, **k: pytest.fail("must not POST"))
        with pytest.raises(RuntimeError, match="base URL not configured"):
            adapters._call({"base": "", "path": "/tts"}, {"text": "x"}, kind="voice")

    def test_bytes_mode_happy_path(self, monkeypatch):
        from pipeline import adapters
        cfg = {"base": "https://v", "path": "/tts", "payload": {"text": "{text}"}, "returns": "mp3-bytes"}
        seen = {}

        def fake_post(url, **kw):
            seen.update(url=url, payload=kw["json"])
            return FakeResp(content=MP3)

        monkeypatch.setattr(adapters.requests, "post", fake_post)
        assert adapters._call(cfg, {"text": "hi"}, kind="voice") == MP3
        assert seen["url"] == "https://v/tts"
        assert seen["payload"] == {"text": "hi"}
        assert adapters.last_format("voice") == "mp3"

    def test_json_url_mode_follows_relative_url(self, monkeypatch):
        from pipeline import adapters
        cfg = {"base": "https://v", "path": "/api/tts", "payload": {"text": "{text}"},
               "returns": "json-url", "url_field": "url"}
        got = {}
        monkeypatch.setattr(adapters.requests, "post",
                            lambda *a, **k: FakeResp(payload={"ok": True, "url": "/outputs/x.wav"}))

        def fake_get(url, **kw):
            got["url"] = url
            return FakeResp(content=WAV)

        monkeypatch.setattr(adapters.requests, "get", fake_get)
        assert adapters._call(cfg, {"text": "hi"}, kind="voice") == WAV
        assert got["url"] == "https://v/outputs/x.wav"
        assert adapters.last_format("voice") == "wav"

    def test_json_url_mode_absolute_cdn_url_and_indexed_field(self, monkeypatch):
        from pipeline import adapters
        cfg = {"base": "https://i", "path": "/api/image", "payload": {"prompt": "{prompt}"},
               "returns": "json-url", "url_field": "urls.0"}
        got = {}
        monkeypatch.setattr(adapters.requests, "post",
                            lambda *a, **k: FakeResp(payload={"urls": ["https://cdn.example/a.webp"]}))

        def fake_get(url, **kw):
            got["url"] = url
            return FakeResp(content=WEBP)

        monkeypatch.setattr(adapters.requests, "get", fake_get)
        assert adapters._call(cfg, {"prompt": "p"}, kind="image") == WEBP
        assert got["url"] == "https://cdn.example/a.webp"

    def test_provider_error_field_is_a_failure(self, monkeypatch):
        from pipeline import adapters
        monkeypatch.setattr(adapters.time, "sleep", lambda s: None)
        cfg = {"base": "https://i", "path": "/api/image", "payload": {}, "returns": "json-url",
               "url_field": "urls.0"}
        monkeypatch.setattr(adapters.requests, "post",
                            lambda *a, **k: FakeResp(payload={"urls": [], "error": "blocked prompt"}))
        with pytest.raises(RuntimeError, match="failed after 5 attempts"):
            adapters._call(cfg, {"prompt": "p"}, kind="image")

    def test_html_body_is_rejected_not_saved(self, monkeypatch):
        from pipeline import adapters
        monkeypatch.setattr(adapters.time, "sleep", lambda s: None)
        cfg = {"base": "https://v", "path": "/tts", "payload": {}, "returns": "mp3-bytes"}
        monkeypatch.setattr(adapters.requests, "post",
                            lambda *a, **k: FakeResp(content=b"<html>gateway timeout</html>"))
        with pytest.raises(RuntimeError, match="failed after 5 attempts"):
            adapters._call(cfg, {"text": "x"}, kind="voice")

    def test_image_bytes_rejected_for_voice_kind(self, monkeypatch):
        from pipeline import adapters
        monkeypatch.setattr(adapters.time, "sleep", lambda s: None)
        cfg = {"base": "https://v", "path": "/tts", "payload": {}, "returns": "mp3-bytes"}
        monkeypatch.setattr(adapters.requests, "post", lambda *a, **k: FakeResp(content=JPEG))
        with pytest.raises(RuntimeError, match="failed after 5 attempts"):
            adapters._call(cfg, {"text": "x"}, kind="voice")

    def test_retries_five_times_with_documented_backoff(self, monkeypatch):
        from pipeline import adapters
        naps = []
        monkeypatch.setattr(adapters.time, "sleep", lambda s: naps.append(s))
        attempts = {"n": 0}

        def fake_post(*a, **k):
            attempts["n"] += 1
            return FakeResp(status=502, text="bad gateway")

        monkeypatch.setattr(adapters.requests, "post", fake_post)
        cfg = {"base": "https://v", "path": "/tts", "payload": {}, "returns": "mp3-bytes"}
        with pytest.raises(RuntimeError):
            adapters._call(cfg, {"text": "x"}, kind="voice")
        assert attempts["n"] == 5
        assert naps == [10, 20, 40, 80]

    def test_recovers_on_a_later_attempt(self, monkeypatch):
        from pipeline import adapters
        monkeypatch.setattr(adapters.time, "sleep", lambda s: None)
        n = {"i": 0}

        def fake_post(*a, **k):
            n["i"] += 1
            return FakeResp(status=500, text="boom") if n["i"] < 3 else FakeResp(content=MP3)

        monkeypatch.setattr(adapters.requests, "post", fake_post)
        cfg = {"base": "https://v", "path": "/tts", "payload": {}, "returns": "mp3-bytes"}
        assert adapters._call(cfg, {"text": "x"}, kind="voice") == MP3
        assert n["i"] == 3

    def test_service_key_becomes_bearer_header(self, monkeypatch):
        from pipeline import adapters
        monkeypatch.setenv("SERVICE_KEY", "sk-live")
        assert adapters._headers({})["Authorization"] == "Bearer sk-live"
        monkeypatch.setenv("SERVICE_KEY", "")
        assert "Authorization" not in adapters._headers({})

    def test_committed_config_is_wired_and_json_valid(self):
        from pipeline import adapters
        cfg = adapters.load_config()
        for kind in ("voice", "image"):
            assert cfg[kind]["base"].startswith("https://"), kind
            assert cfg[kind]["returns"] in ("json-url", "mp3-bytes", "jpeg-bytes")
