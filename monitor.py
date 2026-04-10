"""
monitor.py — Price monitoring logic using Playwright.

Reuses the stealth Playwright approach from amtrak-watcher/poller.py
to scrape ALL trains on a route, then filters by the user's time window.
"""

import json
import logging
import re
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import (
    Browser,
    Page,
    Playwright,
    Response,
    sync_playwright,
    TimeoutError as PWTimeoutError,
)
from playwright_stealth import Stealth

import config
import db
from recommender import format_price_alert

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Station names (for logging)
# ---------------------------------------------------------------------------
STATION_NAMES: dict[str, str] = {
    "NYP": "New York Penn Station",
    "PHL": "Philadelphia",
    "WAS": "Washington Union Station",
    "BOS": "Boston South Station",
    "PVD": "Providence",
    "NHV": "New Haven",
    "STM": "Stamford",
    "WIL": "Wilmington",
    "BAL": "Baltimore",
    "NWK": "Newark",
    "TRE": "Trenton",
    "MET": "Metropark",
    "EWR": "Newark Airport",
    "ALB": "Albany-Rensselaer",
    "CHI": "Chicago Union Station",
}

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_FARE_URL_KEYWORDS = [
    "journey", "trip", "fare", "result", "train", "search",
    "schedule", "offer", "availab", "departure", "graphql",
    "/api/", "services",
]


# ---------------------------------------------------------------------------
# Public: fetch all trains for a route
# ---------------------------------------------------------------------------

def fetch_all_trains(
    origin: str,
    destination: str,
    date: str,
) -> list[dict]:
    """
    Scrape ALL trains for a given route and date.

    Returns a list of dicts:
        [{"train_number": "651", "departure_time": "13:05",
          "price": 68.0, "fare_class": "coach"}, ...]
    """
    log.info("Fetching trains for %s->%s on %s", origin, destination, date)

    stealth = Stealth(
        chrome_runtime=True,
        navigator_webdriver=True,
        navigator_user_agent_override=_DESKTOP_UA,
        navigator_platform_override="Win32",
        navigator_vendor_override="Google Inc.",
    )

    with stealth.use_sync(sync_playwright()) as pw:
        browser = _launch(pw)
        try:
            page = browser.new_page()
            _configure_page(page)
            stealth.apply_stealth_sync(page)

            fare_responses = _navigate(page, origin, destination, date)
            _wait_settle(page)

            trains = _extract_all_trains(page, fare_responses)
            log.info("Found %d trains for %s->%s on %s", len(trains), origin, destination, date)
            return trains
        except Exception as exc:
            _save_debug(page, "monitor_error")
            log.error("Scrape failed: %s", exc)
            return []
        finally:
            browser.close()


def filter_by_time_window(
    trains: list[dict],
    time_start: str | None,
    time_end: str | None,
) -> list[dict]:
    """Filter trains to only those departing within the given time window."""
    if not time_start and not time_end:
        return trains

    filtered = []
    for train in trains:
        dep = train.get("departure_time", "")
        if not dep:
            continue
        try:
            dep_mins = _time_to_minutes(dep)
        except ValueError:
            continue

        if time_start:
            start_mins = _time_to_minutes(time_start)
            if dep_mins < start_mins:
                continue
        if time_end:
            end_mins = _time_to_minutes(time_end)
            if dep_mins > end_mins:
                continue

        filtered.append(train)

    log.info(
        "Time filter %s-%s: %d/%d trains passed",
        time_start, time_end, len(filtered), len(trains),
    )
    return filtered


# ---------------------------------------------------------------------------
# Public: poll a single watch and return alert messages
# ---------------------------------------------------------------------------

