from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ApiUsageEvent, ExternalApiEvent, Invoice, LR, User
from app.schemas import (
    DashboardApiDayStat,
    DashboardBillingStat,
    DashboardSummaryOut,
    DashboardUsageItem,
    ExternalApiStatOut,
    ExternalApiDayStatOut,
    ExternalApiSummaryOut,
    BillingUserStatOut,
    BillingDashboardOut,
)
from app.services.auth import require_admin

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummaryOut)
def dashboard_summary(
    date_from: Optional[date] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> DashboardSummaryOut:
    # Default range: last 7 days
    if date_to is None:
        date_to = date.today()
    if date_from is None:
        date_from = date_to - timedelta(days=6)

    from_dt = datetime.combine(date_from, datetime.min.time())
    to_dt = datetime.combine(date_to, datetime.max.time())

    # --- User stats ---
    total_users = db.scalar(select(func.count()).select_from(User)) or 0
    active_users = db.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True))) or 0

    # --- Invoice stats (all-time) ---
    total_invoices = db.scalar(select(func.count()).select_from(Invoice)) or 0
    generated_invoices = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.status == "generated")) or 0
    draft_invoices = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.status == "draft")) or 0
    failed_invoices = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.status == "failed")) or 0

    # --- API call stats ---
    total_api_calls = db.scalar(select(func.count()).select_from(ApiUsageEvent)) or 0

    today_start = datetime.combine(date.today(), datetime.min.time())
    api_calls_today = db.scalar(
        select(func.count()).select_from(ApiUsageEvent).where(ApiUsageEvent.created_at >= today_start)
    ) or 0

    # Per-day counts in selected range
    all_days_in_range = []
    d = date_from
    while d <= date_to:
        all_days_in_range.append(d)
        d += timedelta(days=1)

    day_count_rows = db.execute(
        select(
            func.strftime("%Y-%m-%d", ApiUsageEvent.created_at).label("day"),
            func.count().label("cnt"),
        )
        .where(ApiUsageEvent.created_at >= from_dt)
        .where(ApiUsageEvent.created_at <= to_dt)
        .group_by("day")
        .order_by("day")
    ).all()
    day_map = {row.day: row.cnt for row in day_count_rows}
    api_calls_by_day = [
        DashboardApiDayStat(date=str(d), count=day_map.get(str(d), 0))
        for d in all_days_in_range
    ]

    # Top endpoints (in range)
    usage_rows = db.execute(
        select(ApiUsageEvent.path, ApiUsageEvent.method, func.count().label("cnt"))
        .where(ApiUsageEvent.created_at >= from_dt)
        .where(ApiUsageEvent.created_at <= to_dt)
        .group_by(ApiUsageEvent.path, ApiUsageEvent.method)
        .order_by(func.count().desc())
        .limit(15)
    ).all()
    top_endpoints = [DashboardUsageItem(path=row.path, method=row.method, count=row.cnt) for row in usage_rows]

    # Billing metrics (all-time)
    amount_re = re.compile(r"[0-9,]+(?:\.\d+)?")
    do_rows_count = 0
    amount_sum = 0.0
    for invoice in db.scalars(select(Invoice)).all():
        for lr in invoice.lrs:
            if not lr.confirmed_fields:
                continue
            for row in lr.confirmed_fields.get("do_rows") or []:
                do_rows_count += 1
                if isinstance(row, dict):
                    m = amount_re.search(str(row.get("total_amount", "") or "").replace(",", ""))
                    if m:
                        try:
                            amount_sum += float(m.group(0))
                        except ValueError:
                            pass

    billing_metrics = [
        DashboardBillingStat(label="Confirmed DO rows", value=do_rows_count),
        DashboardBillingStat(label="Estimated billed amount (INR)", value=int(amount_sum)),
    ]

    return DashboardSummaryOut(
        total_users=total_users,
        active_users=active_users,
        total_invoices=total_invoices,
        generated_invoices=generated_invoices,
        draft_invoices=draft_invoices,
        failed_invoices=failed_invoices,
        total_api_calls=total_api_calls,
        api_calls_today=api_calls_today,
        api_calls_by_day=api_calls_by_day,
        top_endpoints=top_endpoints,
        billing_metrics=billing_metrics,
    )


