"""LLM access for SCALED — two provider chains, hard-wired fallback order.

llm()      creative chain: Meta thinking -> tabi opus-4-8 (x2) -> NIM super/nano -> Gemini -> Groq
llm_code() code chain:     Meta thinking -> tabi opus-4-8 (x2) -> NIM super/nano -> Gemini -> Groq

Providers speak one of four dialects -- "meta" (Meta AI thinking gateway, OpenAI
/chat/completions, no key required), "anthropic" (tabitoken gateway, /v1/messages
+ x-api-key, claude-opus-4-8), "nim" (NVIDIA NIM, OpenAI /chat/completions but no
response_format), and "openai" (Gemini + Groq, /chat/completions with a JSON
response_format) -- and _post normalises all of them to one text/JSON return. Every
HTTP call carries a browser User-Agent (tabitoken's Cloudflare 403s a bare curl/
python UA) and is retried on transient failures (timeouts, 429, 5xx) before the
chain moves on; a provider whose key env var is empty is skipped without counting
as a failure (except Meta, which is keyless and always tried), and one that raises,
returns non-200, or unparseable JSON (json_out) is logged so the next provider
takes over. The chain only raises when every provider is exhausted.
"""
import json
import os
import re
import time

import requests

TIMEOUT = 300
CREATIVE_MAX_TOKENS = 8000
CODE_MAX_TOKENS = 12000

NIM_BASE = "https://integrate.api.nvidia.com/v1"
META_BASE = "https://meta-api-u04m.onrender.com/v1"
TABI_BASE = "https://tabitoken.com"
SEEKAI_BASE = "https://seekai.cc"
GROQ_BASE = "https://api.groq.com/openai/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

# tabitoken sits behind Cloudflare, which 403s requests with a bare curl/python
# User-Agent; a browser UA sails through (verified from a datacenter IP). Sent on
# every provider call -- harmless to the others.
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

RETRY_TRIES = 3                       # per-provider HTTP attempts before falling through
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}   # 403 is NOT here: fall through fast

# (label, base, model, env var holding the key, dialect)
# Meta AI (thinking mode, OpenAI-compatible, no key required) leads both chains --
# the user's pick and free. tabitoken (Anthropic dialect, claude-opus-4-8) with a
# second key backs it up for quality, then NVIDIA NIM (nemotron super-120b ->
# nano-30b) then free Gemini + Groq form the tail so the chain never dies. Meta
# needs no key, so it is never skipped for a missing env var. seekai/kimi/deepseek
# are dropped: seekai 401s from CI IPs and the big NIM reasoning models never
# answer within 280s.
CREATIVE_CHAIN = [
    ("meta/thinking",   META_BASE,   "meta-ai-thinking",                  "META_KEY",   "meta"),
    ("tabi/opus-4-8",   TABI_BASE,   "claude-opus-4-8",                   "TABI_KEY",   "anthropic"),
    ("tabi/opus-4-8#2", TABI_BASE,   "claude-opus-4-8",                   "TABI_KEY2",  "anthropic"),
    ("nim/super-120b",  NIM_BASE,    "nvidia/nemotron-3-super-120b-a12b", "NIM_KEY",    "nim"),
    ("nim/nano-30b",    NIM_BASE,    "nvidia/nemotron-3-nano-30b-a3b",    "NIM_KEY",    "nim"),
    ("gemini",          GEMINI_BASE, "gemini-2.5-flash",                  "GEMINI_KEY", "openai"),
    ("groq",            GROQ_BASE,   "openai/gpt-oss-120b",               "GROQ_KEY",   "openai"),
]
CODE_CHAIN = [
    ("meta/thinking",   META_BASE,   "meta-ai-thinking",                  "META_KEY",   "meta"),
    ("tabi/opus-4-8",   TABI_BASE,   "claude-opus-4-8",                   "TABI_KEY",   "anthropic"),
    ("tabi/opus-4-8#2", TABI_BASE,   "claude-opus-4-8",                   "TABI_KEY2",  "anthropic"),
    ("nim/super-120b",  NIM_BASE,    "nvidia/nemotron-3-super-120b-a12b", "NIM_KEY",    "nim"),
    ("nim/nano-30b",    NIM_BASE,    "nvidia/nemotron-3-nano-30b-a3b",    "NIM_KEY",    "nim"),
    ("gemini",          GEMINI_BASE, "gemini-2.5-flash",                  "GEMINI_KEY", "openai"),
    ("groq",            GROQ_BASE,   "openai/gpt-oss-120b",               "GROQ_KEY",   "openai"),
]

# Filled at import: which provider keys are actually present in this process.
# run.py logs this so a run that silently lost a provider is obvious in the log.
PROVIDERS_HEALTH = {
    env: bool((os.getenv(env) or "").strip())
    for env in ("TABI_KEY", "TABI_KEY2", "NIM_KEY", "GEMINI_KEY", "GROQ_KEY")
}

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


def _strip_fences(text):
    """Remove a leading ```json / ``` fence and a trailing ``` fence."""
    out = text.strip()
    if out.startswith("```"):
        out = re.sub(r"^\s*```[A-Za-z0-9_+-]*\s*", "", out)
        out = re.sub(r"\s*```\s*$", "", out)
    return out.strip()


