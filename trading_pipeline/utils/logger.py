"""
utils/logger.py — Structured logging with rotating file + stdout.
"""

import json
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

RUNS_LOG = Path(__file__).parent.parent / "logs" / "runs.jsonl"

_loggers: dict[str, logging.Logger] = {}


def get_logger(name: str) -> logging.Logger:
    """Return a named logger with rotating file + stdout handlers."""
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # Rotating file: 10 MB, 5 backups
        fh = RotatingFileHandler(
            LOG_DIR / "pipeline.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)

        # Stdout for Railway logs
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)

        logger.addHandler(fh)
        logger.addHandler(sh)
        logger.propagate = False

    _loggers[name] = logger
    return logger


def log_pipeline_run(run_id: str, summary: dict) -> None:
    """Append a JSON line to logs/runs.jsonl for weekly review parsing."""
    record = {
        "run_id": run_id,
        "timestamp": datetime.utcnow().isoformat(),
        **summary,
    }
    try:
        with open(RUNS_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
