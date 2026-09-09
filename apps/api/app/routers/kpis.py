"""راستر API تعاریف KPI — فاز ۲.۳ گام ۳ (docs/PHASE3_KPI_PLAN.md §4)

مرزهای امنیتی (طبق طرح):
  - ماتریس نقش‌ها: مدیریت تعریف (create/patch/delete) → manager+؛ مصرف/محاسبه → analyst+؛
    viewer هیچ دسترسی‌ای ندارد
  - organization_id هرگز از کلاینت پذیرفته نمی‌شود — از membership تأییدشده می‌آید
    (require_role → require_membership → set_rls_context)؛ همه کوئری‌ها org-scoped
  - cross-org id → 404 (بدون افشای وجود) — RLS + فیلتر صریح organization_id
  - DataSource فقط از همان org و فقط usable (status == "mapped") قابل ارجاع است
  - اعتبارسنجی تعریف فقط از قواعد گام ۱ (validate_kpi_definition) — قاعده تکراری وجود ندارد
  - compute هیچ منطق تجمیع/فیلتر/گروه‌بندی ندارد — فقط رکوردهای tenant-scoped را
    به موتور pure گام ۲ می‌دهد (app/services/kpi.py)؛ engine database-agnostic می‌ماند
  - فقط ۶ اندپوینت: create/list/get/patch/delete/compute — /series در گام ۴
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.middleware.tenant import require_role
from app.models.data_source import DataSource
from app.models.fact_row import FactRow
from app.models.kpi_definition import KpiDefinition, KpiDefinitionError, validate_kpi_definition
from app.models.user import User
from app.schemas.kpi import KpiComputeOut, KpiCreate, KpiOut, KpiUpdate
from app.services.kpi import FactRecord, KpiComputationError, compute_kpi

router = APIRouter(prefix="/kpis", tags=["kpis"])

MANAGER_ROLES = ["owner", "admin", "manager"]
ANALYST_ROLES = ["owner", "admin", "manager", "analyst"]


def _get_own_kpi(db: Session, *, kpi_id: uuid.UUID, organization_id: uuid.UUID) -> KpiDefinition:
    """fetch سازمان‌محور — RLS + فیلتر صریح. cross-org → 404 (بدون افشای وجود)."""
    kpi = (
        db.query(KpiDefinition)
        .filter(
            KpiDefinition.id == kpi_id,
            KpiDefinition.organization_id == organization_id,
        )
        .first()
    )
    if not kpi:
        raise HTTPException(status_code=404, detail="KPI یافت نشد")
    return kpi


def _get_own_mapped_source(db: Session, *, data_source_id: uuid.UUID, organization_id: uuid.UUID) -> DataSource:
    """DataSource همان org + usable طبق قواعد lifecycle موجود (status == mapped)."""
    ds = (
        db.query(DataSource)
        .filter(
            DataSource.id == data_source_id,
            DataSource.organization_id == organization_id,
        )
        .first()
    )
    if not ds:
        raise HTTPException(status_code=404, detail="منبع داده یافت نشد")
    if ds.status != "mapped":
        raise HTTPException(status_code=400, detail="منبع داده هنوز map نشده است")
    return ds


def _apply_definition(kpi: KpiDefinition, payload: KpiCreate | KpiUpdate, *, partial: bool) -> None:
    """اعمال فیلدهای payload روی ردیف — بدون پذیرش organization_id (تغییر مالکیت ممنوع)."""
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        if field == "filters":
            value = [f if isinstance(f, dict) else f.model_dump() for f in value]
        setattr(kpi, field, value)
    if not partial:
        # fields with defaults must be explicit on create
        kpi.filters = [f if isinstance(f, dict) else f.model_dump() for f in (payload.filters or [])]
        kpi.enabled = payload.enabled


def _revalidate(kpi: KpiDefinition) -> None:
    """revalidate نتیجه کامل پس از create/patch — همان قواعد گام ۱ (fail-fast به 422)."""
    try:
        kpi.validate_definition()
    except KpiDefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _out(kpi: KpiDefinition) -> dict:
    return {
        "id": kpi.id,
        "organization_id": kpi.organization_id,
        "data_source_id": kpi.data_source_id,
        "name": kpi.name,
        "description": kpi.description,
        "aggregation": kpi.aggregation,
        "filters": kpi.filters or [],
        "group_by": kpi.group_by,
        "granularity": kpi.granularity,
        "date_from": kpi.date_from,
        "date_to": kpi.date_to,
        "enabled": kpi.enabled,
        "created_at": kpi.created_at,
        "updated_at": kpi.updated_at,
    }


@router.post("", response_model=KpiOut, status_code=201)
def create_kpi(
    payload: KpiCreate,
    membership=Depends(require_role(MANAGER_ROLES)),
    db: Session = Depends(get_db),
):
    organization_id = membership.organization_id

    # اعتبارسنجی تعریف قبل از هر INSERT (قواعد گام ۱ — منبع واحد)
    try:
        validate_kpi_definition(
            aggregation=payload.aggregation,
            group_by=payload.group_by,
            granularity=payload.granularity,
            filters=[f.model_dump() for f in payload.filters],
            date_from=payload.date_from,
            date_to=payload.date_to,
        )
    except KpiDefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # DataSource فقط همان org + usable
    _get_own_mapped_source(db, data_source_id=payload.data_source_id, organization_id=organization_id)

    kpi = KpiDefinition(
        organization_id=organization_id,
        data_source_id=payload.data_source_id,
        name=payload.name,
        description=payload.description,
        aggregation=payload.aggregation,
        filters=[f.model_dump() for f in payload.filters],
        group_by=payload.group_by,
        granularity=payload.granularity,
        date_from=payload.date_from,
        date_to=payload.date_to,
        enabled=payload.enabled,
    )
    db.add(kpi)
    try:
        db.flush()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="نام KPI در این سازمان تکراری است")
    out = _out(kpi)  # capture قبل از commit (context RLS تراکنش-local است)
    db.commit()
    return out


@router.get("", response_model=list[KpiOut])
def list_kpis(
    membership=Depends(require_role(ANALYST_ROLES)),
    db: Session = Depends(get_db),
):
    return [
        _out(k)
        for k in db.query(KpiDefinition)
        .filter(KpiDefinition.organization_id == membership.organization_id)
        .order_by(KpiDefinition.created_at.desc(), KpiDefinition.name)  # deterministic
        .all()
    ]


@router.get("/{kpi_id}", response_model=KpiOut)
def get_kpi(
    kpi_id: uuid.UUID,
    membership=Depends(require_role(ANALYST_ROLES)),
    db: Session = Depends(get_db),
):
    kpi = _get_own_kpi(db, kpi_id=kpi_id, organization_id=membership.organization_id)
    return _out(kpi)


@router.patch("/{kpi_id}", response_model=KpiOut)
def update_kpi(
    kpi_id: uuid.UUID,
    payload: KpiUpdate,
    membership=Depends(require_role(MANAGER_ROLES)),
    db: Session = Depends(get_db),
):
    kpi = _get_own_kpi(db, kpi_id=kpi_id, organization_id=membership.organization_id)
    _apply_definition(kpi, payload, partial=True)
    if payload.data_source_id is not None:
        # تغییر منبع فقط به منبع همان org + usable
        _get_own_mapped_source(db, data_source_id=kpi.data_source_id, organization_id=membership.organization_id)
    _revalidate(kpi)  # نتیجه کامل revalidate می‌شود — نه فقط فیلدهای تغییر یافته
    try:
        db.flush()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="نام KPI در این سازمان تکراری است")
    out = _out(kpi)
    db.commit()
    return out


@router.delete("/{kpi_id}", status_code=204)
def delete_kpi(
    kpi_id: uuid.UUID,
    membership=Depends(require_role(MANAGER_ROLES)),
    db: Session = Depends(get_db),
):
    kpi = _get_own_kpi(db, kpi_id=kpi_id, organization_id=membership.organization_id)
    db.delete(kpi)
    db.commit()
    return None


@router.post("/{kpi_id}/compute", response_model=KpiComputeOut)
def compute_kpi_endpoint(
    kpi_id: uuid.UUID,
    membership=Depends(require_role(ANALYST_ROLES)),
    db: Session = Depends(get_db),
):
    """محاسبه on-read — تمام دسترسی DB اینجاست؛ موتور pure گام ۲ محاسبه می‌کند.

    - KPI فقط از org خودش (cross-org → 404 قبل از هر محاسبه)
    - KPI غیرفعال → 400 (همان سمانتیک enabled در طرح §4)
    - fact_rows فقط با فیلتر organization_id (لایه ۳ دفاع کنار RLS) و از طریق
      الگوی ORM موجود خوانده می‌شود — هیچ SQL خامی وجود ندارد
    - هیچ logic تجمیعی در این راستر نیست — فقط FactRecord + compute_kpi
    """
    kpi = _get_own_kpi(db, kpi_id=kpi_id, organization_id=membership.organization_id)
    if not kpi.enabled:
        raise HTTPException(status_code=400, detail="این KPI غیرفعال است")
    # منبع فقط از همان org + usable (status == mapped) — طرح §3.3: منبع map نشده → 400
    _get_own_mapped_source(db, data_source_id=kpi.data_source_id, organization_id=membership.organization_id)

    # tenant-scoped fact rows — فقط ستون‌های واقعی fact_rows؛ organization_id در WHERE
    rows = (
        db.query(FactRow)
        .filter(
            FactRow.organization_id == membership.organization_id,
            FactRow.data_source_id == kpi.data_source_id,
        )
        .all()
    )
    records = [FactRecord.from_fact_row(r) for r in rows]

    try:
        result = compute_kpi(records, kpi)  # موتور pure — هیچ DBی داخل آن نیست
    except KpiDefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except KpiComputationError as exc:
        # داده خراب منبع → 422 (خطای تعریف/ورودی، نه خطای سرور) — بدون جزئیات داخلی
        raise HTTPException(status_code=422, detail=str(exc))

    return KpiComputeOut(kpi_id=kpi.id, value=str(result.value) if result.value is not None else None, rows=result.rows)
