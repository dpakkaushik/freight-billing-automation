"""OCR service.

Wraps EasyOCR with:
  * Lazy model load (EasyOCR loads ~64 MB on first use; we only pay this once)
  * Light image preprocessing for phone-camera photos
  * A clean run_ocr(image_path) -> str interface

The interface is OCR-engine-agnostic. Swap in Google Vision or Tesseract
later by creating another module that exposes the same `run_ocr`.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import cv2
import numpy as np
from loguru import logger

from app.config import settings

if TYPE_CHECKING:  # avoid importing the heavy module at import time
    import easyocr


_reader_lock = threading.Lock()
_reader: Optional["easyocr.Reader"] = None


def _get_reader() -> "easyocr.Reader":
    """Return a process-wide EasyOCR reader, loading it on first use."""
    global _reader
    if _reader is not None:
        return _reader
    with _reader_lock:
        if _reader is None:
            import easyocr  # heavy; deferred

            logger.info(
                "Loading EasyOCR reader (langs={}, gpu={})...",
                settings.ocr_lang_list,
                settings.ocr_use_gpu,
            )
            _reader = easyocr.Reader(
                settings.ocr_lang_list,
                gpu=settings.ocr_use_gpu,
                verbose=False,
            )
            logger.info("EasyOCR reader ready.")
    return _reader


def _preprocess(image_path: Path) -> np.ndarray:
    """Preprocessing tuned for phone-camera photos of printed LR documents."""
    img = cv2.imread(str(image_path))
    if img is None:
        raise ValueError(f"Cannot read image: {image_path}")

    h, w = img.shape[:2]

    # Upscale small images — EasyOCR accuracy drops below ~1500px wide
    min_side = 1800
    if max(h, w) < min_side:
        scale = min_side / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)

    # Downscale only if extremely large (>4000px) to keep memory reasonable
    h, w = img.shape[:2]
    if max(h, w) > 4000:
        scale = 4000.0 / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Boost contrast with CLAHE (handles uneven lighting from phone flash)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    # Denoise before thresholding
    gray = cv2.fastNlMeansDenoising(gray, h=15)

    # Adaptive threshold — much better than global for printed docs under variable lighting
    gray = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=31,
        C=15,
    )

    return gray


def run_ocr(image_path: str | Path) -> str:
    """Run OCR on an image. Returns the full recognised text joined by newlines.

    Raises:
        ValueError: if the file cannot be opened as an image.
        RuntimeError: if EasyOCR itself errors.
    """
    path = Path(image_path)
    if not path.exists():
        raise ValueError(f"Image not found: {path}")

    img = _preprocess(path)
    reader = _get_reader()
    try:
        # detail=0 returns a list[str]; we join with newlines so downstream
        # extraction can use line-aware regex.
        lines = reader.readtext(img, detail=0, paragraph=False)
    except Exception as exc:  # pragma: no cover - depends on EasyOCR runtime
        logger.exception("EasyOCR failure on {}", path)
        raise RuntimeError(f"OCR failed: {exc}") from exc

    text = "\n".join(line.strip() for line in lines if line and line.strip())
    logger.debug("OCR produced {} chars from {}", len(text), path.name)
    return text
