"""SCALED — unit tests. Zero network, zero real keys.

Every test that would otherwise reach an API monkeypatches requests.post /
requests.get, so `pytest tests/ -q` is safe to run anywhere, including CI.
"""
import json
import re
import subprocess
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _committed_json(path):
    """The HEAD blob for `path`, parsed. Skips while the placeholder is still there."""
    out = subprocess.run(["git", "show", "HEAD:%s" % path], cwd=ROOT,
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("%s not committed with content yet" % path)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        pytest.skip("%s at HEAD is not JSON yet" % path)

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


def anthropic_msg(content):
    """An Anthropic Messages-shaped reply carrying `content` (seekai dialect)."""
    return {"content": [{"type": "text", "text": content}]}


def dialect_resp(url, content):
    """Reply in whichever dialect the URL implies, so chain-order tests work."""
    if "/v1/messages" in url:
        return FakeResp(payload=anthropic_msg(content))
    if "/api/chat" in url:
        return FakeResp(payload={"text": content, "model": "meta-ai-thinking"})
    return FakeResp(payload=chat(content))


def meta_chat_url(llm):
    """Where the meta dialect posts: /api/chat at the host root, not under /v1."""
    return llm.META_BASE.rsplit("/v1", 1)[0] + "/api/chat"


# ==========================================================================
# TASK 4 — pipeline/llm.py
# ==========================================================================
class TestLLM:
    def _mod(self, monkeypatch, keys=("TABI_KEY", "NIM_KEY", "GEMINI_KEY", "GROQ_KEY")):
        for env in ("TABI_KEY", "TABI_KEY2", "NIM_KEY", "GEMINI_KEY", "GROQ_KEY"):
            monkeypatch.setenv(env, "k-" + env if env in keys else "")
        from pipeline import llm
        monkeypatch.setattr(llm.time, "sleep", lambda *a, **k: None)  # no real backoff in tests
        return llm

    def test_happy_path_uses_first_provider(self, monkeypatch):
        llm = self._mod(monkeypatch)
        calls = []

        def fake_post(url, **kw):
            calls.append((url, kw["json"].get("model"), kw["headers"]))
            return FakeResp(payload={"text": "Base stats first.", "model": "meta-ai-thinking"})

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("sys", "user") == "Base stats first."
        assert len(calls) == 1
        url, model, headers = calls[0]
        assert url == meta_chat_url(llm)   # Meta is the keyless primary
        assert model is None               # /api/chat takes a bare message, no model
        assert "Mozilla/" in headers["User-Agent"]

    def test_code_chain_starts_at_meta(self, monkeypatch):
        llm = self._mod(monkeypatch)
        seen = {}

        def fake_post(url, **kw):
            seen.update(url=url, message=kw["json"]["message"])
            return FakeResp(payload={"text": "tl.to('#s0-a',{opacity:1},0.5);"})

        monkeypatch.setattr(llm.requests, "post", fake_post)
        llm.llm_code("sys", "user")
        assert seen["url"] == meta_chat_url(llm)
        assert "sys" in seen["message"] and "user" in seen["message"]

    def test_first_provider_fails_second_is_used(self, monkeypatch):
        llm = self._mod(monkeypatch)
        urls = []

        def fake_post(url, **kw):
            urls.append(url)
            if len(urls) == 1:
                return FakeResp(status=400, text="meta down")  # non-retryable -> next provider
            return dialect_resp(url, "second answer")

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u") == "second answer"
        assert urls == [meta_chat_url(llm), llm.TABI_BASE + "/v1/messages"]

    def test_retryable_status_is_retried_on_same_provider(self, monkeypatch):
        llm = self._mod(monkeypatch)
        n = {"i": 0}

        def fake_post(url, **kw):
            n["i"] += 1
            if n["i"] == 1:
                return FakeResp(status=503, text="try later")  # retryable -> retry, don't fall through
            return dialect_resp(url, "ok")

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u") == "ok"
        assert n["i"] == 2

    def test_request_exception_is_retried(self, monkeypatch):
        llm = self._mod(monkeypatch)
        n = {"i": 0}

        def fake_post(url, **kw):
            n["i"] += 1
            if n["i"] == 1:
                raise llm.requests.RequestException("connection reset")
            return dialect_resp(url, "recovered")

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u") == "recovered"
        assert n["i"] == 2

    def test_empty_key_provider_is_skipped_not_called(self, monkeypatch):
        # Only GROQ has a key. Meta (keyless) is tried first; make it fail so the
        # chain must skip the empty-key providers (tabi/nim/gemini) and land on groq.
        llm = self._mod(monkeypatch, keys=("GROQ_KEY",))
        urls = []

        def fake_post(url, **kw):
            urls.append(url)
            if url == meta_chat_url(llm):
                return FakeResp(status=400, text="meta down")
            return FakeResp(payload=chat("groq only"))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u") == "groq only"
        assert urls == [meta_chat_url(llm), llm.GROQ_BASE + "/chat/completions"]

    def test_all_down_raises(self, monkeypatch):
        llm = self._mod(monkeypatch)
        monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResp(status=503, text="down"))
        with pytest.raises(RuntimeError, match="all LLM providers down"):
            llm.llm("s", "u")

    def test_no_keys_only_meta_is_attempted(self, monkeypatch):
        # Meta needs no key, so even with every key empty it is still tried (and is
        # the only provider tried); the keyed providers are all skipped.
        llm = self._mod(monkeypatch, keys=())
        urls = []

        def fake_post(url, **kw):
            urls.append(url)
            return FakeResp(status=400, text="meta down")

        monkeypatch.setattr(llm.requests, "post", fake_post)
        with pytest.raises(RuntimeError, match="all LLM providers down"):
            llm.llm("s", "u")
        assert urls == [meta_chat_url(llm)]

    def test_json_out_sets_response_format_on_openai_dialect(self, monkeypatch):
        llm = self._mod(monkeypatch, keys=("GROQ_KEY",))
        seen = {}

        def fake_post(url, **kw):
            if url == meta_chat_url(llm):
                return FakeResp(status=400, text="meta down")   # fall through to groq
            seen.update(kw["json"])
            return FakeResp(payload=chat('{"verdict":"APEX"}'))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u", json_out=True) == {"verdict": "APEX"}
        assert seen["response_format"] == {"type": "json_object"}

    def test_anthropic_dialect_parses_json_without_response_format(self, monkeypatch):
        llm = self._mod(monkeypatch, keys=("TABI_KEY",))
        seen = {}

        def fake_post(url, **kw):
            if url == meta_chat_url(llm):
                return FakeResp(status=400, text="meta down")   # fall through to tabi
            seen.update(url=url, **kw["json"])
            return FakeResp(payload=anthropic_msg('{"verdict":"APEX"}'))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u", json_out=True) == {"verdict": "APEX"}
        assert "response_format" not in seen        # Anthropic has no such field
        assert seen["system"] == "s"                # system is a top-level param
        assert seen["url"] == llm.TABI_BASE + "/v1/messages"

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
        monkeypatch.setattr(llm.requests, "post", lambda url, **k: dialect_resp(url, raw))
        assert llm.llm("s", "u", json_out=True) == {"a": 1}

    def test_json_array_response_parses(self, monkeypatch):
        llm = self._mod(monkeypatch)
        monkeypatch.setattr(llm.requests, "post",
                            lambda url, **k: dialect_resp(url, '```json\n[{"topic":"x"}]\n```'))
        assert llm.llm("s", "u", json_out=True) == [{"topic": "x"}]

    def test_unparseable_json_is_a_provider_failure(self, monkeypatch):
        llm = self._mod(monkeypatch)
        urls = []

        def fake_post(url, **kw):
            urls.append(url)
            if len(urls) == 1:
                return dialect_resp(url, "I'm afraid I can't do that.")  # meta: non-JSON
            return dialect_resp(url, '{"ok":true}')                      # tabi: JSON
        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u", json_out=True) == {"ok": True}
        assert len(urls) == 2

    def test_providers_health_reflects_env(self, monkeypatch):
        monkeypatch.setenv("TABI_KEY", "x")
        monkeypatch.setenv("GROQ_KEY", "")
        monkeypatch.delenv("TABI_KEY2", raising=False)
        monkeypatch.delenv("NIM_KEY", raising=False)
        monkeypatch.delenv("GEMINI_KEY", raising=False)
        import importlib
        from pipeline import llm as _llm
        llm = importlib.reload(_llm)
        assert llm.PROVIDERS_HEALTH == {
            "TABI_KEY": True, "TABI_KEY2": False,
            "NIM_KEY": False, "GEMINI_KEY": False, "GROQ_KEY": False,
        }

    def test_second_tabi_key_is_the_first_fallback(self, monkeypatch):
        llm = self._mod(monkeypatch, keys=("TABI_KEY", "TABI_KEY2", "NIM_KEY"))
        seen = []

        def fake_post(url, **kw):
            m = kw["json"].get("model")
            seen.append((url, m))
            if url == meta_chat_url(llm):
                return FakeResp(status=400, text="meta down")      # skip past meta
            if [u for u, _ in seen].count(llm.TABI_BASE + "/v1/messages") == 1:
                return FakeResp(status=403, text="cloudflare")     # first tabi key blocked
            return FakeResp(payload=anthropic_msg("second key answer"))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u") == "second key answer"
        # meta fails, then a per-key block on TABI_KEY falls through to TABI_KEY2
        # (still opus, still the anthropic /v1/messages endpoint) before NIM.
        assert seen == [
            (meta_chat_url(llm), None),
            (llm.TABI_BASE + "/v1/messages", "claude-opus-4-8"),
            (llm.TABI_BASE + "/v1/messages", "claude-opus-4-8"),
        ]

    def test_prompts_file_has_every_part_f_section(self):
        from pipeline import llm
        p = llm.load_prompts()
        for name in ("SCRIPT", "BEATS", "COMMENT", "EXTRA_CREDIT", "THUMBNAIL",
                     "STRATEGIST", "TOPICS", "TITLE_AB"):
            assert name in p, name
        assert "SCENE" not in p          # the director fills slots; it never writes tl.* lines
        assert "never write code or CSS" in p["BEATS"]["system"]
        for slot in ("{n}", "{staircase}", "{text}", "{act}"):
            assert slot in p["BEATS"]["user"], slot
        assert "thumb_words" in p["SCRIPT"]["user"] and "staircase" in p["SCRIPT"]["user"]
        assert llm.prompt("COMMENT", "system").startswith("You are Professor Croc replying")

    def test_no_prompt_asks_the_model_for_a_timestamp_or_an_expression(self):
        """C4: Python owns the clock and Croc is one static PNG."""
        from pipeline import llm
        blob = json.dumps(llm.load_prompts()).lower()
        for banned in ("expression", "data-start", "gsap", "tl.to", "tl.fromto", "keyframe"):
            assert banned not in blob, banned


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
            assert cfg[kind]["returns"] in ("json-url", "mcp-tool", "mp3-bytes",
                                            "jpeg-bytes", "png-bytes"), kind
            assert "payload" in cfg[kind] or "form" in cfg[kind], kind
        # The voice host clones a reference recording, so it MUST ride along.
        assert cfg["voice"]["form"]["text"] == "{text}"
        ref = cfg["voice"]["files"]["voice"]
        assert ref.endswith(".mp3") and os.path.isfile(os.path.join(ROOT, ref)), ref
        # The image host is an MCP server: the tool name is what gets called.
        assert cfg["image"]["tool"] == "generate_image"

    def test_mcp_request_is_a_jsonrpc_tools_call(self):
        from pipeline import adapters
        cfg = {"returns": "mcp-tool", "tool": "generate_image",
               "payload": {"prompt": "{prompt}"}}
        kwargs = adapters._request_kwargs(cfg, {"prompt": "a jaw"}, [])
        assert kwargs["json"] == {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "generate_image",
                                             "arguments": {"prompt": "a jaw"}}}

    def test_mcp_inline_base64_beats_the_cdn_url(self):
        """The inline block is already in hand and cannot expire."""
        import base64
        from pipeline import adapters
        doc = {"result": {"content": [
            {"type": "text", "text": "here it is https://cdn.example.com/x.png"},
            {"type": "image", "data": base64.b64encode(b"\x89PNG\r\n").decode()},
        ]}}
        data, url = adapters._mcp_media(doc, "image")
        assert data == b"\x89PNG\r\n" and url is None

    def test_mcp_falls_back_to_a_media_url_in_the_text(self):
        from pipeline import adapters
        doc = {"result": {"content": [
            {"type": "text", "text": "done: https://cdn.example.com/a.png and some prose."}]}}
        data, url = adapters._mcp_media(doc, "image")
        assert data is None and url == "https://cdn.example.com/a.png"

    @pytest.mark.parametrize("doc", [
        {"error": {"code": -1, "message": "nope"}},
        {"result": {"isError": True, "content": [{"type": "text", "text": "tool blew up"}]}},
        {"result": {"content": [{"type": "text", "text": "no media anywhere"}]}},
    ])
    def test_mcp_failures_raise(self, doc):
        from pipeline import adapters
        with pytest.raises(RuntimeError):
            adapters._mcp_media(doc, "image")

    def test_multipart_uploads_the_reference_voice_and_sets_no_content_type(self, tmp_path):
        """requests must set Content-Type itself -- only it knows the boundary."""
        from pipeline import adapters
        ref = tmp_path / "ref.mp3"
        ref.write_bytes(b"ID3ref")
        cfg = {"form": {"text": "{text}", "ttsSteps": 2}, "files": {"voice": str(ref)}}
        opened = []
        try:
            kwargs = adapters._request_kwargs(cfg, {"text": "hello"}, opened)
            assert kwargs["data"] == {"text": "hello", "ttsSteps": "2"}
            assert kwargs["files"]["voice"][0] == "ref.mp3"
            assert opened and not opened[0].closed
        finally:
            for fh in opened:
                fh.close()
        assert "Content-Type" not in adapters._headers(cfg)

    def test_a_missing_upload_raises_before_the_request(self, tmp_path):
        from pipeline import adapters
        cfg = {"form": {"text": "{text}"}, "files": {"voice": str(tmp_path / "gone.mp3")}}
        with pytest.raises(RuntimeError):
            adapters._request_kwargs(cfg, {"text": "hi"}, [])


