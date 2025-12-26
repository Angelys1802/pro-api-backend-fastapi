import os
import sqlite3
import secrets
from datetime import datetime, timezone

from dotenv import load_dotenv
import stripe

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="PRO API Backend", version="1.0.0")

# ====== ENV ======
DB_PATH = os.getenv("DB_PATH", "brigh.db")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  # sk_test...
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")  # whsec_...
PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "price_1Si098RJ6VSjGzsKnYW3Lryc")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

# ====== LIMITS (change if you want) ======
FREE_LIMIT_PER_DAY = int(os.getenv("FREE_LIMIT_PER_DAY", "25"))
PRO_LIMIT_PER_DAY = int(os.getenv("PRO_LIMIT_PER_DAY", "10000"))


# ====== DB HELPERS ======
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    # API keys (plan = free/pro)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS api_keys (
        api_key TEXT PRIMARY KEY,
        plan TEXT DEFAULT 'free',
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT ''
    )
    """)

    # usage per day
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_counters (
        api_key TEXT NOT NULL,
        day TEXT NOT NULL,              -- YYYY-MM-DD (UTC)
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (api_key, day)
    )
    """)

    conn.commit()
    conn.close()


def utc_day_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ensure_key_exists(api_key: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO api_keys(api_key, plan, is_active, created_at) VALUES(?, 'free', 1, ?)",
        (api_key, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_key_row(api_key: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT api_key, plan, is_active FROM api_keys WHERE api_key=?", (api_key,))
    row = cur.fetchone()
    conn.close()
    return row


def upgrade_api_key_to_pro(api_key: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE api_keys SET plan='pro', is_active=1 WHERE api_key=?", (api_key,))
    conn.commit()
    conn.close()


def increment_usage(api_key: str) -> int:
    day = utc_day_str()
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO usage_counters(api_key, day, count) VALUES(?, ?, 0)", (api_key, day))
    cur.execute("UPDATE usage_counters SET count = count + 1 WHERE api_key=? AND day=?", (api_key, day))
    cur.execute("SELECT count FROM usage_counters WHERE api_key=? AND day=?", (api_key, day))
    count = int(cur.fetchone()["count"])
    conn.commit()
    conn.close()
    return count


def get_limit_for_plan(plan: str) -> int:
    return PRO_LIMIT_PER_DAY if plan == "pro" else FREE_LIMIT_PER_DAY


def require_active_and_rate_limit(api_key: str):
    row = get_key_row(api_key)
    if not row:
        raise HTTPException(404, "Unknown api_key. Create one first.")
    if int(row["is_active"]) != 1:
        raise HTTPException(403, "API key is not active.")

    plan = row["plan"] or "free"
    limit = get_limit_for_plan(plan)

    used = increment_usage(api_key)
    if used > limit:
        raise HTTPException(429, f"Daily limit exceeded: {used-1}/{limit} used. Upgrade to PRO.")
    return {"plan": plan, "used_today": used, "limit_today": limit}


# ====== MODELS ======
class CreateKeyResponse(BaseModel):
    api_key: str
    plan: str


class CheckoutBody(BaseModel):
    api_key: str


# ====== FASTAPI LIFECYCLE ======
@app.on_event("startup")
def on_startup():
    init_db()


# ====== ROUTES ======
@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/keys/create", response_model=CreateKeyResponse)
async def create_api_key():
    # generate random key
    api_key = "key_" + secrets.token_urlsafe(24)
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO api_keys(api_key, plan, is_active, created_at) VALUES(?, 'free', 1, ?)",
        (api_key, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return {"api_key": api_key, "plan": "free"}


@app.get("/keys/{api_key}")
async def key_status(api_key: str):
    row = get_key_row(api_key)
    if not row:
        raise HTTPException(404, "Unknown api_key")
    return {"api_key": row["api_key"], "plan": row["plan"], "is_active": bool(int(row["is_active"]))}


@app.post("/billing/checkout")
async def create_checkout(body: CheckoutBody):
    if not stripe.api_key:
        raise HTTPException(500, "STRIPE_SECRET_KEY is missing")

    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(400, "api_key is required")

    ensure_key_exists(api_key)

    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        line_items=[{"price": PRO_PRICE_ID, "quantity": 1}],
        success_url=f"{BASE_URL}/billing/success?api_key={api_key}",
        cancel_url=f"{BASE_URL}/billing/cancel",
        metadata={"api_key": api_key},
    )

    return {"url": session.url, "session_id": session.id}


@app.get("/billing/success")
async def billing_success(api_key: str):
    return {
        "ok": True,
        "message": "Payment success. Webhook will upgrade the key (usually instantly).",
        "api_key": api_key,
    }


@app.get("/billing/cancel")
async def billing_cancel():
    return {"ok": False, "message": "Payment cancelled."}


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")

    if not WEBHOOK_SECRET:
        raise HTTPException(500, "STRIPE_WEBHOOK_SECRET is missing")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    # upgrade after successful checkout
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        api_key = (session.get("metadata") or {}).get("api_key")
        if api_key:
            upgrade_api_key_to_pro(api_key)

    return JSONResponse({"ok": True})


# Example protected endpoint (counts usage)
@app.get("/protected/ping")
async def protected_ping(api_key: str):
    meta = require_active_and_rate_limit(api_key)
    return {"ok": True, "message": "pong", **meta}