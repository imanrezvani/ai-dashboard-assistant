# PHASE 2.4 PLAN — Context Catalog (تصمیم‌یار)

> **Status of this document:** authoritative implementation plan for Phase 2.4.
> A future coding agent must be able to implement Phase 2.4 from this document alone,
> without conversation history.
> **Companion docs:** `docs/PHASE_STATUS.md` (per-phase status, Persian) ·
> `docs/PHASE3_KPI_PLAN.md` (completed Phase 2.3 contract — conventions referenced here).
> **Baseline this plan builds on:** Phase 2.3 COMPLETE at commit `a7a5ba4`
> (`feat(api): complete phase 2.3 kpi series`); 182 tests passed / 0 failed / 0 skipped;
> Alembic head `0006_kpi_definitions` (chain `0001 → 0006`, single head);
> RLS ENABLE + FORCE + fail-closed on 7 tables; app role `tasmim_app` NOSUPERUSER + NOBYPASSRLS.

---

## 0. Evidence base (verified repository facts this plan relies on)

Every design decision below is anchored to current code — none are assumptions:

| Fact | Source |
|---|---|
| `DataSource` already carries: `name`, `file_type` (`csv/xlsx/xls/postgres`), `status` (`pending/mapped/failed`), `row_count`, `uploaded_at`, provenance `database_connection_id` (nullable, SET NULL) | `app/models/data_source.py` |
| `DataSourceColumn` already persists per-file column metadata: `name`, `position`, `dtype`, `mapped_role` (`measure/date/category/label` or NULL) — the mapping semantics survive restarts | `app/models/data_source_column.py`, `app/services/ingest.py` (`persist_columns`) |
| `KpiDefinition` already carries the full analytical contract: `aggregation`, structured `filters`, `group_by`, `granularity`, inclusive window, `enabled`, per-org unique `name` | `app/models/kpi_definition.py` |
| `fact_rows` is intentionally narrow (one measure + 3 dimensions) and has `created_at` (server_default `now()`) → **max(`created_at`) per source is a free freshness signal** (map deletes + re-inserts rows, so max(created_at) == last map time) | `app/models/fact_row.py`, `app/routers/data_sources.py` (`map_data_source`) |
| Re-map deletes previous `fact_rows` first, then re-inserts → any persisted copy of catalog facts would need invalidation on every map | `app/routers/data_sources.py` (`map_data_source`) |
| Volume is bounded: uploads ≤ 10,000 rows (`MAX_UPLOAD_ROWS`), imports clamped ≤ 100,000 rows (`MAX_FETCH_ROWS`) → per-org fact volume is small; aggregate reads are cheap | `app/services/ingest.py`, `app/connectors/postgres.py` |
| `DatabaseConnection` holds `encrypted_password` (ciphertext), `host`, `port`, `database_name`, `username`, `ssl_mode` — **these must never appear in any AI-facing context**; `DataSourceOut` exposes only `database_connection_id` as provenance | `app/models/database_connection.py`, `app/schemas/data_source.py` |
| Established compute-on-read precedent: Phase 2.3 §3.7 rejected cache/snapshot with the same arguments (bounded volume, mutation on re-map, invalidation complexity) | `docs/PHASE3_KPI_PLAN.md` §3.7 |
| KPI consumption is analyst+ (`["owner","admin","manager","analyst"]`), viewer is forbidden, cross-org id → **404** (never 403); no client-supplied `organization_id` anywhere | `app/routers/kpis.py`, `app/middleware/tenant.py` (`require_role`) |
| RLS convention: per-table entry in `rls_tables` dict in `app/main.py` — `ENABLE` + `FORCE` + fail-closed policy; **no RLS in migrations**; currently 7 tables | `app/main.py` |
| Services package is the home for business logic; pure modules are unit-tested without DB (`test_storage_unit.py`, `test_kpi_engine_unit.py` — verified green: 74 unit tests pass with no PostgreSQL in the environment) | `app/services/`, `apps/api/tests/` |
| No catalog/context code exists anywhere yet (`grep -i "catalog" → docs only`); no LLM/embedding/AI dependency exists in `requirements.txt` | repository-wide search |
| Integration-test conventions: real PostgreSQL with module-level skip w/o DB, fixtures-only override manipulation, module-scoped cleanup by unique prefixes (`context-` will be the new prefix) | `tests/test_kpi_rls_postgres.py`, gate lessons in `docs/PHASE_STATUS.md` |

