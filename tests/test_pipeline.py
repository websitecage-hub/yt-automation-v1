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
