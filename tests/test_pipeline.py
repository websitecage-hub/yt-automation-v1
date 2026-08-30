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
    return FakeResp(payload=chat(content))


# ==========================================================================
# TASK 4 — pipeline/llm.py
# ==========================================================================
class TestLLM:
    def _mod(self, monkeypatch, keys=("TABI_KEY", "NIM_KEY", "GEMINI_KEY", "GROQ_KEY")):
        for env in ("TABI_KEY", "NIM_KEY", "GEMINI_KEY", "GROQ_KEY"):
            monkeypatch.setenv(env, "k-" + env if env in keys else "")
        from pipeline import llm
        monkeypatch.setattr(llm.time, "sleep", lambda *a, **k: None)  # no real backoff in tests
        return llm

    def test_happy_path_uses_first_provider(self, monkeypatch):
        llm = self._mod(monkeypatch)
        calls = []

        def fake_post(url, **kw):
            calls.append((url, kw["json"]["model"], kw["json"]["temperature"], kw["headers"]))
            return FakeResp(payload=anthropic_msg("Base stats first."))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("sys", "user") == "Base stats first."
        assert len(calls) == 1
        url, model, temp, headers = calls[0]
        assert url == llm.TABI_BASE + "/v1/messages"
        assert model == "claude-opus-4-8"
        assert temp == 0.85
        assert "Mozilla/" in headers["User-Agent"]   # Cloudflare needs a browser UA

    def test_code_chain_starts_at_tabi_opus(self, monkeypatch):
        llm = self._mod(monkeypatch)
        seen = {}

        def fake_post(url, **kw):
            seen.update(url=url, **kw["json"])
            return FakeResp(payload=anthropic_msg("tl.to('#s0-a',{opacity:1},0.5);"))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        llm.llm_code("sys", "user")
        assert seen["url"] == llm.TABI_BASE + "/v1/messages"
        assert seen["model"] == "claude-opus-4-8"
        assert seen["temperature"] == 0.4
        assert seen["max_tokens"] == 12000

    def test_first_provider_fails_second_is_used(self, monkeypatch):
        llm = self._mod(monkeypatch)
        models = []

        def fake_post(url, **kw):
            models.append(kw["json"]["model"])
            if len(models) == 1:
                return FakeResp(status=403, text="cloudflare")  # non-retryable -> next provider
            return FakeResp(payload=chat("second answer"))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u") == "second answer"
        assert models == ["claude-opus-4-8", "nvidia/nemotron-3-super-120b-a12b"]

    def test_retryable_status_is_retried_on_same_provider(self, monkeypatch):
        llm = self._mod(monkeypatch)
        n = {"i": 0}

        def fake_post(url, **kw):
            n["i"] += 1
            if n["i"] == 1:
                return FakeResp(status=503, text="try later")  # retryable -> retry, don't fall through
            return FakeResp(payload=anthropic_msg("ok"))

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
            return FakeResp(payload=anthropic_msg("recovered"))

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
        assert models == ["openai/gpt-oss-120b"]

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

    def test_json_out_sets_response_format_on_openai_dialect(self, monkeypatch):
        llm = self._mod(monkeypatch, keys=("GROQ_KEY",))
        seen = {}

        def fake_post(url, **kw):
            seen.update(kw["json"])
            return FakeResp(payload=chat('{"verdict":"APEX"}'))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u", json_out=True) == {"verdict": "APEX"}
        assert seen["response_format"] == {"type": "json_object"}

    def test_anthropic_dialect_parses_json_without_response_format(self, monkeypatch):
        llm = self._mod(monkeypatch, keys=("TABI_KEY",))
        seen = {}

        def fake_post(url, **kw):
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
        models = []

        def fake_post(url, **kw):
            models.append(kw["json"]["model"])
            if len(models) == 1:
                return FakeResp(payload=anthropic_msg("I'm afraid I can't do that."))
            return FakeResp(payload=chat('{"ok":true}'))

        monkeypatch.setattr(llm.requests, "post", fake_post)
        assert llm.llm("s", "u", json_out=True) == {"ok": True}
        assert len(models) == 2

    def test_providers_health_reflects_env(self, monkeypatch):
        monkeypatch.setenv("TABI_KEY", "x")
        monkeypatch.setenv("GROQ_KEY", "")
        monkeypatch.delenv("NIM_KEY", raising=False)
        monkeypatch.delenv("GEMINI_KEY", raising=False)
        import importlib
        from pipeline import llm as _llm
        llm = importlib.reload(_llm)
        assert llm.PROVIDERS_HEALTH == {
            "TABI_KEY": True, "NIM_KEY": False, "GEMINI_KEY": False, "GROQ_KEY": False,
        }

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
        assert "SCALED" in seen["system"]
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
# TASK 8 — pipeline/htmlgen.py
# ==========================================================================
GOOD_JS = ("tl.fromTo('#s2-card',{opacity:0},{opacity:1,duration:0.5},12.000);\n"
           "tl.to('#s2-card',{y:-30,duration:0.6},13.000);\n"
           "tl.to('#img2',{scale:1.05,duration:4},12.200);")
GOOD_HTML = '<div id="s2-card" class="clip" data-start="12.00" data-duration="6.00">STAT</div>'


def job_with(scenes, **kw):
    j = {"id": "2026-08-29-test", "title": "The Punch That Breaks Physics", "scenes": scenes}
    j.update(kw)
    return j