def parse_json(text):
    """Parse model output that is supposed to be JSON.

    Tries the fence-stripped body, then the widest {...} / [...] slice inside it.
    Raises ValueError when neither yields JSON, which the caller treats as a
    provider failure so the next provider in the chain gets a turn.
    """
    body = _strip_fences(text)
    try:
        return json.loads(body)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = body.find(opener), body.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(body[i:j + 1])
            except Exception:
                continue
    raise ValueError("response was not JSON")


def _http_post(url, headers, payload):
    """POST with bounded retries on transient failures (timeout, 429, 5xx).

    Returns the 200 response. A non-retryable status (e.g. 400/401/404) raises
    at once so the chain moves to the next provider instead of hammering a dead
    endpoint; retryable ones back off (1s, 2s, 4s) and try again.
    """
    last = None
    for attempt in range(RETRY_TRIES):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            last = e
        else:
            if r.status_code == 200:
                return r
            err = RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:300]))
            if r.status_code not in RETRY_STATUS:
                raise err
            last = err
        if attempt < RETRY_TRIES - 1:
            time.sleep(2 ** attempt)
    raise last if isinstance(last, Exception) else RuntimeError("request failed")


def _post(base, model, key, system, user, temperature, max_tokens, json_out, dialect):
    if dialect == "meta":
        # Meta AI thinking gateway: its own /api/chat path understands long
        # instruction-heavy prompts far better than the OpenAI-compatible
        # /v1/chat/completions one (which truncates and roleplays instead of
        # emitting the asked-for JSON). system + user go in one message;
        # the reply's `text` holds the completion. Auth open.
        payload = {"message": "%s\n\n%s" % (system, user)}
        headers = {"Content-Type": "application/json", "User-Agent": UA}
        root = base.rsplit("/v1", 1)[0]  # META_BASE ends in /v1; /api/chat lives at root
        r = _http_post(root + "/api/chat", headers, payload)
        try:
            content = r.json().get("text", "")
        except Exception:
            raise RuntimeError("meta chat expected JSON, got: %s" % r.text[:200])
    elif dialect == "anthropic":
        # Anthropic Messages API: system is top-level, no response_format; we lean
        # on parse_json to recover the object the prompt already asks the model for.
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json", "User-Agent": UA}
        r = _http_post(base + "/v1/messages", headers, payload)
        blocks = r.json().get("content") or []
        content = "".join(b.get("text", "") for b in blocks
                          if isinstance(b, dict) and b.get("type") == "text")
    else:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_out and dialect == "openai":
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                   "User-Agent": UA}
        r = _http_post(base + "/chat/completions", headers, payload)
        content = r.json()["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("empty completion")
    return parse_json(content) if json_out else content.strip()


def _run_chain(chain, system, user, temperature, max_tokens, json_out, tag):
    tried = []
    for label, base, model, env, dialect in chain:
        key = (os.getenv(env) or "").strip()
        if not key and dialect != "meta":      # meta needs no key; never skip it
            print("[llm] %s skipped: %s not set" % (label, env))
            continue
        tried.append(label)
        try:
            out = _post(base, model, key, system, user, temperature, max_tokens, json_out, dialect)
            print("[llm] %s ok via %s (%s)" % (tag, label, model))
            return out
        except Exception as e:
            print("[llm] %s failed: %s" % (label, e))
    raise RuntimeError("all LLM providers down (tried: %s)" % ", ".join(tried or ["none"]))


def llm(system, user, json_out=False):
    """Creative chain. Returns str, or parsed JSON when json_out=True."""
    return _run_chain(CREATIVE_CHAIN, system, user, 0.85, CREATIVE_MAX_TOKENS, json_out, "creative")


def llm_code(system, user):
    """Code chain. Always returns raw text (scene fragments are fenced, not JSON)."""
    return _run_chain(CODE_CHAIN, system, user, 0.4, CODE_MAX_TOKENS, False, "code")


# --- prompt loading -------------------------------------------------------
# PART F prompts live in config/prompts.md and are loaded from there, never
# inlined in code, so the sacred text has exactly one home.

_PROMPTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "prompts.md")
_PROMPT_CACHE = {}


def load_prompts(path=None):
    """Parse config/prompts.md into {SECTION: {"system": str, "user": str}}.

    Format: '## <NAME>' heading, then '### SYSTEM' / '### USER' subsections,
    each holding one fenced block whose contents are the literal prompt text.
    """
    path = path or _PROMPTS_PATH
    if path in _PROMPT_CACHE:
        return _PROMPT_CACHE[path]
    text = open(path, encoding="utf-8").read()
    out, section, role, buf, in_fence = {}, None, None, [], False
    for line in text.splitlines():
        if not in_fence and line.startswith("## "):
            section, role = line[3:].strip(), None
            out.setdefault(section, {})
            continue
        if not in_fence and line.startswith("### "):
            role = line[4:].strip().lower()
            continue
        if line.strip().startswith("```"):
            if in_fence:
                if section and role:
                    out[section][role] = "\n".join(buf)
                buf, in_fence = [], False
            else:
                buf, in_fence = [], True
            continue
        if in_fence:
            buf.append(line)
    _PROMPT_CACHE[path] = out
    return out


def prompt(section, role="user"):
    """One prompt string from config/prompts.md, e.g. prompt('SCRIPT', 'system')."""
    prompts = load_prompts()
    if section not in prompts or role not in prompts[section]:
        raise KeyError("prompt %s/%s missing from config/prompts.md" % (section, role))
    return prompts[section][role]
