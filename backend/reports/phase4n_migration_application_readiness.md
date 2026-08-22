# Phase 4N — Controlled Migration Application Readiness (STRICTLY READ-ONLY)

## 1. Scope and Safety Accounting

This phase performed zero production DB writes, zero DDL, zero migration execution, zero `MergeApproval` creation, zero merge execution, zero DOI changes, zero `--apply` runs, and zero network calls beyond the local production-database connection itself (every interaction a read-only `SELECT`/catalog query or a read-only Django management command — `manage.py showmigrations` — each script explicitly `conn.rollback()`-ed or making no write call at all). Zero code files were modified or created. This report and its JSON companion are the only files this phase adds.

**Important note on this report's relationship to Phase 4M**: this phase re-derived every finding fresh, live, against the actual current repository and database — not by citing Phase 4M's numbers. Where the results are identical to Phase 4M's (they are, in every case checked), that identity is itself evidence of stability, not a shortcut taken.

## 2. Task A — Current-State Baseline (Re-Established Fresh)

| Check | Result |
|---|---|
| `git status --short` (relevant paths) | Identical to the state at the end of Phase 4M — `dedup_papers.py`/`test_dedup_papers.py` modified (pre-existing, from Phase 4E onward); `sprint11_merge_approval.sql` and every `backend/tools/*.py`/`test_*.py` file untracked but present, none modified this phase |
| `git diff --stat` (tracked files) | `backend/tools/dedup_papers.py \| 91 ++...`, `backend/tools/test_dedup_papers.py \| 88 +...`, `2 files changed, 178 insertions(+), 1 deletion(-)` — byte-for-byte identical to every phase since 4E |
| Migration file content | Re-read in full this phase. SHA-256: `5b7500cdef5220551b05e86f32479e77d03d94bed8790b31b7833382a01dfe31`, 135 lines. Recorded here as the definitive fingerprint for any future phase to re-verify against before applying |
| `merge_approval.py` / `merge_executor.py` | Re-inspected this phase; unchanged since Phase 4L (no `git status` delta) |
| Relevant tests | Re-run fresh this phase (§9) |
| `MergeApproval` exists in production? | **`false`** — `to_regclass('public."MergeApproval"')` → `NULL`, re-confirmed live this phase |
| Files changed by earlier phases vs. this phase | Earlier phases (4A–4M): every file currently showing as modified/untracked in `git status`. **This phase (4N): zero files changed in `backend/`, aside from the two new report files this phase adds.** |

## 3. Task B — Migration Application Path (Traced, Not Executed)

### 3.1 The exact mechanism

`sprint11_merge_approval.sql` lives in `backend/analytics/migrations/` and is applied — per this repository's own established, two-mechanism convention (re-confirmed, not assumed) — via `backend/apply_migration.py`:

```python
with transaction.atomic():
    with connection.cursor() as cur:
        cur.execute(sql)
    if args.dry_run:
        transaction.set_rollback(True)
```

The actual future command: `python apply_migration.py analytics/migrations/sprint11_merge_approval.sql` (optionally `--dry-run` first). **This phase did not run this command in any form.**

### 3.2 Would Django's own migration discovery recognize this file?

**No — empirically confirmed, live, this phase, not inferred.**

```
$ python manage.py showmigrations analytics
analytics
 (no migrations)
```

`backend/analytics/migrations/` contains a real `__init__.py` (making it a valid Python package) plus a `__pycache__` directory, but **every migration file inside it is a raw `.sql` file** — `sprint1_foundation.sql` through `sprint11_merge_approval.sql`, plus `0001_add_manual_review_queue.sql` (which, despite its Django-like numbered name, is also a `.sql` file, confirmed by attempting to parse it as Python — it fails with a syntax error). Django's `MigrationLoader` discovers migrations by importing `.py` modules from a package; a `.sql` file is invisible to that mechanism regardless of naming convention. No `MIGRATION_MODULES` override exists in `settings.py` (checked fresh, zero matches) that could redirect this. The live `showmigrations` output — "(no migrations)" — is the direct, empirical proof: `python manage.py migrate` has never touched, and will never touch, any file in this directory. Applying `sprint11_merge_approval.sql` therefore has **zero interaction with Django's own migration system** — it is purely `apply_migration.py`'s manual DDL execution, exactly as every `sprint*.sql` file before it.