# ==========================================================================
# TASK 7 — pipeline/topics.py
# ==========================================================================
def bank(*specs):
    return [{"topic": t, "lane": l, "why": "because", "score": s, "used": u}
            for t, l, s, u in specs]


@pytest.fixture
def tf(tmp_path):
    """A throwaway bank file; returns (path, write, read)."""
    p = tmp_path / "topics.json"

    def write(entries):
        p.write_text(json.dumps(entries), encoding="utf-8")
        return str(p)

    def read():
        return json.loads(p.read_text(encoding="utf-8"))

    return str(p), write, read


class TestTopics:
    def test_live_bank_holds_the_20_launch_entries_and_stays_valid(self):
        """Structural invariants only -- the live bank grows and gets used-flagged."""
        from pipeline import topics
        b = topics.load()
        assert len(b) >= 20
        assert all(set(t) == {"topic", "lane", "why", "score", "used"} for t in b)
        assert all(t["lane"] in topics.LANES for t in b)
        assert all(1 <= t["score"] <= 10 for t in b)
        assert len({t["topic"] for t in b}) == len(b)

    def test_shipped_bank_file_is_the_20_i_committed(self):
        """Guards the seed file itself against edits, reading the committed blob
        so a production run flipping used/score cannot fail this."""
        seed = _committed_json("data/topics.json")
        assert len(seed) == 20
        assert all(isinstance(t["used"], bool) for t in seed)  # used/score flip in production
        assert all(t["why"] for t in seed)
        assert len({t["topic"] for t in seed}) == 20

    def test_bank_contains_the_five_required_seeds(self):
        from pipeline import topics
        got = {t["topic"]: t["lane"] for t in topics.load()}
        for topic, lane in [
            ("Your Brain Seals Your True Strength (Here's the Key)", "HUMAN"),
            ("The Animal That Refuses to Die", "WEIRD_ANIMAL"),
            ("You Were Built to Run Prey to Death", "HUMAN"),
            ("The Punch That Breaks Physics", "VERSUS"),
            ("Why My Family Doesn't Age", "PROFESSOR"),
        ]:
            assert got.get(topic) == lane, topic

    def test_lesson_001_is_the_launch_topic(self):
        """The shipped bank scores the brain episode highest, so LESSON #001 is it."""
        seed = sorted(_committed_json("data/topics.json"), key=lambda t: -t["score"])
        assert seed[0]["topic"].startswith("Your Brain Seals")

    def test_all_four_lanes_are_represented(self):
        from pipeline import topics
        assert {t["lane"] for t in topics.load()} == set(topics.LANES)

    def test_pick_takes_highest_score_and_marks_used_on_disk(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("low", "HUMAN", 3, False), ("high", "VERSUS", 9, False),
                   ("mid", "PROFESSOR", 6, False)))
        assert topics.pick(p)["topic"] == "high"
        assert [t["used"] for t in read()] == [False, True, False]
        assert topics.pick(p)["topic"] == "mid"
        assert topics.pick(p)["topic"] == "low"
        assert topics.pick(p) is None

    def test_pick_ignores_used_even_when_they_score_higher(self, tf):
        from pipeline import topics
        p, write, _ = tf
        write(bank(("spent", "HUMAN", 10, True), ("fresh", "HUMAN", 2, False)))
        assert topics.pick(p)["topic"] == "fresh"

    def test_pick_breaks_ties_in_bank_order(self, tf):
        from pipeline import topics
        p, write, _ = tf
        write(bank(("first", "HUMAN", 7, False), ("second", "HUMAN", 7, False)))
        assert topics.pick(p)["topic"] == "first"

    def test_pick_returns_a_copy_not_the_live_row(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("a", "HUMAN", 5, False)))
        got = topics.pick(p)
        got["topic"] = "mutated"
        assert read()[0]["topic"] == "a"

    def test_top_unused_orders_and_truncates(self, tf):
        from pipeline import topics
        p, write, _ = tf
        write(bank(("a", "HUMAN", 4, False), ("b", "HUMAN", 9, False),
                   ("c", "HUMAN", 7, True), ("d", "HUMAN", 6, False)))
        assert [t["topic"] for t in topics.top_unused(2, p)] == ["b", "d"]
        assert [t["topic"] for t in topics.top_unused(99, p)] == ["b", "d", "a"]
        assert topics.top_unused(0, p) == []

    def test_reweight_moves_scores_one_step(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("h", "HUMAN", 5, False), ("v", "VERSUS", 5, False),
                   ("p", "PROFESSOR", 5, False)))
        topics.reweight(["HUMAN"], ["VERSUS"], p)
        assert {t["topic"]: t["score"] for t in read()} == {"h": 6, "v": 4, "p": 5}

    def test_reweight_clamps_at_both_ends(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("top", "HUMAN", 10, False), ("bottom", "VERSUS", 1, False)))
        topics.reweight(["HUMAN"], ["VERSUS"], p)
        assert {t["topic"]: t["score"] for t in read()} == {"top": 10, "bottom": 1}
        topics.reweight(["HUMAN"], ["VERSUS"], p)
        assert {t["topic"]: t["score"] for t in read()} == {"top": 10, "bottom": 1}

    def test_reweight_lane_in_both_lists_nets_zero(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("x", "HUMAN", 5, False)))
        topics.reweight(["HUMAN"], ["HUMAN"], p)
        assert read()[0]["score"] == 5

    def test_reweight_also_touches_used_rows_and_accepts_empty_lists(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("spent", "HUMAN", 5, True)))
        topics.reweight(["human"], [], p)
        assert read()[0]["score"] == 6
        topics.reweight([], None, p)
        assert read()[0]["score"] == 6

    def test_refill_is_a_noop_while_15_unused_remain(self, tf):
        from pipeline import topics
        p, write, _ = tf
        write(bank(*[("t%d" % i, "HUMAN", 5, False) for i in range(15)]))
        assert topics.refill(lambda s, u: pytest.fail("must not call the LLM"), p) == 0

    def test_refill_appends_ten_from_mocked_llm(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("old", "HUMAN", 5, False)))
        seen = {}

        def fake_llm(system, user):
            seen.update(system=system, user=user)
            return json.dumps([{"topic": "new %d" % i, "lane": "VERSUS",
                                "why": "w", "score": 7} for i in range(10)])

        assert topics.refill(fake_llm, p) == 10
        after = read()
        assert len(after) == 11
        assert all(t["used"] is False for t in after)
        assert after[1]["topic"] == "new 0"
        assert "Kronvex" in seen["system"]
        assert "old" in seen["user"] and "{existing}" not in seen["user"]

    def test_refill_accepts_a_prelisted_result_and_fenced_text(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("old", "HUMAN", 5, False)))
        assert topics.refill(lambda s, u: [{"topic": "obj", "lane": "HUMAN", "why": "w", "score": 5}], p) == 1
        assert topics.refill(lambda s, u: '```json\n[{"topic":"fenced","lane":"HUMAN","score":5}]\n```', p) == 1
        assert [t["topic"] for t in read()] == ["old", "obj", "fenced"]

    def test_refill_unwraps_a_dict_envelope(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("old", "HUMAN", 5, False)))
        assert topics.refill(lambda s, u: {"topics": [{"topic": "wrapped", "lane": "HUMAN", "score": 5}]}, p) == 1
        assert read()[-1]["topic"] == "wrapped"

    def test_refill_skips_duplicates_case_and_punctuation_insensitively(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("The Animal That Refuses to Die", "WEIRD_ANIMAL", 5, True)))
        added = topics.refill(lambda s, u: [
            {"topic": "the animal that refuses to die!", "lane": "WEIRD_ANIMAL", "score": 9},
            {"topic": "Genuinely New", "lane": "HUMAN", "score": 5},
            {"topic": "Genuinely New", "lane": "HUMAN", "score": 5},
            {"topic": "   ", "lane": "HUMAN", "score": 5},
            "not-a-dict",
        ], p)
        assert added == 1
        assert [t["topic"] for t in read()] == ["The Animal That Refuses to Die", "Genuinely New"]

    def test_refill_clamps_scores_and_normalises_lanes(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("old", "HUMAN", 5, False)))
        topics.refill(lambda s, u: [
            {"topic": "hot", "lane": "weird animal", "score": 99},
            {"topic": "cold", "lane": "nonsense", "score": -4},
            {"topic": "vague", "lane": "VERSUS", "score": "high"},
        ], p)
        got = {t["topic"]: (t["lane"], t["score"]) for t in read()}
        assert got["hot"] == ("WEIRD_ANIMAL", 10)
        assert got["cold"] == ("HUMAN", 1)
        assert got["vague"] == ("VERSUS", 5)

    def test_refill_survives_llm_failure_and_junk_without_touching_the_bank(self, tf):
        from pipeline import topics
        p, write, read = tf
        write(bank(("old", "HUMAN", 5, False)))
        before = read()

        def boom(s, u):
            raise RuntimeError("all LLM providers down")

        assert topics.refill(boom, p) == 0
        assert topics.refill(lambda s, u: "the model apologised instead", p) == 0
        assert topics.refill(lambda s, u: 42, p) == 0
        assert read() == before

    def test_load_tolerates_missing_corrupt_and_wrong_shaped_files(self, tmp_path):
        from pipeline import topics
        assert topics.load(str(tmp_path / "nope.json")) == []
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert topics.load(str(bad)) == []
        obj = tmp_path / "obj.json"
        obj.write_text('{"topic":"solo"}', encoding="utf-8")
        assert topics.load(str(obj)) == []

    def test_load_repairs_partial_rows(self, tmp_path):
        from pipeline import topics
        p = tmp_path / "t.json"
        p.write_text('[{"topic":"  spaced  "},{"nope":1},{"topic":"x","score":50,"used":1}]',
                     encoding="utf-8")
        got = topics.load(str(p))
        assert [t["topic"] for t in got] == ["spaced", "x"]
        assert got[0] == {"topic": "spaced", "lane": "HUMAN", "why": "", "score": 5, "used": False}
        assert got[1]["score"] == 10 and got[1]["used"] is True

    def test_save_round_trips_and_leaves_a_trailing_newline(self, tmp_path):
        from pipeline import topics
        p = str(tmp_path / "deep" / "t.json")
        entries = bank(("a", "HUMAN", 5, False))
        topics.save(entries, p)
        assert topics.load(p) == entries
        assert open(p, encoding="utf-8").read().endswith("]\n")


