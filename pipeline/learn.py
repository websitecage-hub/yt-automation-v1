"""Learning loop for SCALED — read the scoreboard, rewrite the playbook.

run_learn() pulls per-video analytics for every published episode, hands the
STRATEGIST prompt a table it is told never to compare across ages, and writes
the returned playbook to data/strategy.json — the file the SCRIPT prompt then
obeys on the next run. It also feeds the winning/losing lanes straight into
topics.reweight(), and A/B-swaps the title of any video that is old enough to
trust but underperforming on click-through.

The age thresholds are the crux and are unit-tested at their boundaries, so
they live in three tiny pure helpers with the numbers spelled out:

  tier(age):  "fresh"  age < 3 days   -- too new to judge, leave alone
              "review" 3 <= age < 7   -- CTR is trustworthy, still worth fixing
              "locked" age >= 7 days  -- the die is cast, no more title changes

Everything network-bound is guarded and imported lazily, so this module (and
its pure helpers) import and run without google installed and without a
network. When has_yt() is False the analytics table is empty; a playbook can
still be produced from an empty table, and if even that fails the whole thing
no-ops and returns {"rows": 0}.
"""
import json
import os
from datetime import datetime, timezone

from pipeline.publish import read_job, videos_root, write_job, _all_jobs
from pipeline.upload import has_yt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STRATEGY_PATH = os.path.join(REPO, "data", "strategy.json")

# --- age tiers (days) -----------------------------------------------------
# fresh  < 3      : metrics are noise; never act on them.
# review 3 .. <7  : CTR has settled enough to trust; the only window we retitle.
# locked >= 7     : the video's fate is set; leave the title alone.
FRESH_DAYS = 3
LOCKED_DAYS = 7

# --- A/B eligibility ------------------------------------------------------
# Retitle only a review-tier video that both has real reach and is genuinely
# underperforming on click-through:
#   impressions >= 1000  : enough exposure that CTR means something.
#   ctr         <  0.04   : 4% click-through is the floor; below it, the
#                           packaging (not the topic) is the suspect.
AB_MIN_IMPRESSIONS = 1000
AB_MAX_CTR = 0.04


