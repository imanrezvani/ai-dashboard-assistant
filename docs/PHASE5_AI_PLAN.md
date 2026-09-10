# PHASE 2.5 PLAN — AI Decision Layer (تصمیم‌یار)

> **Status of this document:** authoritative implementation plan for Phase 2.5.
> A future coding agent must be able to implement Phase 2.5 from this document alone,
> without conversation history.
> **Companion docs:** `docs/PHASE_STATUS.md` (per-phase status, Persian) ·
> `docs/PHASE4_CONTEXT_CATALOG_PLAN.md` (completed Phase 2.4 contract — the catalog
> this phase consumes) · `docs/PHASE3_KPI_PLAN.md` (completed Phase 2.3 contract —
> the pure KPI engine and series semantics referenced here).
> **Baseline this plan builds on:** Phase 2.4 COMPLETE at commit `c319241`
> (`feat(api): finalize phase 2.4 context catalog`); 205 tests passed / 0 failed /
> 0 skipped; Alembic head `0006_kpi_definitions` (chain `0001 → 0006`, single head);
> RLS ENABLE + FORCE + fail-closed on 7 tables; app role `tasmim_app`
> NOSUPERUSER + NOBYPASSRLS.

---

## 0. Evidence base (verified repository facts this plan relies on)

Every design decision below is anchored to current code — none are assumptions:

| Fact | Source |
|---|---|
| The Phase 2.3 engine is pure and deterministic: `compute_kpi` / `compute_kpi_series` over in-memory `FactRecord`s, Decimal-only, decimal-string wire contract, only observed buckets (no zero-fill) | `app/services/kpi.py`, `docs/PHASE3_KPI_PLAN.md` §3 |
| `GET /kpis/{id}/series` returns `{kpi_id, group_by, buckets:[{key, value, rows}]}` with decimal-string values — the raw material for period-over-period comparison already exists | `app/routers/kpis.py`, `app/schemas/kpi.py` |
| The Phase 2.4 catalog is a bounded, versioned (`schema_version=1`), tenant-scoped read-model with explicit caps (`MAX_SOURCES`, `MAX_KPIS`, `MAX_COLUMNS_PER_SOURCE`, `MAX_DIMENSION_VALUES`) and **no computed KPI values by design** — numbers deliberately live behind compute/series | `app/services/context_catalog.py`, `docs/PHASE4_CONTEXT_CATALOG_PLAN.md` §4/§8 |
| `GET /context/catalog` exists (analyst+, viewer 403, empty org → 200) and is the single entry point for "what data exists and what it means" | `app/routers/context_catalog.py` |
| Secrets pattern is established: `_get_secret(name, dev_fallback)` raises `RuntimeError` in production when missing; `ENCRYPTION_KEY` (Phase 2.2) is the precedent for a third required key | `app/core/config.py` |
| `httpx==0.27.2` is already a dependency; **no AI/LLM dependency exists anywhere** in `requirements.txt` | `apps/api/requirements.txt` |
| Role/tenancy conventions: `require_role([...])` returns the membership; organization_id derives from it, never the client; cross-org resource id → 404; viewer sees nothing | `app/middleware/tenant.py`, all routers |
| RLS convention unchanged across 7 tables (ENABLE + FORCE + fail-closed, no RLS in migrations); `alembic check` gates model↔migration parity every phase | `app/main.py`, `docs/PHASE4_CONTEXT_CATALOG_PLAN.md` §0 |
| Volume bounds: uploads ≤10k rows, imports ≤100k, catalog payload bounded by construction → any LLM prompt assembled from catalog + computed series is bounded by design | `app/services/ingest.py`, `app/connectors/postgres.py`, Phase 2.4 §8 |
| Test conventions: DB-free unit modules; real-PostgreSQL integration modules with module-level skip, fixture-only overrides, own-prefix cleanup; external services are **never** called from the suite (connector tests use a real scratch DB; no live LLM will be called) | `tests/test_*`, three phase gates |
| User-controlled strings (`dimension_category`, `dimension_label`, file/column names, KPI names) can reach LLM context — they are untrusted input for prompt-injection purposes | `app/routers/data_sources.py` (map flow), Phase 2.4 contract |