@router.get("/external-apis", response_model=ExternalApiSummaryOut)
def external_api_summary(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> ExternalApiSummaryOut:
    if date_to is None:
        date_to = date.today()
    if date_from is None:
        date_from = date_to - timedelta(days=6)

    from_dt = datetime.combine(date_from, datetime.min.time())
    to_dt = datetime.combine(date_to, datetime.max.time())

    # Per-API stats
    api_rows = db.execute(
        select(
            ExternalApiEvent.api_name,
            ExternalApiEvent.model,
            func.count().label("total"),
            func.sum(func.cast(ExternalApiEvent.success, Integer)).label("ok"),
            func.avg(ExternalApiEvent.duration_ms).label("avg_ms"),
        )
        .where(ExternalApiEvent.created_at >= from_dt)
        .where(ExternalApiEvent.created_at <= to_dt)
        .group_by(ExternalApiEvent.api_name, ExternalApiEvent.model)
        .order_by(func.count().desc())
    ).all()

    by_api = [
        ExternalApiStatOut(
            api_name=r.api_name,
            model=r.model,
            total_calls=r.total,
            successful=int(r.ok or 0),
            failed=r.total - int(r.ok or 0),
            avg_duration_ms=int(r.avg_ms) if r.avg_ms else None,
        )
        for r in api_rows
    ]

    # Per-day counts
    all_days = []
    d = date_from
    while d <= date_to:
        all_days.append(d)
        d += timedelta(days=1)

    day_rows = db.execute(
        select(
            func.strftime("%Y-%m-%d", ExternalApiEvent.created_at).label("day"),
            func.count().label("cnt"),
        )
        .where(ExternalApiEvent.created_at >= from_dt)
        .where(ExternalApiEvent.created_at <= to_dt)
        .group_by("day").order_by("day")
    ).all()
    day_map = {r.day: r.cnt for r in day_rows}
    by_day = [ExternalApiDayStatOut(date=str(d), count=day_map.get(str(d), 0)) for d in all_days]

    # 20 most recent calls
    recent_rows = db.execute(
        select(ExternalApiEvent)
        .where(ExternalApiEvent.created_at >= from_dt)
        .where(ExternalApiEvent.created_at <= to_dt)
        .order_by(ExternalApiEvent.created_at.desc())
        .limit(20)
    ).scalars().all()
    recent = [
        {
            "id": r.id,
            "api_name": r.api_name,
            "model": r.model,
            "success": r.success,
            "duration_ms": r.duration_ms,
            "error_message": r.error_message,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for r in recent_rows
    ]

    return ExternalApiSummaryOut(by_api=by_api, by_day=by_day, recent=recent)


@router.get("/billing", response_model=BillingDashboardOut)
def billing_dashboard(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> BillingDashboardOut:
    total_invoices = db.scalar(select(func.count()).select_from(Invoice)) or 0
    generated = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.status == "generated")) or 0
    draft = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.status == "draft")) or 0
    failed = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.status == "failed")) or 0
    total_lrs = db.scalar(select(func.count()).select_from(LR)) or 0

    today_start = datetime.combine(date.today(), datetime.min.time())
    invoices_today = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.created_at >= today_start)) or 0

    # Per-user invoice breakdown
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    by_user = []
    for u in users:
        u_total = db.scalar(select(func.count()).select_from(Invoice)) or 0  # all invoices (no user FK yet)
        # Count LRs linked to invoices (approximation until user FK is on Invoice)
        by_user.append(BillingUserStatOut(
            email=u.email,
            full_name=u.full_name,
            total_invoices=u_total,
            generated_invoices=generated,
            draft_invoices=draft,
            total_lrs=total_lrs,
        ))

    # Billing amount metrics
    amount_re = re.compile(r"[0-9,]+(?:\.\d+)?")
    do_rows_count, amount_sum = 0, 0.0
    for invoice in db.scalars(select(Invoice)).all():
        for lr in invoice.lrs:
            if not lr.confirmed_fields:
                continue
            for row in lr.confirmed_fields.get("do_rows") or []:
                do_rows_count += 1
                if isinstance(row, dict):
                    m = amount_re.search(str(row.get("total_amount", "") or "").replace(",", ""))
                    if m:
                        try:
                            amount_sum += float(m.group(0))
                        except ValueError:
                            pass

    billing_metrics = [
        DashboardBillingStat(label="Confirmed DO rows", value=do_rows_count),
        DashboardBillingStat(label="Estimated billed amount (INR)", value=int(amount_sum)),
    ]

    return BillingDashboardOut(
        total_invoices=total_invoices,
        generated_invoices=generated,
        draft_invoices=draft,
        failed_invoices=failed,
        total_lrs=total_lrs,
        invoices_today=invoices_today,
        by_user=by_user,
        billing_metrics=billing_metrics,
    )
