from __future__ import annotations

import os
from pathlib import Path

import logging

DEFAULT_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DEFAULT_LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / os.getenv("LOG_FILENAME", "debug.log")

_configured = False


def configure_logging(
        *,
        level: str | int = DEFAULT_LOG_LEVEL,
        log_file: str | os.PathLike[str] | None = DEFAULT_LOG_FILE,
) -> None:
    global _configured
    if _configured:
        return

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path))

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=False,
    )
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


__all__ = ["DEFAULT_LOG_DIR", "DEFAULT_LOG_FILE", "DEFAULT_LOG_LEVEL", "configure_logging", "get_logger"]
