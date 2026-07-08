"""
llm.py — the analysis brain (Claude), with honest fallbacks.

Three jobs:
  1. profile_company()  — read a homepage, say what the company sells,
                          who its buyers are, who it competes with, and
                          which subreddits/queries to search.
  2. score_threads()    — rate how strong a buyer-intent signal each thread is.
  3. draft_comment()    — write a genuinely-helpful, disclosed comment draft
                          for a thread (a human approves before anything posts).

If ANTHROPIC_API_KEY is unset OR a call fails, we DON'T fabricate data — we
fall back to transparent heuristics and mark the report demo=True so you
always know whether a report is real. Set the key to get real analysis.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger("flintel.llm")

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
_KEY = os.getenv("ANTHROPIC_API_KEY")

_client = None
if _KEY:
    try:
        import anthropic
        _client = anthropic.Anthropic(api_key=_KEY)
        log.info("Claude enabled | model=%s", MODEL)
    except Exception as e:  # noqa: BLE001
        log.warning("anthropic SDK init failed (%s) — using heuristic fallback", e)
else:
    log.warning("ANTHROPIC_API_KEY not set — using heuristic fallback (reports marked demo=true)")


def enabled() -> bool:
    return _client is not None


def _complete(system: str, user: str, max_tokens: int = 1500) -> str:
    """Single-shot Claude call returning concatenated text. Raises on failure."""
    msg = _client.messages.create(  # type: ignore[union-attr]
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise ValueError("no JSON in response")
    depth, opener = 0, text[start]
    closer = "}" if opener == "{" else "]"
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in response")


# ── 1. company profiling ──────────────────────────────────────────────
_PROFILE_SYS = (
    "You are a B2B go-to-market analyst. Given a company's website text, you "
    "identify what they sell, who buys it, who they compete with, and where "
    "their buyers discuss relevant problems on Reddit. Be concrete and specific. "
    "Respond with ONLY a JSON object, no prose."
)


def profile_company(domain: str, site_text: str) -> dict[str, Any]:
    """Returns {summary, buyers, pains[], competitors[], subreddit_queries[], thread_queries[]}."""
    if not _client:
        return _heuristic_profile(domain, site_text)
    user = (
        f"Company domain: {domain}\n\nWebsite text (truncated):\n{site_text[:6000]}\n\n"
        "Return JSON with exactly these keys:\n"
        '{\n'
        '  "summary": "one sentence: what this company sells",\n'
        '  "buyers": "one sentence: who the ideal buyer is",\n'
        '  "pains": ["3-6 short buyer pain phrases in the buyer\'s own words"],\n'
        '  "competitors": ["3-8 competitor or incumbent product names"],\n'
        '  "subreddit_queries": ["4-6 topic phrases to find relevant subreddits"],\n'
        '  "thread_queries": ["6-10 search phrases a frustrated buyer would post"]\n'
        '}'
    )
    try:
        return _extract_json(_complete(_PROFILE_SYS, user))
    except Exception as e:  # noqa: BLE001
        log.warning("profile_company LLM failed (%s) — heuristic fallback", e)
        return _heuristic_profile(domain, site_text)


def _heuristic_profile(domain: str, site_text: str) -> dict[str, Any]:
    """No-LLM fallback: derive crude but *real* signals from the site's own words."""
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{4,}", site_text.lower())
    stop = {"about", "these", "their", "would", "there", "which", "other",
            "using", "based", "learn", "https", "email", "please", "policy",
            "terms", "privacy", "cookie", "contact"}
    freq: dict[str, int] = {}
    for w in words:
        if w not in stop:
            freq[w] = freq.get(w, 0) + 1
    top = [w for w, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:6]]
    name = domain.split(".")[0]
    return {
        "summary": f"{name} (profile derived without LLM — set ANTHROPIC_API_KEY for real analysis)",
        "buyers": "unknown (heuristic mode)",
        "pains": [f"looking for a {name} alternative", f"{top[0] if top else name} problems"],
        "competitors": [],
        "subreddit_queries": (top[:4] or [name]),
        "thread_queries": [f"{name} alternative", f"best {top[0] if top else name} tool",
                           f"switching from {name}", f"{top[1] if len(top) > 1 else name} recommendation"],
        "_heuristic": True,
    }


