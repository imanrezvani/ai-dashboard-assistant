"""فاز ۲.۳ گام ۲ — تست‌های unit موتور pure KPI (بدون PostgreSQL).

پوشش (docs/PHASE3_KPI_PLAN.md §3 و درخواست گام ۲):
  تجمیع‌ها (sum/avg/min/max/count)، قرارداد empty/null، گروه‌بندی day/week/month/
  category/label، پنجره inclusive، فیلترهای ساختاریافته، ورودی غیرقابل‌نگاشت،
  دقت Decimal ۴رقم، مرتب‌سازی deterministic، نبودِ zero-fill.
موتور هیچ SQL/DB ندارد — این تست‌ها هم هیچ DB نمی‌خواهند.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.kpi_definition import KpiDefinition, KpiDefinitionError
from app.services.kpi import (
    FactRecord,
    GroupedResult,
    KpiComputationError,
    compute_kpi,
    compute_kpi_series,
)

D = Decimal


def _kpi(**overrides) -> KpiDefinition:
    """KpiDefinition در-حافظه — بدون DB (فقط فیلدهای مصرفی موتور لازم است)."""
    payload = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        data_source_id=uuid.uuid4(),
        name="kpi-test",
        aggregation="sum",
        filters=[],
    )
    payload.update(overrides)
    return KpiDefinition(**payload)


def _recs(*tuples) -> list[FactRecord]:
    """(measure, date, category, label) → FactRecord — Decimal مستقیم؛ هیچ float."""
    return [FactRecord(measure=m, date=d, category=c, label=l) for (m, d, c, l) in tuples]


# ---------- ۱–۵: aggregations ----------

def test_sum():
    recs = _recs((D("1.5"), date(2026, 1, 1), "a", "x"), (D("2.25"), date(2026, 1, 2), "b", "y"))
    assert compute_kpi(recs, _kpi(aggregation="sum")).value == D("3.7500")


def test_avg():
    recs = _recs((D("1"), date(2026, 1, 1), None, None), (D("2"), date(2026, 1, 2), None, None))
    assert compute_kpi(recs, _kpi(aggregation="avg")).value == D("1.5000")


def test_min_max():
    recs = _recs((D("5"), date(2026, 1, 1), None, None), (D("-2.5"), date(2026, 1, 2), None, None), (D("10"), date(2026, 1, 3), None, None))
    assert compute_kpi(recs, _kpi(aggregation="min")).value == D("-2.5000")
    assert compute_kpi(recs, _kpi(aggregation="max")).value == D("10.0000")


def test_count():
    recs = _recs((D("5"), date(2026, 1, 1), None, None), (D("1"), None, None, None))
    result = compute_kpi(recs, _kpi(aggregation="count"))
    assert result.value == D("2.0000")
    assert result.rows == 2


# ---------- ۶–۷: empty input / null measures ----------

def test_empty_input_contract():
    assert compute_kpi([], _kpi(aggregation="sum")).value == D("0.0000")
    assert compute_kpi([], _kpi(aggregation="count")).value == D("0.0000")
    assert compute_kpi([], _kpi(aggregation="avg")).value is None
    assert compute_kpi([], _kpi(aggregation="min")).value is None
    assert compute_kpi([], _kpi(aggregation="max")).value is None


def test_null_measures_excluded_defensively():
    """measure=None دفاعی حذف می‌شود — count هم آن را نمی‌شمارد (§3.3)."""
    recs = _recs((None, date(2026, 1, 1), "a", "x"), (D("3"), date(2026, 1, 2), "b", "y"))
    assert compute_kpi(recs, _kpi(aggregation="count")).value == D("1.0000")
    assert compute_kpi(recs, _kpi(aggregation="sum")).value == D("3.0000")
    assert compute_kpi(recs, _kpi(aggregation="avg")).value == D("3.0000")


# ---------- ۸–۱۲: grouping ----------

def test_date_grouping_day():
    kpi = _kpi(group_by="date", granularity="day")
    recs = _recs(
        (D("1"), date(2026, 1, 1), None, None),
        (D("2"), date(2026, 1, 1), None, None),
        (D("4"), date(2026, 1, 2), None, None),
    )
    result = compute_kpi_series(recs, kpi)
    assert isinstance(result, GroupedResult)
    assert result.group_by == "date"
    assert [(b.key, b.value) for b in result.buckets] == [
        ("2026-01-01", D("3.0000")), ("2026-01-02", D("4.0000")),
    ]


def test_week_grouping_iso_monday_start():
    kpi = _kpi(group_by="date", granularity="week")
    # 2026-01-01 پنجشنبه است → ISO week 2026-W01؛ 2026-01-05 دوشنبه → W02
    recs = _recs(
        (D("1"), date(2025, 12, 29), None, None),  # دوشنبه W01 (2025 iso-year? no: 2026-W01 starts Mon 2025-12-29)
        (D("2"), date(2026, 1, 4), None, None),    # یکشنبه هنوز W01
        (D("4"), date(2026, 1, 5), None, None),    # دوشنبه W02
    )
    result = compute_kpi_series(recs, kpi)
    keys = [b.key for b in result.buckets]
    assert keys == ["2026-W01", "2026-W02"]
    assert result.buckets[0].value == D("3.0000")
    assert result.buckets[1].value == D("4.0000")


def test_week_grouping_year_boundary_uses_iso_year():
    """مرز سال ISO: 2027-01-01 جمعه است و به هفته 2026-W53 تعلق دارد."""
    kpi = _kpi(group_by="date", granularity="week")
    recs = _recs((D("7"), date(2027, 1, 1), None, None))
    result = compute_kpi_series(recs, kpi)
    assert [b.key for b in result.buckets] == ["2026-W53"]


def test_month_grouping():
    kpi = _kpi(group_by="date", granularity="month")
    recs = _recs(
        (D("1"), date(2026, 1, 31), None, None),
        (D("2"), date(2026, 2, 1), None, None),
        (D("4"), date(2026, 2, 28), None, None),
    )
    result = compute_kpi_series(recs, kpi)
    assert [(b.key, b.value) for b in result.buckets] == [
        ("2026-01", D("1.0000")), ("2026-02", D("6.0000")),
    ]


def test_category_grouping_sorted_and_null_excluded():
    kpi = _kpi(group_by="category")
    recs = _recs(
        (D("1"), None, "beta", None),
        (D("2"), None, "alpha", None),
        (D("4"), None, None, None),      # NULL category → bucket ندارد (§3.3)
        (D("8"), None, "alpha", None),
    )
    result = compute_kpi_series(recs, kpi)
    assert [(b.key, b.value, b.rows) for b in result.buckets] == [
        ("alpha", D("10.0000"), 2), ("beta", D("1.0000"), 1),
    ]


def test_label_grouping():
    kpi = _kpi(group_by="label")
    recs = _recs((D("1"), None, None, "z"), (D("2"), None, None, "a"))
    result = compute_kpi_series(recs, _kpi(group_by="label"))
    assert [(b.key, b.value) for b in result.buckets] == [("a", D("2.0000")), ("z", D("1.0000"))]


def test_grouped_empty_result_is_empty_list():
    assert compute_kpi_series([], _kpi(group_by="category")).buckets == ()
    # همه NULL category → هیچ bucket
    recs = _recs((D("1"), None, None, None))
    assert compute_kpi_series(recs, _kpi(group_by="category")).buckets == ()


def test_ungrouped_kpi_series_raises():
    with pytest.raises(KpiDefinitionError):
        compute_kpi_series([], _kpi(group_by=None))


# ---------- ۱۳–۱۴: date window inclusive ----------

def test_date_from_inclusive():
    kpi = _kpi(date_from=date(2026, 3, 10))
    recs = _recs((D("1"), date(2026, 3, 9), None, None), (D("2"), date(2026, 3, 10), None, None), (D("4"), date(2026, 3, 11), None, None))
    assert compute_kpi(recs, kpi).value == D("6.0000")  # مرز پایین داخل است


def test_date_to_inclusive():
    kpi = _kpi(date_to=date(2026, 3, 10))
    recs = _recs((D("1"), date(2026, 3, 10), None, None), (D("2"), date(2026, 3, 11), None, None))
    assert compute_kpi(recs, kpi).value == D("1.0000")  # مرز بالا داخل است


def test_date_window_excludes_null_dates():
    kpi = _kpi(date_from=date(2026, 1, 1))
    recs = _recs((D("1"), None, None, None), (D("2"), date(2026, 5, 5), None, None))
    assert compute_kpi(recs, kpi).value == D("2.0000")


# ---------- ۱۵–۱۶: structured filters ----------

def test_category_eq_neq_filters():
    recs = _recs(
        (D("1"), None, "sales", None),
        (D("2"), None, "hr", None),
        (D("4"), None, None, None),  # NULL never matches (§3.3)
    )
    eq = compute_kpi(recs, _kpi(filters=[{"field": "category", "op": "eq", "value": "sales"}]))
    assert eq.value == D("1.0000")
    neq = compute_kpi(recs, _kpi(filters=[{"field": "category", "op": "neq", "value": "sales"}]))
    assert neq.value == D("2.0000")  # NULL ردیف هم match نمی‌شود


def test_label_filter():
    recs = _recs((D("1"), None, None, "keep"), (D("2"), None, None, "drop"))
    result = compute_kpi(recs, _kpi(filters=[{"field": "label", "op": "eq", "value": "keep"}]))
    assert result.value == D("1.0000")


def test_date_filters_and_window_are_the_same_mechanism():
    recs = _recs((D("1"), date(2026, 1, 15), None, None), (D("2"), date(2026, 6, 1), None, None))
    flt = compute_kpi(recs, _kpi(filters=[{"field": "date", "op": "gte", "value": "2026-02-01"}]))
    assert flt.value == D("2.0000")
    win = compute_kpi(recs, _kpi(date_from=date(2026, 2, 1)))
    assert win.value == D("2.0000")  # همان نتیجه — §3.4


def test_multiple_filters_are_anded():
    recs = _recs(
        (D("1"), date(2026, 1, 10), "sales", "q1"),
        (D("2"), date(2026, 1, 20), "sales", "q2"),
        (D("4"), date(2026, 2, 10), "hr", "q1"),
        (D("8"), date(2026, 1, 15), "sales", "q1"),
    )
    kpi = _kpi(filters=[
        {"field": "category", "op": "eq", "value": "sales"},
        {"field": "label", "op": "eq", "value": "q1"},
        {"field": "date", "op": "lte", "value": "2026-01-15"},
    ])
    assert compute_kpi(recs, kpi).value == D("9.0000")  # 1 + 8


def test_grouping_respects_filters():
    recs = _recs((D("1"), None, "a", None), (D("2"), None, "b", None), (D("4"), None, "b", None))
    kpi = _kpi(group_by="category", filters=[{"field": "category", "op": "eq", "value": "b"}])
    result = compute_kpi_series(recs, kpi)
    assert [(b.key, b.value) for b in result.buckets] == [("b", D("6.0000"))]


# ---------- ۱۷: invalid / unmappable input ----------

def test_non_factrecord_input_raises():
    with pytest.raises(KpiComputationError):
        compute_kpi([{"measure": 1}], _kpi())


def test_unmappable_measure_type_raises():
    with pytest.raises(KpiComputationError):
        FactRecord.from_fact_row(type("R", (), {"measure_value": [1, 2], "dimension_date": None,
                                                 "dimension_category": None, "dimension_label": None})())


def test_unmappable_measure_string_raises():
    with pytest.raises(KpiComputationError):
        FactRecord.from_fact_row(type("R", (), {"measure_value": "not-a-number",
                                                "dimension_date": None,
                                                "dimension_category": None, "dimension_label": None})())


def test_unmappable_date_type_raises():
    with pytest.raises(KpiComputationError):
        FactRecord.from_fact_row(type("R", (), {"measure_value": Decimal("1"), "dimension_date": 20260101,
                                                "dimension_category": None, "dimension_label": None})())


def test_float_measure_rejected_by_design():
    """float عمداً پذیرفته نمی‌شود — دقت شناور به موتور KPI راه ندارد (§3.2)."""
    with pytest.raises(KpiComputationError):
        FactRecord.from_fact_row(type("R", (), {"measure_value": 1.5, "dimension_date": None,
                                                "dimension_category": None, "dimension_label": None})())


def test_bool_measure_rejected():
    with pytest.raises(KpiComputationError):
        FactRecord.from_fact_row(type("R", (), {"measure_value": True, "dimension_date": None,
                                                "dimension_category": None, "dimension_label": None})())


# ---------- ۱۸: Decimal precision / 4dp quantization ----------

def test_decimal_quantization_round_half_up():
    """avg(0.1, 0.2) = 0.15 → 0.1500؛ float اینجا 0.15000000000000002 می‌داد."""
    recs = _recs((D("0.1"), None, None, None), (D("0.2"), None, None, None))
    assert compute_kpi(recs, _kpi(aggregation="avg")).value == D("0.1500")


def test_sum_precision_no_float_drift():
    recs = _recs(*[(D("0.1"), None, None, None)] * 10)
    assert compute_kpi(recs, _kpi(aggregation="sum")).value == D("1.0000")


def test_measure_from_string_and_int_paths():
    r1 = FactRecord.from_fact_row(type("R", (), {"measure_value": "12.3456", "dimension_date": None,
                                                 "dimension_category": None, "dimension_label": None})())
    r2 = FactRecord.from_fact_row(type("R", (), {"measure_value": 7, "dimension_date": None,
                                                 "dimension_category": None, "dimension_label": None})())
    assert compute_kpi([r1, r2], _kpi(aggregation="sum")).value == D("19.3456")


def test_long_repeating_decimal_quantized():
    recs = _recs((D("1"), None, None, None), (D("1"), None, None, None), (D("1"), None, None, None))
    # avg = 1 ; با ۱۰ و ۳ → 3.333333... → 3.3333 (ROUND_HALF_UP)
    recs2 = _recs((D("10"), None, None, None), (D("1"), None, None, None))
    assert compute_kpi(recs2, _kpi(aggregation="avg")).value == D("5.5000")
    recs3 = _recs((D("10"), None, None, None), (D("0"), None, None, None), (D("1"), None, None, None))
    assert compute_kpi(recs3, _kpi(aggregation="avg")).value == D("3.6667")


# ---------- ۱۹–۲۰: determinism / no zero-fill ----------

def test_deterministic_ordering_independent_of_input_order():
    recs_a = _recs((D("1"), date(2026, 1, 2), "b", None), (D("2"), date(2026, 1, 1), "a", None))
    recs_b = list(reversed(recs_a))
    ka = compute_kpi_series(recs_a, _kpi(group_by="date", granularity="day"))
    kb = compute_kpi_series(recs_b, _kpi(group_by="date", granularity="day"))
    assert ka == kb  # ترتیب ورودی اثری ندارد
    kc = compute_kpi_series(recs_a, _kpi(group_by="category"))
    kd = compute_kpi_series(recs_b, _kpi(group_by="category"))
    assert kc == kd


def test_no_zero_fill_for_missing_buckets():
    """فقط bucketهای مشاهده‌شده — گپ تقویم bucket صفر نمی‌گیرد (§3.5)."""
    kpi = _kpi(group_by="date", granularity="day")
    recs = _recs((D("1"), date(2026, 1, 1), None, None), (D("2"), date(2026, 1, 5), None, None))
    result = compute_kpi_series(recs, kpi)
    assert [b.key for b in result.buckets] == ["2026-01-01", "2026-01-05"]  # بدون 01-02..01-04
    assert len(result.buckets) == 2


def test_month_gap_no_zero_fill():
    kpi = _kpi(group_by="date", granularity="month")
    recs = _recs((D("1"), date(2026, 1, 1), None, None), (D("2"), date(2026, 4, 1), None, None))
    assert [b.key for b in compute_kpi_series(recs, kpi).buckets] == ["2026-01", "2026-04"]