def poll_watch(watch: dict, notify_fn) -> None:
    """
    Poll prices for a single watch. If any price is within the user's
    budget range, call notify_fn to send an alert.

    Parameters
    ----------
    watch     : dict from db.get_active_watches()
    notify_fn : callable(watch, train, alert_msg) to send notification
    """
    trains = fetch_all_trains(watch["origin"], watch["destination"], watch["date"])

    if not trains:
        log.warning("No trains found for watch #%d", watch["id"])
        return

    # Filter by time window
    filtered = filter_by_time_window(
        trains, watch.get("time_start"), watch.get("time_end")
    )

    # Filter by watched train numbers (if specified)
    watched_trains = watch.get("train_numbers", [])
    if watched_trains:
        filtered = [t for t in filtered if t["train_number"] in watched_trains]

    if not filtered:
        log.info("No matching trains for watch #%d", watch["id"])
        return

    # Record all prices in history
    route = f"{watch['origin']}-{watch['destination']}"
    for train in filtered:
        db.record_price(
            watch_id=watch["id"],
            route=route,
            train_number=train["train_number"],
            departure_time=train.get("departure_time"),
            price=train["price"],
            fare_class=train.get("fare_class", "coach"),
        )

    # Check for price drops by comparing to previous prices
    previous_prices = db.get_latest_prices_for_watch(watch["id"])
    prev_map = {p["train_number"]: p["price"] for p in previous_prices}

    for train in filtered:
        prev = prev_map.get(train["train_number"])
        if prev is not None and train["price"] < prev:
            # Price dropped
            params = {
                "origin": watch["origin"],
                "destination": watch["destination"],
                "date": watch["date"],
            }
            alert_msg = format_price_alert(train, params)
            log.info(
                "ALERT: Train %s dropped $%.0f → $%.0f for watch #%d",
                train["train_number"], prev, train["price"], watch["id"],
            )
            notify_fn(watch, train, alert_msg)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def _launch(pw: Playwright) -> Browser:
    return pw.chromium.launch(
        headless=config.HEADLESS,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )


def _configure_page(page: Page) -> None:
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.set_extra_http_headers({
        "User-Agent": _DESKTOP_UA,
        "Accept-Language": "en-US,en;q=0.9",
    })


def _navigate(page: Page, origin: str, dest: str, date: str) -> list[dict]:
    """Navigate to Amtrak results page and capture API responses.

    Tries the query-string URL format first (which seems more reliable for
    triggering Angular to actually hit the fare API), then falls back to the
    hash-fragment format if we don't see any fare responses.
    """
    y, m, d = date.split("-")
    date_param = f"{m}/{d}/{y}"

    url_query = (
        "https://www.amtrak.com/buy/departure.html"
        f"?org={origin}&dest={dest}"
        f"&departDate={date_param}&returnDate="
        "&adult=1&senior=0"
    )
    url_hash = (
        "https://www.amtrak.com/tickets/departure.html"
        f"#df={date_param}&dt=&ot=One-Way"
        f"&org={origin}&dst={dest}"
        "&fs=&ad=1&cx=0&sc=0&se=0&ck=0&vc=0"
    )

    captured: list[Response] = []

    def _on_response(r: Response) -> None:
        captured.append(r)

    page.on("response", _on_response)

    def _try_url(u: str) -> None:
        log.info("Navigating to: %s", u)
        try:
            page.goto(u, wait_until="networkidle", timeout=config.PAGE_LOAD_TIMEOUT_MS)
        except PWTimeoutError:
            log.warning("page.goto() timed out — continuing with %d responses", len(captured))

        # Dismiss cookie banner
        try:
            btn = page.locator("#onetrust-accept-btn-handler")
            btn.wait_for(state="visible", timeout=6_000)
            btn.click()
            page.wait_for_timeout(500)
        except (PWTimeoutError, Exception):
            pass

        # Extra settle for Angular bootstrap + async fare API
        page.wait_for_timeout(15_000)

    # Strategy 1: query-string URL (more reliable at triggering the API)
    _try_url(url_query)

    fare_candidates = _filter_fare_responses(captured)

    # Strategy 2: fall back to hash-fragment URL if nothing came through
    if not fare_candidates:
        log.info("No fare responses from query-string URL — retrying with hash URL")
        _try_url(url_hash)
        fare_candidates = _filter_fare_responses(captured)

    page.remove_listener("response", _on_response)

    log.info("Captured %d fare-keyword JSON responses (from %d total responses)",
             len(fare_candidates), len(captured))

    # Diagnostic: log ALL captured fare response URLs so we can see exactly
    # what Amtrak is returning.
    for i, r in enumerate(fare_candidates):
        log.info("[Monitor] Response %d: %s", i + 1, r["url"])

    # Also log first response body sample
    if fare_candidates:
        try:
            log.info(
                "[Monitor] First JSON response sample: %s",
                json.dumps(fare_candidates[0]["json"])[:500],
            )
        except Exception as e:
            log.warning("[Monitor] Could not serialize first JSON response: %s", e)

    return fare_candidates


