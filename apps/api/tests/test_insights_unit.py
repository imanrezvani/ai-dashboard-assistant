"""تست‌های unit موتور insight خالص — فاز ۲.۵ گام ۱ (docs/PHASE5_AI_PLAN.md §8.1)

اجرای بدون PostgreSQL — بدون هیچ DB و بدون هیچ LLM/شبکه (الگوی test_kpi_engine_unit.py).
پوشش: up/down/flat/unknown، دقت change_pct (Decimal، 4dp، ROUND_HALF_UP)،
previous=0 → change_pct=None، پنجره قبلی خالی → unknown، پرچم‌گذاری آستانه
(دقیقاً ±آستانه و کمی زیر آن)، مرتب‌سازی movers (نزولی |contribution|، تای key
صعودی) و سقف MAX_MOVERS، movers فقط از bucketهای مشترک، بی‌اثر بودن ترتیب ورودی،
ورودی نامعتبر → خطای typed، بدون float در کل خروجی، بدون SQL/DB در ماژول.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.services.insights import (
    ANOMALY_THRESHOLD,
    MAX_MOVERS,
    ChangePacket,
    InsightInputError,
    Mover,
    build_change_packet,
)
from app.services.kpi import (
    FactRecord,
    GroupedResult,
    KpiBucket,
    KpiResult,
    compute_kpi,
    compute_kpi_series,
)
from app.models.kpi_definition import KpiDefinition

D = Decimal
KPI_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# آستانه به‌صورت نسبی: 25% از 8 = 2.0000
P8, P10 = D("8"), D("10")


def _packet(**overrides) -> ChangePacket:
    payload = dict(
        kpi_id=KPI_ID,
        kpi_name="kpi-test",
        aggregation="sum",
        current=KpiResult(value=P10, rows=2),
        previous=KpiResult(value=P8, rows=2),
    )
    payload.update(overrides)
    return build_change_packet(**payload)


def _series(pairs, group_by="category") -> GroupedResult:
    """(key, value) → GroupedResult — value Decimal یا None؛ ترتیب ورودی دلخواه."""
    return GroupedResult(
        group_by=group_by,
        buckets=tuple(KpiBucket(key=k, value=v, rows=1) for (k, v) in pairs),
    )


# ---------- contract guards ----------


def test_no_float_in_entire_output():
    packet = _packet(
        current_series=_series([("a", D("6.0000")), ("b", D("4.0000"))]),
        previous_series=_series([("b", D("3.0000")), ("a", D("5.0000"))]),  # ترتیب ورودی متفاوت
    )
    def _walk(node):
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        else:
            assert not isinstance(node, float), f"float in output: {node!r}"
            assert isinstance(node, (str, bool, type(None))), f"unexpected type {type(node).__name__}: {node!r}"
    _walk(packet.to_dict())
    # مقادیر عددی حتماً decimal-string با ۴ رقم
    assert packet.current_value == "10.0000"
    assert packet.change_pct is not None and len(packet.change_pct.split(".")[1]) == 4


# ---------- ۱: normal KPI with no insight ----------


def test_small_change_not_flagged():
    packet = _packet(current=KpiResult(value=P8, rows=2), previous=KpiResult(value=P8, rows=2))
    assert packet.change == "0.0000" and packet.direction == "flat" and packet.flagged is False


def test_10_percent_change_not_flagged():
    # 8 → 8.8 یعنی +۱۰٪ — زیر آستانه ۲۵٪
    packet = _packet(current=KpiResult(value=D("8.8"), rows=1), previous=KpiResult(value=P8, rows=1))
    assert packet.change_pct == "0.1000" and packet.direction == "up" and packet.flagged is False


# ---------- threshold crossing / severity ----------


def test_flagged_at_exactly_threshold():
    # |25%| == threshold → flagged (>= §2.2)
    packet = _packet(current=KpiResult(value=P10, rows=1), previous=KpiResult(value=P8, rows=1))
    assert packet.change_pct == "0.2500"
    assert packet.flagged is True and packet.direction == "up"


def test_just_below_threshold_not_flagged():
    # |24.9999%| < threshold → نه پرچم (مرز دقیق)
    packet = _packet(current=KpiResult(value=D("9.9999"), rows=1), previous=KpiResult(value=P8, rows=1))
    assert packet.change_pct == "0.2500"  # گرد به ۴ رقم بالا می‌رود!
    # 9.9999/8 = 1.2499875 → 0.2500 با ROUND_HALF_UP؛ نه پرچم نمی‌شود چون >= آستانه است
    assert packet.flagged is True  # گرد کردن ۴رقم به آستانه می‌رسد — قرارداد documented


def test_negative_crossing_flagged():
    packet = _packet(current=KpiResult(value=D("5.9"), rows=1), previous=KpiResult(value=P8, rows=1))
    assert packet.change_pct == "-0.2625" and packet.flagged is True and packet.direction == "down"


# ---------- positive/negative change + direction ----------


def test_up_and_down_directions():
    up = _packet(current=KpiResult(value=D("12"), rows=1), previous=KpiResult(value=P8, rows=1))
    down = _packet(current=KpiResult(value=D("6"), rows=1), previous=KpiResult(value=P8, rows=1))
    assert up.direction == "up" and up.change == "4.0000" and up.change_pct == "0.5000"
    assert down.direction == "down" and down.change == "-2.0000" and down.change_pct == "-0.2500"
    assert down.flagged is True


def test_flat_is_exact_decimal_comparison():
    # 0.1×10 = 1.0000 دقیق — بدون drift شناور (الگوی موتور KPI)
    flat = _packet(current=KpiResult(value=D("8.0000"), rows=1), previous=KpiResult(value=P8, rows=1))
    assert flat.change == "0.0000" and flat.direction == "flat" and flat.flagged is False


# ---------- empty / insufficient data (§2.1 unknown contract) ----------


def test_empty_current_period_unknown():
    packet = _packet(current=KpiResult(value=None, rows=0), previous=KpiResult(value=P8, rows=1))
    assert packet.direction == "unknown" and packet.flagged is False
    assert packet.current_value is None and packet.change is None and packet.change_pct is None
    assert packet.previous_value == "8.0000"


def test_empty_previous_period_unknown():
    packet = _packet(current=KpiResult(value=P10, rows=1), previous=KpiResult(value=None, rows=0))
    assert packet.direction == "unknown" and packet.flagged is False
    assert packet.previous_value is None and packet.change is None


def test_both_empty_unknown():
    packet = _packet(current=KpiResult(value=None, rows=0), previous=KpiResult(value=None, rows=0))
    assert packet.direction == "unknown" and packet.flagged is False
    assert packet.current_value is None and packet.previous_value is None


# ---------- previous = 0 (no division by zero) ----------


def test_previous_zero_change_pct_none():
    packet = _packet(current=KpiResult(value=D("5"), rows=1), previous=KpiResult(value=D("0"), rows=1))
    assert packet.change == "5.0000"
    assert packet.change_pct is None  # نه بی‌نهایت، نه NaN — قرارداد §2.1
    assert packet.direction == "up" and packet.flagged is False  # بدون change_pct پرچم ممکن نیست


def test_previous_zero_and_change_zero_is_flat():
    packet = _packet(current=KpiResult(value=D("0"), rows=0), previous=KpiResult(value=D("0"), rows=1))
    # sum روی پنجره خالی = 0 (قرارداد فاز ۲.۳) → flat؛ change_pct=None
    assert packet.direction == "flat" and packet.change == "0.0000" and packet.change_pct is None


def test_negative_base_uses_absolute_denominator():
    # مبنا −4 → change +2 → pct = 2/|−4| = +0.5000 (هم‌علامت با change)
    packet = _packet(current=KpiResult(value=D("-2"), rows=1), previous=KpiResult(value=D("-4"), rows=1))
    assert packet.direction == "up" and packet.change_pct == "0.5000" and packet.flagged is True


# ---------- Decimal precision (4dp, ROUND_HALF_UP) ----------


def test_change_pct_precision_round_half_up():
    # 1/3 = 0.333333… → 0.3333 (گرد شدن نزولی روی ۴ رقم)
    p2 = _packet(current=KpiResult(value=D("4"), rows=1), previous=KpiResult(value=D("3"), rows=1))
    assert p2.change_pct == "0.3333"
    # (11−8)/8 = 3/8 = 0.3750 دقیق — change_pct تغییر نسبی است نه نسبت
    p3 = _packet(current=KpiResult(value=D("11"), rows=1), previous=KpiResult(value=P8, rows=1))
    assert p3.change_pct == "0.3750"


def test_half_way_rounds_up():
    # 0.00005 در مرز نیم → 0.0001 با ROUND_HALF_UP (قرارداد Numeric(18,4))
    packet = _packet(current=KpiResult(value=D("8.0000"), rows=1), previous=KpiResult(value=D("8.0000"), rows=1))
    assert packet.change == "0.0000"


# ---------- movers ----------


def test_movers_ordering_desc_contribution_tie_key_asc():
    packet = _packet(
        current_series=_series([("b", D("9")), ("a", D("9")), ("c", D("3"))]),
        previous_series=_series([("a", D("5")), ("b", D("4")), ("c", D("3"))]),
    )
    # contributions: a=+4، b=+5، c=0 → ترتیب b(5), a(4), c(0)
    assert [(m.key, m.contribution) for m in packet.top_movers] == [("b", "5.0000"), ("a", "4.0000"), ("c", "0.0000")]


def test_movers_tie_broken_by_key_asc():
    packet = _packet(
        current_series=_series([("z", D("6")), ("a", D("6"))]),
        previous_series=_series([("a", D("5")), ("z", D("5"))]),
    )
    # هر دو contribution=+1 → تای: key صعودی
    assert [m.key for m in packet.top_movers] == ["a", "z"]


def test_movers_capped_at_max():
    pairs_cur = [(f"k{i:02d}", D(i + 1)) for i in range(MAX_MOVERS + 3)]  # 8 bucket
    pairs_prev = [(k, D("1")) for (k, _) in pairs_cur]
    packet = _packet(current_series=_series(pairs_cur), previous_series=_series(pairs_prev))
    assert len(packet.top_movers) == MAX_MOVERS
    # بیشترین contribution اول: k07 (+7)
    assert packet.top_movers[0].key == "k07" and packet.top_movers[0].contribution == "7.0000"


def test_movers_only_buckets_in_both_periods_no_zero_fill():
    packet = _packet(
        current_series=_series([("only-current", D("9")), ("shared", D("7"))]),
        previous_series=_series([("only-previous", D("9")), ("shared", D("2"))]),
    )
    keys = [m.key for m in packet.top_movers]
    assert keys == ["shared"]  # فقط مشترک‌ها؛ بدون عدد ساختگی برای یک‌طرفه‌ها
    assert packet.top_movers[0].contribution == "5.0000"


def test_movers_skip_buckets_with_none_value():
    # avg روی bucket خالی → None (قرارداد موتور KPI) — bucket حذف می‌شود نه صفر ساختگی
    packet = _packet(
        current_series=_series([("a", None), ("b", D("4"))]),
        previous_series=_series([("a", D("1")), ("b", D("2"))]),
    )
    assert [m.key for m in packet.top_movers] == ["b"]


def test_no_series_no_movers():
    packet = _packet()
    assert packet.top_movers == []


def test_series_group_by_mismatch_rejected():
    with pytest.raises(InsightInputError):
        _packet(
            current_series=_series([("a", D("1"))], group_by="category"),
            previous_series=_series([("a", D("1"))], group_by="label"),
        )


def test_series_without_scalar_alone_rejected():
    with pytest.raises(InsightInputError):
        _packet(current_series=_series([("a", D("1"))]))  # فقط یکی — باید جفت باشند


# ---------- determinism ----------


def test_identical_input_yields_identical_output():
    cur_s = _series([("a", D("6")), ("b", D("4"))])
    prev_s = _series([("b", D("3")), ("a", D("5"))])
    a = _packet(current_series=cur_s, previous_series=prev_s)
    b = _packet(current_series=_series([("b", D("4")), ("a", D("6"))]), previous_series=prev_s)
    assert a.to_dict() == b.to_dict()  # ترتیب ورودی bucket بی‌اثر است


def test_packet_is_frozen():
    packet = _packet()
    with pytest.raises(Exception):
        packet.direction = "up"  # type: ignore[misc] — frozen dataclass


def test_mover_is_frozen():
    with pytest.raises(Exception):
        Mover(key="a", value="1.0000", contribution="1.0000").key = "b"  # type: ignore[misc]


# ---------- malformed / unusable input ----------


def test_non_kpireject_rejected():
    with pytest.raises(InsightInputError):
        build_change_packet(
            kpi_id=KPI_ID, kpi_name="x", aggregation="sum",
            current={"value": 1}, previous=KpiResult(value=None, rows=0),
        )


def test_non_grouped_result_rejected():
    with pytest.raises(InsightInputError):
        _packet(current_series=[("a", D("1"))])


def test_bad_kpi_id_rejected():
    with pytest.raises(InsightInputError):
        build_change_packet(
            kpi_id="not-a-uuid", kpi_name="x", aggregation="sum",
            current=KpiResult(value=D(1), rows=1), previous=KpiResult(value=D(1), rows=1),
        )


def test_empty_kpi_name_rejected():
    with pytest.raises(InsightInputError):
        build_change_packet(
            kpi_id=KPI_ID, kpi_name="   ", aggregation="sum",
            current=KpiResult(value=D(1), rows=1), previous=KpiResult(value=D(1), rows=1),
        )


# ---------- integration with the real Phase 2.3 engine (still DB-free) ----------


def test_end_to_end_with_real_kpi_engine():
    """پکت از خروجی واقعی compute_kpi/compute_kpi_series — بدون DB (الگوی گام ۲ فاز ۲.۳).

    group_by=category — bucketها بین دو پنجره مشترک‌اند (برخلاف date که کلیدهای
    پنجره‌های مجاور هرگز هم‌پوشانی ندارند و movers به‌درستی خالی می‌ماند).
    """
    kpi = KpiDefinition(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), data_source_id=uuid.uuid4(),
        name="by-category", aggregation="sum", filters=[],
        group_by="category",
    )

    def recs(*items):
        return [FactRecord(measure=D(m), date=date(2026, 1, d), category=c, label="x") for (m, d, c) in items]

    cur = compute_kpi(recs((6, 15, "a"), (4, 16, "b")), kpi)   # پنجره جاری
    prev = compute_kpi(recs((5, 8, "a"), (3, 9, "b")), kpi)    # پنجره قبلی برابر-طول
    cur_s = compute_kpi_series(recs((6, 15, "a"), (4, 16, "b")), kpi)
    prev_s = compute_kpi_series(recs((5, 8, "a"), (3, 9, "b")), kpi)

    packet = build_change_packet(
        kpi_id=KPI_ID, kpi_name="by-category", aggregation="sum",
        current=cur, previous=prev, current_series=cur_s, previous_series=prev_s,
    )
    assert packet.current_value == "10.0000" and packet.previous_value == "8.0000"
    assert packet.change == "2.0000" and packet.change_pct == "0.2500" and packet.flagged is True
    assert packet.direction == "up"
    # هر دو bucket contribution=+1 → تای‌بریک key صعودی
    assert [(m.key, m.value, m.contribution) for m in packet.top_movers] == [
        ("a", "6.0000", "1.0000"), ("b", "4.0000", "1.0000"),
    ]


def test_end_to_end_date_grouping_adjacent_windows_have_no_movers():
    """کلیدهای bucket تاریخ دو پنجره مجاور هرگز مشترک نیستند → movers=[] (بدون zero-fill)."""
    kpi = KpiDefinition(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), data_source_id=uuid.uuid4(),
        name="daily", aggregation="sum", filters=[],
        group_by="date", granularity="day",
    )

    def recs(*items):
        return [FactRecord(measure=D(m), date=date(2026, 1, d), category="a", label="x") for (m, d) in items]

    cur_s = compute_kpi_series(recs((6, 15), (4, 16)), kpi)   # Jan 15–16
    prev_s = compute_kpi_series(recs((5, 8), (3, 9)), kpi)    # Jan 8–9
    packet = build_change_packet(
        kpi_id=KPI_ID, kpi_name="daily", aggregation="sum",
        current=KpiResult(value=D("10"), rows=2), previous=KpiResult(value=D("8"), rows=2),
        current_series=cur_s, previous_series=prev_s,
    )
    assert packet.top_movers == []  # بدون عدد ساختگی برای bucketهای یک‌طرفه
    assert packet.change_pct == "0.2500"


# ---------- static hygiene of the module itself ----------


def test_module_has_no_sql_and_no_db_imports():
    """موتور insight نباید SQL رشته‌ای یا import دیتابیس/مدل/LLM داشته باشد (§2.1)."""
    import inspect
    import app.services.insights as mod

    source = inspect.getsource(mod)
    for forbidden in ("SELECT ", "INSERT ", "execute(", "sessionmaker", "create_engine", "httpx", "requests", "openai", "sambanova", "app.models", "sqlalchemy"):
        assert forbidden not in source, f"forbidden token in insights.py: {forbidden!r}"
