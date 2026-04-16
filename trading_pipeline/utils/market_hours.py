"""
utils/market_hours.py — Market session timing checks.
Ported and generalized from mcp_server/tools/smc_processor.py.
"""

from datetime import datetime
import pytz

from utils.config_loader import load_config


def is_market_open(market: str, config: dict | None = None) -> bool:
    """
    Return True if the given market is currently open for trading.
    Reads session_times from config.
    """
    cfg = config or load_config()
    session = cfg.get("session_times", {}).get(market.upper())

    if not session:
        return True  # Unknown market: assume open

    tz = pytz.timezone(session.get("timezone", "UTC"))
    now = datetime.now(tz)

    # Check weekday
    allowed_days = session.get("days", list(range(7)))
    if now.weekday() not in allowed_days:
        return False

    open_str  = session.get("open",  "00:00")
    close_str = session.get("close", "23:59")

    open_h,  open_m  = map(int, open_str.split(":"))
    close_h, close_m = map(int, close_str.split(":"))

    now_mins   = now.hour * 60 + now.minute
    open_mins  = open_h * 60 + open_m
    close_mins = close_h * 60 + close_m

    # Handle overnight markets (e.g. GOLD: opens 18:00, closes 17:00 next day)
    if close_mins < open_mins:
        return now_mins >= open_mins or now_mins <= close_mins
    return open_mins <= now_mins <= close_mins


def get_open_markets(config: dict | None = None) -> list[str]:
    """Return list of market names that are currently open."""
    cfg = config or load_config()
    return [m for m in cfg.get("markets", []) if is_market_open(m, cfg)]


def get_closed_markets_message(market: str, config: dict | None = None) -> str:
    """Human-readable message about when a market opens."""
    cfg = config or load_config()
    session = cfg.get("session_times", {}).get(market.upper())
    if not session:
        return f"{market} session info not configured."

    tz_name = session.get("timezone", "UTC")
    open_str = session.get("open", "00:00")
    days = session.get("days", list(range(7)))
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_str = ", ".join(day_names[d] for d in days)
    return (
        f"{market} is currently closed.\n"
        f"Opens: {open_str} {tz_name} on {day_str}.\n"
        f"Available 24/7 markets: CRYPTO, FOREX"
    )


def is_nifty_open() -> bool:
    """Convenience wrapper — Indian equity session."""
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= mins <= (15 * 60 + 30)


def get_session_label() -> str:
    """Return current active trading session label for logging."""
    utc_now = datetime.now(pytz.utc)
    h = utc_now.hour
    if 2 <= h < 8:
        return "ASIA"
    if 8 <= h < 13:
        return "LONDON"
    if 13 <= h < 21:
        return "NY"
    return "OFF_HOURS"