---

## 1. Objective

Phase 2.5 adds the **smallest production-oriented AI decision layer** on top of the
existing deterministic foundation:

1. **Change detection (deterministic, pure):** for one KPI, compare the most recent
   complete period against the previous equal-length period — value change, relative
   change, direction — plus *top contributing dimension values* (movers) from the
   existing grouped series. No statistics beyond arithmetic; no ML.
2. **Org briefing (deterministic assembly):** for all enabled KPIs of an org, assemble
   the per-KPI change packets into one bounded structured briefing.
3. **Narration (LLM, optional by configuration):** a provider-abstracted LLM turns the
   *precomputed, deterministic* packets into short prose. The LLM **never computes**,
   **never sees SQL**, **never sees credentials or raw fact rows**, and its failure
   never breaks the deterministic layer (narrative is simply `null`).

Phase 2.5 answers the first two roadmap questions — *"What is happening?"* and
*"Which KPI changed?"* — and gives the LLM the bounded substrate to phrase *"why"* as
narration of movers. *"What is abnormal / what next / what action?"* remains future
work (§9): this phase ships change detection with thresholds, not anomaly science.

Scope is deliberately narrow: **one pure insight module, one provider abstraction +
one provider, two read-only endpoints, zero new tables, zero migrations**. Everything
else is a non-goal (§9).

---

## 2. Architecture: deterministic engine first, LLM as narrator only

The governing rule (learned from Phases 2.3/2.4): **numbers come from code, words
come from the model.** The LLM consumes a versioned JSON *insight packet* and returns
prose; it is never in the path of a numeric answer.

### 2.1 Pure insight engine — `app/services/insights.py`

New DB-free module in the established `services/` home, same shape as `kpi.py`:

```python
@dataclass(frozen=True)
class ChangePacket:            # per KPI — the unit of the briefing
    kpi_id: uuid.UUID
    kpi_name: str
    aggregation: str
    current_value: str | None  # decimal-string (contract of Phase 2.3)
    previous_value: str | None
    change: str | None         # current - previous, decimal-string
    change_pct: str | None     # relative change as decimal-string ("0.1834" = +18.34%), None when previous is 0/None
    direction: str             # "up" | "down" | "flat" | "unknown"
    flagged: bool              # |change_pct| >= ANOMALY_THRESHOLD
    top_movers: list[Mover]    # from grouped series, when the KPI has group_by

@dataclass(frozen=True)
class Mover:
    key: str                   # bucket key (category/label value or ISO date)
    value: str                 # decimal-string
    contribution: str          # value - previous value of same bucket, decimal-string
```

Inputs are **already-computed artifacts** (no SQL, no DB): the KPI definition data,
current-period series buckets and previous-period series buckets (produced by the
existing pure engine via `compute_kpi_series` with shifted inclusive windows).

Determinism contract (same discipline as Phase 2.3 §3.3):

- Empty/missing data → `direction="unknown"`, `flagged=False`, values `None` — a
  successful computation, never an error, never a fabricated number.
- Division only for `change_pct`; previous value `0` → `change_pct=None`
  (no infinite/NaN can exist; Decimal arithmetic only, quantized to 4 dp with
  `ROUND_HALF_UP` to match `Numeric(18,4)`).
- Movers sorted by descending absolute contribution, tie-broken by key ascending —
  deterministic regardless of DB order; capped at `MAX_MOVERS = 5`.
- No zero-fill: movers only over buckets observed in **both** periods.
- Flat detection: `change == 0` (exact Decimal) → `"flat"`.
- Module contains **zero SQL strings** and never imports models' credential surfaces.

### 2.2 Anomaly semantics (threshold, not statistics)

`ANOMALY_THRESHOLD = Decimal("0.25")` (module constant, ±25% relative change). A KPI
is *flagged* when `|change_pct| >= threshold` and both periods have values. This is
**change detection**; statistical anomaly detection (MAD, z-scores, seasonality) is a
non-goal (§9). The constant is documented, testable, and never sent to the LLM as a
vibe — the packet carries the numbers; the prose describes them.

