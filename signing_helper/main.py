"""Pallia Trans Local Signing Helper.

Runs silently as a Windows system tray app on http://127.0.0.1:7777.
Auto-registers in Windows startup on first launch (no admin rights needed).

Right-click the tray icon to Stop.
"""
from __future__ import annotations

import base64
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from loguru import logger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from signer import SigningError, TokenNotFound, WrongPIN, sign_pdf_bytes

PORT = 7777
VERSION = "1.1.0"   # 1.1: accepts sig_box (EEE-Taxi browser-side signing)

# The .exe runs without a console, so problems go to a log file next to the
# user's local app data: %LOCALAPPDATA%\PalliaSignHelper\helper.log
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PalliaSignHelper"


def _setup_logging() -> None:
    # PyInstaller --noconsole leaves sys.stdout / sys.stderr as None, which
    # breaks anything that calls .isatty() or .write() on them (uvicorn's
    # default log formatter does). Point them at /dev/null instead.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger.add(LOG_DIR / "helper.log", rotation="2 MB", retention=3, level="INFO",
                   enqueue=True, backtrace=False, diagnose=False)
    except Exception:
        pass    # logging must never stop the helper from starting

app = FastAPI(title="Pallia Trans Signing Helper", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── API models ────────────────────────────────────────────────────────────────

class SignRequest(BaseModel):
    pdf_b64: str
    pin: str
    # Optional (x1, y1, x2, y2) in PDF points; EEE-Taxi sends this so the
    # stamp lands in its invoice footer instead of being auto-detected.
    sig_box: Optional[list[float]] = None


class SignResponse(BaseModel):
    signed_pdf_b64: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Pallia Trans Signing Helper",
        "port": PORT,
        "version": VERSION,
        "supports_sig_box": True,
    }


@app.post("/sign", response_model=SignResponse)
def sign(body: SignRequest):
    try:
        pdf_bytes = base64.b64decode(body.pdf_b64)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid base64 PDF data."})

    try:
        signed_bytes = sign_pdf_bytes(pdf_bytes, body.pin, sig_box=body.sig_box)
    except TokenNotFound as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except WrongPIN as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})
    except SigningError as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return SignResponse(signed_pdf_b64=base64.b64encode(signed_bytes).decode())


# ── Windows startup registration ──────────────────────────────────────────────

def _register_startup() -> None:
    """Add this .exe to HKCU startup so it launches on every Windows login.

    Uses HKCU (current user) — no administrator rights required.
    Safe to call on every launch; just overwrites the same key.
    """
    try:
        import winreg
        exe_path = sys.executable          # correct path when frozen by PyInstaller
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "PalliaSignHelper", 0, winreg.REG_SZ, f'"{exe_path}"')
        winreg.CloseKey(key)
    except Exception:
        pass    # non-fatal — user can start manually if registry fails


# ── Tray icon ─────────────────────────────────────────────────────────────────

def _make_icon_image():
    """Purple circle with white 'P' — matches the app's accent colour."""
    from PIL import Image, ImageDraw, ImageFont
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.ellipse([2, 2, size - 2, size - 2], fill=(124, 106, 242, 255))

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 36)
    except Exception:
        font = ImageFont.load_default()

    try:
        bb = draw.textbbox((0, 0), "P", font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
    except AttributeError:
        tw, th = 20, 28

    draw.text(((size - tw) // 2, (size - th) // 2 - 2), "P",
              font=font, fill=(255, 255, 255, 255))
    return img


def _run_server() -> None:
    try:
        logger.info("Signing helper v{} listening on http://127.0.0.1:{}", VERSION, PORT)
        # log_config=None: skip uvicorn's dictConfig, which needs a console.
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", log_config=None)
    except Exception:
        # A silent dead server thread is the worst failure mode for a tray app.
        logger.exception("HTTP server failed to start (is port {} already in use?)", PORT)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pystray

    _setup_logging()

    # Register in Windows startup (idempotent)
    _register_startup()

    # Start HTTP server in a background daemon thread
    threading.Thread(target=_run_server, daemon=True).start()

    # System tray icon
    def on_quit(icon, _item):
        icon.stop()
        sys.exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Pallia Trans Helper  ✓ Running", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Stop", on_quit),
    )

    icon = pystray.Icon(
        "PalliaTransHelper",
        _make_icon_image(),
        "Pallia Trans Signing Helper",
        menu,
    )
    icon.run()   # blocks; uvicorn thread keeps serving in background