---

## 1. Objective

Phase 2.4 adds the **Context Catalog**: a deterministic, bounded, tenant-scoped
read-model that describes *what an organization's analytics actually contain* —
its data sources, their mapped column semantics, fact-layer coverage
(date span, dimension vocabularies, freshness), and its KPI definitions — so the
future AI assistant can answer:

- *"What is happening?"* — needs the KPI list + their compute semantics (catalog provides definitions; values stay compute-on-read via the existing `/kpis/{id}/compute`)
- *"Which KPI changed?"* — needs KPI definitions + available series granularity (future change-detection layer consumes the catalog, not implemented here)
- *"Why did it change?"* — needs the dimension vocabularies (which categories/labels exist, which date ranges are covered) to pick drill-downs
- *"What is abnormal / what next / what action?"* — later phases; they all start from this catalog

Phase 2.4 is **not** the AI. It produces the structured substrate the AI will
consume. Nothing in this phase calls an LLM, builds prompts, or stores embeddings.

Scope is deliberately narrow: **zero new tables, one pure read-model service,
one read-only endpoint**. Everything else is a non-goal (§11).

---

## 2. What to catalogue — and what NOT to duplicate

### 2.1 Already persisted (the catalog references it, never copies it)

| Existing artifact | Already contains | Catalog uses it for |
|---|---|---|
| `data_sources` | identity, type, status, row_count, uploaded_at, provenance | source inventory block |
| `data_source_columns` | original column names, order, dtype, **mapped_role** | schema/semantics block per source (measure/date/category/label mapping) |
| `kpi_definitions` | full structured definition, enabled flag, name, description | KPI inventory block |
| `database_connections` | provenance link only (`database_connection_id` on `data_sources`) | `has_connection: true` / id — **nothing else ever leaves the table** |
| `fact_rows` | the data itself + `created_at` | coverage aggregates only (§2.2), never raw rows |

### 2.2 Genuinely new context (must be derived, is not stored anywhere today)

1. **Fact-layer coverage per source** — `row count in fact_rows`, `min/max dimension_date`,
   `distinct non-NULL dimension_category values`, `distinct non-NULL dimension_label values`.
2. **Freshness** — `max(fact_rows.created_at)` per source = last successful map time
   (derivable; no schema change needed — do **not** add `last_mapped_at` to `data_sources`).
3. **Available groupings** — booleans derived from (2): `has_date_dimension`,
   `has_category_dimension`, `has_label_dimension` — this is exactly what the AI needs
   to know which `group_by` values a KPI over this source can meaningfully use.
4. **Vocabulary bounds** — counts of distinct dimension values (with truncation, §8),
   so the AI knows the filter value space without seeing unbounded lists.

The catalog's only job is to assemble (1)–(4) around the persisted artifacts in
§2.1 into one deterministic structure.

### 2.3 Explicitly NOT catalogued

- Any credential material: `encrypted_password`, `host`, `port`, `database_name`,
  `username`, `ssl_mode` of `database_connections` — not even masked.
- Raw fact rows, sample rows, or aggregates beyond the bounded coverage set.
- User/membership information beyond "the requesting org exists" (not analytics context).
- Column dtype *inference details* beyond the stored `dtype` string.
- Anything about other organizations (enforced by RLS + explicit org filter, §7).

---

## 3. Source of truth: computed-on-read (decision + rationale)

**Decision: compute-on-read. Zero new tables, zero migrations, no cache.**

Options considered:

| Option | Verdict | Rationale from §0 facts |
|---|---|---|
| **Materialized catalog table** (new `context_catalog` table + refresh on every map/delete) | **rejected** | duplicates §2.1 metadata; needs invalidation hooks on every mutation path (upload, map, delete source, delete connection, KPI CRUD) — the exact complexity Phase 2.3 §3.7 rejected; introduces staleness bugs into an AI context path where a stale number is worse than no number |
| **Hybrid** (persist only coverage aggregates) | **rejected for now** | same invalidation problem, smaller surface; coverage queries are cheap at the bounded volumes in §0 (≤10k upload rows / ≤100k import rows per source, org-indexed) |
| **Computed-on-read** (pure service assembling existing rows + bounded aggregates) | **selected** | always-current (no invalidation logic exists to be wrong); matches the Phase 2.3 precedent; testable as a pure module; zero migration risk |

