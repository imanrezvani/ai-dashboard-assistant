# PHASE 2.3 PLAN — KPI Engine (تصمیم‌یار)

> **Status of this document:** authoritative implementation plan for Phase 2.3.
> A future coding agent must be able to implement Phase 2.3 from this document alone,
> without conversation history.
> **Companion docs:** `docs/PHASE_STATUS.md` (per-phase status, Persian) ·
> `docs/PHASE2_PLAN.md` (completed Phase 2.2 contract — conventions referenced here).
> **Baseline this plan builds on:** Phase 2.2 COMPLETE at commit `18e445d`
> (`feat(api): implement phase 2.2 import bridge`); 95 tests passed / 0 failed / 0 skipped;
> Alembic head `0005_data_source_provenance`; app role `tasmim_app` NOSUPERUSER + NOBYPASSRLS.

---

## 0. Evidence base (verified repository facts this plan relies on)

Every design decision below is anchored to current code — none are assumptions:

| Fact | Source |
|---|---|
| Fact layer is intentionally narrow: `fact_rows` = `measure_value Numeric(18,4) NOT NULL`, `dimension_date Date NULL`, `dimension_category String(255) NULL`, `dimension_label String(255) NULL`, `organization_id`, `data_source_id` | `app/models/fact_row.py` |
| Map flow semantics: rows with NULL/unparseable measure are **skipped**; dates coerced via `pd.to_datetime(errors="coerce")`; category/label truncated to 255 chars; re-map deletes previous `fact_rows` first | `app/routers/data_sources.py` (`map_data_source`) |
| `DataSource.status` ∈ `pending / mapped / failed`; only mapped sources have fact rows | `app/models/data_source.py`, upload/map flow |
| Role model: `RoleEnum(owner>admin>manager>analyst>viewer)` + `require_role(allowed_roles: list[str])` matching enum string values; 403 below threshold | `app/models/membership.py`, `app/middleware/tenant.py` |
| Authorization precedent (Phase 2.2): credential/connection management → admin+; config inspection → manager+; data consumption → analyst+; **viewer sees nothing**; cross-org id → **404** (never 403) | `app/routers/database_connections.py`, `docs/PHASE2_PLAN.md` §6 |
| RLS convention: per-table entry in `rls_tables` dict in `app/main.py` — `ENABLE` + `FORCE` + fail-closed policy `organization_id::text = current_setting('app.current_org_id', true)` with identical `WITH CHECK`; **no RLS in migrations** | `app/main.py`, Phase 2.1/2.2 gates |
| Migration chain: `… → 0003_align_created_at_nullable → 0004_database_connections → 0005_data_source_provenance` (single head); **model↔migration parity is enforced by `alembic check`** — every non-Optional `Mapped[X]` must be `nullable=False` in DDL, and index-constrained columns must carry `index=True` on the model (both drift classes were caught by real gates) | `apps/api/alembic/versions/`, `docs/PHASE2_PLAN.md` §2/§8 |
| Ingestion helpers live in `app/services/ingest.py` (Phase 2.2 extraction) — the `services/` package is the established home for business logic | `app/services/ingest.py` |
| Uploads capped at 10,000 rows; imports clamped by connector `MAX_FETCH_ROWS = 100_000` → per-org fact volume is bounded and small; compute-on-read is cheap | `app/services/ingest.py`, `app/connectors/postgres.py` |
| Tests: integration modules run on real PostgreSQL (module-level skip w/o DB), self-restoring `dependency_overrides` fixtures inside fixtures only (never at import time), module-scoped row cleanup by unique prefixes — three gates proved import-time state mutation breaks suites | `tests/test_data_sources.py`, `tests/test_import_bridge.py`, Phase 2.1 gate findings |
| Roadmap: README "مرحله بعد: داشبورد KPI و دستیار هوش مصنوعی"; Phase 2.2 plan §12: 2.3 = KPI Engine, 2.4 = Context Catalog | `README.md`, `docs/PHASE2_PLAN.md` §12 |

---

## 1. Objective

Phase 2.3 adds the **smallest production-oriented KPI layer**: an organization-scoped,
structured KPI definition (no SQL) and a **deterministic, pure computation service**
that aggregates the existing `fact_rows` of one mapped `DataSource`. Results are
computed on read. This is the numerical foundation Phase 2.4's Context Catalog and
the later AI assistant will consume — nothing more.

