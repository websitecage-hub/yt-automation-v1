"""Topic bank for SCALED.

data/topics.json IS the bank -- there is no other store. Every function reads it
from disk and writes it straight back, so a crashed run never loses a decision:
the next run sees exactly the state the last completed write left behind.

Entry shape (PART C / TASK 7):  {"topic","lane","why","score":1-10,"used":bool}
"""

import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, "data", "topics.json")

LANES = ("HUMAN", "WEIRD_ANIMAL", "VERSUS", "PROFESSOR")
MIN_UNUSED = 15      # below this, refill() tops the bank up
REFILL_BATCH = 10    # the TOPICS prompt asks for exactly 10


# ---------------------------------------------------------------- store

def load(path=None):
    """Return the bank as a list. A missing or corrupt file is an empty bank."""
    p = path or PATH
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        print("[topics] %s missing -- starting from an empty bank" % p)
        return []
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print("[topics] %s unreadable (%s) -- starting from an empty bank" % (p, e))
        return []
    if not isinstance(data, list):
        print("[topics] %s is not a JSON list -- starting from an empty bank" % p)
        return []
    return [_clean(t) for t in data if isinstance(t, dict) and str(t.get("topic", "")).strip()]


def save(topics, path=None):
    """Write the bank back. Same formatting every time so diffs stay readable."""
    p = path or PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(topics, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return p


def _clamp(n):
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        n = 5
    return max(1, min(10, n))


def _lane(value):
    lane = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
    if lane in LANES:
        return lane
    print("[topics] unknown lane %r -- filed under HUMAN" % value)
    return "HUMAN"


def _clean(t):
    return {
        "topic": str(t.get("topic", "")).strip(),
        "lane": _lane(t.get("lane")),
        "why": str(t.get("why", "")).strip(),
        "score": _clamp(t.get("score", 5)),
        "used": bool(t.get("used", False)),
    }


def _key(topic):
    """Dedupe key: case- and punctuation-insensitive."""
    return "".join(ch for ch in str(topic).lower() if ch.isalnum())


# ---------------------------------------------------------------- reads

def unused(topics=None, path=None):
    bank = load(path) if topics is None else topics
    return [t for t in bank if not t["used"]]


def top_unused(n, path=None, topics=None):
    """The n highest-scoring unused topics, best first. Ties keep bank order."""
    ranked = sorted(unused(topics, path), key=lambda t: -t["score"])
    return ranked[: max(0, int(n))]


# ---------------------------------------------------------------- writes

def pick(path=None):
    """Take the highest-scoring unused topic, mark it used, save, return it.

    Returns None when the bank is exhausted -- the caller decides whether that
    is a refill or a hard stop.
    """
    bank = load(path)
    best = None
    for t in bank:
        if t["used"]:
            continue
        if best is None or t["score"] > best["score"]:
            best = t
    if best is None:
        print("[topics] no unused topics left in the bank")
        return None
    best["used"] = True
    save(bank, path)
    print("[topics] picked %s [%s] score=%d" % (best["topic"], best["lane"], best["score"]))
    return dict(best)


def reweight(winning_lanes, losing_lanes, path=None):
    """+1 to every topic in a winning lane, -1 in a losing one, clamped 1-10.

    A lane in both lists nets zero, which is the honest answer when the
    analytics disagree with themselves.
    """
    win = {_lane(l) for l in (winning_lanes or [])}
    lose = {_lane(l) for l in (losing_lanes or [])}
    bank = load(path)
    moved = 0
    for t in bank:
        delta = (1 if t["lane"] in win else 0) - (1 if t["lane"] in lose else 0)
        if not delta:
            continue
        before = t["score"]
        t["score"] = _clamp(before + delta)
        if t["score"] != before:
            moved += 1
    save(bank, path)
    print("[topics] reweight win=%s lose=%s -> %d scores moved" % (sorted(win), sorted(lose), moved))
    return bank


def refill(llm_fn, path=None):
    """Top the bank up when fewer than MIN_UNUSED topics remain.

    llm_fn(system, user) may return the parsed JSON already or the raw text --
    both are accepted so the caller can pass llm(..., json_out=True) directly.
    Returns the number of topics appended (0 when no refill was needed).
    """
    bank = load(path)
    left = len([t for t in bank if not t["used"]])
    if left >= MIN_UNUSED:
        print("[topics] %d unused topics -- no refill needed" % left)
        return 0

    from pipeline.llm import parse_json, prompt

    existing = "\n".join("- %s" % t["topic"] for t in bank) or "- (bank is empty)"
    system = prompt("TOPICS", "system")
    user = prompt("TOPICS", "user").replace("{existing}", existing)

    try:
        raw = llm_fn(system, user)
    except Exception as e:
        print("[topics] refill failed: %s -- bank left untouched" % e)
        return 0

    items = raw
    if isinstance(items, str):
        try:
            items = parse_json(items)
        except ValueError as e:
            print("[topics] refill returned unparseable JSON: %s" % e)
            return 0
    if isinstance(items, dict):
        for k in ("topics", "items", "data", "results"):
            if isinstance(items.get(k), list):
                items = items[k]
                break
    if not isinstance(items, list):
        print("[topics] refill returned %s, expected a list" % type(items).__name__)
        return 0

    seen = {_key(t["topic"]) for t in bank}
    added = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = _clean(item)
        entry["used"] = False
        if not entry["topic"] or _key(entry["topic"]) in seen:
            continue
        seen.add(_key(entry["topic"]))
        bank.append(entry)
        added += 1

    if added:
        save(bank, path)
    print("[topics] refill added %d topics (%d unused now)" % (added, left + added))
    return added
