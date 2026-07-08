"""
pipeline.py — turn a domain into the Report JSON the frontend renders.

Output shape is dictated by the frontend (buildMockReport / renderReport):
  {
    domain,
    communities: [{name, members, rel, relClass, quote, posts, mentions}],
    threads:     [{score, sClass, platClass, src, title, snippet, pills, openIdx}],
    stats:       {communityCount, monthlyPosts},
    demo: bool          # true when analysis ran without a live LLM
  }

Every number here is derived from real Reddit data (subscriber counts,
upvotes, comment counts, recency). We do not invent SEO metrics like
"ranks #1 on Google" — those need a paid SEO API we don't assume you have.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from selectolax.parser import HTMLParser

import llm
import reddit

log = logging.getLogger("flintel.pipeline")


# ── site fetch ────────────────────────────────────────────────────────
def fetch_site_text(domain: str) -> str:
    """Grab homepage (and /about if quick) and return visible text."""
    text_parts: list[str] = []
    for path in ("", "/about"):
        for scheme in ("https://", "http://"):
            try:
                r = httpx.get(scheme + domain + path, timeout=12.0,
                              follow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 (FlintelBot)"})
                if r.status_code < 400 and r.text:
                    tree = HTMLParser(r.text)
                    for tag in tree.css("script, style, noscript, svg"):
                        tag.decompose()
                    body = tree.body
                    if body:
                        text_parts.append(body.text(separator=" ", strip=True))
                    break
            except Exception:  # noqa: BLE001 — try next scheme/path
                continue
    return " ".join(text_parts)[:12000]


# ── formatting helpers ────────────────────────────────────────────────
def _fmt_members(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _relative_time(created_utc: float) -> str:
    if not created_utc:
        return "recently"
    days = max(0, (time.time() - created_utc) / 86400)
    if days < 1:
        return "today"
    if days < 14:
        return f"{int(days)} days ago"
    if days < 60:
        return f"{int(days/7)} weeks ago"
    if days < 365:
        return f"{int(days/30)} months ago"
    return f"{int(days/365)} years ago"


def _score_tier(score: int) -> str:
    return "ts-hot" if score >= 85 else "ts-warm" if score >= 75 else "ts-mid"


def _rel_tier(score: int) -> tuple[str, str]:
    if score >= 70:
        return "HIGH", "rel-high"
    if score >= 45:
        return "MED", "rel-med"
    return "LOW", "rel-low"


# ── main entry ────────────────────────────────────────────────────────
def run_scan(domain: str) -> dict[str, Any]:
    """Blocking pipeline. Called in a worker thread from the API."""
    t0 = time.time()
    log.info("scan start | %s", domain)

    site_text = fetch_site_text(domain)
    profile = llm.profile_company(domain, site_text)
    competitors = [c.lower() for c in profile.get("competitors", [])]

    # 1) discover candidate subreddits (queries run concurrently)
    sub_queries = profile.get("subreddit_queries", [])[:6]
    sub_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(lambda q: reddit.search_subreddits(q, limit=6), sub_queries):
            for s in res:
                if s["name"] not in sub_map and s["subscribers"] > 1000:
                    sub_map[s["name"]] = s
    candidates = sorted(sub_map.values(), key=lambda s: -s["subscribers"])[:10]

    # 2) pull buyer-intent threads (global + per top-subreddit), all in parallel
    thread_queries = profile.get("thread_queries", [])[:8]
    jobs: list[tuple[str, str | None]] = [(q, None) for q in thread_queries]
    for s in candidates[:5]:
        sub = s["name"].replace("r/", "")
        for q in thread_queries[:3]:
            jobs.append((q, sub))

    threads: list[dict] = []
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = pool.map(
            lambda job: reddit.search_posts(job[0], subreddit=job[1],
                                            limit=8 if job[1] is None else 5),
            jobs,
        )
        for res in results:
            for t in res:
                if t["permalink"] not in seen and t["title"]:
                    seen.add(t["permalink"])
                    threads.append(t)

    # 3) score threads, keep the strongest
    scores = llm.score_threads(profile, threads)
    for t, sc in zip(threads, scores):
        t["_score"] = sc
    threads.sort(key=lambda t: -t["_score"])
    top_threads = threads[:8]

    # 4) shape communities — attach the best matching quote + real counts
    def best_quote_for(sub_name: str) -> str:
        for t in top_threads:
            if t["subreddit"].lower() == sub_name.lower():
                return f'"{t["title"]}"'
        # else any thread we found in that sub
        for t in threads:
            if t["subreddit"].lower() == sub_name.lower():
                return f'"{t["title"]}"'
        return f'"{sub_name} — active buyer discussion"'

    def mentions_in(sub_name: str) -> int:
        c = 0
        for t in threads:
            if t["subreddit"].lower() != sub_name.lower():
                continue
            blob = (t["title"] + " " + t["selftext"]).lower()
            if any(comp and comp in blob for comp in competitors):
                c += 1
        return c

    def posts_in(sub_name: str) -> int:
        return sum(1 for t in threads if t["subreddit"].lower() == sub_name.lower())

    communities = []
    for s in candidates[:6]:
        # relevance = does this sub actually contain strong threads?
        sub_scores = [t["_score"] for t in threads
                      if t["subreddit"].lower() == s["name"].lower()]
        rel_score = max(sub_scores) if sub_scores else 50
        rel, rel_class = _rel_tier(rel_score)
        matched = posts_in(s["name"])
        communities.append({
            "name": s["name"],
            "members": _fmt_members(s["subscribers"]) + " ",
            "rel": rel,
            "relClass": rel_class,
            "quote": best_quote_for(s["name"]),
            "posts": (f"{matched} buyer posts found" if matched
                      else (s["description"][:60] or "active community")),
            "mentions": f"{mentions_in(s['name'])} mention competitors",
        })

    # 5) shape threads for the report cards (real Reddit pills only)
    report_threads = []
    for t in top_threads:
        score = int(t["_score"])
        pills = [
            f"{t['ups']:,} upvotes",
            f"{t['num_comments']:,} comments",
            "Thread locked" if t["locked"] else "Thread open",
        ]
        report_threads.append({
            "score": score,
            "sClass": _score_tier(score),
            "platClass": "pd-r",
            "src": f"{t['subreddit']} · {_relative_time(t['created_utc'])}",
            "title": f'"{t["title"]}"',
            "snippet": t["selftext"] or "(link post — open thread for context)",
            "pills": pills,
            "openIdx": 2,          # index of the open/locked pill
            "url": t["permalink"],  # extra field; frontend ignores, dashboard uses
        })

    monthly = sum(posts_in(c["name"]) for c in communities)
    report = {
        "domain": domain,
        "profile": {k: profile.get(k) for k in ("summary", "buyers", "competitors")},
        "communities": communities,
        "threads": report_threads,
        "stats": {
            "communityCount": len(communities),
            "monthlyPosts": monthly if monthly else len(threads),
        },
        "demo": not llm.enabled(),
    }
    log.info("scan done | %s | %d subs, %d threads | %.1fs | demo=%s",
             domain, len(communities), len(report_threads), time.time() - t0,
             report["demo"])
    return report
