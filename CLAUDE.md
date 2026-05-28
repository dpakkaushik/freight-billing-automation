# Pallia Trans Billing Automation Bot

This file briefs Claude (in VSCode / Claude Code) on the state, decisions, and conventions of this project. Read it fully before making any change.

---

## What this project does

Python-first FastAPI app that automates billing-document data entry for **Pallia Trans Logistics Private Limited**. The flow:

1. User opens **Billing** tab → starts a new Invoice draft (auto-suggested suffix, today's date).
2. User uploads one or more **LR (Lorry Receipt / Goods Consignment Note)** images. Each LR is an image of a Mahindra Logistics consignment note.
3. Bot extracts structured fields from each LR (currently EasyOCR + regex — being upgraded to Ollama vision).
4. User confirms / edits fields per LR. **Each Delivery Order Number on the LR becomes its own row** in the final Excel, with its own Total Amount.
5. User clicks "Generate Excel + PDF". The bot fills a Mahindra-Gujarat Excel template and converts it to PDF via LibreOffice headless.
6. User downloads the PDF (or future: submits it to the Pallia TMS portal — that button is currently disabled).

The user's broader goal is also an **LLM agent that talks to the TMS database** for natural-language queries. That's a separate future track — the current project is a foundation for it.

---

## Current state (as of handoff)

**Working end-to-end:**
- Tabbed UI (Billing / Tracking / Accounts / Others) — only Billing is implemented; the other tabs are placeholders.
- New-invoice draft creation with auto-suggested suffix (looks at last `GENERATED` invoice, increments).
- LR image upload, runs OCR + regex extraction inline.
- User confirmation screen with editable fields and dynamic Delivery Order rows.
- Excel template fill (openpyxl) — verified against the real template, multi-row + multi-LR works.
- LibreOffice headless PDF conversion.
- Indian-format amount-in-words (num2words, en_IN locale) replaces the template's external `=[1]!SpellCurr(L41)` macro.
- Backup of all generated files under `data/uploads/invoices/<invoice_id>/`.

**Stubbed / disabled:**
- "Submit to TMS Portal" button — returns 501. The Playwright skeleton in `app/services/tms_automation.py` has TODO selectors waiting for a `playwright codegen` session on the real portal.
- Tracking / Accounts / Others tabs.

**Known weak spots:**
- **OCR + regex extraction is fragile.** EasyOCR mangles digits as letters (O/U/Q→0, I/L→1), reads "G.C.N." as "GChNo", drops leading digits ("7412304508" → "12304508"). Current regexes are tolerant but the user has explicitly said this approach is too brittle and wants vision-LLM extraction instead. See **"Next priority"** below.
- Handwritten POD delivery date is intentionally left blank for the user to type.
- "To" city (destination) is intentionally left blank — OCR doesn't preserve the To-label adjacency on this LR layout.

---

## Next priority (per user, not yet built)

**Replace the OCR+regex extractor with an Ollama vision-LLM call.** User chose **Ollama + Llama 3.2 Vision 11B** for fully offline, $0, local extraction.

Required work:
1. Add `app/services/ollama_extractor.py` — POST to `http://localhost:11434/api/generate` with `format: "json"`, base64-encoded image, and a prompt asking for the canonical field schema (see below).
2. In `app/api/billing.py::upload_lr`, call Ollama first. On `OllamaUnavailable` (connection refused / model not pulled), fall back to existing EasyOCR + regex path so the bot still functions if Ollama isn't running.
3. Add config knobs: `OLLAMA_BASE_URL` (default `http://localhost:11434`), `OLLAMA_MODEL` (default `llama3.2-vision`), `EXTRACTION_ENGINE` (`ollama` | `easyocr`, default `ollama`).
4. Add Ollama install instructions to README:
   - Install from https://ollama.com/download (Windows installer)
   - `ollama pull llama3.2-vision` (~6 GB download)
   - Service auto-runs at localhost:11434
5. The expected JSON schema from the model should match `LRConfirmedFields` in `app/schemas.py`:
   ```json
   {
     "vehicle_no": "NL01AK0496",
     "gcn_no": "112032145",
     "gcn_date": "07-Apr-2026",
     "from_city": "Rajkot",
     "destination": "Gabhana",
     "delivery_date": "15/04/2026",
     "qty": "9 Units",
     "do_rows": [
       {"delivery_order_no": "7412304508", "total_amount": ""},
       {"delivery_order_no": "7412304561", "total_amount": ""}
     ]
   }
   ```

After Ollama vision works, the longer-term direction is a **tool-using agent** (Claude Agent SDK or LangGraph) that orchestrates extract → validate → fill → convert → optionally portal-submit, with the LLM as the controller. User is open to this once Level 1 (vision extraction) is proven.

---

## Architecture

```
app/
├── main.py                       FastAPI app, lifespan, mounts /api/billing + static UI
├── config.py                     Pydantic-settings, reads .env
├── database.py                   SQLAlchemy engine, session, Base
├── models.py                     Invoice + LR ORM models (replaced old Job model)
├── schemas.py                    Pydantic request/response schemas
├── api/
│   ├── billing.py                Invoice + LR endpoints (the only live router)
│   ├── upload.py, jobs.py        Deprecated stubs, kept so legacy imports don't break
├── services/
│   ├── ocr.py                    EasyOCR wrapper (lazy-loaded)
│   ├── extraction.py             Regex extractor — OCR-tolerant, to be superseded by Ollama
│   ├── excel_filler.py           openpyxl fill of the Swaraj Gujarat template
│   ├── pdf_converter.py          LibreOffice headless subprocess
│   ├── amount_words.py           Indian-format Rupees-in-words (num2words en_IN)
│   ├── invoice_numbering.py      Suggest next invoice suffix
│   ├── queue.py                  Dormant; reserved for future TMS portal submission
│   ├── tms_automation.py         Playwright skeleton; selectors are TODO
├── workers/
│   └── job_worker.py             Dormant; future portal-submission worker
├── templates/
│   └── swaraj_invoice_gujarat.xlsx   Mahindra-Gujarat invoice template (the source of truth)
├── static/
│   └── index.html                Single-page tabbed UI
├── utils/logging_config.py       loguru setup
data/                             Runtime artifacts (SQLite DB, uploaded images, generated files) — gitignored
logs/                             Rotating logs — gitignored
tests/test_extraction.py         Regex tests; not pytest-compatible without deps; useful as reference
```

The async worker (`app/workers/job_worker.py`) and queue (`app/services/queue.py`) are intentionally dormant. They were built for the original async portal-submission flow and will come back when "Submit to TMS Portal" is enabled.

---

## Excel template — cell map (critical reference)

Template: `app/templates/swaraj_invoice_gujarat.xlsx`
Sheet name: **`Gujarat sawraj`** (note the typo in the original — do NOT correct it).

**Header cells the bot writes:**

| Cell  | Value                                                  |
|-------|--------------------------------------------------------|
| I14   | `Invoice No : PTLM-2627SWM-{suffix}`                   |
| I15   | `Invoice Date : DD/MM/YYYY`                            |

**Data rows (one per Delivery Order No., starting row 27, max row 38 — 12 rows max):**

| Col   | Header                  | Source                                                       |
|-------|-------------------------|--------------------------------------------------------------|
| B     | S.No.                   | Already in template, do not touch                            |
| C     | Invoice No.             | Delivery Order No. from LR                                   |
| D     | LR No.                  | G.C.N. No. from LR                                           |
| E     | LR Date                 | G.C.N. Date from LR                                          |
| F     | Vehicle No.             | Vehicle No. from LR                                          |
| G     | Place of Destination    | "To" city from LR's BA-code box                              |
| H     | Delivery Date           | Hand-written POD date — user types in                        |
| I     | Qty.                    | "9 Units" style — kept as text                               |
| J     | Rate Per Tractor        | **Literal text `"Fixed"`** (merged with K)                   |
| L     | Total Amount            | User input per DO row                                        |

**Template-driven cells (don't touch except L39):**

- `L39` = template ships with `=+L27`. We REPLACE this with `=SUM(L27:L{last_used_row})` when multi-row.
- `L40` = `=+L39*18%` (IGST)
- `L41` = `=+L39+L40` (Grand total)
- `B41` = template formula `=[1]!SpellCurr(L41)` references external macro that LibreOffice cannot resolve. We **overwrite** this cell with the Python-computed Indian-format amount-in-words.

**Merged ranges to be aware of** (openpyxl writes to top-left of merge): `J27:K27`, `J28:K28`, …, `J38:K38` for "Rate Per Tractor" rows; `B25:B26` and other header cells span rows 25–26.

---

## Data model

```
Invoice (id, suffix, invoice_date, status, excel_path, pdf_path, error_message, created_at, updated_at)
  └── LR (id, invoice_id, status, image_path, ocr_text, extracted_fields, confirmed_fields, ...)
        confirmed_fields shape:
          {
            "vehicle_no", "gcn_no", "gcn_date",
            "from_city", "destination",
            "delivery_date", "qty",
            "do_rows": [{"delivery_order_no", "total_amount"}, ...]
          }
```

`InvoiceStatus`: draft | generated | submitted | failed
`LRStatus`: uploaded | ocr_processing | awaiting_confirmation | confirmed | failed

Cascade-delete is on Invoice → LR. Deleting an Invoice removes all its LRs and their image files.

---

## Run it locally (Windows)

User's machine: Windows 11, Python 3.12.10 via `py -3.12`, project in `C:\Users\Admin\Documents\Claude\Projects\Biling Automation Bot`.

```powershell
cd "C:\Users\Admin\Documents\Claude\Projects\Biling Automation Bot"
# First-time setup:
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
copy .env.example .env

# Every run:
.\.venv\Scripts\python.exe run.py
```

LibreOffice required for PDF generation: install from libreoffice.org. Bot auto-finds `C:\Program Files\LibreOffice\program\soffice.exe`.

To wipe state and start fresh: `Remove-Item data\app.db` (and optionally `Remove-Item -Recurse data\uploads`).

---

## User context & preferences

**Who they are:** Pallia Trans Logistics ops team. Not a software developer day-to-day, but capable of running commands and following step-by-step instructions. Uses Windows. Has a Claude account.

**Strong preferences (do not violate):**

- **Local-first, then VM.** User explicitly said: build and verify on their PC first, only then deploy to a GCP VM. Don't bundle deployment work (Docker, docker-compose, systemd, GCP) until they ask for it.
- **Python-first.** Most business logic in Python; UI stays lightweight (vanilla HTML + JS, no React/build step).
- **Step-by-step verification.** When something doesn't work, give one concrete next step and ask them to paste output. Don't dump a wall of options.
- **Cost-conscious about cloud services.** They chose Ollama specifically to avoid recurring API costs. Default to free/local when proposing solutions.

**Things they want next (in their words):**

1. Vision-LLM extraction (Ollama + Llama 3.2 Vision) — most important next step.
2. A real "AI agent" workflow eventually — LLM as orchestrator, tools for extract/fill/convert/submit. They're open to Level 2 once Level 1 works.
3. LLM-over-DB agent (separate future track, not started).

---

## Gotchas & non-obvious decisions

1. **Sheet name has a typo (`Gujarat sawraj`).** Don't "fix" it — the template is user-owned and renaming the sheet would break callers.

2. **DO Rows are 1:1 with Excel rows.** If a single LR has 3 Delivery Order Numbers, that LR contributes 3 rows to the Excel. All other fields (vehicle, GCN, etc.) are duplicated across those rows. Total Amount is **per DO row** — the user decided this explicitly. Don't change to "one total per LR" without re-asking.

3. **Invoice suffix auto-numbering only considers `GENERATED` invoices.** Drafts that get abandoned don't bump the counter — `app/services/invoice_numbering.py`.

4. **B41 amount-in-words is computed on the **grand total** (L41 = L39 + 18% IGST), not the pre-tax subtotal.** This matches what the user expects.

5. **The template's `L39 = =+L27` only covers row 27.** `excel_filler.fill_invoice()` rewrites this to `=SUM(L27:L{last_used_row})` when more than one row is used.

6. **OCR-error tolerance is hard-coded in `_normalize_plate()`.** It only fixes O/U/Q→0 and I/L→1 in *known digit positions* (chars 2–3 and last 4) of the vehicle plate. Don't blanket-replace — would corrupt the letter portions.

7. **Playwright TMS automation is intentionally a skeleton.** Filling in real selectors requires `playwright codegen` on the live portal with valid credentials. The user will do that interactively when they're ready — don't try to guess selectors from the public URL.

8. **The async worker and queue are dormant by design.** They aren't imported by `main.py`. Don't start them up "just in case" — they'll re-enter the picture only when TMS portal submission is enabled.

9. **`api/upload.py` and `api/jobs.py` are deprecated stubs.** Don't import them. The live router is `api/billing.py`.

10. **EasyOCR's first run downloads ~64 MB of model weights** into `~/.EasyOCR/model/`. This is normal; mention it to the user if they see a 30-second pause on the first upload.

11. **Gemini model is `gemini-2.5-flash-lite` in `.env`.** Free tier quotas (as of mid-2026): `gemini-2.5-flash-lite` = 1,000 RPD (best for free use), `gemini-2.5-flash` = 250 RPD, `gemini-2.5-pro` = 0 free, `gemini-2.0-flash` = removed from free tier, `gemini-1.5-flash` = 404 on v1beta API. If extraction fails with 429, the daily quota is exhausted — it resets at **midnight Pacific Time (PT) = 12:30 PM IST** (in summer/PDT). NOT midnight UTC. The `.env` line must read: `GEMINI_MODEL=gemini-2.5-flash-lite`.

---

## How to verify any change

Minimum smoke test before declaring done:

1. `pip install -r requirements.txt` (or just the new pkg) in the venv — succeeds.
2. `python run.py` — server boots, prints "Application startup complete".
3. http://localhost:8000/ loads. The Billing tab is active by default.
4. Create new invoice → upload `LR1.png` (use the sample from the user's earlier chat or any Mahindra LR) → verify fields extract.
5. Confirm fields → Generate Excel + PDF → open the PDF and verify:
   - Row 27/28 layout matches cell map above
   - L39, L40, L41 totals correct
   - B41 has Indian Rupees-in-words
   - Header I14, I15 strings correct

If extraction doesn't work, the `"Show raw OCR text"` toggle in the LR card exposes whatever the extractor produced — used heavily during debugging.

---

## Open issues / things the user has flagged

- Truncated DO numbers on noisy LR scans (e.g. `7412304508` → `12304508`). Will be solved by switching to vision-LLM.
- "To" city not extracted; user types manually. Will be solved by vision-LLM.
- Handwritten POD date never extracted; user types manually. Vision-LLM may catch some, not all handwriting.

---

## Conventions

- **No emojis in code or commit messages** unless explicitly asked.
- **Conservative file changes** — prefer `Edit` over `Write` for existing files.
- **Don't add Docker / docker-compose / GCP artifacts** until the user asks. They are deliberately deferred per the "local-first" preference.
- **When proposing changes, default to one clear option** with the trade-off explained, not a multi-choice menu, unless the choice is genuinely meaningful.
- **Frontend stays vanilla HTML/CSS/JS** in a single `static/index.html`. No build tools, no bundlers.

---

## Useful commands reference

```powershell
# Run the app
.\.venv\Scripts\python.exe run.py

# Reset state
Remove-Item data\app.db
Remove-Item -Recurse data\uploads

# Install Playwright browsers (if you ever wipe .venv)
.\.venv\Scripts\python.exe -m playwright install chromium

# Quick OCR sanity check on a single image
.\.venv\Scripts\python.exe -c "from app.services.ocr import run_ocr; print(run_ocr(r'path\to\image.png'))"

# Quick extraction sanity check
.\.venv\Scripts\python.exe -c "from app.services.extraction import extract_lr_fields; from app.services.ocr import run_ocr; print(extract_lr_fields(run_ocr(r'path\to\image.png')))"
```

---

## When in doubt

- Read this file first. If something here contradicts what you'd guess from the code, this file wins — it captures decisions that aren't visible in code alone.
- Ask the user one targeted question rather than guessing. They prefer step-by-step verification.
- Keep the Billing flow working end-to-end at every commit. Don't refactor large surfaces without verifying the LR → Excel → PDF path still produces a correct PDF.
