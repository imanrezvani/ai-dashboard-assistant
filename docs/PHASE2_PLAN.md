# PHASE 2.2 PLAN — External Database Connections (تصمیم‌یار)

> **Status of this document:** authoritative implementation plan for Phase 2.2.
> A future coding agent must be able to implement Phase 2.2 from this document alone,
> without conversation history.
> **Companion doc:** `docs/PHASE_STATUS.md` (Persian) tracks per-phase completion status.
> **Baseline this plan builds on:** Phase 2.1 COMPLETE at commit `9dad4d9`
> (`fix(api): finalize phase 2.1 postgres verification`).

---

## 1. Objective

Phase 2.2 lets an organization **register an external PostgreSQL database** and lets
authorized users **inspect its tables and sample rows**, so that (in the integration
bridge, §7) a table can be imported into the existing `data_sources → fact_rows`
normalization layer — with credentials stored **encrypted at rest**, tenant-isolated
by the same **RLS ENABLE + FORCE + fail-closed** model as every other data table,
and with **read-only** access to the external database.

Scope is deliberately narrow: **PostgreSQL only**, connection management + discovery +
sampling + one import bridge. Everything else is a non-goal (§11).

---

## 2. DatabaseConnection domain model

New model: `apps/api/app/models/database_connection.py`
New table: `database_connections` (created by Alembic migration `0004_database_connections`,
`down_revision = "0003_align_created_at_nullable"`).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | default `uuid4` (same convention as all models) |
| `organization_id` | UUID, NOT NULL, FK → `organizations.id ON DELETE CASCADE` | tenant anchor; indexed |
| `name` | String(255), NOT NULL | display name, unique per organization (`UniqueConstraint("organization_id", "name", name="uq_db_conn_org_name")`) |
| `engine` | String(20), NOT NULL | only `"postgresql"` accepted in Phase 2.2 (validated in schema) |
| `host` | String(255), NOT NULL | hostname/IP of the external DB |
| `port` | Integer, NOT NULL | default `5432` |
| `database_name` | String(255), NOT NULL | external database name |
| `username` | String(255), NOT NULL | external DB login |
| `encrypted_password` | LargeBinary (bytea), NOT NULL | **ciphertext only** — app-level authenticated encryption of the external password (§3) |
| `ssl_mode` | String(20), nullable | external TLS mode (e.g. `require`, `verify-full`); NULL → driver default |
| `enabled` | Boolean, NOT NULL, default True | soft switch; disabled connections reject test/discovery/import with 400 |
| `status` | String(20), NOT NULL, default `"unknown"` | `unknown` / `ok` / `failed` — updated only by the test endpoint |
| `last_checked_at` | DateTime(timezone=True), nullable | last successful/attempted connection check |
| `last_error` | String(500), nullable | sanitized error from last check — **must not contain the password or full DSN** (§3) |
| `created_at` / `updated_at` | DateTime(timezone=True), NOT NULL | `server_default=func.now()`; `updated_at` via `onupdate=func.now()` — **match nullable=False exactly in the migration** (lesson from `0003`) |

Model exported from `app/models/__init__.py` (same convention as `DataSourceFile`).

**Schema note (important):** every column declared `Mapped[X]` (non-Optional) must be
`nullable=False` in the migration, and `Mapped[Optional[X]]` → `nullable=True`.
`alembic check` must stay clean after `0004` — this exact drift class was found and
fixed in Phase 2.1's gate.

---

## 3. Security requirements (non-negotiable)

1. **Organization isolation** — every query on `database_connections` filters
   `organization_id` explicitly (defense-in-depth layer 3, same as `FileStorage`).
2. **RLS ENABLE + FORCE** — `database_connections` added to the `rls_tables` dict in
   `app/main.py` with the identical strict policy used by `data_sources` /
   `fact_rows` / `data_source_files` / `data_source_columns`:
   `USING (organization_id::text = current_setting('app.current_org_id', true))`
   and the same `WITH CHECK` — **fail-closed** (no `IS NULL` fallback).
3. **App role stays `NOSUPERUSER` + `NOBYPASSRLS`** (`tasmim_app`) — unchanged from
   Phase 2.1; `init-db/01-app-user.sh` is not modified. FORCE is effective because
   the app role owns the tables but cannot bypass RLS.
