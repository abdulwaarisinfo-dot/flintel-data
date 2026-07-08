# Flintel — backend + live frontend

This is the complete backend for the `Flintel_Production_v23.html` frontend,
plus that frontend wired to talk to it (mock mode off). One process serves
the app and the API, so deploying is: run it, point a domain at it.

## Run it locally (60 seconds)

```bash
cd flintel-app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # optional: add ANTHROPIC_API_KEY for real AI analysis
python server.py            # → http://localhost:8000
```

Open http://localhost:8000, type a domain, watch a real scan run.

## What actually happens on a scan

`POST /api/v1/scans` kicks off `pipeline.run_scan(domain)` in a worker thread:

1. **Fetch the site** — homepage + `/about`, strip to visible text.
2. **Profile the company** (`llm.profile_company`) — what they sell, who buys,
   competitors, and which subreddits/queries to search.
3. **Find communities** — real subreddits via Reddit's public search, filtered
   to >1k members.
4. **Pull buyer-intent threads** — real posts, deduped, global + per-subreddit.
5. **Score** each thread 0–100 (`llm.score_threads`), keep the strongest.
6. **Shape** the report to the exact JSON the frontend renders.

`GET /api/v1/scans/{id}/report` long-polls until the worker finishes (up to
`REPORT_WAIT_S`, default 45s). The frontend's scan animation already burns
~10s, so most reports are ready by the time it asks.

## Honest status — real vs. needs-a-key

| Piece | Without keys | With `ANTHROPIC_API_KEY` |
|---|---|---|
| Subreddits, members, thread titles, upvotes, comments, recency | **Real** (live Reddit) | **Real** |
| Company summary, buyer profile, competitor list | crude heuristic from site text | **Real** (Claude) |
| Thread relevance scores | engagement heuristic | **Real** (Claude) |
| Comment drafts | placeholder | **Real** (Claude) |
| Report flag | `demo: true` | `demo: false` |

**One deliberate omission:** the original mock showed pills like *"Ranks #1 on
Google · 8,400 searches/mo."* Those need a paid SEO API (Ahrefs/SEMrush) that
this build doesn't assume you have — so I did **not** fake them. Thread pills
show real Reddit numbers instead (upvotes, comments, open/locked). If you want
the SEO pills back, wire an SEO provider in `pipeline.py` and add them there.

## Endpoints (all implemented, all tested)

```
POST /api/v1/scans                 {url}                    -> 202 {scanId}
GET  /api/v1/scans/{id}/report                              -> Report JSON
POST /api/v1/accounts   {name,email,password,company,plan}  -> 201 {accountId,token}
POST /api/v1/login      {email,password}                    -> 200 {accountId,token}
POST /api/v1/onboarding {profile,audience,subreddits,...}   -> 204   (Bearer)
GET  /api/v1/dashboard                                      -> Dashboard JSON (Bearer)
POST /api/v1/placements/{id}/approve                        -> 204   (Bearer)
GET  /api/v1/health                                         -> {ok, llm, model}
```

Auth: bcrypt-hashed passwords, opaque bearer tokens in SQLite. The frontend
stores the token in memory only, exactly as its contract specifies.

## Deploy to live

**Any container host (Render, Railway, Fly, Cloud Run, a plain VPS):**

```bash
docker build -t flintel .
docker run -p 8000:8000 --env-file .env flintel
```

Then put it behind a reverse proxy with TLS (Caddy/Nginx) and point
`getflintel.com` at it. On Render/Railway just set the env vars in their
dashboard and deploy the repo — the `Dockerfile` is picked up automatically.

**Persistence:** SQLite writes to `flintel.db`. On ephemeral hosts, mount a
volume for it (or set `FLINTEL_DB` to a mounted path) so accounts survive
restarts. When you outgrow one node, swap the helpers in `db.py` for Postgres —
nothing above that layer changes.

## ⚠️ Posting to Reddit — read before you automate this

The frontend's promise is "places your product inside threads." The safe,
compliant version of that — and the one this backend is built for — is
**human-approved**: Flintel drafts a genuinely-helpful, disclosed comment, a
person reviews it in the dashboard, and posts (or you post on their behalf with
disclosure). That's why `approve_placement` sets state and schedules, rather
than auto-posting.

Do **not** bolt on automated mass-posting of promotional comments from throwaway
accounts. Reddit's rules prohibit undisclosed promotion and vote manipulation;
it gets accounts and domains banned and would torch a client like Settla's
brand. Keep a human in the loop and disclose affiliation — it's both the
compliant path and the one that actually converts. `llm.draft_comment` is
prompted to help honestly, not to astroturf.

## Files

```
server.py         FastAPI app: serves frontend + all endpoints
pipeline.py       scan orchestration + report shaping
reddit.py         read-only Reddit client (public JSON)
llm.py            Claude wrapper (profiling / scoring / drafts) + fallbacks
db.py             SQLite persistence
static/index.html your frontend, MOCK off, forms wired to the API
```

## Note on the always-on monitor

Your separate `flintel_v7.x.py` daemon (Twitter/Reddit/Telegram → Slack/HubSpot)
is complementary: it's the *continuous* signal feed, whereas this app is the
*on-demand scan + client dashboard*. When you're ready, have the daemon write
qualified signals into this app's DB (or a shared Postgres) and the dashboard's
placement queue becomes live instead of demo. Kept separate here on purpose so
neither one's failure takes down the other.