### 2.3 LLM narrator — provider abstraction

New package `app/services/ai/`:

```
app/services/ai/
  __init__.py
  base.py          # LlmProvider ABC + typed errors + NarrationResult
  sambanova.py     # SambaNovaProvider (OpenAI-compatible REST via httpx)
  prompts.py       # deterministic prompt assembly from insight packets
```

- `LlmProvider` ABC: `def narrate(self, packet: dict) -> NarrationResult`.
  Factory `get_llm_provider()` returns the configured provider or `None` when
  AI is not configured — **None is a valid state**, the API layer then returns the
  deterministic packet with `narrative: null` (graceful degradation; the product
  still answers "what changed" numerically).
- **SambaNova** is the implemented provider (see §7): OpenAI-compatible
  chat-completions REST endpoint, called with plain `httpx` (already a dependency —
  no new SDK), `response_format: {"type": "json_object"}`, server-side
  `Authorization: Bearer <key>`.
- Typed errors: `AiProviderError`, `AiTimeoutError`, `AiBadResponseError` — sanitized
  (never include the key, full URL query, or raw response bodies).
- Hard timeout (20s) on every call; **exactly one LLM call per API request** (the
  briefing endpoint sends one call with all packets — no per-KPI fan-out).
- Output validation: the narration JSON must match the pydantic schema
  (`NarrationResult.summary` + `NarrationResult.highlights`); a malformed model
  response is a typed error → endpoint returns the packet with `narrative: null` and
  a `narrative_error` code — **never a 500 with a raw model dump**.

### 2.4 Prompt contract (deterministic, bounded, injection-aware)

- System prompt is a fixed string in `prompts.py` (versioned constant): role = BI
  assistant, language = same as the KPI/organization data (Persian default),
  output = strict JSON `{"summary": str, "highlights": [str, ...]}`, no invented
  numbers (instructed to only reference values present in the packet).
- User message = `json.dumps(packet)` — the **structured insight packet only**:
  KPI metadata, current/previous values, change, movers, coverage bounds. It never
  contains credentials, connection metadata, other orgs' data, or SQL.
- **Prompt-injection posture:** dimension/label/KPI names are untrusted data. They
  are transmitted inside JSON (not concatenated into instructions), the system
  prompt states that data fields are not instructions, and the output is validated
  against the schema and length-capped (`summary ≤ 2000 chars`, `highlights ≤ 10 ×
  500 chars`) before it is returned. The LLM can only *phrase* the packet — it
  cannot widen its own permissions.
- Bounded by construction: packet size is capped by the catalog caps (§0) and
  `MAX_MOVERS`; a briefing prompt is O(KPIs × movers), a few KB.

### 2.5 Source of truth: compute-on-read (same decision as 2.3 §3.7 / 2.4 §3)

No cache, no snapshot, no persistence of generated insights in Phase 2.5. Rationale
from repository facts: volumes are bounded; the series/coverage data already computes
in milliseconds; insight correctness depends on "now" (period windows), so any cache
would need the same invalidation logic Phase 2.3 rejected. **Technical-debt trigger
(documented, not scheduled):** if LLM cost/latency for repeated identical packets
becomes measurable, add a short-TTL cache keyed by `(org_id, kpi_id, window)` inside
the provider adapter only — never in the deterministic engine.

---

## 3. API surface (minimum)

New router `app/routers/assistant.py` (`prefix="/assistant"`, `tags=["assistant"]`),
registered in `app/main.py`; the per-KPI insight endpoint extends the existing
`kpis` router (it is KPI-scoped, same conventions).

| Endpoint | Method | Purpose | Authorization |
|---|---|---|---|
| `/kpis/{id}/insight` | GET | per-KPI change packet + narrative (`?window_days=28`, clamped to `MIN_WINDOW_DAYS=7` / `MAX_WINDOW_DAYS=365`) | **analyst+** |
| `/assistant/briefing` | GET | org-wide briefing: all enabled KPIs' packets + one narrative | **analyst+** |

