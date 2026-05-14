"""Automation Bot for Document Data Entry.

Python-first FastAPI app that:
  1. Receives uploaded document images
  2. Runs OCR + field extraction
  3. Asks the user to confirm / edit / reject fields
  4. Queues confirmed jobs FIFO and feeds them to a Playwright worker
     that submits records to the Pallia TMS portal.
"""

__version__ = "0.1.0"
