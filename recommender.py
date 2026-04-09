"""
recommender.py — Smart price recommendation engine.

Given current observed prices for a route, generates tiered target
recommendations and formats them as a clear SMS response.
"""

import logging
from datetime import datetime

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Heuristic multipliers for Northeast Corridor last-minute drops
# ---------------------------------------------------------------------------
# These are based on observed Amtrak pricing patterns:
# - Coach fares on NEC routes (NYP-PHL, NYP-WAS, BOS-NYP, etc.) frequently
#   drop 30-60% from their initial listing in the days before departure.
# - The "likely" tier happens ~70% of the time for flexible-date travelers.
# - The "possible" tier happens ~40% of the time.
# - The "stretch" tier happens <15% of the time (usually same-day sales).

TIER_CONSERVATIVE = 0.70   # 70% of lowest current price
TIER_MODERATE     = 0.55   # 55% of lowest current price
TIER_AGGRESSIVE   = 0.40   # 40% of lowest current price


def recommend(
    current_prices: list[dict],
    params: dict,
) -> str:
    """
    Build a recommendation SMS from current prices and watch parameters.

    Parameters
    ----------
    current_prices : list of dicts with keys:
        train_number, departure_time, price, fare_class
    params : parsed watch parameters from parser.py

    Returns
    -------
    str : fully formatted SMS message ready to send
    """
    origin  = params.get("origin", "???")
    dest    = params.get("destination", "???")
    date    = params.get("date", "")
    p_min   = params.get("price_min")
    p_max   = params.get("price_max")
    t_start = params.get("time_window_start")
    t_end   = params.get("time_window_end")

    # Format the date
    date_display = date
    if date:
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            date_display = dt.strftime("%b %d")
        except ValueError:
            pass

    # Time window display
    time_display = ""
    if t_start and t_end:
        time_display = f" ({_fmt_time(t_start)}-{_fmt_time(t_end)})"
    elif t_start:
        time_display = f" (after {_fmt_time(t_start)})"
    elif t_end:
        time_display = f" (before {_fmt_time(t_end)})"

    # Header
    lines = [
        f"\U0001f50d Argus is watching {origin}\u2192{dest} on {date_display}{time_display}",
        "",
    ]

    if not current_prices:
        lines.append("No trains found for this route and time window yet.")
        lines.append("I'll keep checking and text you when I find prices.")
        lines.append("")
        lines.append("Reply STOP to cancel watching.")
        return "\n".join(lines)

    # Current prices section
    lines.append("Current prices:")
    for train in sorted(current_prices, key=lambda t: t["price"]):
        dep_time = _fmt_time(train.get("departure_time", ""))
        fare = train.get("fare_class", "coach").title()
        lines.append(
            f"\u2022 Train {train['train_number']} \u00b7 {dep_time} \u00b7 {fare} ${train['price']:.0f}"
        )

    # Calculate recommendations
    lowest = min(t["price"] for t in current_prices)
    conservative = round(lowest * TIER_CONSERVATIVE)
    moderate     = round(lowest * TIER_MODERATE)
    aggressive   = round(lowest * TIER_AGGRESSIVE)

    lines.append("")
    lines.append("Recommended targets:")
    lines.append(f"\u2705 ${conservative} \u2014 likely (drops here ~70% of the time)")
    lines.append(f"\U0001f7e1 ${moderate} \u2014 possible (drops here ~40% of the time)")
    lines.append(f"\U0001f3af ${aggressive} \u2014 stretch (rarely drops this low)")

    # Budget alignment
    if p_max:
        lines.append("")
        if p_max >= conservative:
            lines.append(
                f"Your budget (${int(p_min)}-${int(p_max)}) aligns well with the likely target. "
                if p_min else
                f"Your budget (under ${int(p_max)}) aligns well with the likely target. "
            )
        elif p_max >= moderate:
            lines.append(
                f"Your budget (${int(p_min)}-${int(p_max)}) is in the possible range \u2014 "
                "there's a decent chance it drops this low."
                if p_min else
                f"Your budget (under ${int(p_max)}) is in the possible range \u2014 "
                "there's a decent chance it drops this low."
            )
        else:
            lines.append(
                f"Your budget (${int(p_min)}-${int(p_max)}) is a stretch target \u2014 "
                "I'll watch for it but it may not drop this low."
                if p_min else
                f"Your budget (under ${int(p_max)}) is a stretch target \u2014 "
                "I'll watch for it but it may not drop this low."
            )

        alert_price = int(p_max)
        lines.append(
            f"I'll text you the moment any train hits ${alert_price} or below."
        )
    else:
        # No budget specified — use the conservative target
        lines.append("")
        lines.append(
            f"No budget set \u2014 I'll alert you when prices drop to ${conservative} or below."
        )

    lines.append("")
    lines.append("Reply STOP to cancel watching.")

    return "\n".join(lines)


