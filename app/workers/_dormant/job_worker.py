"""Dormant worker module.

Kept so future code can revive the portal-submission worker without
restructuring. See app/services/tms_automation.py for the Playwright
skeleton this worker will drive once we wire up TMS submission.
"""
from __future__ import annotations

from loguru import logger


async def start_workers() -> None:  # pragma: no cover - dormant
    logger.debug("Worker module dormant (no portal submission yet).")


async def stop_workers() -> None:  # pragma: no cover - dormant
    pass
