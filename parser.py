"""
parser.py — Natural language parser using Claude API.

Converts free-form SMS messages into structured watch parameters.
"""

import json
import logging
from datetime import datetime

import anthropic

import config

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a travel assistant that extracts Amtrak booking parameters from natural language messages.

Extract origin, destination, date, time window, price range, and fare class.

Use standard Amtrak station codes:
- NYP = New York Penn Station
- PHL = Philadelphia 30th Street
- WAS = Washington Union Station
- BOS = Boston South Station
- PVD = Providence
- NHV = New Haven
- STM = Stamford
- WIL = Wilmington
- BAL = Baltimore Penn
- NWK = Newark Penn
- TRE = Trenton
- ALB = Albany-Rensselaer
- CHI = Chicago Union Station

For dates, use YYYY-MM-DD format. Today's date is {today}.
For times, use HH:MM in 24-hour format.
For fare class, default to "coach" unless the user mentions business or first class.

Return ONLY valid JSON with these exact keys (no markdown, no explanation):
{{
    "origin": "station code or null",
    "destination": "station code or null",
    "date": "YYYY-MM-DD or null",
    "time_window_start": "HH:MM or null",
    "time_window_end": "HH:MM or null",
    "price_min": number or null,
    "price_max": number or null,
    "fare_class": "coach" or "business" or null
}}

If a parameter is missing or unclear, return null for that key.
If the user says "tomorrow", "next Monday", etc., calculate the actual date.
If the user gives a budget like "$40-60", set price_min=40 and price_max=60.
If the user says "under $50", set price_min=null and price_max=50.
If the user says "around $50", set price_min=40 and price_max=60 (±20%)."""


def parse_message(message: str) -> dict | None:
    """
    Parse a natural language message into structured watch parameters.

    Returns a dict with the extracted parameters, or None if parsing fails.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    system = _SYSTEM_PROMPT.format(today=today)

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": message}],
        )

        raw = response.content[0].text.strip()
        log.info("Claude raw response: %s", raw)

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        parsed = json.loads(raw)
        log.info("Parsed parameters: %s", parsed)
        return parsed

    except json.JSONDecodeError as e:
        log.error("Failed to parse Claude response as JSON: %s", e)
        return None
    except anthropic.APIError as e:
        log.error("Claude API error: %s", e)
        return None
    except Exception as e:
        log.error("Unexpected parser error: %s", e)
        return None


def validate_params(params: dict) -> tuple[bool, str]:
    """
    Check that the minimum required fields are present.

    Returns (is_valid, error_message).
    """
    if not params:
        return False, "I couldn't understand that. Could you try again with your route, date, and budget?"

    missing = []
    if not params.get("origin"):
        missing.append("origin station")
    if not params.get("destination"):
        missing.append("destination station")
    if not params.get("date"):
        missing.append("travel date")

    if missing:
        return False, f"I need a bit more info \u2014 I'm missing: {', '.join(missing)}. Could you include those?"

    return True, ""


def format_confirmation(params: dict) -> str:
    """Format the parsed parameters as a human-readable confirmation."""
    origin = params.get("origin", "?")
    dest   = params.get("destination", "?")
    date   = params.get("date", "?")

    # Format date nicely
    date_display = date
    if date and date != "?":
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            date_display = dt.strftime("%b %d")
        except ValueError:
            pass

    parts = [f"{origin}\u2192{dest} on {date_display}"]

    # Time window
    t_start = params.get("time_window_start")
    t_end   = params.get("time_window_end")
    if t_start and t_end:
        parts.append(f"between {_format_time(t_start)} and {_format_time(t_end)}")
    elif t_start:
        parts.append(f"after {_format_time(t_start)}")
    elif t_end:
        parts.append(f"before {_format_time(t_end)}")

    # Budget
    p_min = params.get("price_min")
    p_max = params.get("price_max")
    if p_min and p_max:
        parts.append(f"budget ${int(p_min)}-${int(p_max)}")
    elif p_max:
        parts.append(f"under ${int(p_max)}")
    elif p_min:
        parts.append(f"above ${int(p_min)}")

    # Fare class
    fare = params.get("fare_class", "coach")
    if fare and fare != "coach":
        parts.append(f"{fare} class")

    return " \u00b7 ".join(parts)


def _format_time(t: str) -> str:
    """Convert HH:MM to a friendly 12-hour format."""
    try:
        dt = datetime.strptime(t, "%H:%M")
        return dt.strftime("%-I:%M%p").lower()
    except ValueError:
        try:
            dt = datetime.strptime(t, "%H:%M")
            return dt.strftime("%I:%M%p").lower().lstrip("0")
        except ValueError:
            return t