Scope is deliberately narrow: **one data source per KPI, five aggregations, structured
filters/grouping, day/week/month series, compute-on-read**. Everything else is a
non-goal (§9).

---

## 2. KpiDefinition domain model

New model `apps/api/app/models/kpi_definition.py`; new table `kpi_definitions`
(created by Alembic migration `0006_kpi_definitions`,
`down_revision = "0005_data_source_provenance"`).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | default `uuid4` (repo convention) |
| `organization_id` | UUID, NOT NULL, FK → `organizations.id ON DELETE CASCADE`, indexed | tenant anchor (same as every data table) |
| `data_source_id` | UUID, NOT NULL, FK → `data_sources.id ON DELETE CASCADE`, indexed | **one source per KPI.** Deleting the source deletes its fact rows (existing CASCADE, `fact_rows.data_source_id`) and its KPIs — no orphan definitions. Rationale for single-source: `fact_rows` has exactly one measure column; cross-source joins/comparison are out of scope (§9) |
| `name` | String(255), NOT NULL | display name; unique per organization (`UniqueConstraint("organization_id", "name", name="uq_kpi_org_name")` — same pattern as `uq_db_conn_org_name`) |
| `description` | String(500), nullable | free text |
| `aggregation` | String(20), NOT NULL | one of `sum / avg / min / max / count` — validated at the schema layer; no other value can be persisted |
| `filters` | JSONB, NOT NULL, default `[]` | **structured filter list, never SQL** (see §3.4 for the exact schema and validation) |
| `group_by` | String(20), nullable | one of `date / category / label`, or NULL (no grouping) |
| `granularity` | String(20), nullable | one of `day / week / month`; **must be non-NULL iff `group_by == "date"`** (validated); NULL otherwise |
| `date_from` / `date_to` | Date, nullable | optional inclusive window on `dimension_date`; validated `date_from <= date_to` when both present |
| `enabled` | Boolean, NOT NULL, default True | disabled KPIs reject compute/series with **400** (same semantic as `database_connections.enabled`) |
| `created_at` / `updated_at` | DateTime(timezone=True), NOT NULL | `server_default=func.now()`; `updated_at` via `onupdate=func.now()` |

**No `comparison` column in Phase 2.3** (§9): previous-period comparison is deferred —
the client can call compute/series twice with two KPI definitions or the series
endpoint already returns the ordered bucket series a comparison needs. Adding a
comparison config now would be speculative structure.

**Schema note (the two `alembic check` drift classes already met in real gates):**
non-Optional `Mapped[X]` → `nullable=False`; `Mapped[Optional[X]]` → `nullable=True`;
`organization_id` and `data_source_id` must carry `index=True` on the model because
the migration creates their indexes. `filters` uses `postgresql.JSONB` — on SQLite
tests SQLAlchemy renders JSON; no dialect-specific code in the model.

Model exported from `app/models/__init__.py` (same convention as `DatabaseConnection`).

---

## 3. KPI Engine — deterministic, pure computation layer

New module `app/services/kpi.py` (the `services/` package established in Phase 2.2).

### 3.1 Design: pure functions over fetched rows

The engine is a **pure Python layer** operating on an in-memory sequence of
normalized fact records (`dataclass FactRecord(measure: Decimal, date: date | None,
category: str | None, label: str | None)`) plus a validated `KpiDefinition`. The API
layer fetches rows through the **existing SQLAlchemy ORM query on `FactRow`**
(RLS + explicit `organization_id` filter apply — defense-in-depth, same three-layer
pattern as `FileStorage`/connector) and converts them to records. Consequences:

- **No arbitrary SQL anywhere** — the engine contains zero SQL strings; the API
  layer uses only ORM filters (§6). There is no method anywhere that accepts SQL text.
