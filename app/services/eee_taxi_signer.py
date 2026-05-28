"""Digital signing for EEE-Taxi invoices via mToken USB (PKCS#11).

Differences from pdf_signer.py (Pallia Trans):
  - Uses mToken.dll instead of CryptoIDA_pkcs11.dll
  - cert_label=None — auto-discovers the first available cert on the token
  - Signature box anchored to "EEE-TAXI" and "Authorised Signatory" text
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from loguru import logger

MTOKEN_PKCS11_LIB = r"C:\Windows\System32\CryptoIDA_pkcs11.dll"
SLOT_NO = 0

# EEE-Taxi footer: right column (Authorised Signatory), ~60% across the page.
# A4 = 595 pts wide, margin = 10 mm ≈ 28 pts each side, content = 539 pts.
# Right col: x1 ≈ 28 + 539*0.6 = 351, x2 ≈ 567.
_SIG_BOX_FALLBACK = (351, 310, 567, 395)
_TS_FMT = "%Y.%m.%d %H:%M:%S +05'30'"
_IMG_W, _IMG_H = 440, 92


class TokenNotFound(Exception):
    """USB mToken not found or driver not loaded."""

class WrongPIN(Exception):
    """Incorrect PIN."""

class SigningError(Exception):
    """Generic signing failure."""


def _load_font(filename: str, size: int):
    from PIL import ImageFont
    for path in [
        f"C:/Windows/Fonts/{filename}",
        f"C:/Windows/Fonts/{filename.lower()}",
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

    font_name   = _load_font("arialbd.ttf", 48)
    font_label  = _load_font("arialbd.ttf", 13)
    font_detail = _load_font("arial.ttf",   13)
    font_script = _load_font("segoesc.ttf", 42)
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
        nw, nh = draw.textsize(signer_name, font=font_name)  # type: ignore[attr-defined]

    if nw > divider_x - 16:
        shrink = (divider_x - 16) / nw
        font_name = _load_font("arialbd.ttf", max(18, int(48 * shrink)))
        try:
            bbox = draw.textbbox((0, 0), signer_name, font=font_name)
            nw, nh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            nw, nh = draw.textsize(signer_name, font=font_name)  # type: ignore[attr-defined]

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


def _find_signature_box(pdf_path: Path) -> tuple[float, float, float, float]:
    """Locate the signature zone in the EEE-Taxi invoice footer.

    Finds 'for EEE-TAXI...' (top of sig zone) and 'Authorised Signatory' (bottom)
    in the right column and places the box between them.
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        page = reader.pages[0]
        page_width = float(page.mediabox.width)

        right_col_x_min = page_width * 0.55  # right column starts at ~60%
        auth_ys: list[float] = []
        eee_ys:  list[float] = []

        def _visit(text: str, cm, tm, font_dict, font_size):
            x = (cm[4] if cm else 0.0) + tm[4]
            y = (cm[5] if cm else 0.0) + tm[5]
            t = text.strip()
            if not t or x <= right_col_x_min:
                return
            if "for EEE" in t or "EEE-TAXI MOBILITY" in t:
                eee_ys.append(y)
            if "Authorised" in t or "Authorized" in t:
                auth_ys.append(y)

        page.extract_text(visitor_text=_visit)

        x1 = page_width * 0.60
        x2 = page_width - 28.0

        if auth_ys and eee_ys:
            auth_y = max(auth_ys)  # baseline of "Authorised Signatory" (lower on page = smaller y)
            eee_y  = min(eee_ys)   # baseline of "for EEE-TAXI..." in right col (higher = larger y)
            if eee_y > auth_y:
                box = (x1, auth_y + 8, x2, eee_y - 2)
                logger.info("EEE-Taxi sig box (bracketed): {}", tuple(round(v, 1) for v in box))
                return box

        if auth_ys:
            auth_y = max(auth_ys)
            box = (x1, auth_y + 8, x2, auth_y + 46)
            logger.info("EEE-Taxi sig box (auth fallback): {}", tuple(round(v, 1) for v in box))
            return box

    except Exception as exc:
        logger.warning("EEE-Taxi sig-zone detection failed ({}); using fallback.", exc)

    return _SIG_BOX_FALLBACK


def _extract_cn(cert) -> str:
    try:
        for rdn in cert.subject.chosen:
            for attr in rdn:
                if attr["type"].native == "common_name":
                    return str(attr["value"].native).strip().split("(")[0].strip()
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


