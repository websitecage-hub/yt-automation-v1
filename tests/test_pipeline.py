"""SCALED — unit tests. Zero network, zero real keys.

Every test that would otherwise reach an API monkeypatches requests.post /
requests.get, so `pytest tests/ -q` is safe to run anywhere, including CI.
"""
import json
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
        assert not any(t["used"] for t in seed)
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