# ==========================================================================
# pipeline/compose.py — the Beat Engine baked into one GSAP timeline
# ==========================================================================
def w(text, s, e):
    return {"w": text, "s": s, "e": e}


def beat(kind, t, **kw):
    b = {"kind": kind, "t": t}
    b.update(kw)
    return b


def demo_job(**kw):
    """Two scenes, 42s total, exercising all six beat kinds at least once.

    Beat times are written by hand rather than through beats.plan_times so a
    change to the pacing formula cannot silently rewrite what these assertions
    are checking -- compose's contract is "render the beats you are given".
    """
    job = {
        "id": "2026-08-29-brain", "lesson": 7, "verdict": "APEX",
        "title": "Your Brain Seals Your True Strength",
        "scenes": [
            {"act": "HOOK", "heading": "SEALED", "text": "Your brain seals your strength.",
             "dur": 24.0, "beats": [
                 beat("img", 0.0, prompt="a brain in a vice", sfx="whoosh"),
                 beat("type", 2.4, text="SEALED", color="yellow", sfx="pop"),
                 beat("stat", 4.8, value="97%", label="CAPPED", sfx="none"),
                 beat("zoom", 7.2, amount=1.2, sfx="zap"),
                 beat("img", 9.6, prompt="a motor neuron firing"),
                 beat("meme", 12.0, prompt="a crocodile unimpressed", caption="SURE BUDDY"),
                 beat("arrow", 14.4, label="RIGHT HERE", dir="left"),
             ]},
            {"act": "PROMISE", "heading": "GRIP", "text": "It bites.", "dur": 18.0, "beats": [
                beat("img", 0.0, prompt="a jaw"),
                beat("type", 2.4, text="IT BITES", color="green"),
            ]},
        ],
    }
    job.update(kw)
    return job


def build(tmp_path, job, media=True):
    """Build a project in tmp_path, optionally with real-ish beat media present."""
    from pipeline import compose
    if media:
        for i, scene in enumerate(job.get("scenes") or []):
            (tmp_path / ("v%s_%d.mp3" % (job["id"], i))).write_bytes(b"ID3mp3")
            for j, b in enumerate(scene.get("beats") or []):
                if b.get("kind") == "img":
                    (tmp_path / ("i%s_%d_%d.jpg" % (job["id"], i, j))).write_bytes(b"\xff\xd8\xff")
                elif b.get("kind") == "meme":
                    (tmp_path / ("m%s_%d_%d.png" % (job["id"], i, j))).write_bytes(b"\x89PNG\r\n")
    proj, total = compose.build_project(job, str(tmp_path))
    page = open(os.path.join(proj, "index.html"), encoding="utf-8").read()
    return proj, total, page


