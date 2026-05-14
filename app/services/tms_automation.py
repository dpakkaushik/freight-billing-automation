"""Playwright automation for the Pallia TMS portal (https://pallia.tmslive.in/).

STATUS: SKELETON — selectors are stubbed with TODO markers.

To finalise the script:
  1. Run `playwright codegen https://pallia.tmslive.in/` locally with valid
     credentials. Playwright will record your clicks and produce real
     selectors.
  2. Copy the locator calls into the TODOs below.
  3. If the portal has CSRF/anti-bot, see the notes at the bottom of this file.

The public interface — `submit_to_tms(fields) -> SubmitResult` — is stable;
the worker depends only on this signature.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings


@dataclass
class SubmitResult:
    success: bool
    error: str | None = None
    portal_reference: str | None = None  # any id/ack the portal returns


# ---------- Browser lifecycle ----------

async def _new_browser(playwright) -> tuple[Browser, BrowserContext, Page]:
    browser = await playwright.chromium.launch(
        headless=settings.tms_headless,
        slow_mo=settings.tms_slow_mo_ms,
    )
    context = await browser.new_context(
        ignore_https_errors=False,
        viewport={"width": 1366, "height": 900},
    )
    page = await context.new_page()
    page.set_default_timeout(20_000)  # 20s per action
    return browser, context, page


# ---------- Steps ----------

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=8), reraise=True)
async def _login(page: Page) -> None:
    """Navigate to login page and authenticate."""
    logger.info("TMS: opening {}", settings.tms_base_url)
    await page.goto(settings.tms_base_url, wait_until="domcontentloaded")

    # ----------------------------------------------------------------------
    # TODO: Replace these selectors after running `playwright codegen`.
    # Common patterns shown — uncomment the one that matches the real DOM.
    # ----------------------------------------------------------------------
    # await page.locator('input[name="username"]').fill(settings.tms_username)
    # await page.locator('input[name="password"]').fill(settings.tms_password)
    # await page.get_by_role("button", name="Login").click()

    # Placeholder so the function is still callable while selectors are pending:
    raise NotImplementedError(
        "TMS login selectors are not configured yet. "
        "Run `playwright codegen https://pallia.tmslive.in/` and fill in app/services/tms_automation.py::_login."
    )

    # After login, wait for a known post-login element:
    # await page.wait_for_url("**/dashboard**", timeout=15_000)


async def _navigate_to_form(page: Page) -> None:
    """Drive the menu to reach the form we want to fill."""
    # TODO: e.g. await page.get_by_role("link", name="New Entry").click()
    raise NotImplementedError("Navigation to the data-entry form is not configured yet.")


async def _fill_form(page: Page, fields: dict[str, Any]) -> None:
    """Fill the form with extracted/confirmed fields.

    Each field is optional — only fill it if present in `fields`.
    """
    # TODO: map each key to the matching input on the portal. Examples:
    #
    # if fields.get("invoice_number"):
    #     await page.locator('input[name="invoiceNo"]').fill(fields["invoice_number"])
    # if fields.get("date"):
    #     await page.locator('input[name="docDate"]').fill(fields["date"])
    # if fields.get("amount"):
    #     await page.locator('input[name="amount"]').fill(str(fields["amount"]))
    # if fields.get("vehicle_number"):
    #     await page.locator('input[name="vehicleNo"]').fill(fields["vehicle_number"])
    # if fields.get("lr_number"):
    #     await page.locator('input[name="lrNo"]').fill(fields["lr_number"])
    # if fields.get("name"):
    #     await page.locator('input[name="partyName"]').fill(fields["name"])

    raise NotImplementedError("Field-to-selector mapping is not configured yet.")


async def _save(page: Page) -> str | None:
    """Click Save / Submit and detect success.

    Returns the portal-side reference number if available, else None.
    Raises on failure.
    """
    # TODO: await page.get_by_role("button", name="Save").click()
    #
    # # Wait for either a success toast or an error
    # try:
    #     await page.wait_for_selector(".toast-success, .alert-success", timeout=15_000)
    # except PlaywrightTimeoutError:
    #     # Look for an inline error
    #     err = await page.locator(".alert-danger, .toast-error").first.text_content()
    #     raise RuntimeError(f"Portal rejected the submission: {err}")
    #
    # # Try to capture a reference number, if the portal shows one:
    # ref_el = page.locator("[data-testid='entry-ref']")
    # return (await ref_el.text_content()) if await ref_el.count() else None

    raise NotImplementedError("Save / success-detection is not configured yet.")


# ---------- Public entry point ----------

async def submit_to_tms(fields: dict[str, Any]) -> SubmitResult:
    """High-level: launch a browser, do the full flow, return a result.

    This function never raises for normal portal errors — it returns
    SubmitResult(success=False, error=...). It only raises for truly
    unexpected programming errors, which the worker catches.
    """
    if not fields:
        return SubmitResult(success=False, error="No fields to submit.")

    logger.info("TMS submission starting with fields: {}", list(fields.keys()))

    async with async_playwright() as p:
        browser, context, page = await _new_browser(p)
        try:
            await _login(page)
            await _navigate_to_form(page)
            await _fill_form(page, fields)
            ref = await _save(page)
            return SubmitResult(success=True, portal_reference=ref)
        except NotImplementedError as exc:
            # Skeleton mode — surface clearly to the worker
            logger.warning("TMS skeleton hit: {}", exc)
            return SubmitResult(success=False, error=str(exc))
        except PlaywrightTimeoutError as exc:
            return SubmitResult(success=False, error=f"Portal timeout: {exc}")
        except Exception as exc:
            logger.exception("Unexpected error during TMS submission")
            return SubmitResult(success=False, error=str(exc))
        finally:
            await context.close()
            await browser.close()


# ----------------------------------------------------------------------
# Notes for finalising
# ----------------------------------------------------------------------
# * If the portal uses a captcha or 2FA, automation will not handle it
#   silently — we'll need a manual-confirmation step in the worker.
# * If the portal uses ASP.NET style postbacks, prefer page.get_by_role
#   or page.get_by_label over CSS selectors — the auto-generated IDs
#   tend to change between deployments.
# * To debug visually, set TMS_HEADLESS=false and TMS_SLOW_MO_MS=300
#   in .env and re-run.
