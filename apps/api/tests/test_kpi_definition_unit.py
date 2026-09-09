"""فاز ۲.۳ گام ۱ — تست‌های unit مدل KpiDefinition و قواعد تعریف (SQLite).

SQLite هیچ RLS ندارد — ایزولاسیون tenant در سطح DB (ENABLE+FORCE+Fail-Closed)
در tests/test_kpi_rls_postgres.py روی PostgreSQL واقعی آزموده می‌شود.
اینجا: رفتار مدل (defaults، یونیک per-org، cascade حذف منبع)، قواعد تعریف
(aggregation/group_by/granularity/filters/پنجره تاریخ) و نبودِ فیلدهای ممنوع.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models import DataSource, Organization
from app.models.kpi_definition import (
    AGGREGATIONS,
    FILTER_FIELDS,
    GRANULARITIES,
    GROUP_BYS,
    MAX_FILTERS,
    KpiDefinition,
    KpiDefinitionError,
    validate_kpi_definition,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def orgs(db):
    org_a = Organization(name="Org A KPI", slug="kpi-org-a")
    org_b = Organization(name="Org B KPI", slug="kpi-org-b")
    db.add_all([org_a, org_b])
    db.commit()
    return org_a, org_b


def _source(db, org_id: uuid.UUID, name: str = "kpi-src") -> DataSource:
    ds = DataSource(organization_id=org_id, name=name, file_type="csv", row_count=0, status="mapped")
    db.add(ds)
    db.commit()
    return ds


def _kpi(org_id: uuid.UUID, source_id: uuid.UUID, **overrides) -> KpiDefinition:
    payload = dict(
        organization_id=org_id,
        data_source_id=source_id,
        name="revenue-total",
        aggregation="sum",
        filters=[],
    )
    payload.update(overrides)
    return KpiDefinition(**payload)


# ---------- model defaults / shape ----------

def test_model_creation_defaults(db, orgs):
    org_a, _ = orgs
    ds = _source(db, org_a.id)
    kpi = _kpi(org_a.id, ds.id)
    db.add(kpi)
    db.commit()
    assert kpi.id is not None
    assert kpi.enabled is True
    assert kpi.description is None
    assert kpi.filters == []
    assert kpi.group_by is None
    assert kpi.granularity is None
    assert kpi.date_from is None
    assert kpi.date_to is None
    assert kpi.created_at is not None
    assert kpi.updated_at is not None
    assert kpi.validate_definition() is None  # تعریف پیش‌فرض معتبر است


def test_valid_definition_with_all_optional_fields(db, orgs):
    org_a, _ = orgs
    ds = _source(db, org_a.id)
    kpi = _kpi(
        org_a.id, ds.id,
        description="monthly revenue",
        group_by="date",
        granularity="month",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 12, 31),
        filters=[{"field": "category", "op": "eq", "value": "sales"}],
    )
    db.add(kpi)
    db.commit()
    kpi.validate_definition()  # نباید خطا بدهد
    assert kpi.date_from < kpi.date_to


def test_no_forbidden_fields_exist():
    """فاز ۲.۳: بدون SQL، بدون cache/snapshot، بدون مقایسه/metric مشتق (§9)."""
    columns = {c.name for c in KpiDefinition.__table__.columns}
    for forbidden in ("sql", "query", "snapshot", "cached_at", "cache",
                      "comparison", "derived", "formula", "expression"):
        assert forbidden not in columns, f"forbidden column {forbidden!r} must not exist"


def test_constant_enums_match_plan():
    assert AGGREGATIONS == ("sum", "avg", "min", "max", "count")
    assert GROUP_BYS == ("date", "category", "label")
    assert GRANULARITIES == ("day", "week", "month")
    assert FILTER_FIELDS == ("category", "label", "date")
    assert MAX_FILTERS == 10


# ---------- definition validation ----------

def test_aggregation_validation():
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="median", group_by=None, granularity=None, filters=[], date_from=None, date_to=None)
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="SUM", group_by=None, granularity=None, filters=[], date_from=None, date_to=None)  # case-sensitive
    for agg in AGGREGATIONS:
        validate_kpi_definition(aggregation=agg, group_by=None, granularity=None, filters=[], date_from=None, date_to=None)


def test_group_by_validation():
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by="region", granularity=None, filters=[], date_from=None, date_to=None)
    validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[], date_from=None, date_to=None)


def test_granularity_rule_required_iff_date_group_by():
    # group_by=date بدون granularity → خطا
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by="date", granularity=None, filters=[], date_from=None, date_to=None)
    # granularity بدون group_by=date → خطا
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by=None, granularity="month", filters=[], date_from=None, date_to=None)
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by="category", granularity="month", filters=[], date_from=None, date_to=None)
    # هر دو جفت = معتبر
    for g in GRANULARITIES:
        validate_kpi_definition(aggregation="sum", group_by="date", granularity=g, filters=[], date_from=None, date_to=None)
    # granularity نامعتبر
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by="date", granularity="quarter", filters=[], date_from=None, date_to=None)


def test_filters_validation_and_maximum():
    # ساختار نامعتبر
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters="amount > 5", date_from=None, date_to=None)  # SQL string rejected
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[{"field": "measure_value", "op": "eq", "value": "x"}], date_from=None, date_to=None)
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[{"field": "category", "op": "gte", "value": "x"}], date_from=None, date_to=None)
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[{"field": "date", "op": "eq", "value": "2026-01-01"}], date_from=None, date_to=None)
    # مقدار category که شبیه تاریخ است، مقدار معتبری است — نباید رد شود
    validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[{"field": "category", "op": "eq", "value": "2026-13-99"}], date_from=None, date_to=None)
    # فیلتر تاریخ با فرمت بد
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[{"field": "date", "op": "gte", "value": "01-2026-01"}], date_from=None, date_to=None)
    # بیش از سقف
    too_many = [{"field": "label", "op": "eq", "value": f"v{i}"} for i in range(MAX_FILTERS + 1)]
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=too_many, date_from=None, date_to=None)
    # سقف مجاز و فیلترهای معتبر همه فیلدها
    ok = [{"field": "label", "op": "eq", "value": f"v{i}"} for i in range(MAX_FILTERS)]
    validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=ok, date_from=None, date_to=None)
    validate_kpi_definition(aggregation="sum", group_by=None, granularity=None,
                            filters=[{"field": "date", "op": "gte", "value": "2026-01-01"},
                                     {"field": "date", "op": "lte", "value": "2026-12-31"}],
                            date_from=None, date_to=None)


def test_date_window_inclusive_ordering():
    validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[],
                            date_from=date(2026, 3, 1), date_to=date(2026, 3, 1))  # برابر = مجاز (inclusive)
    with pytest.raises(KpiDefinitionError):
        validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[],
                                date_from=date(2026, 3, 2), date_to=date(2026, 3, 1))
    # فقط یک طرف پنجره هم معتبر است
    validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[],
                            date_from=date(2026, 3, 1), date_to=None)
    validate_kpi_definition(aggregation="sum", group_by=None, granularity=None, filters=[],
                            date_from=None, date_to=date(2026, 3, 1))


def test_model_validate_definition_raises(db, orgs):
    org_a, _ = orgs
    ds = _source(db, org_a.id)
    kpi = _kpi(org_a.id, ds.id, group_by="date", granularity=None)
    db.add(kpi)
    db.commit()
    with pytest.raises(KpiDefinitionError):
        kpi.validate_definition()


# ---------- uniqueness / relationship / cascade ----------

def test_unique_constraint_is_per_organization(db, orgs):
    org_a, org_b = orgs
    ds_a = _source(db, org_a.id, name="kpi-src-a")
    ds_b = _source(db, org_b.id, name="kpi-src-b")
    db.add(_kpi(org_a.id, ds_a.id, name="dup-kpi"))
    db.commit()
    db.add(_kpi(org_a.id, ds_a.id, name="dup-kpi"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # همان نام در org دیگر مجاز است
    db.add(_kpi(org_b.id, ds_b.id, name="dup-kpi"))
    db.commit()


def test_single_data_source_relationship_and_cascade(db, orgs):
    """دقیقاً یک data_source_id؛ حذف منبع → حذف KPI (CASCADE)، بدون orphan."""
    org_a, _ = orgs
    ds = _source(db, org_a.id)
    db.add(_kpi(org_a.id, ds.id, name="kpi-1"))
    db.add(_kpi(org_a.id, ds.id, name="kpi-2"))
    db.commit()
    assert db.query(KpiDefinition).count() == 2
    db.query(DataSource).filter(DataSource.id == ds.id).delete()
    db.commit()
    assert db.query(KpiDefinition).count() == 0  # CASCADE، نه orphan


def test_filters_persisted_as_structured_json(db, orgs):
    org_a, _ = orgs
    ds = _source(db, org_a.id)
    flt = [{"field": "category", "op": "neq", "value": "test"}]
    kpi = _kpi(org_a.id, ds.id, filters=flt)
    db.add(kpi)
    db.commit()
    db.expire_all()
    row = db.query(KpiDefinition).filter(KpiDefinition.id == kpi.id).one()
    assert row.filters == flt  # round-trip ساختاریافته — رشته SQL نیست
    assert isinstance(row.filters, list)
