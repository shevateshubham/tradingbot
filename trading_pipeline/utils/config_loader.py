"""
utils/config_loader.py — Atomic JSON config read/write with hot-reload.
"""

import json
import os
import shutil
import time
from pathlib import Path
from datetime import datetime

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
HISTORY_DIR = Path(__file__).parent.parent / "data" / "config_history"

_CONFIG: dict = {}
_CONFIG_MTIME: float = 0.0


def load_config() -> dict:
    """Load config.json with file-mtime-based hot-reload cache."""
    global _CONFIG, _CONFIG_MTIME
    try:
        mtime = CONFIG_PATH.stat().st_mtime
        if mtime != _CONFIG_MTIME:
            with open(CONFIG_PATH, "r") as f:
                _CONFIG = json.load(f)
            _CONFIG_MTIME = mtime
    except Exception:
        pass  # Return last good config on parse error
    return _CONFIG


def save_config(config: dict) -> None:
    """Atomically write config.json, backing up the previous version."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup = HISTORY_DIR / f"{timestamp}.json"
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, backup)

    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_PATH)

    # Clear cache
    global _CONFIG, _CONFIG_MTIME
    _CONFIG = config
    _CONFIG_MTIME = CONFIG_PATH.stat().st_mtime


def get_scoring_weights(config: dict | None = None) -> dict:
    cfg = config or load_config()
    return cfg.get("scoring", {})


def get_active_symbols(config: dict | None = None) -> dict:
    """Return symbols dict with disabled_symbols filtered out."""
    cfg = config or load_config()
    disabled = set(cfg.get("disabled_symbols", []))
    result = {}
    for market, symbols in cfg.get("symbols", {}).items():
        active = [s for s in symbols if s not in disabled]
        if active:
            result[market] = active
    return result


def get_symbols_for_market(market: str, config: dict | None = None) -> list[str]:
    """Return active symbols for a single market."""
    active = get_active_symbols(config)
    if market.upper() == "ALL":
        return [sym for syms in active.values() for sym in syms]
    return active.get(market.upper(), [])


def get_market_for_symbol(symbol: str, config: dict | None = None) -> str | None:
    """Reverse-lookup: which market does this symbol belong to?"""
    cfg = config or load_config()
    for market, symbols in cfg.get("symbols", {}).items():
        if symbol in symbols:
            return market
    return None