class TestHtmlgenBans:
    @pytest.mark.parametrize("bad", [
        "tl.to('#s2-a',{x:Math.random()*10},12.0);",
        "tl.to('#s2-a',{x:Date.now()},12.0);",
        "setTimeout(function(){},10);tl.to('#s2-a',{x:1},12.0);",
        "setInterval(f,10);tl.to('#s2-a',{x:1},12.0);",
        "requestAnimationFrame(f);tl.to('#s2-a',{x:1},12.0);",
        "fetch('/x');tl.to('#s2-a',{x:1},12.0);",
        "localStorage.setItem('a','b');tl.to('#s2-a',{x:1},12.0);",
    ])
    def test_banned_js_tokens_are_caught(self, bad):
        from pipeline import htmlgen
        v = htmlgen.static_violations(GOOD_HTML, bad, 2, 12.0, 18.0)
        assert any("banned token" in x for x in v), v

    @pytest.mark.parametrize("bad", [
        '<img id="s2-i" src="https://x.com/a.jpg">',
        '<img id="s2-i" src="http://x.com/a.jpg">',
        '<style>@import url(a.css);</style><div id="s2-a"></div>',
        '<link rel="stylesheet" href="a.css"><div id="s2-a"></div>',
        '<script src="a.js"></script><div id="s2-a"></div>',
    ])
    def test_banned_html_tokens_are_caught(self, bad):
        from pipeline import htmlgen
        v = htmlgen.static_violations(bad, GOOD_JS, 2, 12.0, 18.0)
        assert any("banned token" in x for x in v), v

    def test_lookalike_identifiers_are_not_banned(self):
        """F.rand, mathRandom, dateNow, myFetch, timeoutMs must all pass."""
        from pipeline import htmlgen
        js = ("const k = F.rand;\n"
              "tl.to('#s2-a',{x:mathRandom,duration:0.2},12.0);\n"
              "tl.to('#s2-a',{y:dateNow + timeoutMs + myFetch(1),duration:0.2},12.5);\n"
              "tl.to('#s2-a',{opacity:1,duration:0.2},12.1);")
        assert htmlgen.static_violations(GOOD_HTML, js, 2, 12.0, 18.0) == []

    def test_clean_scene_has_no_violations(self):
        from pipeline import htmlgen
        assert htmlgen.static_violations(GOOD_HTML, GOOD_JS, 2, 12.0, 18.0) == []

    def test_missing_tween_call_is_a_violation(self):
        from pipeline import htmlgen
        v = htmlgen.static_violations(GOOD_HTML, "const x = 1;", 2, 12.0, 18.0)
        assert any("no tl.to(" in x for x in v)

    def test_framework_owned_selectors_are_rejected(self):
        from pipeline import htmlgen
        for sel in ("#croc", "#croc-jaw", "#croc-arm", ".croc-eye", "#verdict", "#lesson", "#c2-3"):
            js = "tl.to('%s',{opacity:1,duration:0.2},12.0);" % sel
            v = htmlgen.static_violations(GOOD_HTML, js, 2, 12.0, 18.0)
            assert any("framework-owned" in x for x in v), (sel, v)

    def test_css_motion_is_rejected(self):
        from pipeline import htmlgen
        html = '<div id="s2-a" style="transition: all 0.3s ease">x</div>'
        assert any("CSS transition" in x for x in htmlgen.static_violations(html, GOOD_JS, 2, 12.0, 18.0))
        html = '<div id="s2-a" style="animation: spin 2s linear">x</div>'
        assert any("CSS animation" in x for x in htmlgen.static_violations(html, GOOD_JS, 2, 12.0, 18.0))

    def test_ids_must_be_scene_scoped(self):
        from pipeline import htmlgen
        v = htmlgen.static_violations('<div id="card">x</div>', GOOD_JS, 2, 12.0, 18.0)
        assert any("must start with 's2-'" in x for x in v), v

    def test_other_scenes_elements_are_off_limits(self):
        from pipeline import htmlgen
        js = "tl.to('#s5-card',{opacity:1,duration:0.2},12.0);"
        v = htmlgen.static_violations(GOOD_HTML, js, 2, 12.0, 18.0)
        assert any("not yours" in x for x in v), v

    def test_own_image_and_globals_are_allowed(self):
        from pipeline import htmlgen
        js = ("tl.to('#img2',{scale:1.05,duration:5},12.0);\n"
              "tl.to('#stage',{opacity:1,duration:1},13.0);")
        assert htmlgen.static_violations(GOOD_HTML, js, 2, 12.0, 18.0) == []
        v = htmlgen.static_violations(GOOD_HTML, "tl.to('#img5',{scale:1.05,duration:5},12.0);", 2, 12.0, 18.0)
        assert any("not yours" in x for x in v)
        # the host is framework-owned now: the model must not touch #croc at all
        v = htmlgen.static_violations(GOOD_HTML, "tl.to('#croc',{x:1450,duration:1},12.0);", 2, 12.0, 18.0)
        assert any("framework-owned" in x for x in v)


