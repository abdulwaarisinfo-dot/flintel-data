# Flintel — handoff / status

Read this first. It says what this codebase **is**, what's **done**, and what's
**left**, with exact file + function pointers for each remaining task.

---

## What this is

A complete backend for the existing Flintel frontend, plus that frontend wired
to it. One Python process (`server.py`, FastAPI) serves both the web page and
the API. Storage is SQLite (one file, no external DB to set up). The scan
actually fetches the target website, works out what the company does, searches
real Reddit for relevant communities + buyer-intent threads, scores them, and
returns the report JSON the frontend renders.

```
server.py          FastAPI: serves frontend + all API endpoints + auth
pipeline.py        scan orchestration (site → profile → reddit → score → report)
reddit.py          read-only Reddit client (public JSON, parallelized)
llm.py             Claude wrapper: profiling / scoring / comment drafts (+ fallbacks)
db.py              SQLite persistence (thread-safe; the swap point for Postgres later)
static/index.html  the frontend — MOCK off, signup + onboarding forms wired to API
Dockerfile         one-command container deploy
.env.example       config template
README.md          run + deploy instructions
```

Run locally: `pip install -r requirements.txt` then `python server.py` →
http://localhost:8000

---

## DONE and tested ✅

- All 6 contract endpoints (`/scans`, `/scans/:id/report`, `/accounts`,
  `/onboarding`, `/dashboard`, `/placements/:id/approve`) **+ `/login`**.
- Real scan pipeline. Reddit calls run **concurrently** (~10–15s, not 40–90s).
- Report JSON matches the frontend field-for-field (verified in tests).
- Auth: bcrypt-hashed passwords + opaque bearer tokens.
- Frontend: `MOCK=false`; signup form (name/email/password/company) and
  onboarding fields (audience/subreddits/guardrails) now POST real values with
  validation. Previously these inputs weren't read at all.
- Thread-safe DB (survives a 150-concurrent-scan stress test, 0 errors).
- Edge cases handled: long passwords, malformed auth headers, empty Reddit
  results, bad domains — all return clean codes, no 500s.
- `Dockerfile` + `/api/v1/health` (tells you if the AI layer is live).

---

## LEFT to do — in priority order

### 1. Set the Anthropic key + confirm the model  ← required for real AI output
Without `ANTHROPIC_API_KEY`, Reddit data is still real, but company summaries
and thread scores drop to heuristics and every report is stamped `demo: true`.
- Set `ANTHROPIC_API_KEY` in `.env`.
- Confirm `ANTHROPIC_MODEL` — I defaulted to `claude-sonnet-4-5-20250929`;
  verify the current string at docs.claude.com/en/api. A wrong value won't
  crash anything (it falls back to heuristics), it just won't use Claude.
- After deploy, hit `/api/v1/health`. If `"llm": false`, the key isn't loading.

### 2. First real Reddit call  ← the one thing untested
The build was validated with stubbed Reddit data (the build env had no Reddit
access). On the real box, run one scan and watch the log:
`scan done | <domain> | N subs, N threads`. If **N is 0**, Reddit is blocking
the request — almost always the `User-Agent` (`REDDIT_USER_AGENT` in `.env`).
Everything downstream of the fetch is already proven.

### 3. Make the dashboard real  ← biggest functional gap
Right now the dashboard **screen is static HTML**. Confirmed:
- `static/index.html` never calls the dashboard endpoint (the screen is
  hardcoded sample content).
- **Nothing calls `db.create_placement`** — so no real placements exist.
- `POST /accounts` receives `scanId` but **doesn't link it** to the account.

To make it live end-to-end, three connected changes:
  a. **Link the scan to the account.** In `server.py → create_account`, after
     creating the account, if `scanId` is present, set that scan's `account_id`
     (add a small `db.link_scan(scan_id, account_id)` helper).
  b. **Generate real placements.** In `server.py → save_onboarding` (or a
     background task after it), load the linked scan's report, take the top N
     threads, call `llm.draft_comment(profile, thread, guardrails)` for each,
     and `db.create_placement(...)`. `/api/v1/dashboard` already returns these.
  c. **Render the dashboard from the API.** In `static/index.html`, add
     `api.getDashboard()` and populate the dashboard screen + the approval modal
     from real data instead of the hardcoded thread/comment. Wire the modal's
     "Approve and schedule" to the real placement id (it currently posts to the
     `pl_demo` placeholder, which the backend accepts as a no-op).

### 4. Reddit posting — product/policy decision, not a missing feature
The "approve" flow sets DB state and schedules; **it does not post to Reddit.**
That's deliberate (see README → "Posting & Reddit policy"). The compliant design
is human-approved, disclosed comments. If we want assisted posting, it's a
separate Reddit OAuth + human-in-the-loop integration — decide the product
stance before building it. Do **not** add silent auto-posting; it gets accounts
and the domain banned.

### 5. Production hardening (not launch-blockers, but needed as you scale)
- **Rate-limit `POST /scans`** — it's anonymous and slow; add a per-IP limit so
  it can't be hammered.
- **Token expiry / logout** — tokens currently never expire. Add a TTL + a
  logout that deletes the token row.
- **Reddit OAuth** — public JSON is fine for low volume; heavy use needs a
  registered Reddit app to avoid 429s. `reddit.py` is where it goes.
- **SQLite → Postgres** when you run more than one instance. All storage is
  behind `db.py`; nothing above it changes.
- **Persistence on the host** — mount a volume for `flintel.db` (or set
  `FLINTEL_DB`) so accounts survive restarts on ephemeral hosts.
- **Password reset / email verification** — not built.

### 6. Optional / cosmetic
- The mock's "Ranks #1 on Google · 8,400 searches/mo" pills need a paid SEO API
  (Ahrefs/SEMrush). I used real Reddit metrics instead. Add in `pipeline.py`
  where thread `pills` are built if you want them back.
- The scan **animation** text ("Found 6 matching communities…") is hardcoded
  and always shows the same numbers. Purely visual — the real report drives the
  actual cards. Make it dynamic later if you care.
- Connect the always-on monitor (`flintel_v7.x.py`) to feed live signals into
  this app's placement queue.

---

## One-line summary for the team

> Scan → report → signup → onboarding is **live and real**. The **dashboard is
> the last screen still showing sample data** — the API behind it works, it just
> needs placements generated (step 3) and the screen wired to fetch them.