def _filter_fare_responses(captured: list[Response]) -> list[dict]:
    """Filter captured responses down to fare-keyword JSON payloads."""
    fare_candidates: list[dict] = []
    for r in captured:
        url_lower = r.url.lower()
        if not any(k in url_lower for k in _FARE_URL_KEYWORDS):
            continue
        if r.status != 200:
            continue
        try:
            data = r.json()
            fare_candidates.append({"url": r.url, "json": data})
        except Exception:
            pass
    return fare_candidates


def _wait_settle(page: Page) -> None:
    page.wait_for_timeout(2_000)


# ---------------------------------------------------------------------------
# Extract ALL trains from page (not just one train like poller.py)
# ---------------------------------------------------------------------------

def _extract_all_trains(page: Page, fare_responses: list[dict]) -> list[dict]:
    """
    Extract all trains and their fares from the page.

    Strategy:
    1. Try API JSON responses first (most reliable)
    2. Fall back to DOM extraction via JavaScript
    """
    trains = []

    # Strategy 1: API JSON
    if fare_responses:
        trains = _parse_all_trains_from_json(fare_responses)
        if trains:
            log.info("Extracted %d trains from API JSON", len(trains))
            return trains

    # Strategy 2: DOM extraction
    log.info("Falling back to DOM extraction for all trains")
    trains = _extract_trains_from_dom(page)
    if trains:
        log.info("Extracted %d trains from DOM", len(trains))
    else:
        log.warning("Could not extract any trains from page")

    return trains


def _parse_all_trains_from_json(fare_responses: list[dict]) -> list[dict]:
    """Parse all trains from captured API JSON responses."""
    trains = []

    for candidate in fare_responses:
        data = candidate["json"]
        found = _recursive_find_trains(data, depth=0)
        trains.extend(found)

    # Deduplicate by train_number + departure_time
    seen = set()
    unique = []
    for t in trains:
        key = (t["train_number"], t.get("departure_time", ""))
        if key not in seen:
            seen.add(key)
            unique.append(t)

    return unique


def _recursive_find_trains(obj, depth: int, max_depth: int = 10) -> list[dict]:
    """Recursively search JSON for train fare data."""
    if depth > max_depth or obj is None:
        return []

    trains = []

    if isinstance(obj, list):
        for item in obj:
            trains.extend(_recursive_find_trains(item, depth + 1))
        return trains

    if not isinstance(obj, dict):
        return []

    obj_str = str(obj).lower()

    # Look for objects that have train-like data
    train_number = None
    departure_time = None
    price = None
    fare_class = "coach"

    for key, val in obj.items():
        k = key.lower()
        v_str = str(val) if val is not None else ""

        # Train number
        if any(tk in k for tk in ("trainnumber", "train_number", "trainnum", "number")):
            train_number = str(val).strip()

        # Departure time
        if any(tk in k for tk in ("departure", "depart", "time", "schedule")):
            if isinstance(val, str) and (":" in val or "am" in val.lower() or "pm" in val.lower()):
                departure_time = _normalize_time(val)

        # Price
        if any(pk in k for pk in ("price", "fare", "amount", "cost", "lowestfare", "value")):
            try:
                p = float(str(val).replace(",", "").replace("$", "").strip())
                if 1 < p < 9999:
                    price = p
            except (ValueError, TypeError):
                pass

        # Fare class
        if any(ck in k for ck in ("class", "category", "cabin", "service", "type")):
            if isinstance(val, str):
                if "business" in val.lower():
                    fare_class = "business"
                elif "first" in val.lower():
                    fare_class = "first"

    if train_number and price:
        trains.append({
            "train_number": train_number,
            "departure_time": departure_time or "",
            "price": price,
            "fare_class": fare_class,
        })

    # Continue recursing into children
    for val in obj.values():
        trains.extend(_recursive_find_trains(val, depth + 1))

    return trains