**Technical-debt trigger (documented, not scheduled):** if a real workload shows
repeated catalog reads over sources with ≥100k fact rows causing measurable
latency, revisit a materialized coverage table with refresh-on-map inside the
existing map transaction. Until then this is debt, like Phase 2.3's cache note.

---

## 4. Output contract (the future AI-facing structure)

New module `app/services/context_catalog.py` returns plain dicts; new schemas in
`app/schemas/context_catalog.py` serialize them. Wire shape (versioned envelope):

```jsonc
{
  "schema_version": 1,
  "generated_at": "<iso8601 UTC>",           // informational only; NOT part of determinism checks
  "organization_id": "<uuid>",
  "summary": {                                // bounded org-level rollup
    "data_source_count": <int>,               // total (may exceed sources[] length, §8)
    "kpi_count": <int>,
    "date_span": {"min": "YYYY-MM-DD"|null, "max": "YYYY-MM-DD"|null}  // across all sources
  },
  "data_sources": [                           // ordered: uploaded_at desc, name asc (GET /data-sources order + new name tiebreaker for determinism); capped
    {
      "id": "<uuid>",
      "name": "<str>",
      "file_type": "<str>",                   // csv|xlsx|xls|postgres
      "status": "<str>",                      // pending|mapped|failed
      "row_count": <int>,                     // DataSource.row_count (last map)
      "uploaded_at": "<iso8601>",
      "database_connection_id": "<uuid>|null",// provenance parity with DataSourceOut — nothing more
      "columns": [                            // ordered by position; capped
        {"name": "<str>", "position": <int>, "dtype": "<str>|null", "mapped_role": "measure|date|category|label|null"}
      ],
      "coverage": {                           // from fact_rows for this source (nulls when no rows / not mapped)
        "fact_row_count": <int>,
        "last_mapped_at": "<iso8601>|null",   // max(fact_rows.created_at)
        "date_min": "YYYY-MM-DD"|null,
        "date_max": "YYYY-MM-DD"|null,
        "has_date_dimension": <bool>,
        "category_values": ["<str>", ...],    // distinct non-NULL, sorted ascending, capped §8
        "category_value_count": <int>,        // true total (may exceed list length)
        "label_values": ["<str>", ...],
        "label_value_count": <int>
      }
    }
  ],
  "kpis": [                                   // ordered: created_at desc, name asc (matches GET /kpis); capped
    {
      "id": "<uuid>",
      "name": "<str>",
      "description": "<str>|null",
      "data_source_id": "<uuid>",
      "aggregation": "sum|avg|min|max|count",
      "group_by": "date|category|label|null",
      "granularity": "day|week|month|null",
      "filters": [ ...structured, exactly as stored... ],
      "date_from": "YYYY-MM-DD"|null,
      "date_to": "YYYY-MM-DD"|null,
      "enabled": <bool>
      // NO computed values here — numbers come from existing /kpis/{id}/compute and /kpis/{id}/series
    }
  ]
}
```

Contract rules (all enforced by tests, §10):

1. **Deterministic** — identical DB state ⇒ byte-identical `data_sources`/`kpis`/
   `coverage`/`summary` content (only `generated_at` may differ). Ordering is fixed;
   dimension values sorted ascending; no reliance on DB row order.
2. **Bounded** — every list is capped (§8); counts always reflect true totals.
3. **Secret-free** — the serialization test asserts the words/values of connection
   host/username/ciphertext never appear in the payload (§10.6).
4. **Numeric types** — only integers and booleans appear (no measure values are
   included); ISO strings for dates/timestamps. No floats anywhere in the payload.
5. **`schema_version`** — starts at 1; any breaking shape change bumps it. The future
   AI layer must be able to detect shape drift without re-reading code.

---

## 5. The deterministic Context Builder (pure read-model service)

New module `app/services/context_catalog.py`:

```python
def build_context_catalog(db: Session, *, organization_id: uuid.UUID) -> dict:
    """Assemble the bounded org context. All queries are org-scoped ORM queries.
    No SQL strings, no raw row payloads, no LLM, no secrets."""
```

Design constraints:

- **Inputs**: the request-scoped `Session` and the **authenticated** `organization_id`
  (derived from membership exactly like every existing router — never from the client body).
