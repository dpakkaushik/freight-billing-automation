"""PDF digital signing via USB DSC token — standalone helper module.

Adapted from app/services/pdf_signer.py for use in the local signing helper.
Entry point: sign_pdf_bytes(pdf_bytes, pin) -> signed_bytes
"""
from __future__ import annotations

import io
import tempfile
from datetime import datetime
from pathlib import Path

from loguru import logger

PKCS11_LIB = r"C:\Windows\System32\CryptoIDA_pkcs11.dll"
CERT_LABEL  = "cont_333741d242fc5a8c459f"   # update after DSC renewal
SLOT_NO     = 0

_SIG_BOX_FALLBACK = (18, 82, 275, 128)
_TS_FMT = "%Y.%m.%d %H:%M:%S +05'30'"
_IMG_W, _IMG_H = 440, 92


class TokenNotFound(Exception):
    """USB token not found or driver not loaded."""

class WrongPIN(Exception):
    """Incorrect PIN."""

class SigningError(Exception):
    """Generic signing failure."""


# ── Appearance image ──────────────────────────────────────────────────────────

def _load_font(filename: str, size: int):
    from PIL import ImageFont
    for path in [
        f"C:/Windows/Fonts/{filename}",
        f"C:/Windows/Fonts/{filename.lower()}",
        f"C:/Windows/Fonts/{filename.upper()}",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _build_appearance_image(signer_name: str, timestamp_str: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (_IMG_W, _IMG_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    font_name   = _load_font("arialbd.ttf",  48)
    font_label  = _load_font("arialbd.ttf",  13)
    font_detail = _load_font("arial.ttf",    13)
    font_script = _load_font("segoesc.ttf",  42)
    font_pawn   = _load_font("seguisym.ttf", 88)

    divider_x = _IMG_W * 40 // 100

    pawn_char = "♟"
    try:
        pb = draw.textbbox((0, 0), pawn_char, font=font_pawn)
        pw, ph = pb[2] - pb[0], pb[3] - pb[1]
    except AttributeError:
        pw, ph = 75, 80
    draw.text((_IMG_W // 2 - pw // 2, _IMG_H // 2 - ph // 2),
              pawn_char, font=font_pawn, fill=(238, 238, 238))

    try:
        bbox = draw.textbbox((0, 0), signer_name, font=font_name)
        nw, nh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        nw, nh = draw.textsize(signer_name, font=font_name)

    if nw > divider_x - 16:
        shrink = (divider_x - 16) / nw
        font_name = _load_font("arialbd.ttf", max(18, int(48 * shrink)))
        try:
            bbox = draw.textbbox((0, 0), signer_name, font=font_name)
            nw, nh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            nw, nh = draw.textsize(signer_name, font=font_name)

    nx = max(8, (divider_x - nw) // 2)
    ny = (_IMG_H - nh) // 2
    draw.text((nx, ny), signer_name, font=font_name, fill=(0, 0, 0))

    initial = signer_name[0] if signer_name else "A"
    try:
        ib = draw.textbbox((0, 0), initial, font=font_script)
        iw, ih = ib[2] - ib[0], ib[3] - ib[1]
    except AttributeError:
        iw, ih = 30, 40
    draw.text((nx + nw - iw // 2, ny + nh - ih // 4),
              initial, font=font_script, fill=(200, 45, 55))

    draw.line([(divider_x, 10), (divider_x, _IMG_H - 10)],
              fill=(160, 160, 160), width=1)

    rx  = divider_x + 12
    lh  = 20
    top = (_IMG_H - lh * 4) // 2
    parts     = timestamp_str.split(" ", 1)
    date_part = parts[0]
    time_part = parts[1] if len(parts) > 1 else ""

    draw.text((rx, top),          "Digitally signed by", font=font_label,  fill=(30, 30, 30))
    draw.text((rx, top + lh),     signer_name,           font=font_detail, fill=(0,  0,  0))
    draw.text((rx, top + lh * 2), f"Date: {date_part}",  font=font_detail, fill=(0,  0,  0))
    draw.text((rx, top + lh * 3), time_part,             font=font_detail, fill=(0,  0,  0))

    return img


# ── Signature-zone detection ──────────────────────────────────────────────────

def _find_signature_box(pdf_path: Path) -> tuple[float, float, float, float]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        page = reader.pages[0]
        page_width = float(page.mediabox.width)
        found: dict[str, float] = {}

        def _visit(text: str, cm, tm, font_dict, font_size):
            y = (cm[5] if cm else 0.0) + tm[5]
            t = text.strip()
            if not t:
                return
            if "For Pallia" in t and "for_pallia" not in found:
                found["for_pallia"] = y
            if ("Authorised" in t or "Authorized" in t) and "auth_sig" not in found:
                found["auth_sig"] = y

        page.extract_text(visitor_text=_visit)

        if "for_pallia" in found and "auth_sig" in found:
            y_top    = max(found["for_pallia"], found["auth_sig"])
            y_bottom = min(found["for_pallia"], found["auth_sig"])
            gap      = y_top - y_bottom
            if gap >= 30:
                margin = 4.0
                x2  = page_width * 0.46
                return (18.0, y_bottom + margin, x2, y_top - margin)
    except Exception as exc:
        logger.warning("Sig-zone auto-detection failed ({}); using fallback.", exc)

    return _SIG_BOX_FALLBACK


# ── CN extraction ─────────────────────────────────────────────────────────────

def _extract_cn(cert) -> str:
    try:
        for rdn in cert.subject.chosen:
            for attr in rdn:
                if attr["type"].native == "common_name":
                    val = str(attr["value"].native).strip()
                    return val.split("(")[0].strip()
    except Exception:
        pass
    try:
        hf = cert.subject.human_friendly
        for part in hf.split(","):
            part = part.strip()
            if part.lower().startswith("common name"):
                return part.split(":", 1)[-1].strip()
    except Exception:
        pass
    return "Authorised Signatory"


# ── Public API ────────────────────────────────────────────────────────────────

def sign_pdf_bytes(pdf_bytes: bytes, pin: str) -> bytes:
    """Sign PDF bytes using the USB DSC token. Returns signed PDF bytes.

    Raises:
        TokenNotFound: USB token not connected.
        WrongPIN: Incorrect PIN.
        SigningError: Any other failure.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "input.pdf"
        tmp_path.write_bytes(pdf_bytes)

        try:
            from pyhanko.sign import signers
            from pyhanko.sign.fields import SigFieldSpec
            from pyhanko.sign.pkcs11 import PKCS11Signer, open_pkcs11_session
            from pyhanko.sign.signers.pdf_signer import PdfSigner
            from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
            from pyhanko.pdf_utils.images import PdfImage
            from pyhanko.stamp import StaticStampStyle
        except ImportError as exc:
            raise SigningError("pyhanko not installed.") from exc

        try:
            session_ctx = open_pkcs11_session(
                lib_location=PKCS11_LIB,
                slot_no=SLOT_NO,
                user_pin=pin,
            )
        except Exception as exc:
            err = str(exc)
            if any(k in err for k in ("CKR_TOKEN_NOT_PRESENT", "CKR_SLOT_ID_INVALID",
                                       "No module", "CKR_GENERAL_ERROR")):
                raise TokenNotFound("USB token not found. Please plug in the DSC pendrive.") from exc
            if any(k in err for k in ("CKR_PIN_INCORRECT", "CKR_PIN_LOCKED")):
                raise WrongPIN("Incorrect PIN. Please try again.") from exc
            raise SigningError(f"Could not open token session: {exc}") from exc

        try:
            with session_ctx as session:
                cert_label_to_use: str | None = CERT_LABEL
                try:
                    cms_signer = PKCS11Signer(pkcs11_session=session, cert_label=cert_label_to_use)
                    _ = cms_signer.signing_cert
                except Exception as probe_exc:
                    if "Could not find certificate" in str(probe_exc):
                        cert_label_to_use = None
                        cms_signer = PKCS11Signer(pkcs11_session=session, cert_label=None)
                    else:
                        raise

                try:
                    signer_name = _extract_cn(cms_signer.signing_cert)
                except Exception:
                    signer_name = "Authorised Signatory"

                timestamp_str    = datetime.now().strftime(_TS_FMT)
                appearance_img   = _build_appearance_image(signer_name, timestamp_str)
                stamp_style      = StaticStampStyle(
                    background=PdfImage(appearance_img),
                    border_width=0,
                    background_opacity=1.0,
                )
                sig_box = _find_signature_box(tmp_path)

                pdf_signer_obj = PdfSigner(
                    signature_meta=signers.PdfSignatureMetadata(field_name="AuthorisedSignatory"),
                    signer=cms_signer,
                    stamp_style=stamp_style,
                    new_field_spec=SigFieldSpec(
                        sig_field_name="AuthorisedSignatory",
                        on_page=0,
                        box=sig_box,
                    ),
                )

                with open(tmp_path, "rb") as f:
                    writer = IncrementalPdfFileWriter(f)
                    sig_result = pdf_signer_obj.sign_pdf(writer)
                    return sig_result.getvalue()

        except (TokenNotFound, WrongPIN, SigningError):
            raise
        except Exception as exc:
            err = str(exc)
            if any(k in err for k in ("CKR_PIN_INCORRECT", "CKR_PIN_LOCKED")):
                raise WrongPIN("Incorrect PIN. Please try again.") from exc
            if "CKR_TOKEN_NOT_PRESENT" in err:
                raise TokenNotFound("USB token not found. Please plug in the DSC pendrive.") from exc
            raise SigningError(f"Signing failed: {exc}") from exc
