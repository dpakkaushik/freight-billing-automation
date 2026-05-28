"""ORM models for the billing pipeline.

Hierarchy:
    Invoice
      - has an invoice_suffix and invoice_date
      - has one or more LR rows
      - generates one Excel + one PDF
    LR  (Lorry Receipt — one uploaded image)
      - belongs to an Invoice
      - holds OCR text + extracted fields + confirmed fields
      - confirmed_fields includes a list of "do_rows", one per Delivery Order No.
        Each do_row carries its own total_amount, so a single LR with N DO#s
        produces N rows in the final Excel.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _new_id() -> str:
    return uuid.uuid4().hex


# ---------- Invoice ----------

class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"              # being filled out
    GENERATED = "generated"      # Excel + PDF produced
    SUBMITTED = "submitted"      # (future) sent to portal
    FAILED = "failed"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    suffix: Mapped[str] = mapped_column(String(16), index=True)  # e.g. "039"
    invoice_date: Mapped[date] = mapped_column(Date, default=date.today)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, native_enum=False, length=16),
        default=InvoiceStatus.DRAFT,
        index=True,
    )

    excel_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    lrs: Mapped[list["LR"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="LR.created_at",
    )

    @property
    def invoice_no_full(self) -> str:
        """The full invoice number that goes on the document."""
        return f"PTLM-2627SWM-{self.suffix}"

    @property
    def signed_pdf_path(self) -> str | None:
        """Return path to signed PDF if it exists on disk, else None."""
        if not self.pdf_path:
            return None
        from pathlib import Path
        p = Path(self.pdf_path)
        signed = p.parent / f"{p.stem}_signed.pdf"
        return str(signed) if signed.exists() else None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Invoice {self.id} suffix={self.suffix} status={self.status}>"


# ---------- Users / Auth ----------

class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, length=16),
        default=UserRole.USER,
        nullable=False,
        index=True,
    )
    # List of module slugs the user can access, e.g. ["billing", "tracking"].
    # Admins bypass this check entirely — any non-null list applies to regular users.
    permissions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    api_events: Mapped[list["ApiUsageEvent"]] = relationship(back_populates="user")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email} role={self.role} active={self.is_active}>"


class ApiUsageEvent(Base):
    __tablename__ = "api_usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    user: Mapped[User | None] = relationship(back_populates="api_events")


# ---------- External API Event ----------

class ExternalApiEvent(Base):
    """One row per call made to an external AI/API service (Gemini, Ollama, etc.)."""
    __tablename__ = "external_api_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)   # "gemini", "ollama", "easyocr"
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)             # e.g. "gemini-2.5-flash"
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lr_id: Mapped[str | None] = mapped_column(ForeignKey("lrs.id", ondelete="SET NULL"), nullable=True, index=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


# ---------- LR ----------

class LRStatus(str, enum.Enum):
    UPLOADED = "uploaded"
    OCR_PROCESSING = "ocr_processing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class LR(Base):
    __tablename__ = "lrs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    status: Mapped[LRStatus] = mapped_column(
        Enum(LRStatus, native_enum=False, length=32),
        default=LRStatus.UPLOADED,
        index=True,
    )

    original_filename: Mapped[str] = mapped_column(String(255))
    image_path: Mapped[str] = mapped_column(String(512))

    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # confirmed_fields shape:
    # {
    #   "vehicle_no": "NL01AK0496",
    #   "gcn_no": "112032145",
    #   "gcn_date": "07-Apr-2026",
    #   "destination": "Gabhana",       # "To" city
    #   "delivery_date": "15/04/2026",
    #   "qty": "9 Units",
    #   "do_rows": [
    #       {"delivery_order_no": "7412304508", "total_amount": "12500"},
    #       {"delivery_order_no": "7412304561", "total_amount": "13000"}
    #   ]
    # }
    confirmed_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    invoice: Mapped[Invoice] = relationship(back_populates="lrs")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LR {self.id} invoice={self.invoice_id} status={self.status}>"


# ---------- EEE-Taxi Batch ----------

class EeeTaxiBatchStatus(str, enum.Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    COMPLETED  = "completed"
    PARTIAL    = "partial"    # some rows failed, some succeeded
    FAILED     = "failed"


class EeeTaxiInvoiceStatus(str, enum.Enum):
    PENDING    = "pending"
    GENERATING = "generating"
    DONE       = "done"
    FAILED     = "failed"


class EeeTaxiBatch(Base):
    __tablename__ = "eee_taxi_batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    status: Mapped[EeeTaxiBatchStatus] = mapped_column(
        Enum(EeeTaxiBatchStatus, native_enum=False, length=16),
        default=EeeTaxiBatchStatus.PENDING,
        index=True,
    )
    invoice_date: Mapped[date] = mapped_column(Date)
    start_suffix: Mapped[int] = mapped_column(Integer)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    csv_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    invoices: Mapped[list["EeeTaxiInvoice"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="EeeTaxiInvoice.row_index",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EeeTaxiBatch {self.id} status={self.status}>"


class EeeTaxiInvoice(Base):
    __tablename__ = "eee_taxi_invoices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("eee_taxi_batches.id", ondelete="CASCADE"), index=True
    )
    row_index: Mapped[int] = mapped_column(Integer)
    invoice_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_gstin: Mapped[str | None] = mapped_column(String(20), nullable=True)

    booking_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "p2p" | "rental"

    status: Mapped[EeeTaxiInvoiceStatus] = mapped_column(
        Enum(EeeTaxiInvoiceStatus, native_enum=False, length=16),
        default=EeeTaxiInvoiceStatus.PENDING,
        index=True,
    )
    pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signed_pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    batch: Mapped[EeeTaxiBatch] = relationship(back_populates="invoices")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EeeTaxiInvoice {self.id} no={self.invoice_no} status={self.status}>"