class TestBuildProject:
    def test_returns_the_project_dir_and_total_duration(self, tmp_path):
        proj, total, _ = build(tmp_path, demo_job())
        assert proj == os.path.join(str(tmp_path), "proj_2026-08-29-brain")
        assert total == 42.0
        assert os.path.isfile(os.path.join(proj, "index.html"))

    def test_composition_contract_attributes(self, tmp_path):
        _, total, page = build(tmp_path, demo_job())
        assert 'data-composition-id="scaled"' in page
        assert 'data-start="0"' in page
        assert 'data-duration="%.3f"' % total in page     # renderer needs a duration source
        assert 'data-width="1920"' in page and 'data-height="1080"' in page
        assert 'data-fps="60"' in page                    # C1: the show is 60fps now
        assert "const tl = gsap.timeline({paused:true});" in page
        assert "window.__timelines = {scaled: tl};" in page

    def test_head_loads_only_the_vendored_gsap_and_inline_css(self, tmp_path):
        proj, _, page = build(tmp_path, demo_job())
        head = page[page.index("<head>"):page.index("</head>")]
        assert head.count("<script") == 1 and 'src="assets/gsap.min.js"' in head
        assert "<link" not in head and "http://" not in page and "https://" not in page
        assert "url('assets/fonts/display.woff2')" in head   # rebased for the project root
        assert os.path.isfile(os.path.join(proj, "assets", "gsap.min.js"))

    def test_every_timed_element_carries_an_id(self, tmp_path):
        """Without an id the renderer cannot discover media -- audio renders SILENT."""
        _, _, page = build(tmp_path, demo_job())
        timed = re.findall(r"<(\w+)([^>]*data-start[^>]*)>", page)
        assert len(timed) > 10
        for tag, attrs in timed:
            assert re.search(r'\bid="', attrs), (tag, attrs)

    def test_img_beat_holds_until_the_next_img_not_the_next_beat(self, tmp_path):
        """Beat 0's art is the base layer through four overlays until beat 4 replaces it."""
        _, _, page = build(tmp_path, demo_job())
        assert ('<img id="b0_0" class="beat-img clip" data-start="0.000" '
                'data-duration="9.600" data-track-index="0" src="assets/b0_0.jpg" alt="">') in page
        assert '<div id="w0_0" class="beat-wrap">' in page
        # the second img runs to the end of the scene: 24.0 - 9.6
        assert 'id="b0_4" class="beat-img clip" data-start="9.600" data-duration="14.400"' in page

    def test_img_is_a_hard_cut_with_a_tiny_settle_and_nothing_else(self, tmp_path):
        """v3: no punch overshoot, no drift. One blink, one 0.18s settle."""
        _, _, page = build(tmp_path, demo_job())
        assert ("tl.fromTo('#b0_0',{opacity:0},{opacity:1,duration:0.030,ease:'none'},0.000);"
                in page)
        # The cut settles from 1.06 to 1 and then never moves again.
        assert ("tl.fromTo('#b0_0',{scale:1.06},{scale:1,duration:0.180,"
                "ease:'power2.out'},0.000);") in page
        # ...and the snap zoom rides the wrapper, never the image.
        assert "tl.to('#w0_0',{scale:1.200,duration:0.080,ease:'expo.out'},7.200);" in page
        assert ("tl.to('#w0_0',{scale:1,duration:0.620,ease:'elastic.out(1,0.55)'},7.280);"
                in page)

    def test_type_beat_is_a_coloured_overlay_that_leaves_fast(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        assert ('<div id="t0_1" class="beat-type clip" data-start="2.400" data-duration="1.450" '
                'data-track-index="1" style="color:var(--yellow)">SEALED</div>') in page
        assert ("tl.fromTo('#t0_1',{scale:0.32,opacity:0,y:26},{scale:1.06,opacity:1,y:0,"
                "duration:0.160,ease:'back.out(3.6)'},2.400);") in page
        assert "tl.to('#t0_1',{scale:1,duration:0.180,ease:'power2.out'},2.560);" in page
        assert "tl.to('#t0_1',{opacity:0,duration:0.300,ease:'power1.in'},3.550);" in page

    def test_stat_beat_holds_two_beats_and_labels_below(self, tmp_path):
        """STAT_BEATS=2, so 4.8 holds until beat 4 at 9.6 -- a 4.8s window."""
        _, _, page = build(tmp_path, demo_job())
        assert ('<div id="s0_2" class="beat-stat-num clip" data-start="4.800" '
                'data-duration="4.800"') in page
        assert ">97%<" in page
        assert ('<div id="s0_2l" class="beat-stat-lbl clip" data-start="4.800" '
                'data-duration="4.800"') in page
        assert ">CAPPED<" in page
        assert ("tl.fromTo('#s0_2l',{opacity:0,y:18},{opacity:1,y:0,duration:0.220,"
                "ease:'power3.out'},4.880);") in page

    def test_stat_shakes_the_frame_behind_it_and_lands_back_at_zero(self, tmp_path):
        """The number is the punchline, so it gets the one impact effect there is."""
        _, _, page = build(tmp_path, demo_job())
        # An ODD repeat with yoyo would finish on the `from` values and leave the
        # art 9px off-centre for the rest of the scene; repeat:4 = 5 passes = ends
        # on `to`, so no corrective tween is needed.
        assert ("tl.fromTo('#w0_0',{x:-9,y:4},{x:0,y:0,duration:0.050,yoyo:true,repeat:4,"
                "ease:'none'},4.800);") in page
        assert "tl.set('#w0_0',{x:0" not in page

    def test_meme_beat_slides_in_and_out_and_uses_its_own_file(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        assert 'id="m0_5" class="beat-meme clip" data-start="12.000" data-duration="2.200"' in page
        assert '<img src="assets/m0_5.png" alt="">' in page
        assert '<div class="beat-meme-cap">SURE BUDDY</div>' in page
        assert ("tl.fromTo('#m0_5',{x:560,opacity:0,rotation:11},{x:0,opacity:1,rotation:0,"
                "duration:0.220,ease:'back.out(2.4)'},12.000);") in page
        assert "tl.to('#m0_5',{x:560,opacity:0,duration:0.300,ease:'power2.in'},13.900);" in page

    def test_arrow_beat_is_a_css_triangle_never_a_glyph(self, tmp_path):
        """Press Start 2P has no arrow codepoints -- a glyph would render as tofu."""
        _, _, page = build(tmp_path, demo_job())
        assert 'id="a0_6" class="beat-arrow clip" data-start="14.400"' in page
        assert 'style="left:180px;top:44%;text-align:center"' in page
        assert '<div class="arrow-glyph arrow-left"></div>RIGHT HERE' in page
        assert ("tl.fromTo('#a0_6',{rotation:-7},{rotation:7,duration:0.090,yoyo:true,repeat:3,"
                "ease:'none'},14.580);") in page

    def test_beat_times_are_absolute_across_scenes(self, tmp_path):
        """Scene 1 starts at 24.0, so its beat at t=2.4 lands at 26.4."""
        _, _, page = build(tmp_path, demo_job())
        assert 'id="b1_0" class="beat-img clip" data-start="24.000" data-duration="18.000"' in page
        assert 'id="t1_1" class="beat-type clip" data-start="26.400"' in page

    def test_zoom_with_nothing_on_screen_emits_nothing(self, tmp_path):
        """A zoom is a tween on live art, so it is also the safe landing spot for
        any beat that cannot be rendered -- and must no-op when nothing is live."""
        job = demo_job()
        job["scenes"] = [{"dur": 8.0, "beats": [beat("zoom", 0.0, amount=1.2)]}]
        _, _, page = build(tmp_path, job, media=False)
        assert "scale:1.200" not in page

    def test_missing_beat_art_degrades_that_beat_only(self, tmp_path):
        _, _, page = build(tmp_path, demo_job(), media=False)
        assert "assets/b0_0.jpg" not in page
        assert 'class="beat-img' not in page
        assert 'id="t0_1"' in page                     # text beats need no files at all
        # A beat that lost its art keeps its SOUND -- that is the whole point of
        # degrading per beat. (This used to assert no <audio> at all, which only
        # passed while assets/audio/sfx/ was empty.)
        assert 'src="assets/audio/whoosh.wav"' in page
        assert "narration" not in page                 # ...but no voice was copied

    def test_bgdiv_media_mode(self, tmp_path, monkeypatch):
        from pipeline import compose
        monkeypatch.setattr(compose, "MEDIA_MODE", "bgdiv")
        _, _, page = build(tmp_path, demo_job())
        assert '<div id="b0_0" class="beat-img clip"' in page
        assert "background-image:url(assets/b0_0.jpg)" in page
        assert '<img id="b0_0"' not in page            # beats are divs here; the avatar stays an img

    def test_narration_is_one_clip_per_scene_in_the_voice_lane(self, tmp_path):
        proj, _, page = build(tmp_path, demo_job())
        assert ('<audio id="v0" class="clip" data-start="0.000" data-duration="24.000" '
                'data-track-index="2" src="assets/a0.mp3"></audio>') in page
        assert 'id="v1" class="clip" data-start="24.000" data-duration="18.000"' in page
        for name in ("a0.mp3", "a1.mp3", "b0_0.jpg", "m0_5.png"):
            assert os.path.isfile(os.path.join(proj, "assets", name)), name

    def test_narration_keeps_the_providers_container(self, tmp_path):
        """Chatterbox returns wav; transcoding it would only add a step that fails."""
        job = demo_job()
        (tmp_path / ("v%s_0.wav" % job["id"])).write_bytes(b"RIFF....WAVE")
        (tmp_path / ("v%s_1.wav" % job["id"])).write_bytes(b"RIFF....WAVE")
        proj, _, page = build(tmp_path, job, media=False)
        assert 'src="assets/a0.wav"' in page and 'src="assets/a1.wav"' in page
        assert os.path.isfile(os.path.join(proj, "assets", "a0.wav"))

    def test_avatar_is_one_static_png_with_only_an_idle_bob(self, tmp_path):
        """One PNG, no rig: no expression swap, no crossfade, no second image."""
        proj, _, page = build(tmp_path, demo_job())
        assert page.count('id="avatar"') == 1
        assert '<img id="avatar" src="assets/avatar/croc.png" alt="">' in page
        tag = page[page.index('<img id="avatar"'):]
        assert "data-start" not in tag[:tag.index(">")]   # global element, not a timed clip
        assert "avatar-alt" not in page and "expression" not in page
        assert os.path.isfile(os.path.join(proj, "assets", "avatar", "croc.png"))
        # ceil(24/1.2)-1 = 19 extra plays: GSAP counts repeat as repeats, not plays
        assert ("tl.fromTo('#avatar',{y:0},{y:-5,duration:1.200,yoyo:true,repeat:19,"
                "ease:'sine.inOut'},0.000);") in page
        assert ("tl.fromTo('#avatar',{y:0},{y:-5,duration:1.200,yoyo:true,repeat:14,"
                "ease:'sine.inOut'},24.000);") in page

    def test_a_missing_avatar_is_loud_but_not_fatal(self, tmp_path, monkeypatch):
        from pipeline import compose
        monkeypatch.setattr(compose, "AVATAR_DIR", str(tmp_path / "no-avatar"))
        _, _, page = build(tmp_path, demo_job())
        # The selector stays in the stylesheet; nothing may reference the element.
        body = page[page.index('id="stage"'):]
        assert 'id="avatar"' not in body and "#avatar" not in body
        assert "window.__timelines = {scaled: tl};" in page

    def test_require_avatar_turns_a_missing_png_into_a_crash(self, tmp_path, monkeypatch):
        from pipeline import compose
        monkeypatch.setattr(compose, "AVATAR_DIR", str(tmp_path / "no-avatar"))
        monkeypatch.setenv("SCALED_REQUIRE_AVATAR", "1")
        with pytest.raises(RuntimeError):
            build(tmp_path, demo_job())

    def test_sfx_are_requested_per_beat_and_silent_when_absent(self, tmp_path, monkeypatch):
        from pipeline import compose
        fx = tmp_path / "sfx"
        fx.mkdir()
        (fx / "pop.mp3").write_bytes(b"ID3pop")
        monkeypatch.setattr(compose, "SFX_DIR", str(fx))
        _, _, page = build(tmp_path, demo_job())
        # pop exists and lands on beat 1 at 2.4; whoosh and zap have no file
        assert ('<audio id="fx1" class="clip" data-start="2.400" data-duration="0.400" '
                'data-track-index="4" data-volume="0.22" src="assets/audio/pop.mp3"></audio>'
                in page)
        assert "whoosh" not in page and "zap" not in page

    def test_music_is_segmented_back_to_back_at_low_volume(self, tmp_path, monkeypatch):
        from pipeline import compose
        lofi = tmp_path / "lofi"
        lofi.mkdir()
        (lofi / "bed.mp3").write_bytes(b"ID3bed")
        monkeypatch.setattr(compose, "LOFI_DIR", str(lofi))
        monkeypatch.setattr(compose, "probe_seconds", lambda p: 30.0)
        _, _, page = build(tmp_path, demo_job())
        assert 'id="mus0" class="clip" data-start="0.000" data-duration="30.000"' in page
        assert 'id="mus1" class="clip" data-start="30.000" data-duration="12.000"' in page
        assert 'data-track-index="3" data-volume="0.12"' in page

    def test_verdict_stamp_owns_the_last_twenty_seconds(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        assert ('<div id="verdict" class="verdict-stamp clip" data-start="22.000" '
                'data-duration="20.000" data-track-index="5">APEX</div>') in page
        assert ("tl.fromTo('#verdict',{scale:2.4,opacity:0},{scale:1,opacity:1,duration:0.400,"
                "ease:'back.out(1.4)'},22.000);") in page

    def test_verdict_is_skipped_when_the_job_has_none(self, tmp_path):
        _, _, page = build(tmp_path, demo_job(verdict=""))
        assert 'id="verdict"' not in page

    def test_lesson_chip_is_zero_padded_and_present_for_the_whole_video(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        assert ('<div id="lesson-chip" class="clip" data-start="0.000" data-duration="42.000" '
                'data-track-index="5">LESSON #007</div>') in page
        assert page.count('id="lesson-chip"') == 1

    def test_html_is_escaped_so_a_script_flavoured_beat_cannot_inject(self, tmp_path):
        job = demo_job()
        job["scenes"][0]["beats"][1]["text"] = '</div><script>alert("x")</script>'
        _, _, page = build(tmp_path, job)
        assert "<script>alert" not in page and "&lt;script&gt;alert" in page

    def test_build_is_byte_identical_across_runs(self, tmp_path):
        _, _, first = build(tmp_path, demo_job())
        _, _, second = build(tmp_path, demo_job())
        assert first == second

    def test_build_is_byte_identical_in_a_different_directory(self, tmp_path):
        """No absolute paths, timestamps or unseeded randomness may leak in."""
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        _, _, first = build(a, demo_job())
        _, _, second = build(b, demo_job())
        assert first == second

    def test_missing_gsap_writes_a_loud_stub(self, tmp_path, monkeypatch):
        from pipeline import compose
        monkeypatch.setattr(compose, "VENDOR_GSAP", str(tmp_path / "nope.js"))
        proj, _, _ = build(tmp_path, demo_job())
        stub = open(os.path.join(proj, "assets", "gsap.min.js"), encoding="utf-8").read()
        assert "throw new Error" in stub and "gsap.min.js was missing" in stub

    def test_empty_job_still_produces_a_valid_composition(self, tmp_path):
        from pipeline import compose
        proj, total = compose.build_project({"id": "empty", "scenes": []}, str(tmp_path))
        page = open(os.path.join(proj, "index.html"), encoding="utf-8").read()
        assert total == 0.0
        assert 'data-duration="0.100"' in page         # never zero: that fails the renderer
        assert "window.__timelines = {scaled: tl};" in page

    def test_a_scene_with_no_beats_holds_the_previous_picture(self, tmp_path):
        job = demo_job()
        job["scenes"][1]["beats"] = []
        _, total, page = build(tmp_path, job)
        assert total == 42.0
        assert 'id="b1_0"' not in page
        assert 'id="v1" class="clip" data-start="24.000"' in page   # it still speaks


# ==================== TASK 11-12: render + youtube modules ====================
import pipeline.render as _r


class TestRender:
    def test_kenburns_frames_equal_round_dur_times_60(self):
        for dur in (8.0, 12.5, 0.03, 500.0):
            assert _r.kenburns_frames(dur) == max(1, round(dur * 60))

    def test_fps_matches_the_composition(self):
        from pipeline import compose
        assert _r.FPS == compose.FPS == 60

    def test_kenburns_frames_never_below_one(self):
        assert _r.kenburns_frames(0.001) == 1
        assert _r.kenburns_frames(0.0) == 1
        assert _r.kenburns_frames(None) == 1

    def test_still_cmd_is_ffmpeg_list(self):
        cmd = _r.ffmpeg_still_cmd("in.jpg", 2.0, "out.mp4")
        assert isinstance(cmd, list) and cmd[0] == "ffmpeg"

    def test_still_cmd_encodes_exact_frame_count(self):
        cmd = _r.ffmpeg_still_cmd("in.jpg", 8.0, "out.mp4")
        assert str(_r.kenburns_frames(8.0)) in cmd

    def test_concat_cmd_is_ffmpeg_list(self):
        cmd = _r.concat_cmd(["a.mp4", "b.mp4"], "out.mp4")
        assert isinstance(cmd, list) and cmd[0] == "ffmpeg"

    def test_mux_cmd_is_ffmpeg_list(self):
        cmd = _r.mux_cmd("v.mp4", "a.mp3", "out.mp4")
        assert isinstance(cmd, list) and cmd[0] == "ffmpeg"

    def test_render_outflag_default_is_the_one_the_cli_accepts(self):
        """hyperframes 0.8.16 answers "Unknown flag: --out" -- only --output/-o work."""
        assert _r.RENDER_OUTFLAG in ("--output", "-o")

    def test_outflag_probe_does_not_read_out_of_the_middle_of_output(self, monkeypatch):
        """The real 0.8.16 help text, trimmed to the line that matters.

        `--out` is a substring of `--output`, so a plain `in` test picks a flag
        the CLI rejects and every render silently falls through to the ffmpeg
        slideshow. The probe has to match whole tokens.
        """
        help_text = ("  -c, --composition=<composition>    Render a specific composition\n"
                     "  -o, --output=<output>    Output path (default: renders/<name>.mp4)\n"
                     "  -f, --fps=<fps>    Frame rate.\n")
        monkeypatch.setattr(_r, "_OUTFLAG_PROBED", False)
        monkeypatch.setattr(_r, "RENDER_OUTFLAG", "--output")
        monkeypatch.setattr(_r.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout=help_text, stderr=""))
        assert _r._render_outflag() == "--output"

    def test_outflag_probe_still_honours_a_genuine_short_only_cli(self, monkeypatch):
        monkeypatch.setattr(_r, "_OUTFLAG_PROBED", False)
        monkeypatch.setattr(_r, "RENDER_OUTFLAG", "--output")
        monkeypatch.setattr(_r.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="  -o <file>   where to write it\n", stderr=""))
        assert _r._render_outflag() == "-o"

    def test_outflag_probe_falls_back_when_help_cannot_run(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("npx not installed")

        monkeypatch.setattr(_r, "_OUTFLAG_PROBED", False)
        monkeypatch.setattr(_r, "RENDER_OUTFLAG", "--output")
        monkeypatch.setattr(_r.subprocess, "run", boom)
        assert _r._render_outflag() == "--output"


from datetime import datetime as _dt, timedelta as _td


@pytest.mark.parametrize("age,expected", [
    (2.9, "fresh"), (3, "review"), (6.9, "review"), (7, "locked"),
])
def test_tier_boundaries(age, expected):
    from pipeline import learn
    assert learn.tier(age) == expected


def _row(days, impressions, ctr, now):
    return {"published_at": (now - _td(days=days)).isoformat(),
            "impressions": impressions, "ctr": ctr}


class TestAbEligible:
    now = _dt(2026, 8, 30, 12, 0, 0)

    def test_eligible_in_review_low_ctr_high_impressions(self):
        from pipeline import learn
        assert learn.ab_eligible(_row(5, 1000, 0.039, self.now), self.now) is True
        assert learn.ab_eligible(_row(3, 5000, 0.01, self.now), self.now) is True

    def test_fresh_never_eligible(self):
        from pipeline import learn
        assert learn.ab_eligible(_row(1, 5000, 0.01, self.now), self.now) is False

    def test_locked_never_eligible(self):
        from pipeline import learn
        assert learn.ab_eligible(_row(30, 5000, 0.01, self.now), self.now) is False

    def test_fails_on_high_ctr(self):
        from pipeline import learn
        assert learn.ab_eligible(_row(5, 5000, 0.04, self.now), self.now) is False

    def test_fails_on_low_impressions(self):
        from pipeline import learn
        assert learn.ab_eligible(_row(5, 999, 0.01, self.now), self.now) is False


class TestHasYt:
    def _set_all(self, mp):
        for v in ("YT_REFRESH_TOKEN", "YT_CLIENT_ID", "YT_CLIENT_SECRET"):
            mp.setenv(v, "x-" + v)
        mp.delenv("DRY_RUN", raising=False)

    def test_true_when_all_present(self, monkeypatch):
        from pipeline import upload
        self._set_all(monkeypatch)
        assert upload.has_yt() is True

    @pytest.mark.parametrize("missing", ["YT_REFRESH_TOKEN", "YT_CLIENT_ID", "YT_CLIENT_SECRET"])
    def test_false_when_missing(self, monkeypatch, missing):
        from pipeline import upload
        self._set_all(monkeypatch)
        monkeypatch.delenv(missing, raising=False)
        assert upload.has_yt() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes"])
    def test_false_when_dry_run(self, monkeypatch, val):
        from pipeline import upload
        self._set_all(monkeypatch)
        monkeypatch.setenv("DRY_RUN", val)
        assert upload.has_yt() is False


class TestCommittedAssets:
    """The two render paths read different files; nothing else checks they agree.

    Pillow cannot decompress a woff2's Brotli glyph tables, so the thumbnail
    needs TTFs while the browser gets woff2 -- and a mismatched pair sets the
    thumbnail's headline in a different typeface from the video's captions.
    """

    FACES = [("display", "Archivo Black"), ("mono", "JetBrains Mono"),
             ("pixel", "Press Start 2P")]

    def _fonts(self, *names):
        return [os.path.join(ROOT, "assets", "fonts", n) for n in names]

    @pytest.mark.parametrize("stem,family", FACES)
    def test_both_formats_are_committed_and_are_the_same_typeface(self, stem, family):
        import struct
        woff, ttf = self._fonts(stem + ".woff2", stem + ".ttf")
        assert os.path.isfile(woff) and os.path.isfile(ttf), stem
        blob = open(woff, "rb").read()
        assert blob[:4] == b"wOF2", stem
        # A latin subset decompresses to tens of KB. `mono.woff2` once shipped as
        # the CYRILLIC-EXT subset -- a valid 1160-byte file with no latin letters
        # in it -- because the download grabbed css2's first @font-face instead
        # of the /* latin */ one. Every lesson chip silently fell back to system
        # monospace. The sfnt size in the header is the cheapest tripwire.
        assert struct.unpack(">I", blob[16:20])[0] > 30000, "%s: wrong subset?" % stem
        from PIL import ImageFont
        assert ImageFont.truetype(ttf, 32).getname()[0] == family, stem

    @pytest.mark.parametrize("stem,_family", FACES)
    def test_pillow_rasterises_real_letters_not_notdef_boxes(self, stem, _family):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("L", (760, 120), 0)
        draw = ImageDraw.Draw(img)
        draw.text((6, 6), "SCALED 97%", font=ImageFont.truetype(
            self._fonts(stem + ".ttf")[0], 64), fill=255)
        assert sum(1 for px in img.tobytes() if px > 40) > 2000, stem

    def test_the_voice_reference_is_committed_not_gitignored(self):
        """Chatterbox clones this file on EVERY tts call, including on the runner.

        It is an input, not an intermediate -- but .gitignore blanket-ignores
        *.mp3 to keep generated narration out of the repo, and for a while that
        rule swallowed this file too.
        """
        from pipeline import adapters
        ref = adapters.load_config()["voice"]["files"]["voice"]
        assert os.path.isfile(os.path.join(ROOT, ref)), ref
        out = subprocess.run(["git", "check-ignore", ref], cwd=ROOT,
                             capture_output=True, text=True)
        assert out.returncode != 0, "%s is gitignored -- CI will render silent" % ref

    def test_no_secret_bearing_file_is_trackable(self):
        out = subprocess.run(["git", "check-ignore", "github-pat.txt", ".env"],
                             cwd=ROOT, capture_output=True, text=True)
        assert "github-pat.txt" in out.stdout, "the PAT file is committable!"

    def test_the_avatar_png_is_committed_where_compose_looks_for_it(self):
        from pipeline import compose
        assert compose.avatar_file(), "no .png in assets/avatar -- Croc is absent"


class TestThumbnail:
    """Thumbnail v3: the host paints the whole frame, headline included."""

    def test_art_prompt_carries_the_subject_and_bakes_the_headline(self):
        from pipeline import thumbnail
        got = thumbnail.art_prompt({"thumbnail_prompt": "a crocodile bench-pressing a car",
                                    "thumb_words": "160 VS 3700",
                                    "thumb_kicker": "bite force, pounds"})
        assert "a crocodile bench-pressing a car" in got
        assert '"160 VS 3700"' in got, "the headline has to be quoted into the prompt"
        assert '"BITE FORCE, POUNDS"' in got, "the kicker is the clue -- it cannot drop"
        assert "{subject}" not in got
        # v2 forbade all text; v3 forbids only the failure modes around it.
        assert "no split screen" in got and "no misspelling" in got

    def test_art_prompt_falls_back_to_the_topic_then_to_a_default(self):
        from pipeline import thumbnail
        assert "the human brain" in thumbnail.art_prompt({"topic": "the human brain"})
        assert thumbnail.art_prompt({}).strip()

    def test_archetype_is_stable_per_episode_and_varies_between_them(self):
        from pipeline import thumbnail
        pick = lambda i: thumbnail.pick_archetype({"id": i})[0]
        assert pick("2026-09-03-jaw") == pick("2026-09-03-jaw")
        seen = {pick("2026-09-%02d-x" % d) for d in range(1, 29)}
        assert len(seen) >= 4, "one archetype for every episode is a template, not variety"

    def test_archetype_can_be_forced_by_env(self, monkeypatch):
        from pipeline import thumbnail
        monkeypatch.setenv("SCALED_THUMB_ARCH", "versus")
        assert thumbnail.pick_archetype({"id": "anything"})[0] == "versus"

    def test_kicker_falls_back_to_the_topic_when_the_model_omits_it(self):
        from pipeline import thumbnail
        assert thumbnail.thumb_kicker({"thumb_kicker": "Bite Force, Pounds"}) == "BITE FORCE, POUNDS"
        assert "JAW" in thumbnail.thumb_kicker({"topic": "the human jaw"})
        assert thumbnail.thumb_kicker({}).strip()

    def test_accent_prefers_a_turn_word_over_a_number(self):
        from pipeline import thumbnail
        assert thumbnail.accent_word(["160", "VS", "3700"]) == "VS"
        assert thumbnail.accent_word(["2.4M", "YEARS", "AGO"]) == "YEARS"

    @pytest.mark.parametrize("job,expected", [
        ({"thumb_words": "97% SEALED"}, ["97%", "SEALED"]),
        ({"thumb_words": ["NEURAL", "brake"]}, ["NEURAL", "BRAKE"]),
        ({"title": "How Your Brain Is The Real Cap"}, ["BRAIN", "REAL", "CAP"]),
    ])
    def test_headline_is_short_uppercase_and_stripped_of_filler(self, job, expected):
        from pipeline import thumbnail
        assert thumbnail.thumb_words(job) == expected

    def test_headline_never_exceeds_four_words(self):
        from pipeline import thumbnail
        got = thumbnail.thumb_words({"thumb_words": "one two three four five six"})
        assert len(got) == thumbnail.MAX_WORDS == 4

    def test_compose_writes_a_1280x720_jpeg_even_with_no_art(self, tmp_path):
        from PIL import Image
        from pipeline import thumbnail
        img = thumbnail.compose(None, {"id": "x", "title": "A Title", "lesson": 7})
        assert img.size == (1280, 720)
        dst = tmp_path / "t.jpg"
        img.save(str(dst), "JPEG")
        assert Image.open(str(dst)).size == (1280, 720)

    def test_backdrop_is_deterministic_per_episode(self):
        from pipeline import thumbnail
        a = thumbnail._backdrop("2026-08-29-brain")
        b = thumbnail._backdrop("2026-08-29-brain")
        assert a.tobytes() == b.tobytes()
        assert a.tobytes() != thumbnail._backdrop("2026-08-30-jaw").tobytes()

    def test_band_picks_the_darker_half_and_env_can_force_it(self, monkeypatch):
        from PIL import Image, ImageDraw
        from pipeline import thumbnail
        img = Image.new("RGB", (1280, 720), (255, 255, 255))
        ImageDraw.Draw(img).rectangle((0, 0, 1280, 300), fill=(0, 0, 0))
        assert thumbnail.band(img) == "top"
        monkeypatch.setenv("SCALED_THUMB_POS", "bottom")
        assert thumbnail.band(img) == "bottom"

    def test_make_thumbnail_uses_the_art_call_and_writes_the_file(self, monkeypatch, tmp_path):
        from pipeline import adapters, thumbnail
        from PIL import Image
        captured = {}
        buf = __import__("io").BytesIO()
        Image.new("RGB", (1920, 1280), (12, 40, 90)).save(buf, "JPEG")

        def fake_image(prompt, size="1280x720"):
            captured["prompt"], captured["size"] = prompt, size
            return buf.getvalue()

        def no_rest(prompt, mode="thinking", timeout=300):
            raise RuntimeError("rest tier skipped in test")

        monkeypatch.setattr(adapters, "image_rest", no_rest)
        monkeypatch.setattr(adapters, "image", fake_image)
        job = {"id": "2026-08-30-brain", "lesson": 7, "thumb_words": "97% SEALED",
               "title": "Your Brain Seals Your Strength", "topic": "the human brain"}
        out = thumbnail.make_thumbnail(job, str(tmp_path))
        assert out and out.endswith("2026-08-30-brain_thumb.jpg")
        assert Image.open(out).size == (1280, 720)
        assert '"97% SEALED"' in captured["prompt"]
        # The 3:2 the host sometimes returns has to be cover-cropped, not squashed.
        assert captured["size"] == "1280x720"

    def test_the_baked_path_leaves_the_art_alone_but_the_fallback_gets_type(self):
        from PIL import Image
        from pipeline import thumbnail
        buf = __import__("io").BytesIO()
        Image.new("RGB", (1280, 720), (9, 9, 9)).save(buf, "JPEG")
        art = buf.getvalue()
        job = {"id": "x", "lesson": 7, "thumb_words": "ONE TWO", "title": "T"}
        baked = thumbnail.compose(art, job, baked=True)
        plain = thumbnail.compose(art, job, baked=False)
        ink = lambda im: sum(v * i for i, v in enumerate(im.convert("L").histogram())
                             if i > 200)
        # Both draw a mark; only the unbaked path typesets the whole headline.
        assert ink(plain) > ink(baked) * 3, "the fallback must still carry the words"

    def test_make_thumbnail_falls_back_to_beat_one_art(self, monkeypatch, tmp_path):
        from pipeline import adapters, thumbnail
        from PIL import Image
        Image.new("RGB", (1920, 1080), (200, 30, 30)).save(
            str(tmp_path / "i2026-08-30-brain_0_0.jpg"), "JPEG")

        def boom(prompt, size="1280x720"):
            raise RuntimeError("art host down")

        def boom_rest(prompt, mode="thinking", timeout=300):
            raise RuntimeError("rest tier down")

        def boom_edit(image_url, instruction):
            raise RuntimeError("edit tier down")

        monkeypatch.setattr(adapters, "image_rest", boom_rest)
        monkeypatch.setattr(adapters, "image", boom)
        monkeypatch.setattr(adapters, "edit_image", boom_edit)
        out = thumbnail.make_thumbnail({"id": "2026-08-30-brain", "lesson": 3,
                                        "title": "Down But Branded"}, str(tmp_path))
        assert out and Image.open(out).size == (1280, 720)

    def test_thumbnail_tries_rest_then_mcp_then_consistency_edit(self, monkeypatch, tmp_path):
        from pipeline import adapters, thumbnail
        from PIL import Image
        order = []
        buf = __import__("io").BytesIO()
        Image.new("RGB", (1280, 720), (9, 9, 9)).save(buf, "JPEG")

        def fake_rest(prompt, mode="thinking", timeout=300):
            order.append(("rest", mode))
            assert '"97% SEALED"' in prompt and "BRAIN" in prompt  # headline + kicker baked
            raise RuntimeError("rest down")

        def fake_mcp(prompt, size="1280x720"):
            order.append(("mcp", size))
            raise RuntimeError("mcp down")

        def fake_upload(path):
            order.append(("upload", os.path.basename(path)))
            return "https://cdn.test/ref.jpg"

        def fake_edit(image_url, instruction):
            order.append(("edit", image_url))
            return buf.getvalue()

        monkeypatch.setattr(adapters, "image_rest", fake_rest)
        monkeypatch.setattr(adapters, "image", fake_mcp)
        monkeypatch.setattr(adapters, "media_upload", fake_upload)
        monkeypatch.setattr(adapters, "edit_image", fake_edit)
        Image.new("RGB", (1920, 1080), (200, 30, 30)).save(
            str(tmp_path / "i2026-08-30-brain_0_0.jpg"), "JPEG")
        job = {"id": "2026-08-30-brain", "lesson": 3, "thumb_words": "97% SEALED",
               "thumb_kicker": "BRAIN, SEALED", "title": "T", "topic": "the human brain"}
        out = thumbnail.make_thumbnail(job, str(tmp_path))
        assert out and Image.open(out).size == (1280, 720)
        kinds = [k for k, _ in order]
        assert kinds == ["rest", "mcp", "upload", "edit"], order
        assert order[0][1] == "thinking"  # thumbnails buy the quality pass

    def test_the_avatar_resolves_the_same_way_as_the_video(self):
        from pipeline import compose, thumbnail
        want = compose.avatar_file()
        got = thumbnail._avatar_file()
        assert (os.path.basename(got) if got else None) == want


# ==========================================================================
# Beat Engine — Python owns the clock, the LLM only fills slots
# ==========================================================================
def grid(step, count, start=0.0):
    """A word timeline with onsets every `step` seconds."""
    return [w("x%d" % i, round(start + i * step, 3), round(start + i * step + 0.1, 3))
            for i in range(count)]


class TestBeatCount:
    @pytest.mark.parametrize("dur,expected", [
        (0, 4), (1.0, 4), (7.6, 4),           # floor(7.6/1.9)=4, at the floor
        (12.0, 6), (24.0, 12),
        (38.0, 20), (60.0, 20), (600.0, 20),  # clamped at BEAT_MAX
    ])
    def test_clamped_to_four_through_twenty(self, dur, expected):
        from pipeline.beats import beat_count
        assert beat_count(dur) == expected

    def test_junk_duration_degrades_to_the_minimum(self):
        from pipeline.beats import beat_count
        assert beat_count(None) == 4
        assert beat_count("abc") == 4
        assert beat_count(-5) == 4


class TestPlanTimes:
    def test_the_grid_is_syncopated_not_a_metronome(self):
        """v2 spaced beats evenly, which reads as a machine. The edit lives here."""
        from pipeline.beats import rhythm_grid
        times = rhythm_grid(24.0, 12)
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert max(gaps) > min(gaps) * 2, "no burst-then-hold contrast in the grid"

    @pytest.mark.parametrize("dur", [12.0, 24.0, 41.0])
    def test_median_cut_lands_in_the_one_to_three_second_brief(self, dur):
        from pipeline.beats import beat_count, plan_times
        times = plan_times(dur, [], beat_count(dur))
        gaps = sorted(b - a for a, b in zip(times, times[1:]))
        assert 1.0 <= gaps[len(gaps) // 2] <= 3.0
        assert max(gaps) >= 2.5, "every scene needs at least one hold to land a joke"

    def test_first_beat_is_pinned_to_zero(self):
        from pipeline.beats import plan_times
        assert plan_times(12.0, grid(0.4, 40))[0] == 0.0

    def test_beats_are_strictly_ascending_and_inside_the_scene(self):
        from pipeline.beats import plan_times
        times = plan_times(20.0, grid(0.37, 60))
        assert times == sorted(times)
        assert len(set(times)) == len(times)
        assert all(0.0 <= t < 20.0 for t in times)

    def test_min_gap_is_never_violated(self):
        from pipeline.beats import plan_times, MIN_GAP
        # Onsets every 0.1s would happily cut every 100ms; MIN_GAP must stop it.
        times = plan_times(24.0, grid(0.1, 240))
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert gaps and min(gaps) >= MIN_GAP - 1e-9

    def test_beats_land_on_word_onsets_when_one_is_reachable(self):
        from pipeline.beats import plan_times
        onsets = {x["s"] for x in grid(0.5, 60)}
        times = plan_times(24.0, grid(0.5, 60))
        assert all(t in onsets for t in times[1:])

    def test_no_word_clock_still_produces_an_even_grid(self):
        from pipeline.beats import plan_times, beat_count
        times = plan_times(12.0, [])
        assert len(times) == beat_count(12.0)
        assert times[0] == 0.0
        assert times == sorted(times)

    def test_explicit_n_overrides_the_formula(self):
        from pipeline.beats import plan_times
        assert len(plan_times(24.0, grid(0.3, 90), n=6)) == 6

    def test_beats_that_cannot_fit_are_dropped_not_stacked(self):
        from pipeline.beats import plan_times
        # 3s of scene cannot hold 12 beats 0.8s apart; expect a short list,
        # never twelve copies of the last frame.
        times = plan_times(3.0, grid(0.2, 15), n=12)
        assert len(times) < 12
        assert len(set(times)) == len(times)

    def test_malformed_words_are_ignored(self):
        from pipeline.beats import plan_times
        words = [{"s": "nope"}, "not-a-dict", {"e": 1.0}, {"s": 2.0, "e": 2.1}]
        times = plan_times(12.0, words)
        assert times[0] == 0.0 and times == sorted(times)

    def test_deterministic(self):
        from pipeline.beats import plan_times
        a = plan_times(18.0, grid(0.31, 70))
        b = plan_times(18.0, grid(0.31, 70))
        assert a == b


class TestNormalise:
    def test_unknown_kind_degrades_to_zoom(self):
        from pipeline.beats import normalise
        assert normalise({"kind": "explode"}, {}, 0)["kind"] == "zoom"
        assert normalise({}, {}, 0)["kind"] == "zoom"
        assert normalise("garbage", {}, 0)["kind"] == "zoom"

    def test_type_text_is_capped_at_five_words_and_uppercased(self):
        from pipeline.beats import normalise, MAX_TYPE_WORDS
        beat = normalise({"kind": "type", "text": "one two three four five six seven"}, {}, 1)
        assert beat["kind"] == "type"
        assert beat["text"] == "ONE TWO THREE FOUR FIVE"
        assert len(beat["text"].split()) == MAX_TYPE_WORDS

    def test_type_colour_is_whitelisted(self):
        from pipeline.beats import normalise, COLORS
        assert normalise({"kind": "type", "text": "GO", "color": "green"}, {}, 0)["color"] == "green"
        assert normalise({"kind": "type", "text": "GO", "color": "chartreuse"},
                         {}, 0)["color"] in COLORS

    def test_type_with_no_text_degrades(self):
        from pipeline.beats import normalise
        assert normalise({"kind": "type", "text": "   "}, {}, 0)["kind"] == "zoom"

    def test_stat_value_capped_at_twelve_chars_label_at_three_words(self):
        from pipeline.beats import normalise
        beat = normalise({"kind": "stat", "value": "3700000000000 psi",
                          "label": "bite force of doom bigly"}, {}, 2)
        assert len(beat["value"]) <= 12
        assert beat["label"] == "BITE FORCE OF"

    def test_stat_missing_value_degrades(self):
        from pipeline.beats import normalise
        assert normalise({"kind": "stat", "label": "x"}, {}, 0)["kind"] == "zoom"

    def test_meme_needs_a_subject_and_clips_the_caption(self):
        from pipeline.beats import normalise
        beat = normalise({"kind": "meme", "prompt": "crocodile at the gym",
                          "caption": "one two three four five"}, {}, 0)
        assert beat["kind"] == "meme"
        assert len(beat["caption"].split()) <= 4
        assert normalise({"kind": "meme", "caption": "hi"}, {}, 0)["kind"] == "zoom"

    def test_no_beat_carries_an_expression(self):
        """Croc is one static PNG -- nothing in the pipeline swaps his face."""
        from pipeline.beats import normalise
        beat = normalise({"kind": "meme", "prompt": "p", "expression": "fire"}, {}, 0)
        assert "expression" not in beat

    def test_img_falls_back_to_the_scene_heading(self):
        from pipeline.beats import normalise
        beat = normalise({"kind": "img"}, {"heading": "The Bite"}, 0)
        assert beat["kind"] == "img" and beat["prompt"] == "The Bite"

    def test_zoom_amount_is_clamped_to_the_legal_range(self):
        from pipeline.beats import normalise, ZOOM_MIN, ZOOM_MAX
        assert normalise({"kind": "zoom", "amount": 9.0}, {}, 0)["amount"] == ZOOM_MAX
        assert normalise({"kind": "zoom", "amount": 0.2}, {}, 0)["amount"] == ZOOM_MIN
        assert ZOOM_MIN <= normalise({"kind": "zoom", "amount": "junk"}, {}, 0)["amount"] <= ZOOM_MAX

    def test_arrow_direction_is_whitelisted(self):
        from pipeline.beats import normalise, DIRS
        assert normalise({"kind": "arrow", "label": "here", "dir": "left"}, {}, 0)["dir"] == "left"
        assert normalise({"kind": "arrow", "label": "here", "dir": "up"}, {}, 0)["dir"] in DIRS

    def test_default_sfx_comes_from_the_kind(self):
        from pipeline.beats import normalise, DEFAULT_SFX
        for kind, spec in (("img", {"prompt": "p"}), ("type", {"text": "GO"}),
                           ("stat", {"value": "9"}), ("zoom", {}),
                           ("arrow", {"label": "L"})):
            spec["kind"] = kind
            assert normalise(spec, {}, 0)["sfx"] == DEFAULT_SFX[kind]

    def test_bogus_sfx_falls_back_to_the_default(self):
        from pipeline.beats import normalise, SFX
        assert normalise({"kind": "type", "text": "GO", "sfx": "airhorn"}, {}, 0)["sfx"] in SFX

    def test_unknown_slots_are_dropped(self):
        from pipeline.beats import normalise
        beat = normalise({"kind": "zoom", "onclick": "alert(1)", "src": "http://x"}, {}, 0)
        assert "onclick" not in beat and "src" not in beat


class TestValidate:
    def _legal(self):
        return [{"i": 0, "kind": "img", "prompt": "a skull"},
                {"i": 1, "kind": "type", "text": "BITE FORCE"},
                {"i": 2, "kind": "stat", "value": "3700 PSI", "label": "JAW"},
                {"i": 3, "kind": "type", "text": "NERFED"},
                {"i": 4, "kind": "zoom", "amount": 1.18},
                {"i": 5, "kind": "meme", "prompt": "filing paperwork",
                 "caption": "ADULTING", "template": "split"}]

    def test_a_legal_scene_reports_nothing(self):
        from pipeline.beats import validate
        assert validate(self._legal()) == []

    def test_empty_scene_is_a_violation(self):
        from pipeline.beats import validate
        assert validate([]) and validate(None)

    def test_first_beat_must_be_img(self):
        from pipeline.beats import validate
        beats = self._legal()
        beats[0] = {"i": 0, "kind": "type", "text": "NOPE"}
        assert any("beat 1" in p for p in validate(beats))

    def test_too_many_img_and_meme_beats_are_reported(self):
        """Five pictures in a twelve-beat scene is over budget; the cap is 4 there."""
        from pipeline.beats import validate
        beats = self._legal() + [{"i": 5, "kind": "img", "prompt": "b"},
                                 {"i": 6, "kind": "img", "prompt": "c"},
                                 {"i": 7, "kind": "img", "prompt": "d"},
                                 {"i": 8, "kind": "img", "prompt": "e"},
                                 {"i": 9, "kind": "meme", "prompt": "m", "caption": "A"},
                                 {"i": 10, "kind": "meme", "prompt": "m2", "caption": "B"},
                                 {"i": 11, "kind": "meme", "prompt": "m3", "caption": "C"}]
        problems = " ".join(validate(beats))
        assert '"img"' in problems and '"meme"' in problems

    def test_too_few_word_beats_is_reported(self):
        from pipeline.beats import validate
        beats = [{"i": 0, "kind": "img", "prompt": "a"}, {"i": 1, "kind": "zoom", "amount": 1.1}]
        assert any("type" in p and "stat" in p for p in validate(beats))

    def test_photo_and_doodle_beats_validate_and_cap(self):
        from pipeline import beats as beat_engine
        from pipeline.beats import validate, normalise
        assert "photo" in beat_engine.KINDS and "doodle" in beat_engine.KINDS
        photo = normalise({"kind": "photo", "prompt": "a cheering crowd",
                           "caption": "SO POPULAR", "counter": "31,957,4!?"},
                          {}, 0)
        assert photo["caption"] == "SO POPULAR" and photo["counter"] == "31,957,4!?"
        assert photo["sfx"] == "none"  # hard cuts need no sound
        doodle = normalise({"kind": "doodle", "prompt": "stick figures",
                            "speech": "YEAH NOT SURE WHY"}, {}, 1)
        assert doodle["speech"] == "YEAH NOT SURE WHY"
        meme = normalise({"kind": "meme", "prompt": "filing paperwork",
                          "template": "full"}, {}, 2)
        assert meme["template"] == "full"
        assert normalise({"kind": "meme", "prompt": "x", "template": "bogus"},
                         {}, 3)["template"] == "split"
        scene = ([{"i": 0, "kind": "img", "prompt": "a"}]
                 + [{"i": i + 1, "kind": "photo", "prompt": "p%d" % i} for i in range(3)]
                 + [{"i": 4, "kind": "type", "text": "A B"},
                    {"i": 5, "kind": "stat", "value": "1", "label": "X"}])
        assert any('"photo"' in p for p in validate(scene))

    def test_base_layer_is_hard_cuts_with_no_drift(self):
        from pipeline import compose
        tags, tweens = compose.t_img("e", "w", "assets/x.jpg", 1.0, 2.0)
        script = " ".join(tweens)
        assert "0.030" in script  # the blink only
        assert "power2.out" not in script or "1.10" not in script  # no drift/coast
        _, photo_tw = compose.t_photo("e", "w", "assets/x.jpg", "CAP", "1!?", 1.0, 2.0)
        assert "scale" not in " ".join(photo_tw)  # deadpan: zero motion

    def test_overlong_text_and_missing_prompts_are_reported(self):
        from pipeline.beats import validate
        beats = [{"i": 0, "kind": "img", "prompt": ""},
                 {"i": 1, "kind": "type", "text": "a b c d e f g"},
                 {"i": 2, "kind": "stat", "value": "x" * 40, "label": "L"}]
        problems = " ".join(validate(beats))
        assert "subject prompt" in problems and "<=5 words" in problems
        assert "<=12 characters" in problems


class TestAutofix:
    SCENE = {"dur": 24.0, "heading": "The Bite",
             "text": "A saltwater crocodile bites at 3700 psi which is more than a lion"}

    def test_output_always_passes_validate(self):
        from pipeline.beats import autofix, validate
        wrecked = [{"i": 0, "kind": "zoom"}, {"i": 1, "kind": "zoom"},
                   {"i": 2, "kind": "arrow", "label": "X"}, {"i": 3, "kind": "zoom"}]
        assert validate(autofix(wrecked, self.SCENE)) == []

    def test_an_existing_img_beat_is_promoted_not_duplicated(self):
        from pipeline.beats import autofix
        beats = [{"i": 0, "kind": "type", "text": "A"}, {"i": 1, "kind": "img", "prompt": "keep me"},
                 {"i": 2, "kind": "stat", "value": "9", "label": "L"}]
        out = autofix(beats, self.SCENE)
        assert out[0]["kind"] == "img" and out[0]["prompt"] == "keep me"
        assert sum(1 for b in out if b["kind"] == "img") == 1

    def test_excess_expensive_beats_become_zooms(self):
        from pipeline.beats import autofix, img_cap, MAX_MEME
        beats = [{"i": i, "kind": "img", "prompt": "p%d" % i} for i in range(5)]
        beats += [{"i": 5 + i, "kind": "meme", "prompt": "m", "caption": "C"} for i in range(3)]
        out = autofix(beats, self.SCENE)
        assert sum(1 for b in out if b["kind"] == "img") <= img_cap(len(out))
        assert sum(1 for b in out if b["kind"] == "meme") <= MAX_MEME

    def test_the_art_budget_grows_with_the_scene(self):
        """A 20-beat scene may change picture five times; a 4-beat scene, twice.

        The flat v2 cap of two stamped eighteen cards over two static pictures.
        """
        from pipeline.beats import img_cap, IMG_FLOOR, IMG_CEIL
        assert img_cap(4) == IMG_FLOOR
        assert img_cap(6) == IMG_FLOOR
        assert img_cap(10) == 3
        assert img_cap(12) == 4
        assert img_cap(20) == IMG_CEIL
        assert img_cap(0) == IMG_FLOOR and img_cap(None) == IMG_FLOOR

    def test_manufactured_type_beats_prefer_a_phrase_with_a_number(self):
        from pipeline.beats import autofix
        out = autofix([{"i": 0, "kind": "img", "prompt": "a"}, {"i": 1, "kind": "zoom"},
                       {"i": 2, "kind": "zoom"}], self.SCENE)
        typed = [b["text"] for b in out if b["kind"] == "type"]
        assert typed and any(any(c.isdigit() for c in t) for t in typed)

    def test_indices_are_renumbered_and_sfx_always_present(self):
        from pipeline.beats import autofix, SFX
        out = autofix([{"i": 9, "kind": "img", "prompt": "a"}, {"i": 4, "kind": "zoom"},
                       {"i": 1, "kind": "zoom"}], self.SCENE)
        assert [b["i"] for b in out] == list(range(len(out)))
        assert all(b["sfx"] in SFX for b in out)

    def test_never_mutates_the_input(self):
        from pipeline.beats import autofix
        beats = [{"i": 0, "kind": "zoom"}]
        autofix(beats, self.SCENE)
        assert beats == [{"i": 0, "kind": "zoom"}]


class TestBuild:
    SCENE = {"dur": 24.0, "heading": "The Bite",
             "text": "A crocodile bites at 3700 psi harder than any lion alive today"}

    def test_model_beat_count_never_changes_the_pacing(self):
        from pipeline.beats import build, plan_times
        words = grid(0.4, 60)
        expected = len(plan_times(self.SCENE["dur"], words))
        assert len(build(self.SCENE, words, [{"kind": "zoom"}] * 40)) == expected
        assert len(build(self.SCENE, words, [{"kind": "zoom"}])) == expected
        assert len(build(self.SCENE, words, None)) == expected

    def test_every_beat_carries_a_window(self):
        from pipeline.beats import build
        out = build(self.SCENE, grid(0.4, 60), [{"kind": "img", "prompt": "p"}])
        assert all("t" in b and "end" in b for b in out)
        assert all(b["end"] >= b["t"] for b in out)
        assert out[-1]["end"] == 24.0

    def test_result_is_always_legal_and_opens_on_art(self):
        from pipeline.beats import build, validate
        out = build(self.SCENE, grid(0.4, 60), [{"kind": "arrow", "label": "X"}])
        assert out[0]["kind"] == "img"
        assert validate(out) == []

    def test_deterministic(self):
        from pipeline.beats import build
        specs = [{"kind": "img", "prompt": "p"}, {"kind": "type", "text": "BIG"}]
        assert build(self.SCENE, grid(0.31, 70), specs) == build(self.SCENE, grid(0.31, 70), specs)


# ==========================================================================
# pipeline/run.py — the stage machine, its resume skips and its degradations
# ==========================================================================
class TestStageMachine:
    def test_stage_order_is_the_dependency_order(self):
        from pipeline import run
        assert run.STAGES == ["idea", "voice", "srt", "beats", "visuals",
                              "render", "upload", "thumbnail", "qc", "done"]
        # voice before srt (nothing to time), srt before beats (nothing to snap
        # to), beats before visuals (we don't know which stills to buy).
        for earlier, later in (("voice", "srt"), ("srt", "beats"),
                               ("beats", "visuals"), ("visuals", "render")):
            assert run.STAGES.index(earlier) < run.STAGES.index(later)

    def test_every_stage_but_idea_and_done_has_a_function(self):
        from pipeline import run
        assert set(run.STAGE_FN) == set(run.STAGES) - {"idea", "done"}

    def test_no_stage_function_is_the_deleted_htmlgen(self):
        from pipeline import run
        assert not hasattr(run, "htmlgen")
        assert "htmlgen" not in dir(run)

    def test_voice_and_art_lookups_accept_whatever_container_arrived(self, tmp_path):
        """The TTS host returns wav and the art host returns webp; resume has to
        find both, or every restart re-buys media it already paid for."""
        from pipeline import run
        job = {"id": "2026-09-03-jaw"}
        assert run._voice_path(job, str(tmp_path), 0) is None
        (tmp_path / "v2026-09-03-jaw_0.wav").write_bytes(b"RIFF....WAVE")
        assert run._voice_path(job, str(tmp_path), 0).endswith("_0.wav")
        (tmp_path / "i2026-09-03-jaw_1_2.webp").write_bytes(b"RIFF....WEBP")
        assert run._beat_art(job, str(tmp_path), 1, 2, "img").endswith("_1_2.webp")
        # memes carry an m prefix so they never collide with the img at the same index
        assert run._beat_art(job, str(tmp_path), 1, 2, "meme") is None
        (tmp_path / "m2026-09-03-jaw_1_2.png").write_bytes(b"\x89PNG\r\n")
        assert run._beat_art(job, str(tmp_path), 1, 2, "meme").endswith("_1_2.png")

    def test_a_zero_byte_file_does_not_count_as_done(self, tmp_path):
        """A killed run can leave an empty file; resuming past it renders silent."""
        from pipeline import run
        (tmp_path / "v2026-09-03-jaw_0.mp3").write_bytes(b"")
        assert run._voice_path({"id": "2026-09-03-jaw"}, str(tmp_path), 0) is None

    def test_stage_voice_skips_a_scene_it_already_narrated(self, tmp_path, monkeypatch):
        from pipeline import adapters, run
        calls = []
        monkeypatch.setattr(adapters, "tts", lambda text: calls.append(text) or b"ID3new")
        monkeypatch.setattr(adapters, "last_format", lambda kind=None: "mp3")
        job = {"id": "j", "scenes": [{"text": "one"}, {"text": "two"}]}
        (tmp_path / "vj_0.mp3").write_bytes(b"ID3already")
        run.stage_voice(job, str(tmp_path))
        assert calls == ["two"]
        assert (tmp_path / "vj_0.mp3").read_bytes() == b"ID3already"

    def test_save_still_cover_crops_to_1920x1080_without_squashing(self, tmp_path):
        from PIL import Image
        from pipeline import run
        buf = __import__("io").BytesIO()
        # the art host returns 3:2; a stretch to 16:9 is instantly visible
        Image.new("RGB", (1920, 1280), (10, 20, 30)).save(buf, "WEBP")
        out = run.save_still(buf.getvalue(), "webp", str(tmp_path / "s.jpg"))
        img = Image.open(out)
        assert img.size == (1920, 1080) and img.format == "JPEG"

    def test_save_still_writes_unreadable_bytes_through_rather_than_dying(self, tmp_path):
        from pipeline import run
        out = run.save_still(b"not an image at all", "jpg", str(tmp_path / "s.jpg"))
        assert os.path.getsize(out) > 0

    def test_stage_srt_falls_back_to_the_ffprobe_clock_with_no_word_list(self, tmp_path, monkeypatch):
        """No GROQ_KEY is a documented degradation: beats land on an even grid."""
        from pipeline import run, srt
        monkeypatch.setattr(srt, "word_timeline",
                            lambda p: (_ for _ in ()).throw(RuntimeError("GROQ_KEY missing")))
        monkeypatch.setattr(run, "audio_seconds", lambda p: 12.0)
        job = {"id": "j", "scenes": [{"text": "one"}]}
        (tmp_path / "vj_0.mp3").write_bytes(b"ID3")
        run.stage_srt(job, str(tmp_path))
        assert job["scenes"][0]["dur"] == round(12.0 + run.CAPTION_PAD, 3)
        assert not job["scenes"][0].get("words")

    def test_stage_beats_spends_exactly_one_repair_call_on_an_illegal_reply(self, tmp_path, monkeypatch):
        from pipeline import beats as beat_engine
        from pipeline import llm, run
        seen = []

        def fake_llm(system, user, json_out=False, **kw):
            seen.append(user)
            if len(seen) == 1:                      # illegal: no img, no punch
                return {"beats": [{"kind": "zoom"}, {"kind": "zoom"}]}
            return {"beats": [{"kind": "img", "prompt": "a jaw"},
                              {"kind": "type", "text": "SEALED"},
                              {"kind": "stat", "value": "160", "label": "LBF"},
                              {"kind": "zoom", "amount": 1.2}]}

        monkeypatch.setattr(llm, "llm", fake_llm)
        monkeypatch.setattr(llm, "prompt",
                            lambda name, half: "sys" if half == "system" else "{text} {n}")
        job = {"id": "j", "scenes": [{"text": "one", "dur": 12.0, "act": "HOOK"}]}
        run.stage_beats(job, str(tmp_path))
        assert len(seen) == 2, "expected exactly one repair attempt"
        assert "was invalid" in seen[1]
        assert not beat_engine.validate(job["scenes"][0]["beats"])
        assert job["stage"] == "visuals"

    def test_stage_beats_survives_a_director_that_never_answers(self, tmp_path, monkeypatch):
        """[] is survivable -- build() pads with zooms, so pacing outlives the model."""
        from pipeline import llm, run
        monkeypatch.setattr(llm, "llm",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("providers down")))
        monkeypatch.setattr(llm, "prompt", lambda name, half: "x")
        job = {"id": "j", "scenes": [{"text": "one", "dur": 12.0, "act": "HOOK"}]}
        run.stage_beats(job, str(tmp_path))
        assert len(job["scenes"][0]["beats"]) >= 4

    def test_stage_visuals_never_raises_when_the_art_host_is_down(self, tmp_path, monkeypatch):
        from pipeline import adapters, run
        monkeypatch.setattr(adapters, "image_styled",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("host down")))
        monkeypatch.setattr(adapters, "meme_img",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("host down")))
        job = {"id": "j", "scenes": [{"beats": [
            {"kind": "img", "prompt": "a jaw"}, {"kind": "meme", "prompt": "a croc"}]}]}
        run.stage_visuals(job, str(tmp_path))
        assert job["stage"] == "render"


# ==========================================================================
# v4 Phase 1 — reliability (R1/R2/R3/R7) and the un-hardcoded {perf}
# ==========================================================================

class TestUploadIdempotency:
    def test_marker_is_deterministic_per_job(self):
        from pipeline.upload import job_marker
        assert job_marker("2026-01-01-x") == "[kronvex:2026-01-01-x]"
        assert job_marker("2026-01-01-x") == job_marker("2026-01-01-x")

    def test_find_upload_adopts_orphan_by_marker(self, monkeypatch):
        from pipeline import upload
        monkeypatch.setenv("YT_REFRESH_TOKEN", "r")
        monkeypatch.setenv("YT_CLIENT_ID", "c")
        monkeypatch.setenv("YT_CLIENT_SECRET", "s")

        class Search:
            def list(self, **k):
                class Ex:
                    def execute(self):
                        return {"items": [{"id": {"videoId": "vid9"}},
                                          {"id": {"videoId": "vid8"}}]}
                return Ex()

        class Videos:
            def list(self, **k):
                class Ex:
                    def execute(self):
                        return {"items": [
                            {"id": "vid9", "snippet": {"description": "hello"}},
                            {"id": "vid8", "snippet": {"description": "x [kronvex:J1] y"}}]}
                return Ex()

        class Svc:
            def search(self):
                return Search()

            def videos(self):
                return Videos()

        monkeypatch.setattr(upload, "_service", lambda kind="youtube": Svc())
        assert upload.find_upload("J1") == "vid8"

    def test_find_upload_returns_none_without_match_or_creds(self, monkeypatch):
        from pipeline import upload
        for v in ("YT_REFRESH_TOKEN", "YT_CLIENT_ID", "YT_CLIENT_SECRET"):
            monkeypatch.delenv(v, raising=False)
        assert upload.find_upload("J1") is None


class TestPublishVerification:
    def _yt(self, monkeypatch, public_after=True):
        monkeypatch.setenv("YT_REFRESH_TOKEN", "r")
        monkeypatch.setenv("YT_CLIENT_ID", "c")
        monkeypatch.setenv("YT_CLIENT_SECRET", "s")
        from pipeline import upload
        state = {"status": "private"}

        class Videos:
            def update(self, **k):
                if public_after:
                    state["status"] = "public"

                class Ex:
                    def execute(self):
                        return {}
                return Ex()

            def list(self, **k):
                class Ex:
                    def execute(self):
                        return {"items": [{"status": {"privacyStatus": state["status"]}}]}
                return Ex()

        class Comments:
            def insert(self, **k):
                class Ex:
                    def execute(self):
                        return {}
                return Ex()

        class Svc:
            def videos(self):
                return Videos()

            def commentThreads(self):
                return Comments()

        monkeypatch.setattr(upload, "_service", lambda kind="youtube": Svc())

    def test_verified_public_marks_published(self, tmp_path, monkeypatch):
        from pipeline import publish
        self._yt(monkeypatch, public_after=True)
        d = str(tmp_path)
        job = {"id": "J1", "stage": "done", "video_id": "vid1", "extra_credit": "Extra credit: x"}
        publish.write_job(d + "/J1.json", job)
        out = publish.publish_one(d)
        assert out["published"] is True and "publish_attempts" not in out

    def test_failed_flip_stays_queued_with_attempts(self, tmp_path, monkeypatch):
        from pipeline import publish
        self._yt(monkeypatch, public_after=False)
        d = str(tmp_path)
        job = {"id": "J1", "stage": "done", "video_id": "vid1"}
        publish.write_job(d + "/J1.json", job)
        out = publish.publish_one(d)
        assert out.get("published") is not True
        assert out["publish_attempts"] == 1


class TestQCGate:
    def _job(self, dur=100.0, beats=None):
        beats = beats if beats is not None else [
            {"kind": "img", "prompt": "a jaw"}, {"kind": "type", "text": "BIG"}]
        return {"id": "J1", "thumb": "", "scenes": [{"dur": dur, "beats": beats}]}

    def test_good_episode_passes(self, tmp_path):
        from pipeline import qc
        d = str(tmp_path)
        open(d + "/vJ1_0.wav", "wb").write(b"\x00" * 100)
        open(d + "/iJ1_0_0.jpg", "wb").write(b"\x00" * 100)
        open(d + "/J1_thumb.jpg", "wb").write(b"\x00" * (31 * 1024))
        open(d + "/wJ1_0.json", "w").write('[{"s":0,"e":1,"w":"hi"}]')
        ok, fails, _warns = qc.check(self._job(), d)
        assert ok and not fails

    def test_short_duration_missing_audio_and_thumb_fail(self, tmp_path):
        from pipeline import qc
        d = str(tmp_path)
        ok, fails, _warns = qc.check(self._job(dur=3.0), d)
        assert not ok
        blob = " ".join(fails)
        assert "duration" in blob and "narration" in blob and "thumbnail" in blob

    def test_low_art_coverage_fails(self, tmp_path):
        from pipeline import qc
        d = str(tmp_path)
        open(d + "/vJ1_0.wav", "wb").write(b"\x00" * 100)
        open(d + "/J1_thumb.jpg", "wb").write(b"\x00" * (31 * 1024))
        beats = ([{"kind": "img", "prompt": "p%d" % i} for i in range(4)]
                 + [{"kind": "type", "text": "A B"}])
        ok, fails, _warns = qc.check(self._job(beats=beats), d)
        assert not ok and any("coverage" in f for f in fails)

    def test_whisper_gaps_warn_without_key_fail_with_key(self, tmp_path, monkeypatch):
        from pipeline import qc
        d = str(tmp_path)
        open(d + "/vJ1_0.wav", "wb").write(b"\x00" * 100)
        open(d + "/iJ1_0_0.jpg", "wb").write(b"\x00" * 100)
        open(d + "/J1_thumb.jpg", "wb").write(b"\x00" * (31 * 1024))
        open(d + "/wJ1_0.json", "w").write("[]")
        monkeypatch.delenv("GROQ_KEY", raising=False)
        ok, _fails, warns = qc.check(self._job(), d)
        assert ok and warns
        monkeypatch.setenv("GROQ_KEY", "k")
        ok2, fails2, _w2 = qc.check(self._job(), d)
        assert not ok2 and any("word timings" in f for f in fails2)


class TestSrtResumeAndPerf:
    def test_stage_srt_reuses_word_file(self, tmp_path):
        from pipeline import run
        d = str(tmp_path)
        words = [{"s": 0.0, "e": 1.5, "w": "hi"}]
        open(d + "/wJ1_0.json", "w").write(__import__("json").dumps(words))
        job = {"id": "J1", "stage": "srt", "scenes": [{"text": "hi there"}]}
        run.stage_srt(job, d)
        assert job["scenes"][0]["dur"] == round(1.5 + run.CAPTION_PAD, 3)
        assert job["stage"] == "beats"

    def test_perf_summary_empty_when_no_history(self, tmp_path, monkeypatch):
        from pipeline import run
        monkeypatch.setattr(run, "VIDEOS", str(tmp_path))
        monkeypatch.setattr(run, "STRATEGY", str(tmp_path / "strategy.json"))
        assert run._perf_summary() == "(no performance data yet)"

    def test_qc_failed_jobs_do_not_resume(self):
        from pipeline import run
        jobs = [{"id": "q", "stage": "qc_failed"}, {"id": "v", "stage": "voice"}]
        assert run.in_progress(jobs)["id"] == "v"
        assert run.in_progress([{"id": "q", "stage": "qc_failed"}]) is None
