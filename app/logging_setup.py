from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def configure_logging(project_root: Path, level_name: str, keep_days: int) -> logging.Logger:
    logs_directory = project_root / "logs"
    logs_directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("arcobaleno")
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = TimedRotatingFileHandler(
        filename=logs_directory / "arcobaleno.log",
        when="midnight",
        backupCount=keep_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