- **Queries** (all ORM, all filtered by `organization_id`, all running under the
  request's RLS context set by `require_membership`):
  1. `DataSource` list (org-scoped, ordered, capped in Python after count).
  2. `DataSourceColumn` for the selected sources (org-scoped, ordered by position).
  3. `KpiDefinition` list (org-scoped, ordered like `GET /kpis`).
  4. Per-source coverage aggregates on `FactRow`: `count(*)`, `min/max(dimension_date)`,
     `max(created_at)` — cheap, indexed by `ix_fact_rows_organization_id`/`ix_fact_rows_data_source_id`.
  5. Per-source distinct dimension values: distinct non-NULL `dimension_category` /
     `dimension_label` — fetched with a LIMIT of `MAX_DIMENSION_VALUES` (+1 sentinel to
     detect truncation) and sorted in Python for deterministic order.
- **Pure assembly**: no mutation, no writes, no external calls, no time-dependent
  logic beyond `generated_at` (which the determinism test excludes).
- **Fail-closed on scale**: if `data_source_count` exceeds `MAX_SOURCES`, the list is
  truncated and `summary.data_source_count` still reports the true total — never
  silently inconsistent.
- **The module contains zero SQL strings** (repo rule from Phase 2.3 §3.1) and zero
  imports of `DatabaseConnection`'s credential fields — it reads only
  `DataSource.database_connection_id`.

The internal function is the primary interface for the future AI layer (it can call
`build_context_catalog` directly inside any authenticated request context); the HTTP
endpoint (§9) is a thin, authorized wrapper — internal service and API surface are
kept separate so AI integration never has to go through HTTP.

---

## 6. Generation / refresh lifecycle

Because the catalog is computed on read:

| Event | Catalog action | Why nothing is needed |
|---|---|---|
| Upload CSV/Excel | none | next read sees the new `pending` source |
| Map / re-map | none | next read sees new `row_count`, `status`, new coverage aggregates and fresh `last_mapped_at` (rows were deleted + re-inserted) |
| Import from connection | none | same as upload + map |
| Delete DataSource | none | source + columns + facts + KPIs cascade away; next read no longer lists it |
| Delete DatabaseConnection | none | provenance id becomes NULL (existing SET NULL); catalog reflects it on next read |
| KPI create/update/delete | none | next read lists current definitions |

There is no refresh job, no scheduler, no cache invalidation — the absence of any
invalidation code is the point (§3).

---

## 7. Multi-tenancy / security (unchanged architecture)

1. **No new table ⇒ no new RLS entry.** All seven existing RLS tables stay exactly
   as they are. The catalog reads only tables already protected by
   ENABLE + FORCE + fail-closed policies.
2. **organization_id** — mandatory parameter derived from `require_membership`;
   every query filters it explicitly (defense-in-depth layer 3). A client-supplied
   `organization_id` does not exist in the endpoint contract.
3. **Cross-org behavior** — there are no path parameters, so cross-org *ids* cannot
   be passed at all; isolation is proven by tests: build the catalog for org A while
   org B's sources/KPIs/facts exist and assert org B data appears **nowhere** in the
   payload (DB-level RLS + explicit filters, §10.5).
4. **Secrets** — the catalog never touches `DatabaseConnection` columns beyond the
   provenance UUID already exposed by `DataSourceOut`. Test asserts non-disclosure
   (§10.6). No new encryption material, no new env vars.
5. **Authorization** — `GET /context/catalog` → **analyst+**
   (`["owner", "admin", "manager", "analyst"]`); viewer → 403; unauthenticated → 401.
   Rationale: the catalog is a read-model of analytics metadata — same sensitivity
   class as `GET /kpis` (analyst+). Managers/admins/owners are supersets. There is
   no write path, so no admin-only surface is needed.
6. **App role unchanged** — `tasmim_app` stays NOSUPERUSER + NOBYPASSRLS;
   `init-db/` untouched.

---

## 8. Performance boundaries (no uncontrolled `fact_rows` scans)

Constants in `app/services/context_catalog.py`:

| Constant | Value | Effect |
|---|---|---|
| `MAX_SOURCES` | 200 | `data_sources` list truncation; true count still reported in `summary` |
| `MAX_KPIS` | 200 | `kpis` list truncation; true count in `summary` |
| `MAX_COLUMNS_PER_SOURCE` | 200 | `columns` truncation per source (sources in practice have ≪200 columns; 10k-row files are rows, not columns) |
| `MAX_DIMENSION_VALUES` | 50 | per-dimension vocabulary list cap; `*_value_count` reports the true distinct total |

- All aggregate/distinct queries are per-source and org-scoped (indexed paths);
  no query ever scans `fact_rows` without both filters.
- Distinct-value queries use `LIMIT MAX_DIMENSION_VALUES + 1` — cost is bounded
  regardless of cardinality; the +1 sentinel detects "more values exist" without
  materializing them.
- The full payload is bounded by construction: worst case ≈ 200 sources ×
  (200 columns + 100 dimension values) + 200 KPIs — a few MB at the absolute limit,
  typically a few KB. A response-size assertion exists in tests at the caps.
- The catalog endpoint performs **no** KPI computation (numbers stay in
  `/kpis/{id}/compute|series`), so catalog reads cannot trigger O(facts × KPIs) work.

---

## 9. API surface (minimum)

New router `app/routers/context_catalog.py` (`prefix="/context"`, `tags=["context"]`),
registered in `app/main.py` after `kpis`.

| Endpoint | Method | Purpose | Authorization |
|---|---|---|---|
| `/context/catalog` | GET | full org context per §4 | **analyst+** → `["owner", "admin", "manager", "analyst"]` |

That is the entire endpoint list. There is intentionally:

- **no** query parameters (filtering is the future AI layer's job, done in-process
  over the bounded structure);
- **no** POST/PUT/PATCH/DELETE (read-model; the catalog has no lifecycle of its own);
- **no** per-resource endpoints (no path params ⇒ no 404-semantics surface to test).

Error semantics: unauthenticated → **401**; viewer → **403**; no other error paths
exist (an empty org returns a valid, empty catalog with HTTP 200 — "no data yet" is
a correct answer, consistent with Phase 2.3's empty-result contract).

---

## 10. Testing strategy

Conventions: pure unit tests need no DB; integration tests run on real PostgreSQL
with module-level skip w/o DB; fixtures-only override manipulation; module-scoped
cleanup with the new `context-` prefix; **no faked RLS**.

1. **Builder unit tests (no DB, SQLite-runnable)** — `tests/test_context_catalog_unit.py`:
   - empty org → valid empty catalog (`summary` zeros, empty lists, `schema_version == 1`);
   - ordering determinism (sources by `uploaded_at desc, name asc` — same as
     `GET /data-sources` plus a `name` tiebreaker added for full determinism since
     `uploaded_at` alone is not unique; columns by
     `position`; KPIs by `created_at desc, name asc`; dimension values ascending);
   - caps (seed > `MAX_DIMENSION_VALUES` categories → list capped, count exact;
     sentinel/truncation flag logic);
   - contract shape: no floats anywhere in the payload; `coverage` nulls for a
     source with no fact rows; `has_date_dimension` false when all dates NULL;
   - freshness: `last_mapped_at` == max(`fact_rows.created_at`), NULL when unmapped.
2. **Integration on real PostgreSQL** — `tests/test_context_catalog_api.py`:
   - **auth matrix**: unauthenticated 401; viewer 403; analyst 200; manager 200; owner 200;
   - **tenant isolation (the critical test)**: two orgs with same-named sources,
     facts, and KPIs — org A's catalog contains **zero** org B identifiers
     (ids, names, category values, KPI names) and vice versa;
   - **determinism**: two consecutive calls with unchanged data ⇒ identical payloads
     except `generated_at`;
   - **end-to-end coverage**: CSV upload → map → KPI create → catalog shows the
     source with correct `mapped_role` columns, coverage aggregates matching seeded
     facts (hand-computed), and the KPI definition;
   - **lifecycle**: delete the KPI → gone from catalog; delete source → gone;
     connection deletion → `database_connection_id` becomes null in catalog;
   - **secret non-disclosure**: with a `DatabaseConnection` (real encrypted password)
     linked via import-style provenance, assert the payload (and error bodies) contain
     no host/username/database_name/ciphertext; `caplog` clean.
3. **RLS regression** — existing RLS assertion loops already cover all seven tables;
   the integration module additionally asserts catalog queries run under the request
   context return only org rows (this is the §10.2 isolation test by construction).
4. **Regression** — `cd apps/api && .venv/bin/pytest -q -rs` all green, 0 skipped,
   on real PostgreSQL; the existing 182 tests must not regress.
5. **Alembic gate** — `alembic current` = `0006_kpi_definitions`, `alembic heads`
   exactly one, `alembic check` clean **before and after** the run (Phase 2.4 must
   produce **zero** migrations — an accidental schema change is a gate failure).

---

## 11. Phase boundaries

### In scope (Phase 2.4)

`app/services/context_catalog.py` (pure builder + caps) · `app/schemas/context_catalog.py` ·
`app/routers/context_catalog.py` (single GET endpoint) · registration in `app/main.py` ·
unit + integration tests per §10 · docs update. **No migration. No model change.**

### Explicit NON-GOALS (must NOT appear in any Phase 2.4 commit)

- **AI / LLM calls, prompts, embeddings, vector DB, RAG, chat UI, autonomous actions,
  external AI providers** — Phase 2.5+; `requirements.txt` must not change
- **Anomaly detection, forecasting, recommendations, natural-language query** — later
- **Answering the product questions** (what changed / why / abnormal) — the catalog
  only supplies the substrate; the comparison/diff layer is a later phase
- **Computed KPI values inside the catalog** — numbers stay behind the existing
  compute/series endpoints
- **Dashboard or any `apps/web` change** — web stays Phase 1
- **New tables, new migrations, cache/snapshot materialization, refresh jobs** — §3
- **Arbitrary SQL** — ORM-only; the builder contains zero SQL strings
- **Additional database engines / connector changes** — connector surface untouched
- **`fact_rows` redesign** — untouched
- **Cross-source joins or merged org-wide fact scans** — per-source aggregates only
- **Prompt construction or context-window packing** — the AI layer will do that on
  top of this contract; Phase 2.4 ships the structure, not the prompts

---

## 12. Implementation sequencing (small, independently testable steps)

Same rhythm as Phases 2.2/2.3: each step ends with a full green gate + docs + commit +
push, then **STOP** until told to continue.

### Step 1 — Pure builder + schemas

- **Goal:** `app/services/context_catalog.py` (builder, caps, deterministic ordering,
  coverage aggregates) + `app/schemas/context_catalog.py` (§4 wire contract).
- **Files:** `app/services/context_catalog.py` (new), `app/schemas/context_catalog.py` (new).
- **Tests:** `tests/test_context_catalog_unit.py` (§10.1) — no DB needed.
- **Acceptance:** unit module green without PostgreSQL; no router yet; no migration;
  `alembic check` still clean.
- **Stop condition:** full suite green → commit `feat(api): implement phase 2.4 context catalog builder` → push → STOP.

### Step 2 — API endpoint + integration tests

- **Goal:** `app/routers/context_catalog.py` with the single §9 endpoint, registered
  in `app/main.py`.
- **Files:** `app/routers/context_catalog.py` (new), `app/main.py` (one line).
- **Tests:** `tests/test_context_catalog_api.py` (§10.2–10.3) on real PostgreSQL.
- **Acceptance:** auth matrix + isolation + determinism + non-disclosure green;
  full suite green in one session (182 baseline must not regress); 0 skipped.
- **Stop condition:** gate green → commit `feat(api): implement phase 2.4 context catalog api` → push → STOP.

### Step 3 — Final gate

- **Goal:** acceptance pass over the checklist:
  - [ ] zero migrations; chain unchanged `0001 → 0006`, single head, `alembic check` clean pre/post
  - [ ] RLS config untouched (7 tables, ENABLE + FORCE + fail-closed); app role unchanged
  - [ ] catalog payload contract proven (§4 rules) by tests
  - [ ] secret non-disclosure proven with a real connection row present
  - [ ] full suite green, 0 skipped; 182 baseline tests not regressed
  - [ ] `docs/PHASE_STATUS.md`: Phase 2.4 COMPLETE with evidence
- **Stop condition:** commit `feat(api): finalize phase 2.4 context catalog` → push → STOP. Do not start Phase 2.5.

---

## 13. Relationship to roadmap

| Phase | Scope | Status |
|---|---|---|
| 2.0 | Alembic baseline, startup migrations | **COMPLETE** |
| 2.1 | Persistent upload storage, FileStorage abstraction | **COMPLETE** — commit `9dad4d9` |
| 2.2 | External Database Connections (+ import bridge) | **COMPLETE** — commit `18e445d` |
| 2.3 | KPI Engine (pure engine + `/kpis` API + series) | **COMPLETE** — commit `a7a5ba4` |
| 2.4 | Context Catalog (this document — AI-assistant preparation) | **PLANNING / NOT STARTED** |
| 2.5+ | AI decision layer (change detection, anomaly, narrative) | future; consumes the Phase 2.4 catalog contract |

Suggested implementation order within Phase 2.4: builder + schemas + unit tests →
endpoint + integration tests → final gate (§12).
