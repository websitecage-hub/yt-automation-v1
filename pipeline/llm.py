"""LLM access for SCALED — two provider chains, hard-wired fallback order.

llm()      creative chain: NIM -> Groq -> Gemini   (temperature 0.85)
llm_code() code chain:     Gemini -> NIM -> Groq   (temperature 0.40)

Every provider speaks the OpenAI /chat/completions dialect, so one POST shape
covers all three. A provider whose API key env var is empty is skipped without
being counted as a failure; a provider that raises, returns non-200, or returns
unparseable JSON (when json_out=True) is logged and the next one is tried.
"""
import json
import os
import re

import requests

TIMEOUT = 300
CREATIVE_MAX_TOKENS = 8000
CODE_MAX_TOKENS = 12000

NIM_BASE = "https://integrate.api.nvidia.com/v1"
GROQ_BASE = "https://api.groq.com/openai/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

# (label, base, model, env var holding the key)
CREATIVE_CHAIN = [
    ("nim", NIM_BASE, "meta/llama-3.3-70b-instruct", "NIM_KEY"),
    ("groq", GROQ_BASE, "llama-3.3-70b-versatile", "GROQ_KEY"),
    ("gemini", GEMINI_BASE, "gemini-2.5-flash", "GEMINI_KEY"),
]
CODE_CHAIN = [
    ("gemini", GEMINI_BASE, "gemini-2.5-flash", "GEMINI_KEY"),
    ("nim", NIM_BASE, "qwen/qwen2.5-coder-32b-instruct", "NIM_KEY"),
    ("groq", GROQ_BASE, "llama-3.3-70b-versatile", "GROQ_KEY"),
]

# Filled at import: which provider keys are actually present in this process.
# run.py logs this so a run that silently lost a provider is obvious in the log.
PROVIDERS_HEALTH = {
    env: bool((os.getenv(env) or "").strip())
    for env in ("NIM_KEY", "GROQ_KEY", "GEMINI_KEY")
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


def _post(base, model, key, system, user, temperature, max_tokens, json_out):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_out:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(
        base + "/chat/completions",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        json=payload,
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError("HTTP %s: %s" % (r.status_code, r.text[:300]))
    content = r.json()["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("empty completion")
    return parse_json(content) if json_out else content.strip()


def _run_chain(chain, system, user, temperature, max_tokens, json_out, tag):
    tried = []
    for label, base, model, env in chain:
        key = (os.getenv(env) or "").strip()
        if not key:
            print("[llm] %s skipped: %s not set" % (model, env))
            continue
        tried.append(model)
        try:
            out = _post(base, model, key, system, user, temperature, max_tokens, json_out)
            print("[llm] %s ok via %s (%s)" % (tag, label, model))
            return out
        except Exception as e:
            print("[llm] %s failed: %s" % (model, e))
    raise RuntimeError("all LLM providers down")


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