- Both are **read-only**, analyst+ (`["owner","admin","manager","analyst"]`); viewer →
  403; unauthenticated → 401; cross-org KPI id → **404** (RLS + explicit org filter,
  existence never leaked); no client-supplied `organization_id` anywhere.
- Response shapes:

```jsonc
// GET /kpis/{id}/insight
{
  "kpi_id": "<uuid>", "kpi_name": "<str>",
  "window_days": 28,
  "current_period":  {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"},
  "previous_period": {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"},
  "current_value": "<decimal-string>|null", "previous_value": "<decimal-string>|null",
  "change": "<decimal-string>|null", "change_pct": "<decimal-string>|null",
  "direction": "up|down|flat|unknown", "flagged": false,
  "top_movers": [{"key": "...", "value": "...", "contribution": "..."}],
  "narrative": {"summary": "...", "highlights": ["..."]} | null,
  "narrative_error": null | "provider_unavailable" | "provider_error" | "bad_response"
}

// GET /assistant/briefing
{
  "generated_at": "<iso8601 UTC>",
  "window_days": 28,
  "kpis": [ ...ChangePacket JSON as above, without narrative fields... ],
  "flagged_count": 1,
  "narrative": {"summary": "...", "highlights": ["..."]} | null,
  "narrative_error": null | "provider_unavailable" | ...
}
```

- Error semantics: KPI disabled → **400** (established compute/series semantics);
  source not mapped → **400**; unknown/cross-org id → **404**; empty data → **200**
  with the unknown-direction contract (§2.1); provider failure → **200** with
  `narrative: null` + `narrative_error` (the deterministic answer is still correct —
  an LLM outage must not fail the endpoint).
- All numeric fields are decimal-strings (Phase 2.3 contract — floats never cross
  the wire).

---

## 4. Data model compatibility

**Zero schema change.** The insight layer reads only `kpi_definitions`,
`data_sources`, and `fact_rows` through the existing tenant-scoped ORM paths, and
reuses `compute_kpi_series` with shifted date windows (the KPI's own `date_from`/
`date_to`, when present, intersect the computed windows). No new table, no migration,
`alembic check` must stay clean — an accidental schema change is a gate failure
(same rule as Phase 2.4).

---

## 5. Multi-tenancy / security

1. **RLS unchanged** — 7 tables, ENABLE + FORCE + fail-closed; no new table ⇒ no new
   policy; app role stays NOSUPERUSER + NOBYPASSRLS.
2. **organization_id** — derived from `require_role` membership exactly like every
   router; every query filters it explicitly (defense-in-depth layer 3).
3. **Cross-org** — `/kpis/{id}/insight` resolves the KPI through RLS + org filter
   (404 before any computation); the briefing endpoint has no path parameters, so
   cross-org ids cannot be passed at all.
4. **What ever leaves for the LLM** — only the §2.4 packet: org-scoped KPI names,
   precomputed aggregates, dimension keys/values (user-controlled but JSON-framed
   and instruction-firewalled), period dates. **Never:** credentials, connection
   host/user/db, ciphertext, SQL, raw fact rows, other orgs' identifiers, or the
   API key itself.
5. **Key handling** — `AI_API_KEY` enters via the established `_get_secret` pattern;
   production fail-fast; dev fallback value obviously dev-only; the key is never
   logged, never echoed in errors, never part of any response.
6. **Log hygiene** — log provider outcome (`ok`/`timeout`/`bad_response`) and
   latency only; never the full prompt or model output (model output can echo
   user-controlled strings; logs stay free of tenant data).

---

## 7. Provider integration: SambaNova (selected)

**Why SambaNova for this stack:** OpenAI-compatible chat-completions REST API —
integrates with plain `httpx` (already in `requirements.txt`), no SDK, server-side
bearer key, JSON response mode fits the narrator-only design, and per-request cost
suits the low-volume briefing/insight usage.

