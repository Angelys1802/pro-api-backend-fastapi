# PRO API Backend (FastAPI + Stripe Subscriptions)

## Setup
```bash
# pro-api-backend-fastapi
FastAPI backend template for a SaaS-style API with **Stripe Subscriptions**, **webhooks**, **API keys**, and **Free/Pro rate limits**.

## Features
- ✅ FastAPI REST backend
- ✅ Stripe Subscription Checkout (hosted Stripe Checkout)
- ✅ Stripe Webhook verification + automatic upgrade to **PRO**
- ✅ API Key management
- ✅ Daily request limits (Free vs Pro)
- ✅ SQLite database (simple MVP storage)

## Tech Stack
- Python
- FastAPI
- Stripe API
- SQLite
- Uvicorn

---

## Setup

### 1) Create virtual environment
```bash
python3 -m venv .venv
source .venv/bin/activate