4. **Credentials encrypted at rest** — the external password is never stored as
   plaintext. Use authenticated encryption from the `cryptography` package
   (add `cryptography==...` to `requirements.txt`):
   - `ENCRYPTION_KEY` env var (Fernet-compatible 32-byte key) read through the
     existing `_get_secret(name, dev_fallback)` pattern in `app/core/config.py` →
     **raises `RuntimeError` in production when missing** (same as `JWT_SECRET`).
   - Dev fallback value must be obviously dev-only
     (e.g. `"dev-only-encryption-key-do-not-use-in-prod"`), consistent with existing
     fallbacks; production is fail-fast.
   - Encryption lives in a small helper module `app/core/credentials.py`
     (`encrypt_secret(plaintext) -> bytes`, `decrypt_secret(ciphertext) -> str`);
     key is read once via `settings.ENCRYPTION_KEY`.
   - Technical debt (document, do not build): per-organization key rotation.
5. **ENCRYPTION_KEY required in production** — see 4; same contract as
   `JWT_SECRET` / `TASMIM_APP_DB_PASSWORD` / `DATABASE_URL` in `config.py`.
6. **Plaintext passwords never returned by API** — response schemas carry
   `has_stored_credentials: bool` and **no** password field, no masked hint, no DSN.
   `POST` accepts the password (write-only), responses never echo it.
7. **No secrets in logs** — never log the DSN, password, or ciphertext. SQLAlchemy
   `echo` stays off. Connector exceptions are wrapped so driver errors (which can
   embed conninfo) are reduced to a sanitized message + error class before storage
   in `last_error` or logging.
8. **No secrets committed to Git** — `ENCRYPTION_KEY` comes from the environment
   only; `.env` files are never committed (repo convention).
9. **Credentials never cross organization boundaries** — decryption happens only
   inside the connector path after the `database_connections` row has been fetched
   **through RLS + explicit `organization_id` filter** for the requesting org. A
   cross-org id must 404 before any decryption occurs (tested, §9).

---

## 4. Connector abstraction

New package `apps/api/app/core/connectors/`:

```python
# base.py
class ConnectorError(Exception): ...          # sanitized, safe to surface
class ConnectorTimeout(ConnectorError): ...
class ConnectorReadOnlyViolation(ConnectorError): ...

class DatabaseConnector(ABC):
    @abstractmethod
    def test_connection(self) -> ConnectionCheck: ...
    """SELECT 1 with short timeouts; returns ok/latency or raises."""

    @abstractmethod
    def discover_tables(self) -> list[TableInfo]: ...
    """schema-qualified table names (+ approximate row counts where cheap)."""

    @abstractmethod
    def sample_rows(self, table: str, limit: int) -> list[dict]: ...
    """parameterized SELECT with hard LIMIT."""

    @abstractmethod
    def fetch_dataframe(self, table: str, limit: int) -> "pd.DataFrame": ...
    """same SELECT, returned as a DataFrame for the import bridge (§7)."""
```

**Phase 2.2 implements `PostgresConnector` only** (`postgres.py`, psycopg3 — already a
dependency). The ABC exists so MySQL/SQL Server/Oracle can slot in later; **do not
implement or stub them now** (§11). `engine == "postgresql"` is the only accepted
value; the factory `get_connector(conn: DatabaseConnection) -> DatabaseConnector`
raises `ConnectorError` for unknown engines.

## 5. PostgreSQL connector security

- **Read-only operations only; SELECT only.** The connector builds every statement
  internally: `SELECT * FROM <table> LIMIT <n>` and catalog queries
  (`information_schema.tables` / `pg_class`) for discovery. There is **no method that
  accepts raw SQL**, and **no API endpoint that executes arbitrary SQL** (§11).
- **Safe identifier handling** — table/schema names are quoted with
  `psycopg.sql.Identifier`; never f-strings/concatenation. `table` inputs are
  validated against `discover_tables()` results before use.
- **Configurable row limits** — module constants `MAX_SAMPLE_ROWS = 1000`,
  `MAX_FETCH_ROWS = 100_000`; request-supplied limits are clamped to these.
- **Query timeout** — `statement_timeout` applied via connect options
  (`options="-c statement_timeout=<ms>"`); constructor parameter (default from a
  module constant, e.g. 15s) so tests can force it deterministically.
