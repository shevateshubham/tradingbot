"""
telegram_interface/session_store.py — Per-user session state.
Tracks market, trade type, last run time, and paused state.
Backed by JSON files in data/sessions/.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path(__file__).parent.parent / "data" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _path(chat_id: int | str) -> Path:
    return SESSIONS_DIR / f"{chat_id}.json"


def load_session(chat_id: int | str) -> dict:
    p = _path(chat_id)
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "chat_id":        str(chat_id),
        "last_market":    None,
        "trade_type":     None,
        "last_run":       None,
        "paused":         False,
        "run_count":      0,
        "created_at":     datetime.now(timezone.utc).isoformat(),
    }


def save_session(chat_id: int | str, session: dict) -> None:
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(_path(chat_id), "w") as f:
        json.dump(session, f, indent=2)


def update_last_run(chat_id: int | str, market: str, trade_type: str | None = None) -> None:
    s = load_session(chat_id)
    s["last_market"] = market
    s["last_run"]    = datetime.now(timezone.utc).isoformat()
    s["run_count"]   = s.get("run_count", 0) + 1
    if trade_type:
        s["trade_type"] = trade_type
    save_session(chat_id, s)


def set_paused(chat_id: int | str, paused: bool) -> None:
    s = load_session(chat_id)
    s["paused"] = paused
    save_session(chat_id, s)


def is_paused(chat_id: int | str) -> bool:
    return load_session(chat_id).get("paused", False)


def get_last_market(chat_id: int | str) -> str | None:
    return load_session(chat_id).get("last_market")


def get_trade_type(chat_id: int | str) -> str | None:
    return load_session(chat_id).get("trade_type")


def set_trade_type(chat_id: int | str, trade_type: str) -> None:
    s = load_session(chat_id)
    s["trade_type"] = trade_type
    save_session(chat_id, s)
