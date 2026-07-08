"""
reddit.py — read-only Reddit client.

Uses Reddit's public JSON endpoints (no login needed for reads). Reddit
*does* block generic/empty User-Agents, so we send a descriptive one.
For low volume this is fine; at scale add OAuth app credentials
(REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET) and Reddit lifts the rate cap.

Nothing in here posts, votes, or writes. Placement/posting is deliberately
a separate, human-approved step (see README → "Posting & Reddit policy").
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("flintel.reddit")

UA = os.getenv(
    "REDDIT_USER_AGENT",
    "web:flintel-market-scan:v1.0 (buyer-intent research; contact hello@getflintel.com)",
)
_BASE = "https://www.reddit.com"
_TIMEOUT = httpx.Timeout(15.0)


def _get(path: str, params: dict[str, Any]) -> dict | None:
    """GET a Reddit .json endpoint with two retries and polite backoff."""
    url = _BASE + path
    for attempt in range(3):
        try:
            r = httpx.get(url, params=params, headers={"User-Agent": UA},
                          timeout=_TIMEOUT, follow_redirects=True)
            if r.status_code == 429:
                wait = 2 * (attempt + 1)
                log.warning("reddit 429 on %s — backing off %ss", path, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 — network is best-effort here
            log.warning("reddit GET %s failed (attempt %d): %s", path, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    return None


def search_subreddits(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Find subreddits by topic. Returns name, subscribers, description."""
    data = _get("/subreddits/search.json",
                {"q": query, "limit": limit, "include_over_18": "false"})
    if not data:
        return []
    out = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("over18") or d.get("subreddit_type") == "private":
            continue
        out.append({
            "name": "r/" + d.get("display_name", ""),
            "subscribers": d.get("subscribers") or 0,
            "description": (d.get("public_description") or "").strip(),
        })
    return out


def search_posts(query: str, subreddit: str | None = None, limit: int = 15,
                 timeframe: str = "year") -> list[dict[str, Any]]:
    """
    Search posts by relevance. If `subreddit` is given (e.g. "digitalnomad",
    no r/ prefix), the search is restricted to it.
    """
    if subreddit:
        sub = subreddit.replace("r/", "").strip("/")
        path = f"/r/{sub}/search.json"
        params = {"q": query, "restrict_sr": 1, "sort": "relevance",
                  "t": timeframe, "limit": limit}
    else:
        path = "/search.json"
        params = {"q": query, "sort": "relevance", "t": timeframe, "limit": limit,
                  "type": "link"}
    data = _get(path, params)
    if not data:
        return []
    out = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("over_18"):
            continue
        out.append({
            "title": d.get("title", ""),
            "selftext": (d.get("selftext") or "")[:600],
            "subreddit": "r/" + d.get("subreddit", ""),
            "ups": d.get("ups") or 0,
            "num_comments": d.get("num_comments") or 0,
            "created_utc": d.get("created_utc") or 0,
            "permalink": "https://www.reddit.com" + d.get("permalink", ""),
            "locked": bool(d.get("locked")),
        })
    return out