- **Connection timeout** — `connect_timeout` in the DSN (e.g. 5–10s) so the API
  never hangs on an unreachable host.
- **Read-only session** — connect options include
  `default_transaction_read_only=on`; the operator is additionally advised (README
  note) to provision the external credential as a read-only role. The connector
  never issues `INSERT/UPDATE/DELETE/DDL`, and there is no code path that could.
- **SSL** — `ssl_mode` from the row is passed through to the driver
  (`sslmode=`); NULL → driver default.
- **SSRF note (documented risk, minimal handling in 2.2):** host is user-supplied,
  so the server makes outbound connections to it. Phase 2.2 validates host format
  only; a host allow-list is future hardening — record as technical debt in
  `docs/PHASE_STATUS.md` when implementing.

---

## 6. API endpoints

New router `apps/api/app/routers/database_connections.py`,
`prefix="/database-connections"`, `tags=["database-connections"]`, registered in
`app/main.py` after the existing routers. All endpoints follow the existing
conventions: `Depends(get_current_user)` + `Depends(get_current_organization_id)` +
`require_membership` / `require_role(...)`; `X-Organization-Id` header semantics
unchanged.

| Endpoint | Method | Purpose | Authorization |
|---|---|---|---|
| `/database-connections` | POST | create connection (password write-only) | **admin+** → `require_role(["owner", "admin"])` |
| `/database-connections` | GET | list connections for the org (no secrets) | **manager+** → `["owner", "admin", "manager"]` |
| `/database-connections/{id}` | GET | single connection (no secrets) | **manager+** |
| `/database-connections/{id}` | DELETE | hard delete (FK `ON DELETE SET NULL` on `data_sources`) | **admin+** |
| `/database-connections/{id}/test` | POST | run `test_connection()`, update `status`/`last_checked_at`/sanitized `last_error` | **admin+** (triggers outbound connection with stored credentials) |
| `/database-connections/{id}/tables` | GET | `discover_tables()` | **manager+** |
| `/database-connections/{id}/tables/{table}/sample` | GET | `sample_rows(table, limit≤MAX_SAMPLE_ROWS)` | **analyst+** → `["owner", "admin", "manager", "analyst"]` |

Authorization rationale (least privilege): viewers see nothing; analysts consume
data (sample); managers inspect configuration; only admins manage credentials and
trigger outbound connections. `require_role` matches on the RoleEnum string value —
do not invent new roles. Cross-org ids must return **404** (not 403) so existence is
not leaked — enforced by RLS + explicit org filter, same as `/data-sources/{id}`.

A disabled (`enabled=False`) connection returns **400** on test/tables/sample.

There is intentionally **no PUT/PATCH** in Phase 2.2 (rotate = delete + recreate).

---

## 7. DataSource integration (external table → fact layer)

Phase 2.1 established the normalization path:
`upload → data_source_files (bytea) + data_source_columns → POST /data-sources/{id}/map → fact_rows`.
Phase 2.2 **reuses it unchanged** for external sources:

1. **One new integration endpoint:**
   `POST /database-connections/{id}/import` (admin+, body: `{"table": str,
   "limit": int ≤ MAX_FETCH_ROWS, "name"?: str}`). This is the *minimum required
   integration* — no scheduling, no incremental sync (§11).
2. **Flow inside the endpoint** (single DB transaction on the app session):
   `fetch_dataframe(table, limit)` → build the exact same artifacts the upload
   endpoint builds: a `DataSource` row (`file_type="postgres"`,
   `status="pending"`) + `FileStorage.save(...)` of a CSV serialization of the
   DataFrame + `_persist_columns(...)` for metadata. Reuse the existing helper
   functions from `app/routers/data_sources.py` (extract them into a shared module,
   e.g. `app/services/ingest.py`, **without changing their behavior** — the CSV
   upload flow must keep passing).
3. **Mapping stays the existing flow:** the client then calls
   `POST /data-sources/{ds_id}/map` exactly as today → `fact_rows` with
   `organization_id`. Zero changes to `MapRequest`/`MapResponse`/`fact_rows`.