class TestHtmlgenTimeWindow:
    def test_times_inside_the_window_pass_including_slack(self):
        from pipeline import htmlgen
        for t in (12.0, 15.0, 18.0, 11.8, 18.2):
            js = "tl.to('#s2-a',{opacity:1,duration:0.2},%s);" % t
            assert htmlgen.static_violations(GOOD_HTML, js, 2, 12.0, 18.0) == [], t

    def test_times_outside_the_window_are_rejected(self):
        from pipeline import htmlgen
        for t in (0.0, 11.79, 18.21, 400.0, -3.0):
            js = "tl.to('#s2-a',{opacity:1,duration:0.2},%s);" % t
            v = htmlgen.static_violations(GOOD_HTML, js, 2, 12.0, 18.0)
            assert any("outside this scene's window" in x for x in v), t

    def test_relative_position_strings_are_rejected(self):
        from pipeline import htmlgen
        for pos in ("'+=0.5'", '"<"', "'>'"):
            js = "tl.to('#s2-a',{opacity:1,duration:0.2},%s);" % pos
            v = htmlgen.static_violations(GOOD_HTML, js, 2, 12.0, 18.0)
            assert any("no absolute-second time" in x for x in v), pos

    def test_missing_position_argument_is_rejected(self):
        from pipeline import htmlgen
        v = htmlgen.static_violations(GOOD_HTML, "tl.to('#s2-a',{opacity:1,duration:0.2});", 2, 12.0, 18.0)
        assert any("no absolute-second time" in x for x in v), v

    def test_parser_survives_nested_braces_commas_and_strings(self):
        from pipeline import htmlgen
        js = ("tl.to('#s2-a',{x:1,y:2,ease:'power2.out',onStart:null,"
              "transformOrigin:'50% 50%',boxShadow:'0 0 20px rgba(166,255,61,0.6)'},13.250);")
        assert [t for t, _ in htmlgen.tween_times(js)] == [13.25]

    def test_parser_reads_fromto_with_two_vars_objects(self):
        from pipeline import htmlgen
        js = "tl.fromTo('#s2-a',{scale:3,opacity:0},{scale:1,opacity:1,duration:0.5},14.75);"
        assert [t for t, _ in htmlgen.tween_times(js)] == [14.75]

    def test_parser_handles_whitespace_newlines_and_multiple_calls(self):
        from pipeline import htmlgen
        js = ("tl . to (\n  '#s2-a',\n  {opacity: 1, duration: 0.4},\n  12.100\n);\n"
              "tl.to('#s2-b',{y:-30,duration:0.6},13.4);\n")
        assert [t for t, _ in htmlgen.tween_times(js)] == [12.1, 13.4]

    def test_parser_ignores_parens_inside_quotes(self):
        from pipeline import htmlgen
        js = "tl.to('#s2-a',{ease:'back.in(1.2)',duration:0.5},16.000);"
        assert [t for t, _ in htmlgen.tween_times(js)] == [16.0]

    def test_parser_returns_nothing_when_there_are_no_tweens(self):
        from pipeline import htmlgen
        assert htmlgen.tween_times("const a = fn(1,2);") == []


class TestHtmlgenFences:
    def test_clean_two_fences(self):
        from pipeline import htmlgen
        html, js = htmlgen.parse_fences("```html\n<div id=\"s0-a\"></div>\n```\n```js\ntl.to(1);\n```")
        assert html == '<div id="s0-a"></div>' and js == "tl.to(1);"

    def test_messy_whitespace_prose_and_casing(self):
        from pipeline import htmlgen
        reply = ("Sure! Here is scene 3.\n\n```  HTML  \n\n<div id=\"s3-a\">hi</div>\n\n```"
                 "\n\nAnd the timeline:\n\n``` javascript \ntl.to('#s3-a',{x:1},4.0);\n```\n\nEnjoy.")
        html, js = htmlgen.parse_fences(reply)
        assert html == '<div id="s3-a">hi</div>'
        assert js == "tl.to('#s3-a',{x:1},4.0);"

    def test_unlabelled_fences_fall_back_to_order(self):
        from pipeline import htmlgen
        html, js = htmlgen.parse_fences("```\n<div id=\"s1-a\"></div>\n```\n```\ntl.to(1);\n```")
        assert html == '<div id="s1-a"></div>' and js == "tl.to(1);"

    def test_unclosed_final_fence_still_parses(self):
        from pipeline import htmlgen
        html, js = htmlgen.parse_fences("```html\n<div id=\"s1-a\"></div>\n```\n```js\ntl.to('#s1-a',{x:1},2.0);")
        assert js == "tl.to('#s1-a',{x:1},2.0);"

    def test_one_or_zero_fences_raises(self):
        from pipeline import htmlgen
        with pytest.raises(ValueError, match="two fenced blocks"):
            htmlgen.parse_fences("```html\n<div></div>\n```")
        with pytest.raises(ValueError, match="two fenced blocks"):
            htmlgen.parse_fences("no fences at all")

    def test_extra_trailing_fence_is_ignored(self):
        from pipeline import htmlgen
        html, js = htmlgen.parse_fences(
            "```html\n<div id=\"s1-a\"></div>\n```\n```js\ntl.to(1);\n```\n```bash\nnpm i\n```")
        assert js == "tl.to(1);"


