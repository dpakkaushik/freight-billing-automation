"""Pallia Trans Local Signing Helper.

Runs silently as a Windows system tray app on http://127.0.0.1:7777.
Auto-registers in Windows startup on first launch (no admin rights needed).

Right-click the tray icon to Stop.
"""
from __future__ import annotations

import base64
import sys
import threading

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from signer import SigningError, TokenNotFound, WrongPIN, sign_pdf_bytes

PORT = 7777

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


class SignResponse(BaseModel):
    signed_pdf_b64: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "Pallia Trans Signing Helper", "port": PORT}


@app.post("/sign", response_model=SignResponse)
def sign(body: SignRequest):
    try:
        pdf_bytes = base64.b64decode(body.pdf_b64)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid base64 PDF data."})

    try:
        signed_bytes = sign_pdf_bytes(pdf_bytes, body.pin)
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
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pystray

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