def _extract_trains_from_dom(page: Page) -> list[dict]:
    """
    Extract trains from the rendered DOM using JavaScript.

    Mirrors the strategy from the Chrome extension's content.js:
    find .train-name elements, walk up to card boundary, extract prices.
    """
    result = page.evaluate(r"""
    () => {
        const trains = [];
        const trainEls = document.querySelectorAll('.train-name, [class*="train-name"]');

        for (const el of trainEls) {
            // Walk up to find the card container
            let card = el;
            for (let i = 0; i < 8; i++) {
                if (!card.parentElement) break;
                card = card.parentElement;
                const cls = card.className || '';
                if (cls.includes('card') || cls.includes('result') || cls.includes('journey'))
                    break;
            }

            const cardText = card.textContent || '';

            // Extract train number
            const numMatch = el.textContent.match(/(\d{1,4})/);
            if (!numMatch) continue;
            const trainNumber = numMatch[1];

            // Extract departure time
            const timeMatch = cardText.match(/(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))/);
            const depTime = timeMatch ? timeMatch[1] : '';

            // Extract prices
            const priceMatches = [...cardText.matchAll(/\$\s*([\d,]+(?:\.\d{1,2})?)/g)];
            const prices = priceMatches
                .map(m => parseFloat(m[1].replace(/,/g, '')))
                .filter(p => p > 1 && p < 10000);

            if (prices.length === 0) continue;

            // Determine fare class from context
            const isCoach = /coach/i.test(cardText);
            const isBusiness = /business/i.test(cardText);

            // Add coach price (lowest) and business price (highest) if both exist
            if (isCoach) {
                trains.push({
                    train_number: trainNumber,
                    departure_time: depTime,
                    price: Math.min(...prices),
                    fare_class: 'coach',
                });
            }
            if (isBusiness && prices.length > 1) {
                trains.push({
                    train_number: trainNumber,
                    departure_time: depTime,
                    price: Math.max(...prices),
                    fare_class: 'business',
                });
            }
            if (!isCoach && !isBusiness) {
                trains.push({
                    train_number: trainNumber,
                    departure_time: depTime,
                    price: Math.min(...prices),
                    fare_class: 'coach',
                });
            }
        }

        return trains;
    }
    """)

    return result if result else []


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _normalize_time(raw: str) -> str:
    """Normalize various time formats to HH:MM."""
    raw = raw.strip()

    # "1:05 PM" / "13:05"
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", raw, re.IGNORECASE)
    if m:
        h, mn, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        return f"{h:02d}:{mn:02d}"

    return raw


def _time_to_minutes(t: str) -> int:
    """Convert HH:MM to minutes since midnight."""
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _save_debug(page: Page, label: str) -> None:
    """Save screenshot + HTML for debugging."""
    try:
        debug_dir = Path(config.DEBUG_SCREENSHOT_DIR)
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            page.screenshot(path=str(debug_dir / f"{ts}_{label}.png"), full_page=True, timeout=10_000)
        except Exception:
            pass
        try:
            (debug_dir / f"{ts}_{label}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
    except Exception as ex:
        log.error("Could not save debug artifacts: %s", ex)
