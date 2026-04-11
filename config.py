"""
config.py — All credentials and settings for the Argus agent.

Uses environment variables for secrets. Set them before running.
"""

import os

# ─── Anthropic (Claude) ─────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─── Security ────────────────────────────────────────────────────────────────
ARGUS_API_SECRET = os.environ.get("ARGUS_API_SECRET", "")

# ─── Flask ───────────────────────────────────────────────────────────────────
FLASK_PORT = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", 5000)))

# ─── Web Push (VAPID) ───────────────────────────────────────────────────────
VAPID_PRIVATE_KEY    = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY     = os.environ.get("VAPID_PUBLIC_KEY")
VAPID_CLAIMS_EMAIL   = os.environ.get("VAPID_CLAIMS_EMAIL", "kieranbarr7@gmail.com")

# ─── Polling ─────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 90   # Base interval between price checks
POLL_JITTER_SECONDS   = 15   # ± random jitter added to each poll

# ─── Playwright ──────────────────────────────────────────────────────────────
HEADLESS              = True
PAGE_LOAD_TIMEOUT_MS  = 90_000
DEBUG_SCREENSHOT_DIR  = "debug_screenshots"

# ─── Database ────────────────────────────────────────────────────────────────
# DATABASE_URL takes precedence (PostgreSQL in production).
# Falls back to SQLite via DB_PATH for local development.
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DB_PATH", "argus_agent.db")
