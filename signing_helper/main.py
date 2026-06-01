"""Pallia Trans Local Signing Helper.

Runs on http://127.0.0.1:7777 and signs PDFs using the USB DSC token.
The Pallia Trans web app calls this from the user's browser via fetch.

Start: python main.py
Build as .exe: run build.bat
"""
from __future__ import annotations

import base64
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from signer import SigningError, TokenNotFound, WrongPIN, sign_pdf_bytes

PORT = 7777

app = FastAPI(title="Pallia Trans Signing Helper", docs_url=None, redoc_url=None)

# Allow any origin — the helper only exposes signing, which still requires
# the user to enter their PIN.  Restricted to localhost so the port is not
# reachable from the internet.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Models ────────────────────────────────────────────────────────────────────

class SignRequest(BaseModel):
    pdf_b64: str   # base64-encoded unsigned PDF
    pin: str       # USB token PIN


class SignResponse(BaseModel):
    signed_pdf_b64: str   # base64-encoded signed PDF


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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  Pallia Trans Signing Helper")
    print(f"  Running on http://127.0.0.1:{PORT}")
    print("  Keep this window open while signing invoices.")
    print("  Press Ctrl+C to stop.")
    print("=" * 55)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
