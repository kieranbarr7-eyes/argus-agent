"""
main.py — Entry point for the Argus agent.

Starts the Flask server with web push endpoints, Claude chat proxy,
rate limiting, CORS, API secret validation, and the APScheduler
background poller for all active watches.
"""

import json
import logging
import random
import signal
import sys
import time

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
)


# ---------------------------------------------------------------------------
# Security — API secret validation
# ---------------------------------------------------------------------------

def validate_secret():
    """Check X-Argus-Secret header. Skip if ARGUS_API_SECRET is not set."""
    if not config.ARGUS_API_SECRET:
        return True  # Skip validation if not configured
    secret = request.headers.get("X-Argus-Secret")
    return secret == config.ARGUS_API_SECRET


# ---------------------------------------------------------------------------
# Public endpoints (no secret required)
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    watches = db.get_active_watches()
    return {"status": "ok", "active_watches": len(watches)}, 200


@app.route("/vapid-public-key", methods=["GET"])
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
    if not validate_secret():
        return jsonify({"error": "Unauthorized"}), 401

    if not config.ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500

    data = request.get_json(silent=True)
    if not data or "messages" not in data:
        return jsonify({"error": "Missing 'messages' in request body"}), 400

    messages = data.get("messages", [])
    watch_context = data.get("watchContext", "")

    system_prompt = (
        "You are Argus, a friendly AI assistant that helps users monitor "
        "Amtrak ticket prices. "
        f"{watch_context} "
        "Do not ask users to re-enter information you already have. "
        "Ask one question at a time. Be conversational and brief — no more "
        "than 2-3 sentences per message."
    )

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=system_prompt,
            messages=messages,
        )
        return jsonify({"content": response.content[0].text})
    except anthropic.AuthenticationError:
        log.error("Anthropic API authentication failed")
        return jsonify({"error": "Claude API authentication failed"}), 500
    except Exception as e:
        log.error("Chat endpoint error: %s", e)
        return jsonify({"error": "Chat failed"}), 500


@app.route("/subscribe", methods=["POST"])
@limiter.limit("20 per minute")
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
        return {"error": "Missing 'subscription' in request body"}, 400

    subscription = data["subscription"]
    endpoint = subscription.get("endpoint")
    if not endpoint:
        return {"error": "Subscription missing 'endpoint'"}, 400

    watch_id = data.get("watch_id")

    sub_id = db.store_subscription(
        endpoint=endpoint,
        subscription_json=json.dumps(subscription),
        watch_id=watch_id,
    )

    log.info("Stored push subscription #%d (watch_id=%s)", sub_id, watch_id)
    return {"status": "subscribed", "subscription_id": sub_id}, 201


@app.route("/register", methods=["POST"])
@limiter.limit("10 per minute")
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
        return {"error": "Missing 'route' in request body"}, 400

    route = data["route"]
    origin = route.get("origin", "").strip().upper()
    destination = route.get("destination", "").strip().upper()
    date = route.get("date", "").strip()

    if not origin or not destination:
        return {"error": "Route must include 'origin' and 'destination'"}, 400

    trains = data.get("trains", [])
    train_numbers = [t.get("trainNumber", t.get("train_number", "")) for t in trains]
    train_numbers = [t for t in train_numbers if t]  # filter empties

    # Check for existing active watch on same route+date
    existing = db.find_active_watch(origin, destination, date)
    if existing:
        watch_id = existing["id"]
        # Update the watched trains
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

    # If a push subscription was included, link it to this watch
    subscription = data.get("subscription")
    if subscription and subscription.get("endpoint"):
        db.store_subscription(
            endpoint=subscription["endpoint"],
            subscription_json=json.dumps(subscription),
            watch_id=watch_id,
        )

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