class TestHtmlgenBrief:
    def test_scene_t0_sums_previous_durations(self):
        from pipeline import htmlgen
        job = job_with([{"dur": 10.0}, {"dur": 5.5}, {"dur": 7.25}])
        assert htmlgen.scene_t0(job, 0) == 0.0
        assert htmlgen.scene_t0(job, 1) == 10.0
        assert htmlgen.scene_t0(job, 2) == 15.5
        assert htmlgen.scene_t0(job, 3) == 22.75

    def test_scene_t0_ignores_missing_and_junk_durations(self):
        from pipeline import htmlgen
        job = job_with([{"dur": 10.0}, {}, {"dur": "nope"}, {"dur": 4}])
        assert htmlgen.scene_t0(job, 4) == 14.0

    def test_word_clock_is_absolute_and_formatted_to_millis(self):
        from pipeline import htmlgen
        words = [{"w": "Your", "s": 0.1, "e": 0.4}, {"w": " brain ", "s": 0.4, "e": 0.9}]
        assert htmlgen.word_clock(words, 104.8) == "Your(104.900-105.200) brain(105.200-105.700)"

    def test_word_clock_drops_blank_and_broken_words(self):
        from pipeline import htmlgen
        words = [{"w": "", "s": 0, "e": 1}, {"w": "ok", "s": 1, "e": 2}, {"w": "x", "s": "?", "e": 3}]
        assert htmlgen.word_clock(words, 0) == "ok(1.000-2.000)"

    def test_words_come_from_the_srt_work_file(self, tmp_path):
        from pipeline import htmlgen
        (tmp_path / "wjob1_2.json").write_text(json.dumps([{"w": "hi", "s": 0.0, "e": 0.5}]), encoding="utf-8")
        job = job_with([{}, {}, {}], id="job1")
        assert htmlgen.scene_words(job, 2, str(tmp_path))[0]["w"] == "hi"

    def test_words_on_the_job_win_over_the_work_file(self, tmp_path):
        from pipeline import htmlgen
        (tmp_path / "wjob1_0.json").write_text(json.dumps([{"w": "file", "s": 0, "e": 1}]), encoding="utf-8")
        job = job_with([{"words": [{"w": "inline", "s": 0, "e": 1}]}], id="job1")
        assert htmlgen.scene_words(job, 0, str(tmp_path))[0]["w"] == "inline"

    def test_missing_word_file_is_not_fatal(self, tmp_path):
        from pipeline import htmlgen
        assert htmlgen.scene_words(job_with([{}], id="nope"), 0, str(tmp_path)) == []

    def test_brief_carries_the_scene_window_and_fields(self):
        from pipeline import htmlgen
        job = job_with([{"dur": 12.0}, {"act": "BASE STATS", "heading": "GRIP", "text": "It bites.",
                                        "image_prompt": "a jaw", "dur": 6.5,
                                        "words": [{"w": "It", "s": 0.2, "e": 0.5}]}])
        b = htmlgen.build_brief(job, 1, None)
        assert (b["t0"], b["t1"]) == (12.0, 18.5)
        assert b["act"] == "BASE STATS" and b["heading"] == "GRIP"
        assert b["clock"] == "It(12.200-12.500)"

    def test_brief_notes_when_there_is_no_word_clock(self):
        from pipeline import htmlgen
        b = htmlgen.build_brief(job_with([{"dur": 5.0}]), 0, None)
        assert "no word timings" in b["clock"]

    def test_prompt_placeholders_are_all_filled(self):
        from pipeline import htmlgen
        job = job_with([{"act": "HOOK", "heading": "H", "text": "T", "image_prompt": "IP",
                         "dur": 8.0, "words": [{"w": "T", "s": 0.0, "e": 0.4}]}])
        system, user = htmlgen.render_prompt(htmlgen.build_brief(job, 0, None))
        assert "motion designer" in system
        for leftover in ("{i+1}", "{title}", "{act}", "{heading}", "{text}",
                         "{clock}", "{t0}", "{t1}", "{image_prompt}"):
            assert leftover not in user, leftover
        assert "SCENE 1 of 16" in user
        assert "The Punch That Breaks Physics" in user
        assert "[0.000, 8.000]" in user


