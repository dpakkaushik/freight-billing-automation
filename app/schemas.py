"""Pydantic schemas for the billing API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import InvoiceStatus, LRStatus


# ---------- DO row ----------

class DORowIn(BaseModel):
    delivery_order_no: str
    total_amount: str = ""


class DORowOut(DORowIn):
    pass


# ---------- LR ----------

class LRConfirmedFields(BaseModel):
    """Shape of the confirmed_fields JSON on an LR."""
    vehicle_no: str | None = None
    gcn_no: str | None = None
    gcn_date: str | None = None
    destination: str | None = None
    delivery_date: str | None = None
    qty: str | None = None
    do_rows: list[DORowOut] = Field(default_factory=list)


class LRConfirmIn(BaseModel):
    """Body of the LR confirm endpoint."""
    fields: LRConfirmedFields


class LROut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    invoice_id: str
    status: LRStatus
    original_filename: str
    ocr_text: str | None = None
    extracted_fields: dict[str, Any] | None = None
    confirmed_fields: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------- Invoice ----------

class InvoiceCreateIn(BaseModel):
    suffix: str | None = None              # if omitted, server auto-suggests
    invoice_date: date | None = None       # if omitted, today


class InvoiceUpdateIn(BaseModel):
    suffix: str | None = None
    invoice_date: date | None = None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    suffix: str
    invoice_no_full: str
    invoice_date: date
    status: InvoiceStatus
    excel_path: str | None = None
    pdf_path: str | None = None
    signed_pdf_path: str | None = None
    error_message: str | None = None
    lrs: list[LROut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class InvoiceListOut(BaseModel):
    items: list[InvoiceOut]
    total: int


class SuggestedSuffixOut(BaseModel):
    suggested: str


ALL_MODULES = ["billing", "tracking", "accounts", "others", "api_dashboard"]


class UserCreateIn(BaseModel):
    email: EmailStr
    password: str
    role: str = "user"
    full_name: str | None = None
    permissions: list[str] = Field(default_factory=list)


class UserLoginIn(BaseModel):
    email: EmailStr
    password: str


class UserUpdateIn(BaseModel):
    full_name: str | None = None
    is_active: bool | None = None
    permissions: list[str] | None = None
    password: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    full_name: str | None = None
    role: str
    permissions: list[str] = Field(default_factory=list)
    is_active: bool
    last_login: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("permissions", mode="before")
    @classmethod
    def coerce_permissions(cls, v):
        return v if v is not None else []


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class DashboardUsageItem(BaseModel):
    path: str
    method: str
    count: int


class DashboardApiDayStat(BaseModel):
    date: str   # "YYYY-MM-DD"
    count: int


class DashboardBillingStat(BaseModel):
    label: str
    value: int


class ExternalApiStatOut(BaseModel):
    api_name: str
    model: str | None
    total_calls: int
    successful: int
    failed: int
    avg_duration_ms: int | None


class ExternalApiDayStatOut(BaseModel):
    date: str
    count: int


class ExternalApiSummaryOut(BaseModel):
    by_api: list[ExternalApiStatOut]
    by_day: list[ExternalApiDayStatOut]
    recent: list[dict]


class BillingUserStatOut(BaseModel):
    email: str
    full_name: str | None
    total_invoices: int
    generated_invoices: int
    draft_invoices: int
    total_lrs: int


class BillingDashboardOut(BaseModel):
    total_invoices: int
    generated_invoices: int
    draft_invoices: int
    failed_invoices: int
    total_lrs: int
    invoices_today: int
    by_user: list[BillingUserStatOut]
    billing_metrics: list[DashboardBillingStat]


class DashboardSummaryOut(BaseModel):
    total_users: int
    active_users: int
    total_invoices: int
    generated_invoices: int
    draft_invoices: int
    failed_invoices: int
    total_api_calls: int
    api_calls_today: int
    api_calls_by_day: list[DashboardApiDayStat]
    top_endpoints: list[DashboardUsageItem]
    billing_metrics: list[DashboardBillingStat]