# ── 2. thread scoring ─────────────────────────────────────────────────
_SCORE_SYS = (
    "You score how strong a BUYER-INTENT signal each Reddit thread is for a "
    "specific company — i.e. how likely the poster (or readers) are an in-market "
    "buyer the company could genuinely help. 90-100 = explicit purchase intent or "
    "active switching; 75-89 = strong pain that maps to the product; 60-74 = "
    "relevant discussion; below 60 = weak. Respond with ONLY a JSON array."
)


def score_threads(profile: dict[str, Any], threads: list[dict[str, Any]]) -> list[int]:
    """Returns a parallel list of 0-100 scores. Falls back to an engagement heuristic."""
    if not _client or not threads:
        return [_heuristic_score(t) for t in threads]
    listing = "\n".join(
        f'{i}. [{t["subreddit"]}] {t["title"]} — {t["selftext"][:160]}'
        for i, t in enumerate(threads)
    )
    user = (
        f"Company: {profile.get('summary', '')}\n"
        f"Ideal buyer: {profile.get('buyers', '')}\n"
        f"Competitors: {', '.join(profile.get('competitors', []))}\n\n"
        f"Threads:\n{listing}\n\n"
        f'Return a JSON array of {len(threads)} objects: '
        '[{"i": 0, "score": 87}, ...] — one per thread, in order.'
    )
    try:
        arr = _extract_json(_complete(_SCORE_SYS, user, max_tokens=1000))
        by_i = {int(o["i"]): max(0, min(100, int(o["score"]))) for o in arr}
        return [by_i.get(i, _heuristic_score(threads[i])) for i in range(len(threads))]
    except Exception as e:  # noqa: BLE001
        log.warning("score_threads LLM failed (%s) — heuristic fallback", e)
        return [_heuristic_score(t) for t in threads]


def _heuristic_score(t: dict[str, Any]) -> int:
    """Engagement-weighted score when the LLM is unavailable. Real, if crude."""
    ups, comments = t.get("ups", 0), t.get("num_comments", 0)
    base = 55 + min(25, ups // 40) + min(15, comments // 15)
    text = (t.get("title", "") + " " + t.get("selftext", "")).lower()
    for kw in ("alternative", "switching", "recommend", "best", "vs ", "instead of"):
        if kw in text:
            base += 3
    return max(40, min(96, base))


# ── 3. comment drafting ───────────────────────────────────────────────
_DRAFT_SYS = (
    "You draft a Reddit comment that is genuinely useful to the thread FIRST and "
    "mentions the product only if it authentically fits. It must comply with "
    "Reddit rules: transparent, non-spammy, adds real value, discloses affiliation "
    "when recommending the product. Never fabricate features or claims. Honor the "
    "client's guardrails exactly. Output only the comment text."
)


def draft_comment(profile: dict[str, Any], thread: dict[str, Any],
                  guardrails: str = "") -> str:
    if not _client:
        return ("[Draft unavailable in heuristic mode — set ANTHROPIC_API_KEY.] "
                "This thread looks relevant; write a helpful, disclosed reply that "
                "answers the poster's actual question before mentioning the product.")
    user = (
        f"Product: {profile.get('summary', '')}\n"
        f"Guardrails (MUST follow): {guardrails or 'none specified'}\n\n"
        f"Thread: {thread.get('title', '')}\n{thread.get('selftext', '')[:500]}\n\n"
        "Write a helpful comment (max ~120 words). Lead with real help. If you "
        "mention the product, disclose the affiliation in-line."
    )
    try:
        return _complete(_DRAFT_SYS, user, max_tokens=400).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("draft_comment LLM failed (%s)", e)
        return "[Draft generation failed — please write manually.]"