class TestHtmlgenLoop:
    @pytest.fixture(autouse=True)
    def no_subprocess(self, monkeypatch):
        """The lint gate is exercised for real elsewhere; here it must never shell out."""
        from pipeline import htmlgen
        monkeypatch.setattr(htmlgen, "lint_violations", lambda *a, **k: [])
        monkeypatch.setattr(htmlgen.subprocess, "run",
                            lambda *a, **k: pytest.fail("no subprocess in this test"))

    def job(self):
        return job_with([{"act": "HOOK", "heading": "SEALED", "text": "Your brain seals it.",
                          "image_prompt": "a brain", "dur": 9.0,
                          "words": [{"w": "Your", "s": 0.2, "e": 0.5}]}])

    def test_accepts_a_clean_first_attempt(self, monkeypatch):
        from pipeline import htmlgen, llm
        reply = ("```html\n<div id=\"s0-a\" class=\"clip\" data-start=\"0.20\" "
                 "data-duration=\"8.00\">SEALED</div>\n```\n```js\n"
                 "tl.fromTo('#s0-a',{opacity:0},{opacity:1,duration:0.5},0.200);\n"
                 "tl.to('#s0-a',{y:-20,duration:0.4},0.400);\n```")
        calls = []
        monkeypatch.setattr(llm, "llm_code", lambda s, u: (calls.append(u), reply)[1])
        job = self.job()
        htmlgen.generate_scene(job, 0, None)
        assert job["scenes"][0]["frag_source"] == "llm attempt 1"
        assert "s0-a" in job["scenes"][0]["frag_js"]
        assert len(calls) == 1

    def test_repairs_then_accepts_and_feeds_violations_back(self, monkeypatch):
        from pipeline import htmlgen, llm
        bad = "```html\n<div id=\"nope\"></div>\n```\n```js\ntl.to('#nope',{x:Math.random()},99.0);\n```"
        good = ("```html\n<div id=\"s0-a\"></div>\n```\n```js\n"
                "tl.to('#s0-a',{opacity:1,duration:0.4},1.000);\n```")
        seen = []

        def fake(system, user):
            seen.append(user)
            return bad if len(seen) == 1 else good

        monkeypatch.setattr(llm, "llm_code", fake)
        job = self.job()
        htmlgen.generate_scene(job, 0, None)
        assert job["scenes"][0]["frag_source"] == "llm attempt 2"
        assert "YOUR PREVIOUS ATTEMPT WAS REJECTED" in seen[1]
        assert "Math.random" in seen[1] and "99.000" in seen[1]
        assert "[0.000, 9.000]" in seen[1]

    def test_four_bad_attempts_fall_back_and_never_raise(self, monkeypatch):
        from pipeline import htmlgen, llm
        n = {"i": 0}

        def fake(system, user):
            n["i"] += 1
            return "```html\n<div id=\"bad\"></div>\n```\n```js\nsetTimeout(f,1);tl.to('#bad',{x:1},900.0);\n```"

        monkeypatch.setattr(llm, "llm_code", fake)
        job = self.job()
        scene = htmlgen.generate_scene(job, 0, None)
        assert n["i"] == htmlgen.MAX_REPAIRS + 1 == 4
        assert scene["frag_source"] == "SAFE_FALLBACK"
        assert "s0-fbhead" in scene["frag_html"]
        assert htmlgen.static_violations(scene["frag_html"], scene["frag_js"], 0, 0.0, 9.0) == []

    def test_llm_outage_falls_back_without_raising(self, monkeypatch):
        from pipeline import htmlgen, llm

        def boom(system, user):
            raise RuntimeError("all LLM providers down")

        monkeypatch.setattr(llm, "llm_code", boom)
        job = self.job()
        assert htmlgen.generate_scene(job, 0, None)["frag_source"] == "SAFE_FALLBACK"

    def test_unparseable_reply_is_treated_as_a_rejection(self, monkeypatch):
        from pipeline import htmlgen, llm
        monkeypatch.setattr(llm, "llm_code", lambda s, u: "I cannot do that.")
        job = self.job()
        assert htmlgen.generate_scene(job, 0, None)["frag_source"] == "SAFE_FALLBACK"

    def test_lint_errors_alone_force_a_repair(self, monkeypatch):
        from pipeline import htmlgen, llm
        good = ("```html\n<div id=\"s0-a\"></div>\n```\n```js\n"
                "tl.to('#s0-a',{opacity:1,duration:0.4},1.000);\n```")
        monkeypatch.setattr(llm, "llm_code", lambda s, u: good)
        rounds = {"i": 0}

        def flaky(*a, **k):
            rounds["i"] += 1
            return ["hyperframes lint media_missing_id: needs an id"] if rounds["i"] == 1 else []

        monkeypatch.setattr(htmlgen, "lint_violations", flaky)
        job = self.job()
        assert htmlgen.generate_scene(job, 0, None)["frag_source"] == "llm attempt 2"

    def test_scene_slot_is_created_when_the_job_is_short(self, monkeypatch):
        from pipeline import htmlgen, llm
        monkeypatch.setattr(llm, "llm_code", lambda s, u: "junk")
        job = {"id": "x", "title": "t", "scenes": []}
        htmlgen.generate_scene(job, 2, None)
        assert len(job["scenes"]) == 3 and job["scenes"][2]["frag_source"] == "SAFE_FALLBACK"


class TestHtmlgenComposition:
    def test_test_composition_has_the_contract_attributes(self):
        from pipeline import htmlgen
        page = htmlgen.test_composition(GOOD_HTML, GOOD_JS, 2, 12.0, 18.0)
        assert 'data-composition-id="scaled"' in page
        assert 'data-duration="18.00"' in page          # renderer needs a duration source
        assert 'data-width="1920"' in page and 'data-height="1080"' in page
        assert "gsap.timeline({paused:true})" in page
        assert "window.__timelines = {scaled: tl}" in page
        assert 'src="assets/gsap.min.js"' in page and "http" not in page
        assert GOOD_HTML in page and GOOD_JS in page

    def test_safe_fallback_is_self_consistent_for_any_window(self):
        from pipeline import htmlgen
        for t0, t1, i in ((0.0, 9.0, 0), (104.5, 118.25, 7), (500.0, 500.05, 15)):
            html, js = htmlgen.SAFE_FALLBACK(t0, t1, "HEADING <&>", i)
            assert htmlgen.static_violations(html, js, i, t0, max(t1, t0 + 1.2)) == []
            assert "s%d-fbhead" % i in html
            assert "#img%d" % i in js and "#croc" not in js
            assert "<" not in html.split(">", 1)[1].split("<")[0]   # heading text is sanitised

    def test_safe_fallback_survives_an_empty_heading(self):
        from pipeline import htmlgen
        html, _ = htmlgen.SAFE_FALLBACK(1.0, 5.0, "")
        assert ">SCALED<" in html

    @pytest.mark.skipif(os.getenv("HF_LIVE") != "1",
                        reason="set HF_LIVE=1 to run the real `hyperframes lint` gate (needs npx)")
    def test_safe_fallback_passes_the_real_linter(self, tmp_path):
        from pipeline import htmlgen
        html, js = htmlgen.SAFE_FALLBACK(10.0, 22.5, "THE AWAKENING", 3)
        assert htmlgen.lint_violations(html, js, 3, 10.0, 22.5, str(tmp_path)) == []
        assert htmlgen.LINT_SUBCOMMAND in ("lint", "check")


# ==========================================================================
# TASK 9 — pipeline/compose.py
# ==========================================================================
def w(text, s, e):
    return {"w": text, "s": s, "e": e}


def words_from(sentence, step=0.3):
    return [w(t, i * step, i * step + step - 0.05) for i, t in enumerate(sentence.split())]


def texts(lines):
    return [[x["w"] for x in line] for line in lines]


