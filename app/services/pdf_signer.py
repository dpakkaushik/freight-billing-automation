"""PDF digital signing via USB DSC token (CryptoID / PKCS#11).

Visual appearance replicates Adobe Acrobat's two-column signature box:
  Left  column — signer name in large bold font
  Right column — "Digitally signed by / Date:" details (same as Adobe)

Raises:
    TokenNotFound  — USB token is not connected / driver not loaded
    WrongPIN       — incorrect PIN entered
    SigningError   — any other signing failure
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from loguru import logger

PKCS11_LIB = r"C:\Windows\System32\CryptoIDA_pkcs11.dll"
CERT_LABEL  = "cont_333741d242fc5a8c459f"
SLOT_NO     = 0

# Signature box on the invoice PDF (PDF points, origin bottom-left of page).
# (x1, y1, x2, y2) — bottom-left origin.  Placed in the Authorised Signatory zone.
SIG_BOX = (18, 82, 275, 128)   # measured: For Pallia y=144.8-153.1, Auth Signatory y=90.8-109.7

# Timestamp format matching Adobe's display: "2026.05.13 14:50:06 +05'30'"
_TS_FMT = "%Y.%m.%d %H:%M:%S +05'30'"

# Rendered image size for the stamp.  Aspect ratio matches SIG_BOX (205 × 93 pts ≈ 2.2:1).
_IMG_W, _IMG_H = 440, 92   # pixels — matches box aspect ratio 223×46 pts ≈ 4.8:1


class TokenNotFound(Exception):
    """USB token not found or driver not loaded."""

class WrongPIN(Exception):
    """Incorrect PIN."""

class SigningError(Exception):
    """Generic signing failure."""


# ── Appearance image ──────────────────────────────────────────────────────────

def _load_font(filename: str, size: int):
    """Load a Windows TrueType font by name; fall back to PIL default."""
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
    """Return a PIL Image matching the Adobe two-column DSC style with pawn watermark."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (_IMG_W, _IMG_H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    font_name   = _load_font("arialbd.ttf",  48)
    font_label  = _load_font("arialbd.ttf",  13)
    font_detail = _load_font("arial.ttf",    13)
    font_script = _load_font("segoesc.ttf",  42)   # red cursive flourish
    font_pawn   = _load_font("seguisym.ttf", 88)   # chess pawn watermark

    divider_x = _IMG_W * 40 // 100

    # ── Light pawn watermark centred behind everything ────────────────────────
    pawn_char = "♟"
    try:
        pb = draw.textbbox((0, 0), pawn_char, font=font_pawn)
        pw, ph = pb[2] - pb[0], pb[3] - pb[1]
    except AttributeError:
        pw, ph = 75, 80
    draw.text((_IMG_W // 2 - pw // 2, _IMG_H // 2 - ph // 2),
              pawn_char, font=font_pawn, fill=(238, 238, 238))

    # ── Left column: large signer name ───────────────────────────────────────
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

    # ── Red cursive initial — signature flourish ──────────────────────────────
    initial = signer_name[0] if signer_name else "A"
    try:
        ib = draw.textbbox((0, 0), initial, font=font_script)
        iw, ih = ib[2] - ib[0], ib[3] - ib[1]
    except AttributeError:
        iw, ih = 30, 40
    draw.text((nx + nw - iw // 2, ny + nh - ih // 4),
              initial, font=font_script, fill=(200, 45, 55))

    # ── Thin vertical divider ─────────────────────────────────────────────────
    draw.line([(divider_x, 10), (divider_x, _IMG_H - 10)],
              fill=(160, 160, 160), width=1)

    # ── Right column: label / name / date split over two lines ───────────────
    rx  = divider_x + 12
    lh  = 20
    top = (_IMG_H - lh * 4) // 2

    # Split "2026.05.13 14:50:06 +05'30'" into date and time parts
    parts     = timestamp_str.split(" ", 1)
    date_part = parts[0]
    time_part = parts[1] if len(parts) > 1 else ""

    draw.text((rx, top),          "Digitally signed by", font=font_label,  fill=(30, 30, 30))
    draw.text((rx, top + lh),     signer_name,           font=font_detail, fill=(0,  0,  0))
    draw.text((rx, top + lh * 2), f"Date: {date_part}",  font=font_detail, fill=(0,  0,  0))
    draw.text((rx, top + lh * 3), time_part,             font=font_detail, fill=(0,  0,  0))

    return img


# ── CN extraction from certificate ───────────────────────────────────────────

def _extract_cn(cert) -> str:
    """Extract the Common Name from an asn1crypto x509 Certificate."""
    try:
        for rdn in cert.subject.chosen:
            for attr in rdn:
                if attr["type"].native == "common_name":
                    val = str(attr["value"].native).strip()
                    # Some DSC certs have format "VINOD (VINOD PALLIATRANS)" — keep first part
                    return val.split("(")[0].strip()
    except Exception:
        pass
    try:
        # Fallback: asn1crypto subject.human_friendly → "Common Name: VINOD, ..."
        hf = cert.subject.human_friendly
        for part in hf.split(","):
            part = part.strip()
            if part.lower().startswith("common name"):
                return part.split(":", 1)[-1].strip()
    except Exception:
        pass
    return "Authorised Signatory"


# ── Main signing function ─────────────────────────────────────────────────────

def sign_invoice_pdf(pdf_path: Path, pin: str) -> Path:
    """Sign *pdf_path* with the USB DSC token and return the signed PDF path.

    The signed file is saved as  <original_stem>_signed.pdf  in the same folder.
    Calling this a second time overwrites the previous signed copy.
    """
    if not pdf_path.exists():
        raise SigningError(f"PDF not found: {pdf_path}")

    signed_path = pdf_path.parent / f"{pdf_path.stem}_signed.pdf"

    try:
        from pyhanko.sign import signers
        from pyhanko.sign.fields import SigFieldSpec
        from pyhanko.sign.pkcs11 import PKCS11Signer, open_pkcs11_session
        from pyhanko.sign.signers.pdf_signer import PdfSigner
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        from pyhanko.pdf_utils.images import PdfImage
        from pyhanko.stamp import StaticStampStyle
    except ImportError as exc:
        raise SigningError("pyhanko not installed. Run: pip install pyhanko[pkcs11]") from exc

    logger.info("Signing PDF {} with token slot={}", pdf_path.name, SLOT_NO)

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
            cms_signer = PKCS11Signer(
                pkcs11_session=session,
                cert_label=CERT_LABEL,
            )

            # Read signer name from the DSC certificate for the visual appearance
            try:
                signer_name = _extract_cn(cms_signer.signing_cert)
            except Exception:
                signer_name = "Authorised Signatory"
            logger.info("DSC signer name: {}", signer_name)

            timestamp_str = datetime.now().strftime(_TS_FMT)

            # Build the Adobe-like two-column appearance image
            appearance_img   = _build_appearance_image(signer_name, timestamp_str)
            stamp_style = StaticStampStyle(
                background=PdfImage(appearance_img),
                border_width=0,
                background_opacity=1.0,
            )

            pdf_signer_obj = PdfSigner(
                signature_meta=signers.PdfSignatureMetadata(
                    field_name="AuthorisedSignatory",
                ),
                signer=cms_signer,
                stamp_style=stamp_style,
                new_field_spec=SigFieldSpec(
                    sig_field_name="AuthorisedSignatory",
                    on_page=0,
                    box=SIG_BOX,
                ),
            )

            with open(pdf_path, "rb") as f:
                writer = IncrementalPdfFileWriter(f)
                sig_result = pdf_signer_obj.sign_pdf(writer)
                signed_bytes = sig_result.getvalue()

    except (TokenNotFound, WrongPIN, SigningError):
        raise
    except Exception as exc:
        err = str(exc)
        if any(k in err for k in ("CKR_PIN_INCORRECT", "CKR_PIN_LOCKED")):
            raise WrongPIN("Incorrect PIN. Please try again.") from exc
        if "CKR_TOKEN_NOT_PRESENT" in err:
            raise TokenNotFound("USB token not found. Please plug in the DSC pendrive.") from exc
        raise SigningError(f"Signing failed: {exc}") from exc

    signed_path.write_bytes(signed_bytes)
    logger.info("Signed PDF saved: {}", signed_path)
    return signed_path
