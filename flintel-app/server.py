"""
server.py — Flintel API + static host.

Implements the exact contract the frontend declares (see the BACKEND
CONTRACT block in index.html). One process serves both the app and the API,
so deploying is "run this file, point a domain at it."

Endpoints (all under /api/v1):
  POST /scans                      {url}                         -> 202 {scanId}
  GET  /scans/{id}/report                                        -> Report JSON
  POST /accounts    {name,email,password,company,plan,scanId...} -> 201 {accountId,token}
  POST /login       {email,password}                             -> 200 {accountId,token}
  POST /onboarding  {profile,audience,subreddits,guardrails}     -> 204
  GET  /dashboard                                                -> Dashboard JSON
  POST /placements/{id}/approve                                  -> 204
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import uuid
from typing import Any, Optional

import bcrypt
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("flintel.server")

import db          # noqa: E402
import pipeline     # noqa: E402

db.init()

app = FastAPI(title="Flintel API", version="1.0")
HERE = os.path.dirname(__file__)
STATIC_DIR = os.path.join(HERE, "static")

# max seconds GET /report will wait for a still-processing scan before
# returning whatever is ready (frontend animation already burns ~10s).
REPORT_WAIT_S = int(os.getenv("REPORT_WAIT_S", "60"))


# ── helpers ───────────────────────────────────────────────────────────
def _hash_pw(pw: str) -> str:
    # bcrypt only considers the first 72 bytes and raises on longer input.
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt()).decode()


def _check_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except Exception:  # noqa: BLE001
        return False


def _auth(authorization: Optional[str]) -> str:
    """Resolve Bearer token -> account_id, or 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    parts = authorization.split(" ", 1)
    token = parts[1].strip() if len(parts) > 1 else ""
    acct = db.account_id_for_token(token) if token else None
    if not acct:
        raise HTTPException(401, "invalid token")
    return acct


def _account_from_header(authorization: Optional[str]) -> Optional[str]:
    """Best-effort account resolution for optionally-authed routes. Never raises."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    parts = authorization.split(" ", 1)
    token = parts[1].strip() if len(parts) > 1 else ""
    return db.account_id_for_token(token) if token else None


async def _body(request: Request) -> dict[str, Any]:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


# ── SCANS ─────────────────────────────────────────────────────────────
@app.post("/api/v1/scans", status_code=202)
async def create_scan(request: Request, authorization: Optional[str] = Header(None)):
    body = await _body(request)
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    domain = (url.lower()
              .replace("https://", "").replace("http://", "")
              .replace("www.", "").split("/")[0])
    if "." not in domain:
        raise HTTPException(400, "invalid domain")

    account_id = _account_from_header(authorization)
    scan_id = "scan_" + uuid.uuid4().hex[:16]
    db.create_scan(scan_id, account_id, domain)

    # run the (slow) pipeline off the request thread
    def _work():
        try:
            report = pipeline.run_scan(domain)
            db.finish_scan(scan_id, report)
        except Exception as e:  # noqa: BLE001
            log.exception("scan %s failed", scan_id)
            db.fail_scan(scan_id, str(e))

    threading.Thread(target=_work, daemon=True).start()
    return {"scanId": scan_id}


@app.get("/api/v1/scans/{scan_id}/report")
def get_report(scan_id: str):
    deadline = time.time() + REPORT_WAIT_S
    while True:
        row = db.get_scan(scan_id)
        if not row:
            raise HTTPException(404, "unknown scan")
        if row["status"] == "ready":
            import json
            return JSONResponse(json.loads(row["report_json"]))
        if row["status"] == "error":
            raise HTTPException(502, f"scan failed: {row['error']}")
        if time.time() > deadline:
            raise HTTPException(504, "report still processing — retry shortly")
        time.sleep(1.0)


# ── ACCOUNTS / AUTH ───────────────────────────────────────────────────
@app.post("/api/v1/accounts", status_code=201)
async def create_account(request: Request):
    b = await _body(request)
    email = (b.get("email") or "").strip().lower()
    password = b.get("password") or ""
    name = (b.get("name") or "").strip()
    company = (b.get("company") or "").strip()
    plan = b.get("plan") or "Growth"

    # The signup screen collects credentials; require them for a real account.
    if not email or "@" not in email:
        raise HTTPException(400, "valid email required")
    if len(password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    if db.get_account_by_email(email):
        raise HTTPException(409, "an account with this email already exists")

    account_id = "acct_" + uuid.uuid4().hex[:16]
    db.create_account(account_id, name, email, _hash_pw(password), company, plan)
    token = secrets.token_urlsafe(32)
    db.create_token(token, account_id)
    log.info("account created | %s | %s | plan=%s", account_id, email, plan)
    return {"accountId": account_id, "token": token}


@app.post("/api/v1/login")
async def login(request: Request):
    b = await _body(request)
    email = (b.get("email") or "").strip().lower()
    acct = db.get_account_by_email(email)
    if not acct or not _check_pw(b.get("password") or "", acct["password_hash"]):
        raise HTTPException(401, "invalid email or password")
    token = secrets.token_urlsafe(32)
    db.create_token(token, acct["id"])
    return {"accountId": acct["id"], "token": token}


# ── ONBOARDING ────────────────────────────────────────────────────────
@app.post("/api/v1/onboarding", status_code=204)
async def save_onboarding(request: Request, authorization: Optional[str] = Header(None)):
    account_id = _auth(authorization)
    b = await _body(request)
    db.save_onboarding(
        account_id,
        profile=(b.get("profile") or "").strip(),
        audience=(b.get("audience") or "").strip(),
        subreddits=(b.get("subreddits") or "").strip(),
        guardrails=(b.get("guardrails") or "").strip(),
    )
    log.info("onboarding saved | %s", account_id)
    return JSONResponse(status_code=204, content=None)


# ── DASHBOARD ─────────────────────────────────────────────────────────
@app.get("/api/v1/dashboard")
def dashboard(authorization: Optional[str] = Header(None)):
    account_id = _auth(authorization)
    acct = db.get_account(account_id)
    placements = db.list_placements(account_id)
    return {
        "company": acct["company"] if acct else "",
        "plan": acct["plan"] if acct else "",
        "placements": [{
            "id": p["id"],
            "subreddit": p["subreddit"],
            "threadTitle": p["thread_title"],
            "threadUrl": p["thread_url"],
            "draft": p["draft_comment"],
            "status": p["status"],
            "scheduledFor": p["scheduled_for"],
        } for p in placements],
        "stats": {"queued": sum(1 for p in placements if p["status"] == "draft"),
                  "approved": sum(1 for p in placements if p["status"] == "approved")},
    }


# ── PLACEMENTS ────────────────────────────────────────────────────────
@app.post("/api/v1/placements/{placement_id}/approve", status_code=204)
def approve_placement(placement_id: str, authorization: Optional[str] = Header(None)):
    _auth(authorization)
    # demo placeholder id from the static dashboard still returns cleanly
    if placement_id == "pl_demo":
        return JSONResponse(status_code=204, content=None)
    scheduled = time.time() + 3 * 86400  # +3 days, matches the UI copy
    if not db.approve_placement(placement_id, scheduled):
        raise HTTPException(404, "unknown placement")
    return JSONResponse(status_code=204, content=None)


# ── health + static host ──────────────────────────────────────────────
@app.get("/api/v1/health")
def health():
    import llm
    return {"ok": True, "llm": llm.enabled(), "model": llm.MODEL if llm.enabled() else None}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# everything else (favicon, etc.) — serve static dir, fall back to index
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