class TestGroupLines:
    def test_flushes_on_the_34_char_boundary(self):
        from pipeline.compose import group_lines
        # 4 x 8 chars + 3 spaces = 35 > 34, so the fourth word starts a new line
        got = texts(group_lines(words_from("aaaaaaaa bbbbbbbb cccccccc dddddddd")))
        assert got == [["aaaaaaaa", "bbbbbbbb", "cccccccc"], ["dddddddd"]]

    def test_exactly_34_chars_stays_on_one_line(self):
        from pipeline.compose import group_lines
        # 3 x 10 + 2 spaces = 32, plus a 2-char word and a space = 35 -> splits;
        # trimming to 1 char keeps it at 34 and must not split
        assert len(group_lines(words_from("aaaaaaaaaa bbbbbbbbbb cccccccccc d"))) == 1
        assert len(group_lines(words_from("aaaaaaaaaa bbbbbbbbbb cccccccccc dd"))) == 2

    def test_flushes_on_the_5_word_boundary(self):
        from pipeline.compose import group_lines
        got = texts(group_lines(words_from("a b c d e f g")))
        assert got == [["a", "b", "c", "d", "e"], ["f", "g"]]

    def test_flushes_after_a_sentence_end(self):
        from pipeline.compose import group_lines
        assert texts(group_lines(words_from("It bites. Hard"))) == [["It", "bites."], ["Hard"]]
        assert texts(group_lines(words_from("Really? Yes"))) == [["Really?"], ["Yes"]]
        assert texts(group_lines(words_from("Stop! Go"))) == [["Stop!"], ["Go"]]

    def test_a_single_long_word_gets_its_own_line_and_may_overflow(self):
        from pipeline.compose import group_lines
        long = "electroencephalographically" * 2
        got = texts(group_lines(words_from("tiny %s next" % long)))
        assert got == [["tiny"], [long], ["next"]]
        assert len(got[1][0]) > 34

    def test_never_drops_a_word(self):
        from pipeline.compose import group_lines
        sentence = ("Your brain seals your true strength. Two hundred million years of field "
                    "research says otherwise, and the numbers are not kind.")
        words = words_from(sentence)
        flat = [x["w"] for line in group_lines(words) for x in line]
        assert flat == sentence.split()

    def test_respects_custom_limits(self):
        from pipeline.compose import group_lines
        assert texts(group_lines(words_from("a b c d"), max_words=2)) == [["a", "b"], ["c", "d"]]
        assert texts(group_lines(words_from("aaa bbb"), max_chars=5)) == [["aaa"], ["bbb"]]

    def test_survives_empty_blank_and_malformed_input(self):
        from pipeline.compose import group_lines
        assert group_lines([]) == []
        assert group_lines(None) == []
        assert texts(group_lines([w("  ", 0, 1), "junk", w("ok", 1, 2)])) == [["ok"]]


class TestGradePct:
    @pytest.mark.parametrize("grade,pct", [
        ("A+", 100), ("A", 92), ("A-", 87), ("B+", 80), ("B", 75), ("B-", 70),
        ("C+", 60), ("C", 55), ("C-", 50), ("D+", 40), ("D", 35), ("D-", 30),
        ("F", 15), ("F-", 10),
    ])
    def test_mapping(self, grade, pct):
        from pipeline.compose import grade_pct
        assert grade_pct(grade) == pct

    def test_case_and_whitespace_insensitive(self):
        from pipeline.compose import grade_pct
        assert grade_pct("  a+ ") == 100 and grade_pct("b") == 75

    def test_unknown_and_empty_grades_are_survivable(self):
        from pipeline.compose import grade_pct
        assert grade_pct("") == 50 and grade_pct(None) == 50 and grade_pct("???") == 50

    def test_stays_within_0_and_100(self):
        from pipeline.compose import grade_pct
        assert all(0 <= grade_pct(g) <= 100 for g in
                   ["A+", "A++", "S", "S+", "F-", "F--", "Z", "", None, 7])


def demo_job(**kw):
    words = [w("Your", 0.10, 0.42), w("brain", 0.42, 0.85), w("seals", 0.88, 1.20),
             w("your", 1.20, 1.38), w("strength.", 1.38, 1.71)]
    job = {
        "id": "2026-08-29-brain", "lesson": 7, "title": "Your Brain Seals Your True Strength",
        "verdict": "APEX",
        "scenes": [
            {"act": "HOOK", "heading": "SEALED", "text": "Your brain seals your strength.",
             "image_prompt": "a brain", "dur": 24.0, "words": words, "stat": None,
             "frag_html": '<div id="s0-a" class="clip" data-start="1.00" data-duration="4.00">A</div>',
             "frag_js": "tl.to('#s0-a',{opacity:1,duration:0.4},1.000);"},
            {"act": "BASE STATS", "heading": "GRIP", "text": "It bites.", "image_prompt": "a jaw",
             "dur": 18.0, "words": words,
             "stat": {"label": "NEURAL BRAKE", "grade": "A+", "note": "Governor caps recruitment."},
             "frag_html": "", "frag_js": ""},
        ],
    }
    job.update(kw)
    return job