- Base URL / model are constants overridable by settings:
  `AI_PROVIDER=sambanova` (only implemented value in 2.5),
  `AI_MODEL` (default `Meta-Llama-3.3-70B-Instruct`), optional
  `AI_BASE_URL` (default `https://api.sambanova.ai/v1`).
- Required env var: **`SAMBANOVA_API_KEY`** (via `AI_API_KEY` setting resolution:
  `AI_API_KEY` → `SAMBANOVA_API_KEY`; missing in production → `RuntimeError`
  at settings load; missing in dev → `get_llm_provider()` returns `None` →
  deterministic endpoints still work with `narrative: null`).
- Request: `POST {base_url}/chat/completions` with
  `{"model", "messages", "response_format": {"type": "json_object"}, "temperature": 0}`
  — temperature 0 for determinism-of-phrasing; headers `Authorization: Bearer`,
  `Content-Type: application/json`; `httpx` timeout 20s.
- Tests never call the live API: the provider is exercised with
  `httpx.MockTransport` (success, malformed JSON, timeout, non-200) and the API layer
  is tested with a stub provider injected via a module-level seam
  (`app.services.ai.factory` override) — mirroring how the connector is tested
  against a real DB but never against a foreign cloud.

---

## 8. Testing strategy

Conventions unchanged: DB-free unit modules; real-PostgreSQL integration with
module-level skip, fixture-only overrides, own-prefix cleanup (`ai-` / `ins-`);
**no live LLM anywhere in the suite**.

1. **Insight engine unit tests (no DB)** — `tests/test_insights_unit.py`:
   up/down/flat/unknown; `change_pct` exactness (Decimal, 4dp, `ROUND_HALF_UP`);
   previous=0 → `change_pct=None` (no division by zero); empty previous period →
   unknown contract; flagged at exactly ±threshold and just below; movers ordering
   (desc |contribution|, tie by key asc) and `MAX_MOVERS` cap; movers only over
   buckets present in both periods; input-order independence (determinism).
2. **Provider unit tests (MockTransport)** — `tests/test_ai_provider_unit.py`:
   request shape (URL, headers without leaking key in error paths, temperature 0,
   response_format); success → `NarrationResult`; non-200 → `AiProviderError`
   (sanitized); malformed JSON / schema violation → `AiBadResponseError`; timeout →
   `AiTimeoutError`; missing key in production settings → `RuntimeError` at load;
   missing key in dev → factory returns `None`.
3. **API integration on real PostgreSQL** — `tests/test_assistant_api.py`:
   auth matrix (viewer 403, analyst/manager/owner 200, unauth 401, no-org 400);
   cross-org KPI id → 404; window clamp (7/365) and 422 on invalid; deterministic
   packets with hand-computed values from seeded facts (two adjacent windows);
   `narrative: null` + `narrative_error="provider_unavailable"` when unconfigured;
   with a stub provider — narrative present and **prompt payload asserted** to
   contain no credential material and no other-org identifiers (two-org seed);
   disabled KPI → 400; briefing with zero KPIs → 200 empty; non-disclosure scan of
   every response body.
4. **Regression + Alembic gate** — full `pytest -q -rs` green (205 baseline must not
   regress), 0 skipped; `alembic current` = `0006_kpi_definitions`, single head,
   `alembic check` clean **before and after** (zero migrations expected all phase).

---

## 9. Phase boundaries

### In scope (Phase 2.5)

`app/services/insights.py` (pure engine) · `app/services/ai/` (base + sambanova +
prompts) · settings additions (`AI_PROVIDER`/`AI_MODEL`/`AI_BASE_URL`/`AI_API_KEY`) ·
`/kpis/{id}/insight` + `/assistant/briefing` + tests · docs. **Zero migrations.**

### Explicit NON-GOALS (must NOT appear in any Phase 2.5 commit)

- **Autonomous actions, scheduled monitoring, alerting (email/webhook), cron** —
  read-only on-request insight only
- **Chat UI / conversation memory / multi-turn sessions on `apps/web`** — web stays
  Phase 1; endpoints are stateless single-shot
