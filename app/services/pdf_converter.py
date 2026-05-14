"""Convert .xlsx to .pdf using LibreOffice in headless mode.

We use `soffice --headless --convert-to pdf` rather than depending on
Microsoft Excel, so the same code runs on the user's Windows PC and a
headless Linux VM.

LibreOffice install paths we probe:

  Windows  C:\\Program Files\\LibreOffice\\program\\soffice.exe
           C:\\Program Files (x86)\\LibreOffice\\program\\soffice.exe
  Linux    /usr/bin/soffice  /usr/bin/libreoffice
  macOS    /Applications/LibreOffice.app/Contents/MacOS/soffice
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from loguru import logger


class LibreOfficeNotFound(RuntimeError):
    """Raised when no soffice / libreoffice binary can be located."""


_KNOWN_WINDOWS_PATHS = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]

_KNOWN_UNIX_PATHS = [
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]


def find_libreoffice() -> str:
    """Return a path to a runnable soffice binary, or raise LibreOfficeNotFound."""
    # Honour an explicit override first
    override = os.environ.get("LIBREOFFICE_PATH")
    if override and Path(override).exists():
        return override

    # PATH lookup
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found

    # Platform-specific known locations
    candidates = _KNOWN_WINDOWS_PATHS if sys.platform.startswith("win") else _KNOWN_UNIX_PATHS
    for c in candidates:
        if Path(c).exists():
            return c

    raise LibreOfficeNotFound(
        "Could not find LibreOffice. Install it from https://www.libreoffice.org/ "
        "or set LIBREOFFICE_PATH in .env to the soffice executable."
    )


def xlsx_to_pdf(xlsx_path: Path, output_dir: Path | None = None, timeout: int = 90) -> Path:
    """Convert an .xlsx file to .pdf in the same directory (or `output_dir`).

    Returns the resulting PDF path. Raises RuntimeError on conversion failure.
    """
    xlsx_path = Path(xlsx_path).resolve()
    if not xlsx_path.exists():
        raise FileNotFoundError(f"xlsx not found: {xlsx_path}")
    out_dir = (output_dir or xlsx_path.parent).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    soffice = find_libreoffice()

    # LibreOffice insists on its own user profile dir; if two conversions
    # run in parallel with the same profile they conflict. Use a temp
    # profile per invocation.
    with tempfile.TemporaryDirectory(prefix="lo-profile-") as profile_dir:
        cmd = [
            soffice,
            "--headless",
            "--norestore",
            "--nologo",
            f"-env:UserInstallation=file:///{profile_dir.replace(os.sep, '/')}",
            "--convert-to", "pdf",
            "--outdir", str(out_dir),
            str(xlsx_path),
        ]
        logger.info("Running LibreOffice: {}", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"LibreOffice timed out after {timeout}s") from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"LibreOffice failed (exit {result.returncode}):\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    pdf_path = out_dir / (xlsx_path.stem + ".pdf")
    if not pdf_path.exists():
        raise RuntimeError(
            f"LibreOffice returned success but PDF not found at {pdf_path}. "
            f"stdout: {result.stdout}"
        )
    logger.info("Converted to PDF: {}", pdf_path)
    return pdf_path
