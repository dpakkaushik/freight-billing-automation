"""Centralised loguru configuration.

Logs are written both to stdout (so `docker logs` / `journalctl` see them)
and to a rotating file under LOG_DIR.
"""
from __future__ import annotations

import sys

from loguru import logger

from app.config import settings


_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings.ensure_dirs()
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.log_level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
        ),
    )
    if not settings.log_to_file:
        # Serverless (Vercel): stdout is collected by the platform, the disk is
        # ephemeral, and the multiprocessing queue behind enqueue=True isn't available.
        _CONFIGURED = True
        logger.info("Logging initialised (level={}, stdout only)", settings.log_level)
        return
    logger.add(
        settings.log_dir / "app.log",
        level=settings.log_level.upper(),
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        enqueue=True,  # safe for multi-process
    )
    _CONFIGURED = True
    logger.info("Logging initialised (level={})", settings.log_level)
