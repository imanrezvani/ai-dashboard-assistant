"""تست‌های unit Context Catalog — فاز ۲.۴ گام ۱ (docs/PHASE4_CONTEXT_CATALOG_PLAN.md §10.1)

اجرای بدون PostgreSQL (الگوی test_storage_unit.py — SQLite در-حافظه).
چه چیزی اینجا آزموده می‌شود — منطق خودِ builder:
  - خالی بودن org → کاتالوگ معتبر خالی
  - determinism (ترتیب منابع/KPIها/ستون‌ها/واژه‌نامه ابعاد)
  - سقف‌ها (§8) + true-total بودن countها
  - شکل قرارداد (§4): بدون float، coverage null برای منبع unmapped
  - freshness: last_mapped_at = max(created_at)

چه چیزی اینجا آزموده نمی‌شود — RLS واقعی PostgreSQL و ماتریس نقش‌های API:
آن مسیرها در tests/test_context_catalog_api.py (گام ۲) روی PostgreSQL واقعی
پوشش داده می‌شوند؛ این ماژول رفتار RLS را fake نمی‌کند.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models import DataSource, DataSourceColumn, FactRow, KpiDefinition, Organization
from app.schemas.context_catalog import ContextCatalogOut
from app.services import context_catalog as cc

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_source(db, org, name, *, file_type="csv", status="mapped", row_count=0,
                uploaded_at=None, connection_id=None):
    ds = DataSource(
        organization_id=org.id,
        name=name,
        file_type=file_type,
        status=status,
        row_count=row_count,
        database_connection_id=connection_id,
    )
    if uploaded_at is not None:
        ds.uploaded_at = uploaded_at
    db.add(ds)
    db.flush()
    return ds


def _add_column(db, org, ds, name, position, *, dtype="object", mapped_role=None):
    db.add(DataSourceColumn(
        organization_id=org.id,
        data_source_id=ds.id,
        name=name,
        position=position,
        dtype=dtype,
        mapped_role=mapped_role,
    ))


def _add_fact(db, org, ds, measure, *, d=None, cat=None, lab=None, created_at=None):
    fr = FactRow(
        organization_id=org.id,
        data_source_id=ds.id,
        measure_value=measure,
        dimension_date=d,
        dimension_category=cat,
        dimension_label=lab,
    )
    if created_at is not None:
        fr.created_at = created_at
    db.add(fr)


def _add_kpi(db, org, ds, name, *, aggregation="sum", group_by=None, granularity=None,
             filters=None, date_from=None, date_to=None, enabled=True, created_at=None):
    kpi = KpiDefinition(
        organization_id=org.id,
        data_source_id=ds.id,
        name=name,
        aggregation=aggregation,
        filters=filters if filters is not None else [],
        group_by=group_by,
        granularity=granularity,
        date_from=date_from,
        date_to=date_to,
        enabled=enabled,
    )
    if created_at is not None:
        kpi.created_at = created_at
    db.add(kpi)


# ---------------------------------------------------------------------------
# empty org / envelope
# ---------------------------------------------------------------------------


def test_empty_org_returns_valid_empty_catalog(db):
    org = Organization(name="Empty Org", slug="empty-org")
    db.add(org)
    db.commit()

    raw = cc.build_context_catalog(db, organization_id=org.id)
    out = ContextCatalogOut.model_validate(raw)

    assert out.schema_version == 1
    assert out.summary.data_source_count == 0
    assert out.summary.kpi_count == 0
    assert out.summary.date_span.min is None
    assert out.summary.date_span.max is None
    assert out.data_sources == []
    assert out.kpis == []
    assert out.generated_at.endswith("Z")  # ISO-8601 UTC (§4)


def test_organization_id_required():
    with pytest.raises(ValueError):
        cc.build_context_catalog(db=None, organization_id=None)


# ---------------------------------------------------------------------------
# determinism / ordering
# ---------------------------------------------------------------------------


def test_source_ordering_uploaded_at_desc_then_name_asc(db):
    org = Organization(name="Order Org", slug="order-org")
    db.add(org)
    db.flush()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # آپلود هم‌زمان → تای‌بریک name صعودی
    _add_source(db, org, "zeta.csv", uploaded_at=base)
    _add_source(db, org, "alpha.csv", uploaded_at=base)
    _add_source(db, org, "newest.csv", uploaded_at=base + timedelta(days=1))
    _add_source(db, org, "oldest.csv", uploaded_at=base - timedelta(days=1))
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    assert [s.name for s in out.data_sources] == ["newest.csv", "alpha.csv", "zeta.csv", "oldest.csv"]


def test_kpi_ordering_created_at_desc_then_name_asc_matches_get_kpis(db):
    org = Organization(name="Kpi Order Org", slug="kpi-order-org")
    db.add(org)
    db.flush()
    ds = _add_source(db, org, "src.csv")
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    _add_kpi(db, org, ds, "z-old", created_at=base)
    _add_kpi(db, org, ds, "a-old", created_at=base)
    _add_kpi(db, org, ds, "new", created_at=base + timedelta(days=1))
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    assert [k.name for k in out.kpis] == ["new", "a-old", "z-old"]


def test_columns_ordered_by_position(db):
    org = Organization(name="Col Org", slug="col-org")
    db.add(org)
    db.flush()
    ds = _add_source(db, org, "cols.csv")
    _add_column(db, org, ds, "amount", 2, dtype="float64", mapped_role="measure")
    _add_column(db, org, ds, "day", 0, dtype="object", mapped_role="date")
    _add_column(db, org, ds, "segment", 1, dtype="object", mapped_role="category")
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    assert [(c.name, c.position) for c in out.data_sources[0].columns] == [
        ("day", 0), ("segment", 1), ("amount", 2),
    ]
    assert out.data_sources[0].columns[0].mapped_role == "date"


def test_identical_input_yields_identical_payload_except_generated_at(db):
    org = Organization(name="Det Org", slug="det-org")
    db.add(org)
    db.flush()
    ds = _add_source(db, org, "det.csv")
    _add_fact(db, org, ds, 10, d=date(2026, 1, 5), cat="b", lab="y")
    _add_fact(db, org, ds, 20, d=date(2026, 1, 6), cat="a", lab="x")
    _add_kpi(db, org, ds, "k1", group_by="category")
    db.commit()

    a = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    b = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    assert a.model_dump(exclude={"generated_at"}) == b.model_dump(exclude={"generated_at"})


# ---------------------------------------------------------------------------
# coverage semantics
# ---------------------------------------------------------------------------


def test_unmapped_source_has_zero_coverage(db):
    org = Organization(name="Unmapped Org", slug="unmapped-org")
    db.add(org)
    db.flush()
    ds = _add_source(db, org, "pending.csv", status="pending")
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    cov = out.data_sources[0].coverage
    assert cov.fact_row_count == 0
    assert cov.last_mapped_at is None
    assert cov.date_min is None
    assert cov.date_max is None
    assert cov.has_date_dimension is False
    assert cov.category_values == [] and cov.category_value_count == 0
    assert cov.label_values == [] and cov.label_value_count == 0


def test_coverage_aggregates_and_freshness(db):
    org = Organization(name="Cov Org", slug="cov-org")
    db.add(org)
    db.flush()
    ds = _add_source(db, org, "cov.csv")
    t0 = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
    _add_fact(db, org, ds, 5, d=date(2026, 1, 10), cat="b", lab="x", created_at=t0)
    _add_fact(db, org, ds, 7, d=date(2026, 1, 2), cat="a", lab="x", created_at=t0 + timedelta(hours=1))
    _add_fact(db, org, ds, 9, d=None, cat=None, lab=None, created_at=t0 + timedelta(hours=2))
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    cov = out.data_sources[0].coverage
    assert cov.fact_row_count == 3
    assert cov.date_min == "2026-01-02"
    assert cov.date_max == "2026-01-10"
    assert cov.has_date_dimension is True
    # last_mapped_at = max(created_at) — §2.2
    assert cov.last_mapped_at == (t0 + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    assert cov.category_values == ["a", "b"]
    assert cov.category_value_count == 2
    assert cov.label_values == ["x"]
    assert cov.label_value_count == 1


def test_all_null_dates_means_no_date_dimension(db):
    org = Organization(name="NullDate Org", slug="nulldate-org")
    db.add(org)
    db.flush()
    ds = _add_source(db, org, "nodate.csv")
    _add_fact(db, org, ds, 1, d=None, cat="c1")
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    cov = out.data_sources[0].coverage
    assert cov.has_date_dimension is False
    assert cov.date_min is None and cov.date_max is None
    assert cov.fact_row_count == 1


def test_global_date_span_spans_sources(db):
    org = Organization(name="Span Org", slug="span-org")
    db.add(org)
    db.flush()
    ds1 = _add_source(db, org, "one.csv")
    ds2 = _add_source(db, org, "two.csv")
    _add_fact(db, org, ds1, 1, d=date(2026, 5, 1))
    _add_fact(db, org, ds2, 1, d=date(2025, 12, 31))
    _add_fact(db, org, ds2, 1, d=date(2026, 2, 20))
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    assert out.summary.date_span.min == "2025-12-31"
    assert out.summary.date_span.max == "2026-05-01"


# ---------------------------------------------------------------------------
# caps (§8)
# ---------------------------------------------------------------------------


def test_dimension_values_capped_with_true_total(db):
    org = Organization(name="Cap Org", slug="cap-org")
    db.add(org)
    db.flush()
    ds = _add_source(db, org, "cap.csv")
    # 60 مقدار distinct category > MAX_DIMENSION_VALUES (50)
    for i in range(60):
        _add_fact(db, org, ds, 1, d=None, cat=f"cat-{i:03d}", lab=f"lab-{i:03d}")
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    cov = out.data_sources[0].coverage
    assert len(cov.category_values) == cc.MAX_DIMENSION_VALUES
    assert cov.category_values[0] == "cat-000"  # صعودی deterministic
    assert cov.category_value_count == 60  # true total — نه طول لیست (§4)
    assert cov.label_values[0] == "lab-000"
    assert cov.label_value_count == 60


def test_kpis_and_sources_caps(db):
    org = Organization(name="Max Org", slug="max-org")
    db.add(org)
    db.flush()
    for i in range(cc.MAX_KPIS + 5):
        ds = _add_source(db, org, f"src-{i:03d}.csv", status="pending")
        if i < cc.MAX_KPIS + 5 - 3:  # بیشتر از سقف KPI
            _add_kpi(db, org, ds, f"kpi-{i:03d}", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i))
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    assert out.summary.data_source_count == cc.MAX_KPIS + 5  # true total
    assert len(out.data_sources) == cc.MAX_SOURCES  # truncate
    assert out.summary.kpi_count == cc.MAX_KPIS + 2
    assert len(out.kpis) == cc.MAX_KPIS  # truncate
    # truncate شده‌ها جدیدترین‌ها هستند (created_at desc)
    assert out.kpis[0].name == f"kpi-{cc.MAX_KPIS + 1:03d}"


# ---------------------------------------------------------------------------
# contract shape (§4)
# ---------------------------------------------------------------------------


def test_payload_contains_no_floats_and_no_secrets(db):
    org = Organization(name="Shape Org", slug="shape-org")
    db.add(org)
    db.flush()
    conn_id = uuid.uuid4()
    ds = _add_source(db, org, "shape.csv", connection_id=conn_id)
    _add_column(db, org, ds, "amount", 0, mapped_role="measure")
    _add_fact(db, org, ds, 1.5, d=date(2026, 1, 1), cat="a")
    _add_kpi(db, org, ds, "k", filters=[{"field": "category", "op": "eq", "value": "a"}])
    db.commit()

    raw = cc.build_context_catalog(db, organization_id=org.id)

    def _walk(node):
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        else:
            assert not isinstance(node, float), f"float in payload: {node!r}"

    _walk(raw)
    # provenance فقط id — نه هیچ شیء اتصال (§2.3/§7.4)
    assert raw["data_sources"][0]["database_connection_id"] == conn_id
    blob = str(raw)
    for forbidden in ("host", "password", "username", "encrypted", "ssl_mode", "database_name"):
        assert forbidden not in blob


def test_kpi_block_carries_definition_not_computed_values(db):
    org = Organization(name="KpiShape Org", slug="kpishape-org")
    db.add(org)
    db.flush()
    ds = _add_source(db, org, "k.csv")
    _add_kpi(db, org, ds, "rev", aggregation="avg", group_by="month", granularity="month",
             date_from=date(2026, 1, 1), date_to=date(2026, 6, 30), enabled=False)
    db.commit()

    out = ContextCatalogOut.model_validate(cc.build_context_catalog(db, organization_id=org.id))
    k = out.kpis[0]
    assert k.aggregation == "avg" and k.group_by == "month" and k.granularity == "month"
    assert k.date_from == "2026-01-01" and k.date_to == "2026-06-30"
    assert k.enabled is False
    assert not hasattr(k, "value") and not hasattr(k, "buckets")  # بدون عدد محاسبه‌شده (§4)
