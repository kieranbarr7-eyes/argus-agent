"""
main.py — Entry point for the Argus agent.

Starts the Flask server with web push endpoints, Claude chat proxy,
rate limiting, CORS, API secret validation, input sanitization,
and the APScheduler background poller for all active watches.
"""

import json
import logging
import random
import re
import signal
import sys
import time
import traceback

import anthropic
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler

import config
import db
from bot import notify_watch_subscribers
from monitor import poll_watch

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-20s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("argus-agent")

# ---------------------------------------------------------------------------
# Flask app + CORS + rate limiting
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KB max request body

CORS(app, origins=[
    "http://localhost:5173",
    "https://argusfare.com",
    "https://www.argusfare.com",
    "https://argus-pwa-pied.vercel.app",
])

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per hour", "50 per minute"],
    storage_uri="memory://",
)


# ---------------------------------------------------------------------------
# Security — API secret validation
# ---------------------------------------------------------------------------

def validate_secret():
    """Check X-Argus-Secret header. Skip if ARGUS_API_SECRET is not set."""
    if not config.ARGUS_API_SECRET:
        return True  # Skip validation if not configured
    secret = request.headers.get("X-Argus-Secret", "")
    if not secret:
        return False
    # Constant-time comparison to prevent timing attacks
    import hmac
    return hmac.compare_digest(secret, config.ARGUS_API_SECRET)


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

# Amtrak station codes: 3 uppercase letters
_STATION_CODE_RE = re.compile(r"^[A-Z]{3}$")

# Date formats we accept: YYYY-MM-DD or MM/DD/YYYY
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})$")

# Train numbers: 1-4 digits
_TRAIN_NUM_RE = re.compile(r"^\d{1,4}$")

# Max lengths for string fields
_MAX_STATION_LEN = 3
_MAX_DATE_LEN = 10
_MAX_TRAIN_NUM_LEN = 4
_MAX_ENDPOINT_LEN = 2048
_MAX_CONTEXT_LEN = 5000
_MAX_MESSAGE_CONTENT_LEN = 2000
_MAX_MESSAGES_COUNT = 50


def sanitize_station(code: str) -> str | None:
    """Validate and sanitize a station code. Returns None if invalid."""
    if not isinstance(code, str):
        return None
    code = code.strip().upper()[:_MAX_STATION_LEN]
    return code if _STATION_CODE_RE.match(code) else None


def sanitize_date(date_str: str) -> str | None:
    """Validate and sanitize a date string. Returns None if invalid."""
    if not isinstance(date_str, str):
        return None
    date_str = date_str.strip()[:_MAX_DATE_LEN]
    return date_str if _DATE_RE.match(date_str) else None


def sanitize_train_number(num: str) -> str | None:
    """Validate and sanitize a train number. Returns None if invalid."""
    if not isinstance(num, str):
        return None
    num = num.strip()[:_MAX_TRAIN_NUM_LEN]
    return num if _TRAIN_NUM_RE.match(num) else None


def validate_chat_payload(data: dict) -> tuple[bool, str]:
    """Validate /chat request payload. Returns (ok, error_message)."""
    if not isinstance(data, dict):
        return False, "Invalid request format"

    messages = data.get("messages")
    if not isinstance(messages, list) or len(messages) == 0:
        return False, "Missing or empty 'messages' array"
    if len(messages) > _MAX_MESSAGES_COUNT:
        return False, f"Too many messages (max {_MAX_MESSAGES_COUNT})"

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return False, f"Message {i} is not an object"
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            return False, f"Message {i} has invalid role '{role}'"
        content = msg.get("content", "")
        if not isinstance(content, str):
            return False, f"Message {i} content must be a string"
        if len(content) > _MAX_MESSAGE_CONTENT_LEN:
            return False, f"Message {i} content too long (max {_MAX_MESSAGE_CONTENT_LEN} chars)"

    watch_context = data.get("watchContext", "")
    if not isinstance(watch_context, str):
        return False, "watchContext must be a string"
    if len(watch_context) > _MAX_CONTEXT_LEN:
        return False, f"watchContext too long (max {_MAX_CONTEXT_LEN} chars)"

    return True, ""


def validate_subscription_payload(subscription: dict) -> tuple[bool, str]:
    """Validate a push subscription object. Returns (ok, error_message)."""
    if not isinstance(subscription, dict):
        return False, "Subscription must be an object"

    endpoint = subscription.get("endpoint", "")
    if not isinstance(endpoint, str) or not endpoint:
        return False, "Subscription missing 'endpoint'"
    if len(endpoint) > _MAX_ENDPOINT_LEN:
        return False, "Endpoint URL too long"
    if not endpoint.startswith("https://"):
        return False, "Endpoint must be HTTPS"

    keys = subscription.get("keys")
    if keys is not None:
        if not isinstance(keys, dict):
            return False, "Subscription 'keys' must be an object"
        for key_name in ("p256dh", "auth"):
            val = keys.get(key_name, "")
            if val and (not isinstance(val, str) or len(val) > 512):
                return False, f"Invalid subscription key '{key_name}'"

    return True, ""