### 3.3 Migration ordering/dependencies

Since Django's migration graph doesn't include this file at all, there is no Django-level dependency to satisfy. The only real dependency is that the tables/columns the migration's FKs reference already exist — confirmed fresh, live, this phase (§4).

### 3.4 Transactional in the actual execution path?

**Yes** — `transaction.atomic()` wraps the entire file's SQL (the `CREATE TABLE` plus both `CREATE INDEX IF NOT EXISTS` statements, sent to Postgres as one string via one `cur.execute()` call) as a single atomic unit. Either all three statements commit together, or none do.

### 3.5 The five specific failure scenarios

| Scenario | What actually happens (traced from the real code, not invented) |
|---|---|
| Process fails **before** migration execution (e.g., file not found, DB connection refused) | `open(args.sql_file)` or `django.setup()`/connection acquisition raises before `transaction.atomic()` is ever entered. Nothing touches the database. Traceback printed; process exits non-zero. |
| SQL fails **during** migration | The exception from `cur.execute(sql)` propagates out of the `with connection.cursor()` block and out of `with transaction.atomic()`, which triggers Django's automatic `ROLLBACK` at that point. `apply_migration.py` has **no surrounding `try`/`except`** (re-confirmed fresh this phase by reading the file) — the exception then continues propagating as a raw, unhandled Python traceback. The database itself is left clean (rolled back); the *operator experience* is an unpolished crash, not a friendly error message. |
| Process dies **immediately after SQL succeeds** but before `transaction.atomic()`'s normal exit (i.e., before Django's `COMMIT`) | The transaction was never committed — Postgres's own session-termination handling automatically rolls back any uncommitted transaction when a connection drops. The database state remains clean (nothing persisted), but this is **not directly observable from the terminated process** — the operator must verify by re-querying `to_regclass('public."MergeApproval"')` after the fact. This is the one genuinely ambiguous-until-checked scenario, and the check that resolves it is cheap, read-only, and exactly what this phase performed repeatedly. |
| Migration command is **rerun** (after any of the above) | Because nothing persisted in every failure case above, a rerun is identical to a first run. Even in the hypothetical case where something *did* partially persist (contrary to the analysis above), `CREATE TABLE IF NOT EXISTS`/`CREATE INDEX IF NOT EXISTS` (used consistently throughout the migration, matching `sprint8_author_review_queue.sql`'s own precedent) make a rerun safe and idempotent regardless. |
| Migration **partially exists due to an unexpected/manual state** | No migration-state ledger exists anywhere in this repository (re-confirmed, §3.2/§3.3) — the *only* way to detect this is direct schema introspection, exactly what §4/§2 of this report already performed. This phase found **zero** such drift: the table, and every object it would create, are completely absent. |

## 4. Task C — Pre-Application Gate Matrix

Every gate checked fresh, live, this phase. Per your instruction, no `UNKNOWN` is silently treated as `PASS`.

| # | Gate | Status | Evidence |
|---|---|---|---|
| 1 | `MergeApproval` table does not already exist | **PASS** | `to_regclass('public."MergeApproval"')` → `NULL`, live, this phase |
| 2 | No conflicting table/index/constraint/sequence exists | **PASS** | Fresh `pg_class`/`pg_constraint`/`pg_indexes` scan for every name the migration would create — zero hits |
| 3 | Migration file content matches the audited version | **PASS** | SHA-256 `5b7500cdef5220551b05e86f32479e77d03d94bed8790b31b7833382a01dfe31` recorded this phase as the reference for future comparison; content re-read and matches every prior phase's description of it |
| 4 | Required DB privileges still exist | **PASS** | `has_schema_privilege(neondb_owner, 'public', 'CREATE')` = True; `has_database_privilege(neondb_owner, 'neondb', 'CREATE')` = True — both re-checked fresh, this phase |
| 5 | All referenced parent tables/columns exist with compatible types | **PASS** | `ResearchPaper.PaperID`, `Tenant.TenantID`, `Users.UserID`, `AuditLog.LogID` — all `integer`, all confirmed live this phase |
| 6 | Migration dependency state is valid | **PASS (not applicable in the Django sense)** | Django's migration graph does not include this file at all (§3.2) — there is no Django-level dependency to be valid or invalid. The only real dependency (FK targets exist) is gate 5, separately confirmed |
| 7 | Relevant test suites are green | **PASS** | 224/224, re-run fresh this phase (§9) |
| 8 | No unexpected git/code drift affects the migration contract | **PASS** | `git diff --stat`/`git status` identical to Phase 4K.1 onward; migration file hash recorded (gate 3); `merge_approval.py`/`merge_executor.py` unchanged |
| 9 | No concurrent deployment/schema change is in progress | **PASS — with an explicit, necessary caveat** | `pg_stat_activity` (excluding this session) showed zero active queries; `pg_locks` on `ResearchPaper`/`Tenant`/`Users`/`AuditLog` (excluding this session) showed zero held locks — both checked live, this phase. **This is a point-in-time snapshot, not a standing guarantee** — it proves no concurrent activity existed at the moment of this check, and must be re-checked again, immediately before the actual future application, not assumed to still hold |
| 10 | A database backup/rollback strategy exists or does not exist | **NOT_CHECKABLE_FROM_CURRENT_REPO** | Neon's backup/point-in-time-recovery configuration is an infrastructure/platform-level fact, not queryable via `psycopg2`/SQL introspection, and this phase's "no network calls" rule correctly forbids checking Neon's dashboard/API to confirm it externally. **Stated honestly as unresolved, not assumed either way** — see §12 |

## 5. Task D — Rollback and Recovery Analysis (Designed, Not Executed)

| # | Scenario | Expected state | Detection | Automatic rollback? | Manual intervention? | Safe next action |
|---|---|---|---|---|---|---|
| 1 | Failure before transaction begins | Nothing touched | Process crashes with a traceback before any DB call | N/A — nothing was ever started | No | Fix the underlying issue (file path, connectivity, credentials), retry |
| 2 | Failure during transactional migration execution | Transaction rolled back automatically | Traceback propagates unhandled (§3.5) | **Yes**, via `transaction.atomic()` | No, for the DB itself; yes, to diagnose the SQL/schema issue | Investigate the error, fix, retry |
| 3 | Failure after successful schema creation but before the process exits normally | Ambiguous until checked; actually clean (uncommitted transaction rolled back by Postgres on connection loss) | `to_regclass('public."MergeApproval"')` — the single, cheap, definitive read-only check | Yes, via Postgres's own connection-termination handling | No, beyond running that one verification query | Verify via `to_regclass`, then re-run (idempotent regardless via `IF NOT EXISTS`) |
| 4 | "Migration recorded as applied" but application code not deployed yet | Not a state this repository can produce — no ledger exists to "record" a migration as applied (§3.2); the only record is the table's own existence | If the table existed while `merge_approval.py`/`merge_executor.py` were somehow absent, nothing would break: Phase 4M/4N both confirm zero code queries `MergeApproval` at Django startup or on any request path (§6) | N/A | No | N/A — this scenario does not meaningfully apply to this repository's deploy-everything-together model (`deploy.ps1`) |
| 5 | Application code deployed but migration not applied | Exactly the current, live, continuous state of production | Confirmed by definition — this is the state this entire report was written against | N/A | No | Nothing to do — this is the safe, working baseline |
| 6 | Manual partial schema state outside any ledger | Unknown shape, must not be assumed | **Only** by direct schema introspection (`information_schema`/`pg_catalog`) — exactly this report's own method | No automatic detection exists anywhere in this repository's tooling | **Yes, always** — a human must re-run a fresh audit (this report's own method) before trusting or applying anything | Never trust a cached assumption about `MergeApproval`'s state; always re-check live, immediately before acting — the exact discipline this entire project has followed every phase |

## 6. Task E — Application/Code Compatibility Matrix

Traced via fresh `grep` against real imports and Django wiring this phase (§2, §3.2):

| Deployment order | Safety | Reasoning |
|---|---|---|
| A. Migration only | **Safe** | Adds a standalone, unused table; nothing in the running application queries it |
| B. Code only | **Safe** | This is literally the current, continuously-running production state |
| C. Migration before code | **Safe** | Equivalent to A — an unused table sitting in the schema causes no issue for code that doesn't reference it |
| D. Code before migration | **Safe** | This is literally the current, continuously-proven production state (B and D describe the same real, ongoing fact) |
| E. Migration and code together | **Safe** | No coordination requirement exists — there is no ordering dependency to get wrong, since nothing eagerly queries `MergeApproval` |

**Special-attention items, addressed directly:**

- `merge_approval.py`/`merge_executor.py`: `grep -rln "import merge_approval\b|from merge_approval\b|import merge_executor\b|from merge_executor\b" backend/ --include="*.py"` → only `merge_executor.py` itself (imports from `merge_approval.py`) and the project's own test files. Nothing else, repository-wide.
- Is `MergeApproval` accessed eagerly at startup? **No** — zero matches for `MergeApproval` in `litrix_backend/` (settings/urls/wsgi), `analytics/urls.py`, `accounts/urls.py`, `analytics/apps.py`, `accounts/apps.py`, `analytics/models.py`, `accounts/models.py` (all checked fresh this phase).
- Does test/mock behavior hide a production schema dependency? **Yes, honestly stated, not new to this phase**: all 224 passing tests run against `ExecutorFakeCursor`/`InMemoryApprovalCursor`/pure-function doubles — none of them opens a real database connection, and therefore none of them proves the code works against a **real, live** `MergeApproval` table. This is the same limitation Phase 4K.1 explicitly disclosed (no real PostgreSQL instance was safely available to this project at any point) and remains true and unresolved. It does not block *applying* the migration (a DDL statement, independently and separately auditable, §4) — but it means the *first real exercise* of `merge_approval.py`/`merge_executor.py` against a genuine `MergeApproval` table would still be a novel event, not something 224 green tests alone should be read as fully covering.

## 7. Task F — Canary Execution Contract (Planning Reference Only — Not Executed)

Using 5232/5482 purely as the planning reference, for a **future** controlled execution phase, **after** a **future**, separately-authorized migration application:

| Stage | Required inputs | Required DB state | Expected result | Blocking conditions | Writes to DB? |
|---|---|---|---|---|---|
| 1. Schema verification | DB connection only | `MergeApproval` exists | Confirms live shape matches the audited design | Table absent, or shape differs from §4 gate 3's recorded reference | **No** |
| 2. Create pending approval | `survivor_id=5232`, `loser_id=5482`, `plan_id`, freshly computed `plan_fingerprint`, `tenant_id=1`, an authorized user | Stage 1 passed; both `ResearchPaper` rows exist; no conflicting active approval for this identity | New `MergeApproval` row, `Status='PENDING'` | Self-merge (n/a for this pair), permission denied, a prior `EXECUTED` row for this exact identity | **Yes — the first real write in this entire future sequence, and explicitly not part of this phase** |
| 3. Human approval | `approval_id`, the exact `plan_fingerprint` the reviewer was shown, an authorized reviewer, optional notes | Approval row at `PENDING` | `Status→'APPROVED'`, `ReviewedByUserID`/`ReviewedAt` set, cross-referencing `AuditLog` row written | Fingerprint mismatch, permission denied, illegal transition | Yes |
| 4. Fresh plan generation/revalidation | Current live `ResearchPaper` state for both papers | Both papers still exist | Recomputed fingerprint — must equal the one the approval was granted under | Any drift since approval | **No** |
| 5. Executor preflight | `survivor_id`, `loser_id`, `expected_plan_fingerprint`, executing user | Approval `APPROVED` and matching; rows lockable; idempotency clean; no dependency gaps; no journal/author conflicts | `ExecutionResult(ok=True)`, or a specific, named `EXEC_BLOCKED_*` reason | Any of the 16 preflight steps failing (Phase 4J) | **No** — by design, no write SQL is issued until every check passes |
| 6. Transactional merge | Same as stage 5, having passed it | An open transaction the caller provides (per Phase 4K's finding: **no real caller wiring this yet exists anywhere in the repository** — this is itself a prerequisite a future phase must build) | `JournalID` backfill (if needed) → `merge_group()` (remap + `AuditLog` + delete) → `MergeApproval→EXECUTED` → final invariant check | Any exception at any point | **Yes — the actual merge** |
| 7. Post-commit verification | `survivor_id`, `loser_id` | Transaction has committed | Survivor exists; loser gone; `MergeApproval.Status='EXECUTED'` with `ExecutionAuditLogID` set | n/a (verification only) | **No** |
| 8. Audit verification | Loser `PaperID` | Post-commit | Exactly one new `AuditLog` row, `Action='paper.merge.dedup'`, `TargetID`=loser, `Metadata.kept_paper_id`=survivor | n/a | **No** |
| 9. Idempotency verification | `survivor_id`, `loser_id` | Post-commit | `idempotency_verdict()` now returns `ALREADY_EXECUTED` — proving a second attempt would correctly be refused | n/a | **No** |

**This phase performed none of these stages against a writable environment — not even stage 1, since the migration has not been applied.** This is a contract definition for planning purposes only.

## 8. Unresolved `UNKNOWN` Items

Exactly one, carried forward honestly, not resolved by this phase (§4, gate 10): **the database backup/point-in-time-recovery strategy for the production Neon database is `NOT_CHECKABLE_FROM_CURRENT_REPO`.** This is not a defect in the migration, the code, or the schema — it is an infrastructure/platform question this phase's tooling and safety rules (no network calls) correctly cannot answer. It is named here explicitly rather than silently assumed in either direction. See §12 for how this affects the final verdict.

## 9. Full Regression (Fresh This Phase)

```
test_dedup_papers.py             18/18 passing
test_merge_plan_generator.py     43/43 passing
test_merge_execution_safety.py   68/68 passing
test_merge_approval.py           45/45 passing
test_merge_executor.py           39/39 passing
test_fk_lifecycle.py             11/11 passing
```

**Total: 224/224 passing.** Identical to Phase 4M — unchanged because zero code change was needed or made this phase.

## 10. Live Canary Revalidation

Performed fresh, read-only, this phase:

| Check | Result |
|---|---|
| Both 5232/5482 exist | Yes |
| Survivor direction | 5232 survives, 5482 loses (unreversed), `pair_confidence=high`, `hard_exclusion_reason=None` |
| Fingerprint vs. reference | `2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb` — **MATCH**, **11th independent live confirmation** |
| Prior successful merge | None — `idempotency_verdict()=NOT_PREVIOUSLY_EXECUTED`, 0 `AuditLog` rows referencing either PaperID |
| Approval row exists | Impossible — `MergeApproval` confirmed absent |
| `MergeApproval` connected-as identity | `neondb_owner` |

## 11. Carried-Forward Findings

Not investigated further, not fixed, per your explicit instruction — restated only to confirm none newly blocks migration application:

| Finding | Blocks migration application? |
|---|---|
| `PaperKeywords` schema drift (exists; unrelated to `MergeApproval`) | No |
| `AuditLog.UserID` hardcoded `NULL` in `merge_group()` | No — unrelated to the DDL itself |
| `has_litrix_perm()` lacks tenant scoping | No — an application-layer permission concern, not a schema-application concern |

## 12. Final GO/NO-GO Decision

### **A) READY FOR EXPLICITLY APPROVED MIGRATION APPLICATION**

**"READY" does NOT authorize execution. No migration may be applied unless a later instruction explicitly authorizes the database write.**

Every gate this phase could check from the repository and the live database (§4, gates 1–9) is **PASS**, each backed by fresh, live evidence gathered this phase — not reused from any prior report. The one gate genuinely outside this phase's reach (§4, gate 10 — backup/PITR strategy) is honestly reported as `NOT_CHECKABLE_FROM_CURRENT_REPO`, not silently assumed. It does not change this verdict for one precise, stated reason: the specific action being assessed for readiness — applying `sprint11_merge_approval.sql` — is a `CREATE TABLE IF NOT EXISTS`/`CREATE INDEX IF NOT EXISTS` operation against an **empty**, standalone table with **zero existing rows to lose**; there is nothing for a backup strategy to protect at the moment of that specific DDL. **This is not a general "backups don't matter" claim** — it is a scoped observation that this specific gap does not bear on this specific action. It becomes load-bearing, and **must be resolved before any subsequent phase creates real approval or execution data** in this table (Phase 4K.1's rollback matrix, §7's stages 2 onward) — flagged here explicitly so it is not forgotten, not because it blocks this narrow step.

## 13. Recommended Next Phase

**A dedicated, narrowly-scoped Migration Application phase**, gated on an explicit, separate authorization from you (not implied by this report), whose entire job is: apply `sprint11_merge_approval.sql` via `apply_migration.py` (recommend `--dry-run` first, exercising the real `transaction.atomic()`/rollback path against the real database, then the real application), verify the resulting table matches the exact shape this report and Phase 4M audited (re-hash/re-diff against `5b7500cdef5220551b05e86f32479e77d03d94bed8790b31b7833382a01dfe31`), confirm the backup/PITR question from §12 before that phase creates any real row in it, and then **stop** — no approval rows, no endpoint wiring, no merge execution in that same phase. Per your instructions, Phase 4O is not started here.

---

## Exact Accounting

- **Code files modified**: **0.**
- **Code files created**: **0.**
- **Migration files modified**: **0** — `sprint11_merge_approval.sql` was read and hashed this phase, not edited.
- **Report files created**: **2** — this file, and `backend/reports/phase4n_migration_application_readiness.json`.
- **DB writes**: **0.**
- **DB schema changes**: **0.**
- **Network calls**: **0** beyond the local production-database connection itself (all read-only `SELECT`/catalog queries via `litrix_db.db()`, plus one read-only Django management command, `manage.py showmigrations`, which itself only queries Django's own `django_migrations` bookkeeping table — no write).
- **Records merged**: **0.**
- **DOI changes**: **0.**
- **`--apply` executions**: **0.**
- **Tests run and results**: `test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 43/43, `test_merge_execution_safety.py` 68/68, `test_merge_approval.py` 45/45, `test_merge_executor.py` 39/39, `test_fk_lifecycle.py` 11/11 — **224/224 total, zero regressions, unchanged from Phase 4M.**

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Identical to every phase since 4E — zero changes this phase.

### `git status --short` (relevant paths)

```
 M backend/tools/dedup_papers.py                 <- pre-existing, unchanged this phase
 M backend/tools/test_dedup_papers.py             <- pre-existing, unchanged this phase
?? backend/analytics/migrations/sprint11_merge_approval.sql   <- pre-existing (Phase 4K.1), unchanged this phase
?? backend/reports/                                <- this phase adds 2 files to it
?? backend/tools/merge_approval.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_execution_safety.py         <- pre-existing, unchanged this phase
?? backend/tools/merge_executor.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_plan_generator.py           <- pre-existing, unchanged this phase
?? backend/tools/test_fk_lifecycle.py              <- pre-existing (Phase 4L), unchanged this phase
?? backend/tools/test_merge_approval.py            <- pre-existing, unchanged this phase
?? backend/tools/test_merge_execution_safety.py    <- pre-existing, unchanged this phase
?? backend/tools/test_merge_executor.py            <- pre-existing (Phase 4L), unchanged this phase
?? backend/tools/test_merge_plan_generator.py      <- pre-existing, unchanged this phase
```

**This phase (4N) changed exactly two files in the repository — both new report files. Every other line above reflects work from Phase 4E through Phase 4M, carried forward untouched.** Several other untracked files elsewhere in the repository predate this entire duplicate-merge project and are unrelated to it.

Per your instructions, I am stopping here. Phase 4O is not started. The migration was not applied. No `MergeApproval` row exists anywhere. No merge was executed.