4. **Provenance (the ONLY DataSource change):** add one nullable column
   `database_connection_id UUID NULL REFERENCES database_connections(id) ON DELETE
   SET NULL` to `data_sources` in migration `0004` (plus index). Rationale: lineage
   ("which connection fed this source") with zero redesign — file uploads keep
   `NULL`. `DataSourceOut` gains the optional field; nothing else on the model
   changes. Imported `DataSource` rows keep participating in RLS, storage, and
   deletion exactly like uploaded ones (`data_source_files` row included).
5. **Row_count/status semantics** unchanged: `row_count` = imported rows,
   `status` → `"pending"` until mapped.

---

## 8. Migration requirements

- **Alembic only** — one new migration
  `apps/api/alembic/versions/0004_database_connections.py`,
  `down_revision = "0003_align_created_at_nullable"` (linear chain
  `<base> → 0001_baseline → 0002_persist_uploads → 0003_align_created_at_nullable → 0004`,
  single head).
- **No `create_all` startup migration** — schema changes only via Alembic; app
  startup keeps `run_startup_migrations(engine)` (Phase 2.0 behavior). Test modules
  may keep their existing `create_all`-if-missing pattern.
- `0004` creates `database_connections` (+ unique constraint, indexes). The
  provenance column ships as its own migration `0005_data_source_provenance`
  (`down_revision = "0004_database_connections"`) — nullable
  `data_sources.database_connection_id` FK `ON DELETE SET NULL` + index.
- **No RLS in the migration** — repo convention: RLS is applied at runtime by
  `app/main.py` (`ENABLE + FORCE + fail-closed`), consistent with Phase 2.1. Add
  `database_connections` to the `rls_tables` dict there, and to the RLS setup +
  policy-assertion loops in `tests/test_data_sources.py` (same pattern used for
  `data_source_files`/`data_source_columns`).
- **`alembic check` must be clean** after `0004` (models ↔ migration parity — the
  Phase 2.1 gate lesson; every non-Optional `Mapped` column must be `nullable=False`
  in DDL).
- Upgrade must be tested against a **fresh empty database** (the Phase 2.1 gate
  procedure) and must not touch existing tables other than the one added column.

---

## 9. Testing requirements

Follow existing conventions: PostgreSQL integration tests live in
`tests/test_database_connections.py` (module-level skip without a live DB, same
pattern as `test_data_sources.py`/`test_rls_postgres.py`); SQLite-runnable unit
tests where clean (pattern of `test_storage_unit.py` / `test_tenant_isolation.py`);
**no faked RLS**.

1. **Model tests** — constraints (unique org+name, FKs), defaults, `updated_at` behavior.
2. **Encryption tests** — `encrypt_secret`/`decrypt_secret` round-trip; ciphertext ≠
   plaintext; tampered ciphertext raises; `ENCRYPTION_KEY` missing + `ENV=production`
   → `RuntimeError` at settings load.
3. **Tenant isolation** — org B sees zero `database_connections` rows under RLS;
   cross-org GET/DELETE/test/tables/sample → 404; cross-org connector use impossible
   (row never visible → decryption never happens).
4. **RLS runtime tests against real PostgreSQL** — for `database_connections`:
   `relrowsecurity` (ENABLED) and `relforcerowsecurity` (FORCE) both true; policy
   qual fail-closed (no `IS NULL`), contains `current_setting('app.current_org_id', true)`
   and `organization_id`; no-context query returns 0 rows; insert without context fails
   (`WITH CHECK`) — extend the existing policy-assertion loop.
5. **Connector connection test** — `test_connection()` ok against a real scratch
   PostgreSQL; failure path returns sanitized `last_error`, `status="failed"`.
6. **Timeout behavior** — unreachable host → `connect_timeout` bounds the call;
   `statement_timeout` aborts a long read — the test seeds a large scratch table
   via its own admin fixture, constructs the connector with a 1ms timeout, and
   asserts `sample_rows` raises `ConnectorTimeout` (no raw SQL needed).
7. **Row-limit enforcement** — request limit > `MAX_SAMPLE_ROWS`/`MAX_FETCH_ROWS`
   is clamped; returned rows never exceed the cap (scratch DB seeded with more rows).