def sign_eee_taxi_pdf(
    pdf_path: Path,
    pin: str,
    sig_box: tuple[float, float, float, float] | None = None,
) -> Path:
    """Sign *pdf_path* with the mToken USB DSC and return the signed PDF path.

    *sig_box* is (x1, y1, x2, y2) in PDF user-space coords captured at generation
    time by _SigCapture.  Falls back to _find_signature_box() if not supplied.
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

    logger.info("Signing EEE-Taxi PDF {} with mToken", pdf_path.name)

    try:
        session_ctx = open_pkcs11_session(
            lib_location=MTOKEN_PKCS11_LIB,
            slot_no=SLOT_NO,
            user_pin=pin,
        )
    except Exception as exc:
        err = str(exc)
        if any(k in err for k in ("CKR_TOKEN_NOT_PRESENT", "CKR_SLOT_ID_INVALID",
                                   "No module", "CKR_GENERAL_ERROR", "cannot load")):
            raise TokenNotFound(
                "mToken USB not found. Please plug in the EEE-Taxi DSC token."
            ) from exc
        if any(k in err for k in ("CKR_PIN_INCORRECT", "CKR_PIN_LOCKED")):
            raise WrongPIN("Incorrect PIN. Please try again.") from exc
        raise SigningError(f"Could not open mToken session: {exc}") from exc

    try:
        with session_ctx as session:
            cms_signer = PKCS11Signer(pkcs11_session=session, cert_label=None)
            try:
                _ = cms_signer.signing_cert
            except Exception as probe_exc:
                raise SigningError(
                    f"Could not find certificate on mToken: {probe_exc}"
                ) from probe_exc

            try:
                signer_name = _extract_cn(cms_signer.signing_cert)
            except Exception:
                signer_name = "Authorised Signatory"
            logger.info("EEE-Taxi DSC signer: {}", signer_name)

            timestamp_str  = datetime.now().strftime(_TS_FMT)
            appearance_img = _build_appearance_image(signer_name, timestamp_str)
            stamp_style    = StaticStampStyle(
                background=PdfImage(appearance_img),
                border_width=0,
                background_opacity=1.0,
            )
            if sig_box is None:
                sig_box = _find_signature_box(pdf_path)
            logger.info("Using sig_box: {}", tuple(round(v, 1) for v in sig_box))

            pdf_signer_obj = PdfSigner(
                signature_meta=signers.PdfSignatureMetadata(
                    field_name="AuthorisedSignatory",
                ),
                signer=cms_signer,
                stamp_style=stamp_style,
                new_field_spec=SigFieldSpec(
                    sig_field_name="AuthorisedSignatory",
                    on_page=0,
                    box=sig_box,
                ),
            )

            with open(pdf_path, "rb") as f:
                writer       = IncrementalPdfFileWriter(f)
                sig_result   = pdf_signer_obj.sign_pdf(writer)
                signed_bytes = sig_result.getvalue()

    except (TokenNotFound, WrongPIN, SigningError):
        raise
    except Exception as exc:
        err = str(exc)
        if any(k in err for k in ("CKR_PIN_INCORRECT", "CKR_PIN_LOCKED")):
            raise WrongPIN("Incorrect PIN. Please try again.") from exc
        if "CKR_TOKEN_NOT_PRESENT" in err:
            raise TokenNotFound("mToken USB not found.") from exc
        raise SigningError(f"Signing failed: {exc}") from exc

    signed_path.write_bytes(signed_bytes)
    logger.info("Signed PDF saved: {}", signed_path)
    return signed_path


def sign_eee_taxi_pdf_dummy(
    pdf_path: Path,
    sig_box: tuple[float, float, float, float] | None = None,
) -> Path:
    """Overlay a visible 'DUMMY SIGNATURE' stamp at the same position as the real DSC.

    No USB token or PIN needed.  For testing the PDF layout and batch flow.
    Remove or disable this function before production use.
    """
    from io import BytesIO
    from datetime import datetime

    try:
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.colors import Color
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise SigningError(f"reportlab or pypdf not installed: {exc}") from exc

    if not pdf_path.exists():
        raise SigningError(f"PDF not found: {pdf_path}")

    if sig_box is None:
        sig_box = _find_signature_box(pdf_path)

    x1, y1, x2, y2 = sig_box
    w  = x2 - x1
    h  = y2 - y1
    cx = x1 + w / 2
    cy = y1 + h / 2

    # Build a transparent overlay page — only the stamp rectangle is painted.
    buf = BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)

    c.setFillColor(Color(0.95, 0.93, 0.93))          # light red-tinted fill
    c.setStrokeColor(Color(0.55, 0.10, 0.10))         # dark-red border
    c.setLineWidth(0.8)
    c.rect(x1, y1, w, h, fill=1, stroke=1)

    font_main = max(7.0, min(h * 0.30, 13.0))
    font_sub  = max(5.5, min(h * 0.18, 8.5))

    c.setFillColor(Color(0.50, 0.08, 0.08))
    c.setFont("Helvetica-Bold", font_main)
    c.drawCentredString(cx, cy + font_main * 0.30, "DUMMY SIGNATURE")

    c.setFillColor(Color(0.40, 0.40, 0.40))
    c.setFont("Helvetica", font_sub)
    c.drawCentredString(cx, cy - font_main * 0.55,
                        f"Test only  •  {datetime.now().strftime('%Y-%m-%d')}")
    c.save()
    buf.seek(0)

    # Merge stamp onto page 0 of the original PDF.
    reader  = PdfReader(str(pdf_path))
    writer  = PdfWriter()
    overlay = PdfReader(buf)

    page = reader.pages[0]
    page.merge_page(overlay.pages[0])
    writer.add_page(page)
    for p in reader.pages[1:]:
        writer.add_page(p)

    signed_path = pdf_path.parent / f"{pdf_path.stem}_signed.pdf"
    with open(str(signed_path), "wb") as f:
        writer.write(f)

    logger.info("Dummy-signed PDF saved: {}", signed_path)
    return signed_path