def _parse_iso(value):
    """A datetime from an ISO string (tolerating a trailing 'Z'), or None."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def age_days(published_at_iso, now):
    """Days between a published-at ISO time and `now`. Unknown time -> 0.0.

    Naive/aware mismatches are reconciled by comparing both as naive, so a
    caller's clock never has to match the stored timestamp's tz to get an age.
    """
    when = _parse_iso(published_at_iso)
    if when is None:
        return 0.0
    a, b = now, when
    if (a.tzinfo is None) != (b.tzinfo is None):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return (a - b).total_seconds() / 86400.0


def tier(age):
    """Age-in-days -> "fresh" (<3), "review" (3..<7), or "locked" (>=7)."""
    if age < FRESH_DAYS:
        return "fresh"
    if age < LOCKED_DAYS:
        return "review"
    return "locked"


def ab_eligible(row, now):
    """True only for a review-tier video with CTR < 0.04 AND impressions >= 1000.

    row is a dict with keys published_at, impressions, ctr.
    """
    if tier(age_days(row.get("published_at"), now)) != "review":
        return False
    try:
        ctr = float(row.get("ctr"))
        impressions = float(row.get("impressions"))
    except (TypeError, ValueError):
        return False
    return ctr < AB_MAX_CTR and impressions >= AB_MIN_IMPRESSIONS


# --- strategy file --------------------------------------------------------

def _read_strategy():
    try:
        with open(STRATEGY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_strategy(playbook):
    os.makedirs(os.path.dirname(STRATEGY_PATH), exist_ok=True)
    with open(STRATEGY_PATH, "w", encoding="utf-8") as fh:
        json.dump(playbook, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("[learn] wrote playbook -> %s" % STRATEGY_PATH)


# --- analytics ------------------------------------------------------------

def _video_id(job):
    return job.get("videoId") or job.get("video_id") or job.get("youtube_id")


def _published_pairs(videos_dir):
    """(job, path) for every published episode with a video id."""
    return [(j, p) for j, p in _all_jobs(videos_dir)
            if j.get("published") and _video_id(j)]


def _metric(headers, values, name):
    """Pull one named metric out of an Analytics reports.query response row."""
    try:
        return float(values[[h["name"] for h in headers].index(name)])
    except (ValueError, TypeError, IndexError):
        return 0.0


def _one_row(svc, job, path, video_id, now):
    """Analytics for one video as a strategist row. Missing metrics read 0."""
    published_at = job.get("published_at")
    row = {
        "id": job.get("id"),
        "video_id": video_id,
        "title": job.get("title") or "",
        "topic": job.get("topic") or "",
        "published_at": published_at,
        "age_days": round(age_days(published_at, now), 2),
        "views": 0.0,
        "impressions": 0.0,
        "ctr": 0.0,
        "avg_view_pct": 0.0,
        "_path": path,
    }
    when = _parse_iso(published_at)
    start_date = when.date().isoformat() if when else "2005-01-01"
    end_date = now.date().isoformat()

    # Core watch metrics and the impression/CTR pair are separate reports so a
    # channel that cannot serve impressions still yields views.
    try:
        core = svc.reports().query(
            ids="channel==MINE", startDate=start_date, endDate=end_date,
            metrics="views,averageViewPercentage", filters="video==%s" % video_id,
        ).execute()
        rows = core.get("rows") or []
        if rows:
            headers = core.get("columnHeaders") or []
            row["views"] = _metric(headers, rows[0], "views")
            row["avg_view_pct"] = _metric(headers, rows[0], "averageViewPercentage")
    except Exception as e:
        print("[learn] core analytics failed for %s: %s" % (video_id, e))

    try:
        imp = svc.reports().query(
            ids="channel==MINE", startDate=start_date, endDate=end_date,
            metrics="impressions,impressionClickThroughRate", filters="video==%s" % video_id,
        ).execute()
        rows = imp.get("rows") or []
        if rows:
            headers = imp.get("columnHeaders") or []
            row["impressions"] = _metric(headers, rows[0], "impressions")
            # the API reports CTR as a percentage; ab_eligible wants a fraction.
            row["ctr"] = _metric(headers, rows[0], "impressionClickThroughRate") / 100.0
    except Exception as e:
        print("[learn] impression analytics failed for %s: %s" % (video_id, e))

    return row


def _gather_rows(videos_dir, now):
    """Analytics rows for every published video, or [] when we cannot query."""
    if not has_yt():
        return []
    pairs = _published_pairs(videos_dir)
    if not pairs:
        return []
    try:
        from pipeline.upload import _service
        svc = _service("analytics")
    except Exception as e:
        print("[learn] analytics client unavailable: %s -- no rows" % e)
        return []
    rows = []
    for job, path in pairs:
        rows.append(_one_row(svc, job, path, _video_id(job), now))
    return rows


# --- strategist + A/B -----------------------------------------------------

def _rows_table(rows):
    """The {rows} block for the STRATEGIST prompt — age is on every line."""
    if not rows:
        return "(no published videos with analytics yet)"
    lines = []
    for r in rows:
        lines.append(
            '- "%s" [%s] age=%.1fd views=%d impressions=%d ctr=%.3f avg_view=%.1f%%'
            % (r.get("title", ""), r.get("topic", ""), r.get("age_days", 0.0),
               int(r.get("views", 0)), int(r.get("impressions", 0)),
               r.get("ctr", 0.0), r.get("avg_view_pct", 0.0))
        )
    return "\n".join(lines)


def _strategise(rows):
    """Ask the STRATEGIST for a playbook. None on failure or a non-object reply."""
    from pipeline import llm
    user = (
        llm.prompt("STRATEGIST", "user")
        .replace("{strategy}", json.dumps(_read_strategy(), ensure_ascii=False))
        .replace("{rows}", _rows_table(rows))
    )
    try:
        playbook = llm.llm(llm.prompt("STRATEGIST", "system"), user, json_out=True)
    except Exception as e:
        print("[learn] strategist call failed: %s" % e)
        return None
    if not isinstance(playbook, dict):
        print("[learn] strategist returned %s, not an object" % type(playbook).__name__)
        return None
    for key in ("title_patterns", "avoid", "winning_lanes", "losing_lanes"):
        if not isinstance(playbook.get(key), list):
            playbook[key] = []
    playbook.setdefault("notes", "")
    return playbook


def _propose_title(row, patterns):
    """A sharper replacement title via the TITLE_AB prompt. None on failure."""
    from pipeline import llm
    user = (
        llm.prompt("TITLE_AB", "user")
        .replace("{impressions}", str(int(row.get("impressions") or 0)))
        .replace("{ctr}", "%.3f" % float(row.get("ctr") or 0.0))
        .replace("{title}", str(row.get("title") or ""))
        .replace("{topic}", str(row.get("topic") or ""))
        .replace("{patterns}", patterns)
    )
    try:
        out = llm.llm(llm.prompt("TITLE_AB", "system"), user)
    except Exception as e:
        print("[learn] title A/B call failed: %s" % e)
        return None
    title = " ".join(str(out or "").split()).strip().strip('"').strip()
    return title[:100] or None


def _apply_title(svc, row, new_title):
    """videos.update the title in place, then keep the job file coherent."""
    video_id = row.get("video_id")
    try:
        current = svc.videos().list(part="snippet", id=video_id).execute()
        items = current.get("items") or []
        if not items:
            print("[learn] video %s not found for title swap" % video_id)
            return False
        snippet = items[0]["snippet"]
        snippet["title"] = new_title
        svc.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()
        print("[learn] A/B swapped %s title -> %r" % (video_id, new_title))
    except Exception as e:
        print("[learn] title update failed for %s: %s" % (video_id, e))
        return False
    path = row.get("_path")
    if path:
        try:
            job = read_job(path)
            job["title"] = new_title
            write_job(path, job)
        except (OSError, ValueError) as e:
            print("[learn] could not persist new title for %s: %s" % (video_id, e))
    return True


def _run_ab(rows, now, playbook):
    """Retitle every eligible video. Returns the number of swaps (or proposals)."""
    patterns = "\n".join("- %s" % p for p in (playbook.get("title_patterns") or [])) or "(none yet)"
    swaps = 0
    svc = None
    for row in rows:
        if not ab_eligible(row, now):
            continue
        new_title = _propose_title(row, patterns)
        if not new_title:
            continue
        if not has_yt():
            print("[learn] would A/B swap %s -> %r" % (row.get("video_id"), new_title))
            swaps += 1
            continue
        if svc is None:
            try:
                from pipeline.upload import _service
                svc = _service("youtube")
            except Exception as e:
                print("[learn] youtube client unavailable for A/B: %s" % e)
                return swaps
        if _apply_title(svc, row, new_title):
            swaps += 1
    return swaps


# --- the step -------------------------------------------------------------

def run_learn(videos_dir="data/videos"):
    """Gather analytics, rewrite the playbook, reweight topics, A/B titles.

    Returns {"rows": n, "ab_swaps": n, "playbook": ...}. When no playbook can be
    produced (the LLM is down and the table is empty), no-ops to {"rows": 0}.
    """
    now = datetime.now(timezone.utc)
    root = videos_root(videos_dir)
    rows = _gather_rows(root, now)

    playbook = _strategise(rows)
    if playbook is None:
        print("[learn] no playbook produced -- nothing learned (%d rows)" % len(rows))
        return {"rows": len(rows)}

    _write_strategy(playbook)
    try:
        from pipeline import topics
        topics.reweight(playbook.get("winning_lanes") or [], playbook.get("losing_lanes") or [])
    except Exception as e:
        print("[learn] topics.reweight failed: %s" % e)

    swaps = _run_ab(rows, now, playbook)
    print("[learn] %d rows, %d A/B title swaps" % (len(rows), swaps))
    return {"rows": len(rows), "ab_swaps": swaps, "playbook": playbook}


if __name__ == "__main__":
    run_learn()