# ---------------------------------------------------------------------------
# Public endpoints (no secret required)
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
@limiter.limit("60 per minute")
def health():
    """Health check endpoint."""
    watches = db.get_active_watches()
    return {"status": "ok", "active_watches": len(watches)}, 200


@app.route("/vapid-public-key", methods=["GET"])
@limiter.limit("30 per minute")
def vapid_public_key():
    """Return the public VAPID key so the browser can subscribe to push."""
    if not config.VAPID_PUBLIC_KEY:
        return {"error": "VAPID_PUBLIC_KEY not configured"}, 500
    return {"publicKey": config.VAPID_PUBLIC_KEY}, 200


# ---------------------------------------------------------------------------
# Protected endpoints (secret + rate limited)
# ---------------------------------------------------------------------------

@app.route("/chat", methods=["POST"])
@limiter.limit("30 per minute")
def chat():
    """
    Proxy Claude API calls so the Anthropic key stays on the server.

    Expects JSON body:
    {
        "messages": [{"role": "user", "content": "..."}],
        "watchContext": "optional context string"
    }
    """
    try:
        if not validate_secret():
            return jsonify({"error": "Unauthorized"}), 401

        data = request.json
        messages = data.get("messages", [])
        watch_context = data.get("watchContext", "")

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=f"You are Argus, a friendly AI assistant that helps users monitor Amtrak ticket prices. {watch_context}",
            messages=messages,
        )
        return jsonify({"content": response.content[0].text})
    except Exception as e:
        log.error("[Argus] Chat endpoint error: %s", traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/subscribe", methods=["POST"])
@limiter.limit("5 per 15 minutes")
def subscribe():
    """
    Store a web push subscription from the browser.

    Expects JSON body:
    {
        "subscription": { "endpoint": "...", "keys": { "p256dh": "...", "auth": "..." } },
        "watch_id": 123  (optional — link to an existing watch)
    }
    """
    if not validate_secret():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "subscription" not in data:
        return jsonify({"error": "Missing 'subscription' in request body"}), 400

    subscription = data["subscription"]
    ok, err = validate_subscription_payload(subscription)
    if not ok:
        return jsonify({"error": err}), 400

    endpoint = subscription["endpoint"]

    # Validate optional watch_id
    watch_id = data.get("watch_id")
    if watch_id is not None:
        if not isinstance(watch_id, int) or watch_id < 0:
            return jsonify({"error": "Invalid watch_id"}), 400

    sub_id = db.store_subscription(
        endpoint=endpoint,
        subscription_json=json.dumps(subscription),
        watch_id=watch_id,
    )

    log.info("Stored push subscription #%d (watch_id=%s)", sub_id, watch_id)
    return {"status": "subscribed", "subscription_id": sub_id}, 201


@app.route("/register", methods=["POST"])
@limiter.limit("5 per 15 minutes")
def register_watch():
    """
    Register a watch from the Chrome extension or PWA.

    Expects JSON body:
    {
        "route": { "origin": "NYP", "destination": "PHL", "date": "04/07/2026" },
        "trains": [{ "trainNumber": "197" }, ...],
        "subscription": { "endpoint": "...", "keys": { ... } }  (optional)
    }
    """
    if not validate_secret():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "route" not in data:
        return jsonify({"error": "Missing 'route' in request body"}), 400

    route = data.get("route")
    if not isinstance(route, dict):
        return jsonify({"error": "'route' must be an object"}), 400

    # Sanitize station codes
    origin = sanitize_station(route.get("origin", ""))
    destination = sanitize_station(route.get("destination", ""))
    if not origin or not destination:
        return jsonify({"error": "Invalid station code (must be 3 uppercase letters)"}), 400

    # Sanitize date
    raw_date = route.get("date", "").strip()
    date = sanitize_date(raw_date) if raw_date else ""
    if raw_date and not date:
        return jsonify({"error": "Invalid date format (use YYYY-MM-DD or MM/DD/YYYY)"}), 400

    # Sanitize train numbers
    trains = data.get("trains", [])
    if not isinstance(trains, list):
        return jsonify({"error": "'trains' must be an array"}), 400
    if len(trains) > 20:
        return jsonify({"error": "Too many trains (max 20)"}), 400

    train_numbers = []
    for t in trains:
        if not isinstance(t, dict):
            continue
        raw_num = t.get("trainNumber", t.get("train_number", ""))
        num = sanitize_train_number(str(raw_num))
        if num:
            train_numbers.append(num)

    # Check for existing active watch on same route+date
    existing = db.find_active_watch(origin, destination, date)
    if existing:
        watch_id = existing["id"]
        db.update_watch_trains(watch_id, train_numbers)
        log.info("Updated existing watch #%d: %s->%s on %s, trains=%s",
                 watch_id, origin, destination, date, train_numbers)
    else:
        watch_id = db.create_watch(
            origin=origin,
            destination=destination,
            date=date,
            train_numbers=train_numbers,
        )
        log.info("Created new watch #%d: %s->%s on %s, trains=%s",
                 watch_id, origin, destination, date, train_numbers)

    # If a push subscription was included, validate and link it to this watch
    subscription = data.get("subscription")
    if subscription and isinstance(subscription, dict):
        ok, err = validate_subscription_payload(subscription)
        if ok:
            db.store_subscription(
                endpoint=subscription["endpoint"],
                subscription_json=json.dumps(subscription),
                watch_id=watch_id,
            )
        else:
            log.warning("Invalid subscription in /register: %s", err)

    return {
        "status": "registered",
        "watch_id": watch_id,
        "route": {"origin": origin, "destination": destination, "date": date},
        "trains": train_numbers,
    }, 200


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

def _send_push_for_watch(watch: dict, train: dict, alert_msg: str) -> None:
    """Send a web push notification for a price alert."""
    origin = watch["origin"]
    dest = watch["destination"]
    date = watch.get("date", "")
    price = train["price"]
    train_num = train["train_number"]

    # Build Amtrak booking URL
    if date:
        parts = date.split("-") if "-" in date else []
        if len(parts) == 3:
            date_param = f"{parts[1]}/{parts[2]}/{parts[0]}"
        else:
            date_param = date
    else:
        date_param = ""

    book_url = (
        f"https://www.amtrak.com/tickets/departure.html"
        f"#df={date_param}&org={origin}&dst={dest}"
    )

    notify_watch_subscribers(
        watch_id=watch["id"],
        title=f"🟢 Train {train_num} dropped to ${price:.0f}!",
        body=(
            f"{origin}→{dest} · {train.get('fare_class', 'coach').title()} · "
            f"Book the Flexible fare (fully refundable) — cancel & rebook free if it drops again."
        ),
        url=book_url,
    )


def poll_all_watches() -> None:
    """Poll prices for every active watch. Called by APScheduler."""
    watches = db.get_active_watches()
    if not watches:
        log.info("No active watches to poll")
        return

    log.info("Polling %d active watch(es)...", len(watches))

    for watch in watches:
        try:
            # Add jitter to avoid hammering Amtrak with concurrent requests
            jitter = random.uniform(0, config.POLL_JITTER_SECONDS)
            time.sleep(jitter)

            poll_watch(watch, _send_push_for_watch)
        except Exception as e:
            log.error("Poll failed for watch #%d: %s", watch["id"], e)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def main() -> None:
    # Initialize database
    db.init_db()

    # Log active watches on startup
    watches = db.get_active_watches()
    log.info("=" * 60)
    log.info("ARGUS AGENT")
    log.info("=" * 60)
    if watches:
        log.info("Active watches on startup: %d", len(watches))
        for w in watches:
            log.info(
                "  Watch #%d: %s->%s on %s, trains=%s",
                w["id"], w["origin"], w["destination"],
                w["date"], w.get("train_numbers", "all"),
            )
    else:
        log.info("No active watches. Waiting for /register requests...")
    log.info("=" * 60)

    # Validate config
    missing = []
    if not config.ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not config.VAPID_PRIVATE_KEY:
        missing.append("VAPID_PRIVATE_KEY")
    if not config.VAPID_PUBLIC_KEY:
        missing.append("VAPID_PUBLIC_KEY")

    if missing:
        log.warning(
            "Missing credentials: %s — "
            "the agent will start but some features won't work until these are set.",
            ", ".join(missing),
        )

    if config.ARGUS_API_SECRET:
        log.info("API secret validation: ENABLED")
    else:
        log.warning("ARGUS_API_SECRET not set — endpoints are unprotected!")

    # Start background scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        poll_all_watches,
        "interval",
        seconds=config.POLL_INTERVAL_SECONDS,
        id="price-poller",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info(
        "Background poller started — checking every %ds (±%ds jitter)",
        config.POLL_INTERVAL_SECONDS,
        config.POLL_JITTER_SECONDS,
    )

    # Graceful shutdown
    def shutdown(signum, frame):
        log.info("Shutting down gracefully...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start Flask
    log.info("Flask server starting on port %d", config.FLASK_PORT)
    log.info("Endpoints:")
    log.info("  POST /register         — register a watch (protected)")
    log.info("  POST /subscribe        — store a push subscription (protected)")
    log.info("  POST /chat             — Claude chat proxy (protected)")
    log.info("  GET  /vapid-public-key — get VAPID public key")
    log.info("  GET  /health           — health check")

    app.run(
        host="0.0.0.0",
        port=config.FLASK_PORT,
        debug=False,
        use_reloader=False,  # Reloader conflicts with APScheduler
    )


if __name__ == "__main__":
    main()