- **Deterministic and unit-testable without a database** (plain-Python unit tests
  on SQLite-less CI mirrors `test_storage_unit.py`'s pattern).
- Volume is bounded (≤10k upload rows; ≤100k import clamp) — in-memory aggregation
  is correct and fast; streaming/vectorization is premature (§9).

### 3.2 Supported aggregations (exact semantics)

| Aggregation | Semantics over the filtered row set |
|---|---|
| `sum` | Σ `measure` |
| `avg` | arithmetic mean of `measure` |
| `min` / `max` | smallest / largest `measure` |
| `count` | number of rows in the filtered set (measure is always NOT NULL in `fact_rows`, so count == non-null measure count) |

`measure` values are read as `Decimal` (the column is `Numeric(18,4)`) — **no float
aggregation**. Results are quantized to **4 decimal places** (`Decimal.quantize`,
ROUND_HALF_UP) matching the column scale, and serialized as strings in JSON to avoid
float precision loss (pydantic field typed to accept Decimal; document the wire
format as a decimal string).

### 3.3 Null and empty handling (deterministic contract)

- `fact_rows.measure_value` is NOT NULL by schema — no null measures can occur; the
  engine still tolerates `None` measures defensively by **excluding them** from
  sum/avg/min/max and from `count`.
- Filters on `category`/`label`: rows with **NULL on the filtered dimension never
  match** a filter (SQL `=`-like semantics, documented).
- Grouping by `category`/`label`: NULL-dimension rows are **excluded from grouped
  output** (a "NULL bucket" would leak schema noise into dashboards); they still
  count toward the ungrouped total.
- Empty filtered set: `sum`/`count` → `0`, `avg`/`min`/`max` → `null`; grouped/series
  result → empty list. HTTP 200 in all cases (an empty result is a valid answer, not
  an error). A KPI whose `data_source` has status != `mapped` → **400** at compute
  time ("source not mapped") — no silent zeros from an unmapped source.

### 3.4 Filters (structured, allow-listed)

`filters` is a JSON array; each element must match exactly:

```json
{"field": "category" | "label", "op": "eq" | "neq", "value": "<string, 1..255 chars>"}
```

plus optionally one element with `{"field": "date", "op": "gte" | "lte", "value": "YYYY-MM-DD"}`.
Rules: max **10** filter elements; unknown `field`/`op` → validation error (422);
`date` filters and the definition's `date_from`/`date_to` are the same mechanism —
both are applied to `dimension_date` (definition window AND filter window, both
inclusive). Everything else (free-text predicates, `IN` lists, regex, cross-field
logic) is a non-goal (§9). Pydantic validates the shape at the API boundary; the
engine re-validates defensively (fail-closed: an invalid filter present in the DB
row yields HTTP 500-avoidant 422-style error at compute, never a wrong number).

### 3.5 Grouping and time series

- `group_by = "category" | "label"` → one result row per distinct non-NULL value of
  that dimension (after filters), each with the same aggregation semantics as §3.2,
  **sorted by the dimension value ascending** (byte/codepoint order — deterministic).
- `group_by = "date"` with `granularity` → buckets via calendar boundaries in
  **UTC-agnostic date arithmetic on the `Date` column** (no timezone conversion —
  `dimension_date` is a plain date): `day` = the date itself, `week` = ISO week
  starting Monday, `month` = first of month. Buckets sorted ascending; **only
  buckets present in the data are returned** (zero-filling the calendar is a
  presentation concern for the client and is documented as deferred, §9).
- Response shape (grouped/series): `{"group_by": ..., "buckets": [{"key": "<value or YYYY-MM-DD>", "value": "<decimal-string or null>", "rows": <int>}, ...]}`.

### 3.6 Validation of definitions (single source of rules)

Pydantic schemas (`app/schemas/kpi.py`) enforce: name 1..255, description ≤500,
`aggregation` ∈ enum, `filters` allow-list (§3.4), `group_by` ∈ enum ∪ NULL,
granularity rule (§2), date ordering (§2). The engine independently re-validates the
persisted definition before computing (defensive; DB rows could in principle be
written by other paths) and raises a typed `KpiDefinitionError` mapped to 422.

### 3.7 Source of truth: compute-on-read (decision + rationale)

**Decision: compute-on-read. No cache, no snapshot table.**

Rationale from repository facts: (a) per-org fact volume is bounded and small
(§0), so aggregation is milliseconds; (b) `fact_rows` mutate on re-map (delete +
re-insert), so any cache would need invalidation logic on every map — complexity
with no measurable benefit today; (c) no dashboard exists yet that would amplify
read load (the web app is still Phase 1). A `kpi_snapshots` table or TTL cache is
**premature infrastructure** and is explicitly listed in §9; revisit only if a real
workload appears (document as technical debt trigger, not as scheduled work).

---

## 4. API surface

Router `app/routers/kpis.py` (`prefix="/kpis"`, `tags=["kpis"]`), registered in
`app/main.py`. Same request conventions as existing routers:
`require_role(...)` + `X-Organization-Id` semantics + cross-org id → **404**
(RLS + explicit `organization_id` filter, existence never leaked).

| Endpoint | Method | Purpose | Authorization |
|---|---|---|---|
| `/kpis` | POST | create definition (validated per §3.6; `data_source_id` must be an org-scoped **mapped** source → 404 cross-org, 400 if not mapped) | **manager+** → `["owner", "admin", "manager"]` |
| `/kpis` | GET | list org KPIs (ordered `created_at desc`) | **analyst+** |
| `/kpis/{id}` | GET | single definition | **analyst+** |
| `/kpis/{id}` | PATCH | partial update (same validation as create; rotate = edit — no secrets involved, unlike connections) | **manager+** |
| `/kpis/{id}` | DELETE | hard delete | **manager+** |
| `/kpis/{id}/compute` | POST | compute-on-read → `{"kpi_id", "value", "rows", "computed_at"}` (ungrouped scalar) | **analyst+** |
| `/kpis/{id}/series` | GET | grouped/series result per §3.5 (`group_by`/`granularity` from the definition) | **analyst+** |

Authorization rationale (least privilege, mirroring Phase 2.2 §6): KPI definitions
carry **no credentials** and trigger **no outbound connections**, so configuration is
one notch lower than connections (manager+ instead of admin+); consuming computed
numbers is analysis work (analyst+); viewers see nothing. Role strings are exactly
the `RoleEnum` values — no new roles.

Error semantics (all documented, deterministic): unknown/cross-org id → **404**;
disabled KPI compute/series → **400**; source not mapped → **400**; invalid
definition payload → **422**; empty result → **200** with §3.3 semantics; duplicate
name in org → **409** (flush-catch pattern like `create_connection`).

---

## 5. Data model compatibility

The fact layer stays **exactly as-is** — no column added, no semantics changed
(`fact_rows` already carries everything a KPI needs: one measure + three
dimensions). The only new table is `kpi_definitions`. The compute path reads
`fact_rows` through the ORM; re-map invalidates nothing because there is no cache
(§3.7). `DataSource` is untouched; `kpi_definitions.data_source_id` merely
references it. This satisfies "do not redesign `fact_rows` unless strictly
necessary" — it is not necessary.

---

## 6. Multi-tenancy / security

1. **organization_id** — mandatory NOT NULL column; every query in the router
   filters it explicitly (defense-in-depth layer 3, same as `FileStorage`).
2. **RLS ENABLE + FORCE** — `kpi_definitions` added to the `rls_tables` dict in
   `app/main.py` with the **identical fail-closed policy** used by `data_sources` /
   `fact_rows` / `data_source_files` / `data_source_columns` / `database_connections`
   (`USING` + `WITH CHECK` on `organization_id::text = current_setting('app.current_org_id', true)`,
   no `IS NULL` fallback). No RLS statements in the migration (repo convention).
3. **App role unchanged** — `tasmim_app` stays `NOSUPERUSER` + `NOBYPASSRLS`;
   `init-db/` untouched; FORCE is effective because the app role owns the table but
   cannot bypass RLS.
4. **Compute is tenant-scoped by construction** — the fact query inside compute
   filters `FactRow.organization_id == membership.organization_id` **and** runs
   under the request's RLS context (`set_rls_context` via `require_membership`);
   the KPI row itself was already resolved through RLS + org filter (cross-org → 404
   before any computation).
5. **No secrets involved** — KPI definitions contain no credentials; nothing to
   encrypt; responses never embed raw row data, only aggregates.

---

## 7. Alembic

- One migration: `apps/api/alembic/versions/0006_kpi_definitions.py`,
  `down_revision = "0005_data_source_provenance"` — linear chain
  `<base> → 0001 → 0002 → 0003 → 0004 → 0005 → 0006`, single head.
- Creates `kpi_definitions` (columns per §2, unique constraint, two indexes).
- **No `create_all`** anywhere; startup keeps `run_startup_migrations(engine)`.
- Gate procedure (established): fresh empty PostgreSQL → `upgrade head` as the app
  role → `current` = `0006_kpi_definitions` → exactly one head →
  `alembic check` = "No new upgrade operations detected" **before and after** the
  test run.

---

## 8. Testing strategy

Follow existing conventions: real-PostgreSQL integration modules with module-level
skip w/o DB, fixtures-only override manipulation, module-scoped row cleanup by
unique prefixes (`kpi-*` slugs/names, `kpi-` emails); pure unit tests need no DB.

1. **Definition validation** — schema-level: name bounds, aggregation enum,
   filter allow-list (unknown field/op/value length), granularity↔group_by rule,
   date ordering; model-level: per-org unique name (409), FK to `data_sources`,
   defaults, timestamps.
2. **Aggregation correctness (pure unit)** — sum/avg/min/max/count against
   hand-computed fixtures; Decimal quantization to 4dp (e.g. `0.1+0.2` case);
   deterministic sort order of grouped output.
3. **Filters (pure unit)** — category/label eq/neq incl. NULL-dimension exclusion
   (§3.3), date window inclusive bounds, definition window AND filter window,
   >10 filters rejected.
4. **Grouping / series (pure unit)** — category/label groups; day/week/month bucket
   boundaries (month-end, ISO week Monday start, multi-year span); only observed
   buckets; empty data → empty list.
5. **Null/empty (pure unit + integration)** — §3.3 contract exactly (0 vs null vs
   empty list); unmapped source → 400 through the API.
6. **Tenant isolation (integration, real PostgreSQL)** — org B cannot GET/compute/
   PATCH/DELETE org A's KPI (**404**); org B's compute never sees org A's fact rows
   even when both orgs define a KPI over same-named sources; RLS row-level checks.
7. **RLS runtime (integration, real PostgreSQL)** — `relrowsecurity` + `relforcerowsecurity`
   true on `kpi_definitions`; fail-closed policy qual; no-context SELECT → 0 rows;
   no-context INSERT → rejected by `WITH CHECK` (extend the existing assertion loop
   pattern in `tests/test_data_sources.py`).
8. **Authorization (integration)** — 401 unauthenticated; viewer → 403 on
   everything; analyst → 403 on create/update/delete, 200 on list/get/compute/
   series; manager → 200 on all; role strings only from `RoleEnum`.
9. **API behavior (integration)** — create over cross-org source → 404; over
   unmapped source → 400; compute correctness end-to-end (seed CSV → upload → map →
   compute == expected Decimal); series endpoint shape; PATCH partial update;
   DELETE; duplicate name → 409; disabled → 400.
10. **Cascade (integration)** — deleting the mapped `DataSource` deletes its KPIs
    (FK CASCADE) and their fact rows; no orphans.
11. **Regression** — `cd apps/api && .venv/bin/pytest -q -rs` all green, 0 skipped,
    on real PostgreSQL; the existing 95 tests must not regress.
12. **Alembic** — fresh-DB chain per §7; `alembic check` clean pre/post tests.

---

## 9. Phase boundaries

### In scope (Phase 2.3)

`kpi_definitions` table + migration `0006`; pure engine `app/services/kpi.py`;
`/kpis` router (7 endpoints per §4); RLS wiring + tests per §6/§8; docs update.

### Explicit NON-GOALS (must NOT appear in any Phase 2.3 commit)

- **AI / LLM calls, natural-language analytics, recommendations** — Phase 2.4+
- **Anomaly detection, forecasting** — later phases
- **Dashboard redesign / any `apps/web` change** — web stays Phase 1
- **Arbitrary SQL** — no SQL string is accepted, stored, or executed anywhere;
  `filters` is a validated allow-list structure, never a query fragment
- **ETL / scheduled evaluation / background jobs** — compute-on-read only
- **Caching, materialized views, `kpi_snapshots`** — §3.7 decision; revisit only on
  measured need (technical-debt note, not scheduled work)
- **Additional database engines** — connector surface is untouched
- **`fact_rows` redesign** — untouched (§5)
- **Cross-source / multi-source KPIs, joins between sources** — one source per KPI
- **Comparison / previous-period config, derived metrics (ratio KPIs), weighted
  aggregations, median/percentile, custom expressions** — deferred; the 5
  aggregations + structured filters cover the Phase 2.4 Context Catalog's needs
- **Zero-filled calendar series** — client-side concern, deferred
- **Per-KPI ownership beyond roles, KPI sharing/export** — deferred

---

## 10. Implementation sequencing (small, independently testable steps)

Same rhythm as Phase 2.2: each step ends with a full green gate + docs + commit +
push, then **STOP** until told to continue.

### Step 1 — Model + migration + RLS foundation
- **Goal:** `KpiDefinition` model (§2), export in `models/__init__.py`, migration
  `0006_kpi_definitions` (§7), `rls_tables` entry in `app/main.py`.
- **Files:** `app/models/kpi_definition.py` (new), `app/models/__init__.py`,
  `alembic/versions/0006_kpi_definitions.py` (new), `app/main.py` (1 dict entry).
- **Tests:** model defaults/uniqueness/FK (unit, SQLite-runnable);
  RLS ENABLE/FORCE/fail-closed/org-scoped assertions for `kpi_definitions`
  (extend the existing loop in `tests/test_data_sources.py`).
- **Acceptance:** fresh-DB chain → `0006`, single head, `alembic check` clean;
  full suite green; no API/router code yet.
- **Stop condition:** gate green → commit `feat(api): implement phase 2.3 kpi definition foundation` → push → STOP.

### Step 2 — Pure KPI engine
- **Goal:** `app/services/kpi.py` per §3 (records, 5 aggregations, filters,
  grouping, series, null/empty/precision contract) + Pydantic schemas
  `app/schemas/kpi.py` with validation (§3.6).
- **Files:** `app/services/kpi.py` (new), `app/schemas/kpi.py` (new).
- **Tests:** exhaustive pure unit module `tests/test_kpi_engine_unit.py` — no DB
  needed (§8.2–8.5); validation cases (§8.1).
- **Acceptance:** engine module fully green without PostgreSQL; no router yet.
- **Stop condition:** commit `feat(api): implement phase 2.3 kpi computation engine` → push → STOP.

### Step 3 — API endpoints
- **Goal:** `/kpis` router (§4) registered in `app/main.py`; 404/400/409/422/403
  semantics exactly as specified.
- **Files:** `app/routers/kpis.py` (new), `app/main.py` (registration).
- **Tests:** integration module `tests/test_kpis_api.py` on real PostgreSQL —
  auth matrix, cross-org 404, create-over-source validation (404/400), compute
  end-to-end through real upload→map→fact_rows, duplicate 409, disabled 400,
  tenant isolation, cascade (§8.6–8.10).
- **Acceptance:** full suite green incl. all integration modules in one session.
- **Stop condition:** commit `feat(api): implement phase 2.3 kpi api endpoints` → push → STOP.

### Step 4 — Series endpoint + final gate
- **Goal:** `GET /kpis/{id}/series` (if not already landed in Step 3 — implement
  grouped/series responses here if Step 3 shipped compute only) and final
  acceptance pass over §10 checklist below.
- **Files:** `app/routers/kpis.py`, tests.
- **Acceptance (Phase 2.3 COMPLETE checklist, all must be green):**
  - [ ] chain `… → 0005 → 0006`, single head, fresh-DB `upgrade head` clean
  - [ ] `alembic check` clean pre/post tests on real PostgreSQL
  - [ ] RLS ENABLE + FORCE + fail-closed verified at runtime on `kpi_definitions`
  - [ ] cross-org 404 on every endpoint; DB-level isolation proven
  - [ ] engine contract (aggregations/filters/grouping/series/null/empty/precision)
        proven by pure unit tests
  - [ ] compute end-to-end equals hand-computed expectation through the real
        upload → map → fact_rows path
  - [ ] auth matrix verified (viewer 403 / analyst consume / manager manage)
  - [ ] full suite green, 0 skipped; no regression of the 95 baseline tests
  - [ ] `docs/PHASE_STATUS.md`: Phase 2.3 COMPLETE with evidence
- **Stop condition:** commit `feat(api): finalize phase 2.3 kpi engine` → push → STOP.

---

## 11. Relationship to roadmap

| Phase | Scope | Status |
|---|---|---|
| 2.0 | Alembic baseline, startup migrations | **COMPLETE** |
| 2.1 | Persistent upload storage, FileStorage abstraction | **COMPLETE** — commit `9dad4d9` |
| 2.2 | External Database Connections (+ import bridge) | **COMPLETE** — commit `18e445d` |
| 2.3 | KPI Engine (this document) | **PLANNING / NOT STARTED** |
| 2.4 | Context Catalog (AI-assistant preparation) | not planned in detail yet |

Suggested implementation order within Phase 2.3: model + migration + RLS →
pure engine + unit tests → API + integration tests → series + final gate (§10).
