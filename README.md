# Automation Bot for Document Data Entry

A Python-first FastAPI application that:

1. Accepts an uploaded document image
2. Runs OCR (EasyOCR) and extracts structured fields
3. Shows extracted fields to the user for confirm / edit / reject
4. After confirmation, queues a Playwright job that logs into the Pallia
   TMS portal and submits the record
5. Handles multiple documents FIFO without dropping queued jobs across
   restarts

The codebase is structured so it can later swap SQLite -> PostgreSQL
and asyncio-queue -> Celery+Redis with no API changes.

---

## Project layout

```
.
+-- app/
|   +-- main.py                 FastAPI entrypoint + lifespan
|   +-- config.py               Pydantic settings (loads .env)
|   +-- database.py             SQLAlchemy engine / session
|   +-- models.py               Job ORM model (8 status states)
|   +-- schemas.py              Pydantic request/response models
|   +-- api/
|   |   +-- upload.py           POST /api/upload (OCR + extraction)
|   |   +-- jobs.py             GET/POST /api/jobs/...
|   +-- services/
|   |   +-- ocr.py              EasyOCR wrapper (lazy-loaded)
|   |   +-- extraction.py       Regex field extractor
|   |   +-- queue.py            Restart-safe FIFO queue
|   |   +-- tms_automation.py   Playwright skeleton (TODO selectors)
|   +-- workers/
|   |   +-- job_worker.py       Background async worker(s)
|   +-- utils/
|   |   +-- logging_config.py   loguru setup
|   +-- static/
|       +-- index.html          Single-page UI
+-- tests/
|   +-- test_extraction.py
+-- requirements.txt
+-- .env.example
+-- run.py                       Dev entrypoint
```

---

## Run it locally (Windows / Mac / Linux)

### 0. Prerequisites
- Python 3.10 or 3.11 (EasyOCR + numpy wheels are best on these)
- ~2 GB free disk for the EasyOCR model
- Git (optional)

### 1. Set up a virtual environment
```powershell
cd "C:\Users\Admin\Documents\Claude\Projects\Biling Automation Bot"
python -m venv .venv
.\.venv\Scripts\activate           # PowerShell on Windows
# or:  source .venv/bin/activate   # macOS / Linux
```

### 2. Install Python dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```
The first install pulls torch (~700 MB) because EasyOCR depends on it.
This is the only heavy download.

### 3. Install Playwright browsers
```bash
python -m playwright install chromium
```

### 4. Create your `.env`
```bash
copy .env.example .env             # Windows
# or:  cp .env.example .env        # macOS / Linux
```
Open `.env` and at minimum set:
- `TMS_USERNAME` and `TMS_PASSWORD` (only used once selectors are filled in)
- Leave the rest as defaults for now

### 5. Run the app
```bash
python run.py
```

You should see:
```
INFO     Logging initialised (level=INFO)
INFO     Starting Automation Bot v0.1.0
INFO     Loading EasyOCR reader ...   (first time only, ~30s)
INFO     EasyOCR reader ready.
INFO     Worker #1 started.
INFO     Uvicorn running on http://0.0.0.0:8000
```

Open http://localhost:8000/ in your browser, drop an image, and you'll
see the extracted fields render in the UI.

### 6. Try the OCR + extraction without the portal
After uploading, the job will sit in **awaiting_confirmation**. When
you click **Confirm**, the worker will try to run Playwright. Because
the TMS selectors are still stubbed, the job will move to **failed**
with a clear `NotImplementedError` in the error_message — this is
expected at this stage.

---

## Next step: fill in the real TMS selectors

Once the local pipeline is verified end-to-end, we'll record the real
portal flow:

```bash
python -m playwright codegen https://pallia.tmslive.in/
```

Playwright launches Chromium, you log in and walk through one entry,
and it prints the exact `locator(...)` calls. Paste those into the
TODO blocks in `app/services/tms_automation.py`:
- `_login(page)`
- `_navigate_to_form(page)`
- `_fill_form(page, fields)`
- `_save(page)`

Once filled in, set `TMS_HEADLESS=false` and `TMS_SLOW_MO_MS=300` in
`.env` while debugging so you can watch the browser drive itself; then
flip back to `TMS_HEADLESS=true` once it's working.

---

## Run the test
```bash
pytest -q
```
Currently covers field extraction — pure regex, no heavy dependencies.

---

## API reference (auto-generated)

With the server running, open http://localhost:8000/docs for an
interactive Swagger UI.

Key endpoints:
- `POST /api/upload` — multipart form, field `file`. Runs OCR
  synchronously and returns the new Job.
- `GET  /api/jobs` — list recent jobs.
- `GET  /api/jobs/{id}` — single job detail.
- `POST /api/jobs/{id}/confirm` — body `{"fields": {...}}`. Optionally
  edit fields before queueing; missing body uses extracted fields as-is.
- `POST /api/jobs/{id}/reject` — body `{"reason": "..."}`.

---

## Architecture (at a glance)

```
   User uploads image
          v
   /api/upload  ----- OCR (EasyOCR) ----- field extraction
          v
   Job row: status = awaiting_confirmation
          v
   User clicks Confirm in UI
          v
   /api/jobs/{id}/confirm  ---->  asyncio.Queue (FIFO)
                                       v
                              background worker(s)
                                       v
                         Playwright -> Pallia TMS portal
                                       v
                          Job row: success | failed
```

Key properties:
- The user can keep uploading while a job is processing — uploads are
  fully synchronous (OCR is fast), and only the portal submission goes
  through the worker.
- Worker concurrency is configurable (`WORKER_CONCURRENCY` in `.env`).
  Default `1` = strict FIFO.
- On restart, any job that was `confirmed`, `in_queue`, or `processing`
  is re-enqueued automatically.

---

## Future (after local verification)

These are intentionally deferred:
- **Docker / docker-compose** — once the local flow works end-to-end
  we'll add a `Dockerfile` so it runs the same on the GCP VM.
- **GCP VM deployment** — `gcloud compute instances create ...` + systemd
  unit for headless run.
- **Postgres + Celery + Redis** — only needed once multi-instance / true
  durability is required.
- **LLM-based field extraction** — `extraction.py` is intentionally a
  single function so we can plug in an LLM call later without touching
  the API layer.

---

## Configuration reference

All settings come from environment variables (see `.env.example`):

| Variable | Default | Notes |
|---|---|---|
| `APP_HOST` | `0.0.0.0` | Bind address |
| `APP_PORT` | `8000` | Bind port |
| `LOG_LEVEL` | `INFO` | DEBUG for verbose |
| `DATABASE_URL` | `sqlite:///./data/app.db` | Postgres later |
| `UPLOAD_DIR` | `./data/uploads` | |
| `LOG_DIR` | `./logs` | Rotated daily |
| `OCR_LANGUAGES` | `en` | Comma-list, e.g. `en,hi` |
| `OCR_USE_GPU` | `false` | EasyOCR CUDA toggle |
| `TMS_BASE_URL` | `https://pallia.tmslive.in/` | |
| `TMS_USERNAME` | `changeme` | |
| `TMS_PASSWORD` | `changeme` | |
| `TMS_HEADLESS` | `true` | `false` while debugging |
| `TMS_SLOW_MO_MS` | `0` | Add e.g. 300 to watch the browser |
| `WORKER_CONCURRENCY` | `1` | FIFO if 1 |
| `JOB_MAX_RETRIES` | `2` | Per portal-side failure |
