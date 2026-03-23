# ================================================================
#  logger_setup.py — Centralized Logging
# ================================================================

import os
import logging
import logging.handlers
from datetime import datetime

def setup_logger(name: str = "warroom") -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    os.makedirs("data", exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(name)-18s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.handlers.TimedRotatingFileHandler(
        f"logs/warroom_{datetime.now().strftime('%Y%m%d')}.log",
        when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f"warroom.{module}")