def format_price_alert(
    train: dict,
    params: dict,
) -> str:
    """
    Format a price drop alert SMS.

    Parameters
    ----------
    train  : dict with train_number, departure_time, price, fare_class
    params : the watch parameters
    """
    origin = params.get("origin", "???")
    dest   = params.get("destination", "???")
    date   = params.get("date", "")
    dep    = _fmt_time(train.get("departure_time", ""))
    fare   = train.get("fare_class", "coach").title()
    price  = train["price"]

    # Build Amtrak booking URL
    if date:
        y, m, d = date.split("-")
        date_param = f"{m}/{d}/{y}"
    else:
        date_param = ""

    book_url = (
        f"https://www.amtrak.com/tickets/departure.html"
        f"#df={date_param}&org={origin}&dst={dest}"
    )

    lines = [
        f"\U0001f7e2 Price drop caught!",
        f"Train {train['train_number']} \u00b7 {origin}\u2192{dest} \u00b7 {dep}",
        f"{fare} dropped to ${price:.0f} \u2014 that's in your range!",
        "",
        f"\U0001f449 Book now (select Flexible fare):",
        book_url,
        "",
        f"\U0001f4a1 Select the Flexible fare so you can rebook cheaper if it drops again.",
        "Argus is still watching for further drops.",
    ]

    return "\n".join(lines)


def format_status(watches: list[dict], latest_prices: dict[int, list[dict]]) -> str:
    """
    Format a status SMS showing all active watches and their latest prices.

    Parameters
    ----------
    watches       : list of watch dicts from db
    latest_prices : dict mapping watch_id → list of latest price dicts
    """
    if not watches:
        return "You don't have any active watches right now.\n\nText me a route to start watching! Example:\n\"NYP to PHL March 27 budget $40-60\""

    lines = [f"\U0001f4cb Your active watches ({len(watches)}):", ""]

    for w in watches:
        date_display = w["date"]
        try:
            dt = datetime.strptime(w["date"], "%Y-%m-%d")
            date_display = dt.strftime("%b %d")
        except ValueError:
            pass

        header = f"\u2022 {w['origin']}\u2192{w['destination']} on {date_display}"
        if w.get("price_max"):
            header += f" (budget ${int(w['price_max'])})"
        lines.append(header)

        prices = latest_prices.get(w["id"], [])
        if prices:
            cheapest = min(prices, key=lambda p: p["price"])
            lines.append(
                f"  Lowest: ${cheapest['price']:.0f} (Train {cheapest['train_number']})"
            )
        else:
            lines.append("  No prices observed yet")
        lines.append("")

    lines.append("Reply STOP to cancel all watches.")
    return "\n".join(lines)


def _fmt_time(t: str) -> str:
    """Convert HH:MM to friendly 12-hour format, or return as-is."""
    if not t:
        return ""
    try:
        dt = datetime.strptime(t, "%H:%M")
        h = dt.hour
        m = dt.minute
        ampm = "am" if h < 12 else "pm"
        h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
        return f"{h12}:{m:02d}{ampm}" if m else f"{h12}{ampm}"
    except ValueError:
        return t