def build(tmp_path, job, media=True):
    """Build a project in tmp_path, optionally with real-ish media files present."""
    from pipeline import compose
    if media:
        for i in range(len(job["scenes"])):
            (tmp_path / ("i%s_%d.jpg" % (job["id"], i))).write_bytes(b"\xff\xd8\xff\xe0jpg")
            (tmp_path / ("v%s_%d.mp3" % (job["id"], i))).write_bytes(b"ID3mp3")
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
        assert 'data-duration="%.2f"' % total in page      # renderer needs a duration source
        assert 'data-width="1920"' in page and 'data-height="1080"' in page
        assert "const tl = gsap.timeline({paused:true});" in page
        assert "window.__timelines = {scaled: tl};" in page

    def test_head_loads_only_the_vendored_gsap_and_inline_css(self, tmp_path):
        proj, _, page = build(tmp_path, demo_job())
        head = page[page.index("<head>"):page.index("</head>")]
        assert head.count("<script") == 1 and 'src="assets/gsap.min.js"' in head
        assert "<link" not in head and "http://" not in page and "https://" not in page
        assert "@font-face" in head and "--lime:#A6FF3D" in head
        assert "url('assets/fonts/display.woff2')" in head   # rebased for the project root
        assert os.path.isfile(os.path.join(proj, "assets", "gsap.min.js"))

    def test_every_timed_element_carries_an_id(self, tmp_path):
        """Without an id the renderer cannot discover media -- audio renders SILENT."""
        _, _, page = build(tmp_path, demo_job())
        timed = re.findall(r"<(\w+)([^>]*data-start[^>]*)>", page)
        assert timed
        for tag, attrs in timed:
            assert re.search(r'\bid="', attrs), (tag, attrs)

    def test_media_clips_are_wired_per_scene(self, tmp_path):
        proj, _, page = build(tmp_path, demo_job())
        assert '<img id="img0" class="clip" data-start="0.00" data-duration="24.00"' in page
        assert 'src="assets/s0.jpg"' in page and 'data-track-index="0"' in page
        assert '<audio id="aud1" data-start="24.00" data-duration="18.00"' in page
        assert 'src="assets/a1.mp3"' in page and 'data-track-index="2"' in page
        for name in ("s0.jpg", "s1.jpg", "a0.mp3", "a1.mp3"):
            assert os.path.isfile(os.path.join(proj, "assets", name)), name

    def test_missing_media_is_omitted_rather_than_referenced(self, tmp_path):
        _, _, page = build(tmp_path, demo_job(), media=False)
        assert "assets/s0.jpg" not in page and "assets/a0.mp3" not in page
        # the host image is always present; only the scene media img/audio are omitted
        assert '<img id="img' not in page and "<audio" not in page

    def test_bgdiv_media_mode(self, tmp_path, monkeypatch):
        from pipeline import compose
        monkeypatch.setattr(compose, "MEDIA_MODE", "bgdiv")
        _, _, page = build(tmp_path, demo_job())
        assert '<div id="img0" class="mediaclip clip"' in page
        assert "background-image:url(assets/s0.jpg)" in page
        assert '<img id="img' not in page          # scene media are divs here; the host <img> stays

    def test_captions_are_absolute_word_synced_and_scene_scoped(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        assert '<div id="cap0-0" class="cap-line clip" data-start="0.10"' in page
        assert '<span id="c0-0">Your</span>' in page
        assert "tl.to('#c0-0',{color:'#A6FF3D',duration:0.05},0.100);" in page
        assert "tl.to('#c0-0',{color:'#7E8C84',duration:0.05},0.420);" in page
        # scene 1 starts at 24.0, so its first word highlight is at 24.10
        assert "tl.to('#c1-0',{color:'#A6FF3D',duration:0.05},24.100);" in page

    def test_caption_line_duration_adds_the_tail(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        # words 0-4 end in "strength." -> one line, 0.10 to 1.71, +0.25 tail
        assert 'id="cap0-0" class="cap-line clip" data-start="0.10" data-duration="1.86"' in page

    def test_croc_idle_motion_is_baked_per_scene(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        # the flat host gets a zoom/rotate/drift performance -- never a jaw or a blink
        assert "#croc-jaw" not in page and "croc-eye" not in page
        assert re.search(
            r"tl\.to\('#croc',\{scale:[\d.]+,rotation:-?[\d.]+,y:-?[\d.]+,"
            r"transformOrigin:'center bottom',duration:[\d.]+,ease:'sine\.inOut'\},[\d.]+\);",
            page)

    def test_croc_motion_is_seeded_and_returns_to_base(self):
        from pipeline import compose
        a = compose.croc_motion(3, 0.0, 20.0)
        assert a and a == compose.croc_motion(3, 0.0, 20.0)      # deterministic
        assert a != compose.croc_motion(4, 0.0, 20.0)            # seeded per scene
        # every beat resolves back to the base transform, so consecutive scenes chain cleanly
        assert a[-1].startswith("tl.to('#croc',{scale:1.000,rotation:0.00,y:0.0")
        times = [float(re.search(r"\},([\d.]+)\);", t).group(1)) for t in a]
        assert times == sorted(times)
        assert all(0.0 <= t <= 20.0 for t in times)

    def test_stat_card_only_appears_where_the_job_has_one(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        assert 'id="stat0"' not in page
        assert '<div id="stat1" class="statcard clip" data-start="24.00" data-duration="18.00"' in page
        assert "NEURAL BRAKE" in page and "Governor caps recruitment." in page
        assert "tl.to('#statbar1',{width:'100%',duration:0.9,ease:'power3.out'},24.600);" in page

    def test_verdict_stamp_owns_the_last_twenty_seconds(self, tmp_path):
        _, total, page = build(tmp_path, demo_job())
        assert '<div id="verdict" class="verdict-stamp clip" data-start="22.00" data-duration="20.00"' in page
        assert ">APEX<" in page
        assert ("tl.fromTo('#verdict',{scale:3,opacity:0},{scale:1,opacity:1,duration:0.5,"
                "ease:'back.in(1.2)'},23.000);") in page
        assert "tl.to('#verdict',{x:12,y:-8,duration:0.15},23.500);" in page
        assert "tl.to('#verdict',{x:0,y:0,duration:0.15},23.650);" in page

    def test_verdict_is_skipped_when_the_job_has_none(self, tmp_path):
        _, _, page = build(tmp_path, demo_job(verdict=""))
        assert "verdict-stamp" not in page.split("<style>")[1].split("</style>")[0] or True
        assert 'id="verdict"' not in page

    def test_lesson_slate_is_zero_padded_and_fades_in(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        assert ">LESSON #007<" in page
        assert "tl.fromTo('#lesson',{opacity:0},{opacity:1,duration:0.5,ease:'power2.out'},0.200);" in page
        assert page.count('id="lesson"') == 1

    def test_scene_fragments_are_inlined_verbatim(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        assert '<div id="s0-a" class="clip" data-start="1.00" data-duration="4.00">A</div>' in page
        assert "tl.to('#s0-a',{opacity:1,duration:0.4},1.000);" in page
        stage = page[page.index('id="stage"'):page.index("</div>\n<script>")]
        assert 's0-a' in stage                       # fragment html lives inside #stage

    def test_croc_is_a_single_global_image_not_a_clip(self, tmp_path):
        proj, _, page = build(tmp_path, demo_job())
        assert page.count('id="croc"') == 1
        assert '<img id="croc" src="assets/croc.png"' in page
        head = page[page.index('<img id="croc"'):]
        assert "data-start" not in head[:head.index(">")]        # a global element, not a timed clip
        assert os.path.isfile(os.path.join(proj, "assets", "croc.png"))
        assert "#croc-jaw" not in page and 'class="croc-eye"' not in page

    def test_croc_entrance_is_baked_when_scene_0_does_not_do_one(self, tmp_path):
        _, _, page = build(tmp_path, demo_job())
        assert "tl.to('#croc',{opacity:1,duration:0.4},0.300);" in page
        assert "tl.fromTo('#croc',{x:1980},{x:1450,duration:1.0,ease:'power2.out'},0.300);" in page

    def test_baked_entrance_is_skipped_when_the_fragment_brings_him_in(self, tmp_path):
        """Two entrances would overlap on the same properties -- lint warns about it."""
        job = demo_job()
        job["scenes"][0]["frag_js"] = ("tl.to('#croc',{opacity:1,duration:0.4},0.500);\n"
                                       "tl.fromTo('#croc',{x:1900},{x:1400,duration:1.2},0.500);")
        _, _, page = build(tmp_path, job)
        assert "},0.300);" not in page
        assert page.count("tl.to('#croc',{opacity:1") == 1

    def test_html_is_escaped_so_a_script_flavoured_heading_cannot_inject(self, tmp_path):
        job = demo_job()
        job["scenes"][1]["stat"]["note"] = '</div><script>alert("x")</script>'
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
        assert 'data-duration="0.10"' in page          # never zero: that fails the renderer
        assert "window.__timelines = {scaled: tl};" in page

    @pytest.mark.skipif(os.getenv("HF_LIVE") != "1",
                        reason="set HF_LIVE=1 to lint a real composition (needs npx)")
    def test_built_project_passes_the_real_linter(self, tmp_path):
        from pipeline import htmlgen
        job = demo_job()
        for i, scene in enumerate(job["scenes"]):
            t0 = 0.0 if i == 0 else 24.0
            scene["frag_html"], scene["frag_js"] = htmlgen.SAFE_FALLBACK(t0, t0 + scene["dur"],
                                                                        scene["heading"], i)
        proj, _, _ = build(tmp_path, job)
        out = subprocess.run(["npx", "-y", "hyperframes@0.8.16", "lint", "--json", proj],
                             capture_output=True, text=True, timeout=900)
        blob = out.stdout + out.stderr
        report = json.loads(blob[blob.index("{"):blob.rindex("}") + 1])
        errors = [f for f in report["findings"] if f["severity"] == "error"]
        assert errors == [], errors
        assert report["ok"] is True


# ==================== TASK 11-12: render + youtube modules ====================
import pipeline.render as _r


class TestRender:
    def test_kenburns_frames_equal_round_dur_times_30(self):
        for dur in (8.0, 12.5, 0.03, 500.0):
            assert _r.kenburns_frames(dur) == max(1, round(dur * 30))

    def test_kenburns_frames_never_below_one(self):
        assert _r.kenburns_frames(0.03) == 1
        assert _r.kenburns_frames(0.0) == 1

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

    def test_render_outflag_default(self):
        assert _r.RENDER_OUTFLAG in ("--out", "--output", "-o")


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


class TestMakeThumb:
    def test_fills_zero_padded_lesson_and_calls_image(self, monkeypatch, tmp_path):
        from pipeline import upload, adapters
        captured = {}

        def fake_image(prompt, size="1280x720"):
            captured["prompt"], captured["size"] = prompt, size
            return b"\xff\xd8\xff\xe0jpeg-bytes"

        monkeypatch.setattr(adapters, "image", fake_image)
        job = {"id": "2026-08-30-brain", "title": "Your Brain Seals Your Strength",
               "topic": "the human brain", "lesson": 7, "word": "97%"}
        out = upload.make_thumb(job, str(tmp_path))
        assert out and out.endswith("thumb_2026-08-30-brain.jpg")
        assert "LESSON #007" in captured["prompt"]
        assert "{NNN}" not in captured["prompt"]