8. **Read-only enforcement** — connector raises on any non-SELECT statement type;
   session is opened `default_transaction_read_only=on` (assert a write attempt on
   the scratch DB via the connector's session raises); no write path exists on the API.
9. **Credential non-disclosure** — POST/GET responses (and error bodies) contain no
   password/DSN/ciphertext; DB row stores non-plaintext bytes decryptable only with
   the configured key; `caplog` shows no secret material; `last_error` sanitized.
10. **API authorization** — 401 unauthenticated; 403 for below-threshold roles on
    every endpoint (viewer on all; analyst on create/delete/test; manager on
    create/delete/test); 200 for permitted roles (mirror `test_tenant_isolation.py` style).
11. **Import bridge** — import creates `DataSource(file_type="postgres")` + persisted
    bytes + `data_source_columns` + provenance FK; existing map flow normalizes into
    `fact_rows`; deleting the connection sets `database_connection_id=NULL` and
    leaves the ingested source intact.
12. **Full regression suite** — `cd apps/api && .venv/bin/pytest -q -rs` all green
    (Phase 2.1 baseline: 23 passed, 0 failed, 0 skipped — must not regress), plus
    `alembic check` / `alembic current` / `alembic heads` clean on a real PostgreSQL,
    using the Phase 2.1 gate procedure (real server or `docker-compose.test.yml`).

---

## 10. Acceptance criteria (all must be green to mark Phase 2.2 COMPLETE)

- [x] `database_connections` table exists via migration `0004` only; chain linear
      (`… → 0003 → 0004 → 0005`), single head; no `create_all` at startup.
- [x] `alembic upgrade head` clean on a fresh empty PostgreSQL; `alembic current`
      = `0005_data_source_provenance`; `alembic check` reports no operations.
- [x] RLS verified at runtime on `database_connections`: ENABLED + FORCE +
      fail-closed + organization-scoped; app role `NOSUPERUSER`/`NOBYPASSRLS` unchanged.
- [x] Cross-org access returns 404 on every endpoint; RLS blocks DB-level access.
- [x] Password stored only as ciphertext; plaintext never appears in any API
      response, log, or error; `ENCRYPTION_KEY` enforced in production.
- [x] Connector is PostgreSQL-only, SELECT-only, identifier-safe, timeout-bounded,
      row-limit-clamped; no arbitrary-SQL endpoint exists.
- [x] All 7 management/inspection endpoints + `POST /database-connections/{id}/import`
      behave per §6/§7 with the §6 authorization matrix.
- [x] Imported tables normalize into `fact_rows` via the unchanged map flow;
      provenance column set; connection deletion leaves ingested data intact.
- [x] Full suite green with 0 skipped integration tests on a real PostgreSQL
      (Phase 2.1's 23 tests must not regress).
- [x] `docs/PHASE_STATUS.md` updated: Phase 2.2 COMPLETE with evidence.

## 11. Explicit non-goals (Phase 2.2 must NOT include)

- KPI engine, AI assistant, forecasting, anomaly detection, dashboard redesign
- MySQL / SQL Server / Oracle connectors (ABC only; no stubs beyond the factory error)
- Arbitrary SQL execution (no user-supplied SQL anywhere in the connector or API)
- ETL orchestration beyond the single synchronous import bridge in §7
  (no scheduling, no incremental/CDC sync, no transformations)
- Connection update endpoint, credential rotation, per-org encryption keys
  (documented debt), host allow-listing (documented debt)

## 12. Relationship to roadmap

| Phase | Scope | Status |
|---|---|---|
| 2.0 | Alembic baseline, startup migrations | **COMPLETE** |
| 2.1 | Persistent upload storage (`data_source_files`/`data_source_columns`), FileStorage abstraction, PENDING_UPLOADS removal | **COMPLETE** — commit `9dad4d9` |
| 2.2 | External Database Connections (this document) | **COMPLETE** — Steps 1 (foundation), 2 (PostgreSQL connector), 3 (API endpoints), 4 (import bridge + provenance `0005`) all implemented and gated |
| 2.3 | KPI Engine | not planned in detail yet |
| 2.4 | Context Catalog (AI-assistant preparation) | not planned in detail yet |

Implementation order within Phase 2.2 (suggested): credentials module + config →
model + migration `0004` + RLS wiring → connector ABC + `PostgresConnector` →
management/inspection endpoints + tests → import bridge + DataSource provenance →
full gate (§10).