- **Natural-language-to-SQL or any query generation** — arbitrary SQL remains
  forbidden repo-wide; the LLM never parametrizes a query
- **Statistical anomaly detection (MAD/z-score/seasonality), forecasting,
  recommendations, what-if simulation** — threshold change detection only (§2.2)
- **Embeddings, vector DB, RAG** — the catalog is already the bounded context
- **Fine-tuning, agent frameworks, tool-calling loops, streaming** — one call, one
  response, validated
- **Multi-provider rotation/fallback chains** — one provider (`sambanova`), the ABC
  is the only extension point
- **Persisting insights or conversations** — compute-on-read (§2.5)
- **Dashboard redesign** — endpoints only
- **New tables/migrations, ETL, new database engines, `fact_rows` redesign,
  cross-source KPIs, derived metrics** — all unchanged

---

## 10. Implementation sequencing (small, independently testable steps)

Same rhythm as Phases 2.2/2.3/2.4: each step ends with a full green gate + docs +
commit + push, then **STOP** until told to continue.

### Step 1 — Pure insight engine
- **Goal:** `app/services/insights.py` (ChangePacket/Mover, comparison, threshold
  flagging, movers) — no LLM, no DB, no router.
- **Tests:** `tests/test_insights_unit.py` (§8.1).
- **Acceptance:** unit module green without PostgreSQL; zero SQL; determinism
  proven; full suite green; `alembic check` clean.
- **Stop:** commit `feat(api): implement phase 2.5 insight engine` → push → STOP.

### Step 2 — Provider abstraction + SambaNova
- **Goal:** `app/services/ai/` (base ABC, typed sanitized errors, `prompts.py`,
  `sambanova.py` via httpx, factory with None-when-unconfigured), settings
  additions with the `_get_secret` production contract.
- **Tests:** `tests/test_ai_provider_unit.py` with MockTransport (§8.2).
- **Acceptance:** no live network call in the suite; key never in logs/errors;
  factory returns None without config; full suite green.
- **Stop:** commit `feat(api): implement phase 2.5 ai provider` → push → STOP.

### Step 3 — API endpoints + integration tests
- **Goal:** `/kpis/{id}/insight` (in the kpis router) + `/assistant/briefing`
  (new router, registered in `app/main.py`); stub-provider seam for tests.
- **Tests:** `tests/test_assistant_api.py` (§8.3) on real PostgreSQL.
- **Acceptance:** auth matrix + cross-org 404 + hand-computed packets +
  non-disclosure green; full suite green in one session (205 baseline not
  regressed); 0 skipped.
- **Stop:** commit `feat(api): implement phase 2.5 assistant api` → push → STOP.

### Step 4 — Final gate
- **Goal:** acceptance checklist — zero migrations; RLS/7 tables + app role
  unchanged live-verified; prompt payload proven secret-free and org-scoped;
  deterministic layer provably independent of provider availability; full suite
  green; `alembic check` clean pre/post; `docs/PHASE_STATUS.md` Phase 2.5 COMPLETE
  with evidence.
- **Stop:** commit `feat(api): finalize phase 2.5 ai decision layer` → push → STOP.
  Do not start Phase 2.6.

---

## 11. Relationship to roadmap

| Phase | Scope | Status |
|---|---|---|
| 2.0–2.2 | Baseline, uploads, external connections + import bridge | **COMPLETE** |
| 2.3 | KPI Engine (pure engine + `/kpis` API + series) | **COMPLETE** |
| 2.4 | Context Catalog (AI-assistant preparation) | **COMPLETE** — `c319241` |
| 2.5 | AI Decision Layer (this document — change detection + narration) | **PLANNING / NOT STARTED** |
| 2.6+ | Anomaly science, scheduled monitoring, chat UX, actions | future; requires the Phase 2.5 insight + narration contract |

Implementation order within Phase 2.5: insight engine → provider → API → final gate
(§10). The deterministic layer must be complete and green **before** the first LLM
call exists anywhere in the codebase.
