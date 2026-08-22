# Phase 4O — Controlled MergeApproval Migration Application

## Result Summary

**The migration was applied successfully to production.** `MergeApproval` now exists, with the exact audited schema, zero rows, and every existing table in the database — `ResearchPaper`, `Authors`, `AuditLog`, and specifically the canary pair (5232/5482) — confirmed byte-for-byte unchanged. No approval was created. No merge occurred. No DOI was touched.

## Scope Confirmation

The **only** production change this phase made was applying `sprint11_merge_approval.sql` (one `CREATE TABLE`, two `CREATE INDEX` statements, all `IF NOT EXISTS`) via `apply_migration.py`. No other SQL file was applied. No application code was modified. No `MergeApproval` row was created. No approval was created, approved, rejected, or revoked. `merge_group()`/`execute_approved_merge()` were never called against production. No `ResearchPaper` row was inserted, updated, or deleted. No DOI was changed. `dedup_papers.py --apply` was never run. Zero network calls beyond the one production-database connection this entire phase used throughout.

---

## Task A — Final Pre-Write Gate

Every gate re-verified live, immediately before the write, this phase:

| # | Gate | Status | Evidence |
|---|---|---|---|
| 1 | Migration file content re-inspected | **PASS** | Re-read fresh, 135 lines, unchanged from every prior phase's description |
| 2 | Hash matches Phase 4N's audited hash | **PASS** | `sha256sum` → `5b7500cdef5220551b05e86f32479e77d03d94bed8790b31b7833382a01dfe31` — **exact match** to the value recorded in `phase4n_migration_application_readiness.md`/`.json` |
| 3 | `MergeApproval` still does not exist | **PASS** | `to_regclass('public."MergeApproval"')` → `NULL`, live, immediately before the write |
| 4 | No conflicting partial artifacts (table/index/constraint/sequence) | **PASS** | Fresh `pg_class`/`pg_constraint`/`pg_indexes`/`information_schema.sequences` scan for every planned name — zero hits |
| 5 | Application mechanism/command verified | **PASS** | `apply_migration.py`, re-read fresh (confirmed unchanged since Phase 4N by the tool's own "unchanged since last read" signal), wraps `cur.execute(sql)` in `transaction.atomic()`. Exact command used: `python apply_migration.py analytics/migrations/sprint11_merge_approval.sql` (run from `backend/`) |
| 6 | Target is only `sprint11_merge_approval.sql` | **PASS** | Confirmed by construction — this was the sole file path passed to the command |
| 7 | DB connection targets the intended production database | **PASS** | `current_database()='neondb'`, `current_user='neondb_owner'`, host `ep-fragrant-violet-alciwd6u-pooler.c-3.eu-central-1.aws.neon.tech` — the same production Neon database every prior phase in this project connected to via `DATABASE_URL` |
| 8 | No unexpected schema drift | **PASS** | Fresh, final re-check of all four FK-target columns (`ResearchPaper.PaperID`, `Tenant.TenantID`, `Users.UserID`, `AuditLog.LogID`) — all `integer`, unchanged |

**All 8 gates PASS. Zero UNKNOWN, zero FAIL.** Per your instructions, this cleared the way to proceed to Task B.

---

## Task B — Migration Application (Exact Result)

```
$ python apply_migration.py analytics/migrations/sprint11_merge_approval.sql
File   : analytics/migrations/sprint11_merge_approval.sql
Size   : 8613 bytes
Action : EXECUTE
======================================================================

[ok] migration applied.
```

**Result: success, on the first attempt, no retry needed.**

One harmless discrepancy noted for completeness, not a defect: `apply_migration.py` reports `Size: 8613 bytes` (Python's `len(sql)` after UTF-8 decoding — i.e., character count) while the file on disk is 8616 bytes (byte count via `ls`/`wc`). This is explained entirely by one or more multi-byte UTF-8 characters in the file's prose comments (e.g. a typographic dash) — the content itself was independently verified byte-identical to the Phase 4N-audited version via SHA-256 (Gate 2) *before* this run, so this is a decoding-representation artifact, not a content change.

No other SQL file was applied. `apply_migration.py`'s own `transaction.atomic()` wrapping means this was one atomic unit — the `CREATE TABLE` and both `CREATE INDEX` statements committed together, in a single transaction, exactly as Phase 4N's static analysis predicted.

---

## Task C — Post-Application Forensic Verification

### Table

`to_regclass('public."MergeApproval"')` → `"MergeApproval"` — **exists.**

### Columns (18 total, every one verified against the audited design)

| Column | Type | Nullable | Default | Matches design? |
|---|---|---|---|---|
| `ApprovalID` | integer | NO | `nextval('"MergeApproval_ApprovalID_seq"')` | Yes (`SERIAL PRIMARY KEY`) |
| `SurvivorPaperID` | integer | NO | — | Yes |
| `LoserPaperID` | integer | NO | — | Yes |
| `PlanID` | character varying | NO | — | Yes |
| `PlanFingerprint` | character varying | NO | — | Yes |
| `ApprovalVersion` | integer | NO | `1` | Yes |
| `TenantID` | integer | NO | — | Yes |
| `Status` | character varying | NO | `'PENDING'` | Yes |
| `ReviewedByUserID` | integer | YES | — | Yes |
| `ReviewedAt` | timestamptz | YES | — | Yes |
| `ReviewerNotes` | text | YES | — | Yes |
| `RevokedByUserID` | integer | YES | — | Yes |
| `RevokedAt` | timestamptz | YES | — | Yes |
| `RevocationReason` | text | YES | — | Yes |
| `ExecutedAt` | timestamptz | YES | — | Yes |
| `ExecutionAuditLogID` | integer | YES | — | Yes |
| `PlanSnapshotJSON` | jsonb | YES | — | Yes |
| `CreatedAt` | timestamptz | NO | `now()` | Yes |

**Every column, type, and nullability matches the audited design exactly. Zero discrepancies.**

### Constraints — special attention items, verified directly

- **`SurvivorPaperID` behavior**: `MergeApproval_SurvivorPaperID_fkey` → `ResearchPaper.PaperID`, `ON DELETE NO ACTION` — **exactly as designed**, the protective FK is real and live.
- **`LoserPaperID` — no FK, confirmed by absence**: the live constraint list contains **no** `MergeApproval_LoserPaperID_fkey` entry anywhere — only `SurvivorPaperID` has a foreign-key constraint on the two paper-reference columns. This is the exact, intentional Phase 4K.1 design (historical identifier, no live referential dependency on `ResearchPaper`), now confirmed present in the real, live schema, not merely in the file.
- **Self-merge guard**: `chk_merge_approval_not_self` → `CHECK (("SurvivorPaperID" <> "LoserPaperID"))` — exact expression, pulled directly via `pg_get_constraintdef()`, matches the migration file verbatim.
- **`Status` closed-state constraint**: `MergeApproval_Status_check` → `CHECK ((("Status")::text = ANY (ARRAY['PENDING','APPROVED','REJECTED','REVOKED','EXECUTED']::text[])))` — exact 5-value enum, matches `merge_approval.py`'s `ALL_STATUSES` character for character.
- **Fingerprint uniqueness/identity behavior**: `uq_merge_approval_identity`, a `UNIQUE` constraint (and its auto-generated backing index) on `("SurvivorPaperID","LoserPaperID","PlanFingerprint","ApprovalVersion")` — confirmed present with the exact four-column composite key the design requires.
- **Other FKs**: `ReviewedByUserID`/`RevokedByUserID` → `Users.UserID` `ON DELETE SET NULL` (both); `TenantID` → `Tenant.TenantID` `ON DELETE NO ACTION`; `ExecutionAuditLogID` → `AuditLog.LogID` `ON DELETE SET NULL` — all four confirmed exactly as designed.

### Indexes (4 total)

`MergeApproval_pkey` (auto, from `PRIMARY KEY`), `uq_merge_approval_identity` (auto, from the `UNIQUE` constraint), `idx_merge_approval_status_created` (explicit), `idx_merge_approval_lookup` (explicit) — **all four present**, matching the migration's two explicit `CREATE INDEX IF NOT EXISTS` statements plus the two implicit ones Postgres always creates for a `PRIMARY KEY`/`UNIQUE` constraint.

### Data

```
SELECT COUNT(*) FROM "MergeApproval"  →  0
```

**Zero rows. No approval record of any kind exists.** No creation, approval, rejection, revocation, or execution occurred.

### Existing-data safety (targeted, not broad, per your instruction)

| Check | Before (Phase 4M baseline) | After this phase | Unchanged? |
|---|---|---|---|
| `ResearchPaper` row count | 2,031 | **2,031** | Yes |
| Canary pair `5232` (DOI/JournalID/PubYear/TenantID/Title) | `10.1155/2022/8531213`, `1803`, `2022`, `1`, "Optimal deep learning model..." | **identical, byte-for-byte** | Yes |
| Canary pair `5482` (DOI/JournalID/PubYear/TenantID/Title) | `NULL`, `NULL`, `2022`, `1`, "Research Article Optimal Deep Learning Model..." | **identical, byte-for-byte** | Yes |
| `Authors` rows for the canary pair | 1 row each, `UserID=97`, same raw name string, both sides | **identical, byte-for-byte** | Yes |
| `AuditLog` row count | 956 | **956** | Yes — `apply_migration.py` contains no `AuditLog` reference of any kind (confirmed by direct source read); this migration is pure DDL and writes to no other table |
| `paper.merge.dedup` `AuditLog` rows referencing the canary pair | 0 | **0** | Yes — confirms no merge occurred, before or during this phase |

**Every existing-data check is unchanged. The migration touched exactly one thing: it created one new, empty table with its indexes and constraints. Nothing else in the database was affected.**

---

## Task D — Application Code Compatibility

1. **Does importing/using the code write anything immediately?** No — confirmed by direct test: `merge_approval.fetch_current_approval()` (the real, unmodified production function) was called against the real, newly-created production table with a query that structurally cannot match any row (`plan_fingerprint='nonexistent-fingerprint-compat-check'`). It executed a single `SELECT`, returned `None` correctly, and the table's row count was re-confirmed still `0` immediately after.
2. **Does the actual schema match what `merge_approval.py`/`merge_executor.py`/`merge_execution_safety.py` expect?** **Yes, proven directly, not merely inferred** — `merge_approval.APPROVAL_COLUMNS` (the exact 17-column list every one of that module's SQL statements is built from) was used, unmodified, in a real query against the real table, and it succeeded without a `psycopg2.errors.UndefinedColumn` or any other schema-mismatch error. Had there been any column-name or type mismatch, this call would have failed immediately — it did not.
3. **Test suites re-run**: `test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 43/43, `test_merge_execution_safety.py` 68/68, `test_merge_approval.py` 45/45, `test_merge_executor.py` 39/39, `test_fk_lifecycle.py` 11/11 — **224/224 passing, zero regressions.**
4. **No real approval row was created merely to test compatibility** — the one live call made (`fetch_current_approval`) is a pure, read-only lookup by construction; it cannot create a row under any circumstance, and none was created (confirmed, table still at 0 rows).

**No mismatch was found. Nothing to report or stop for.**

---

## Task E — Recovery Check (Documented, Not Executed)

- **Snapshot/pre-change recovery context supplied for this phase**: per your message, Neon PITR is confirmed available with a 6-hour history window, and a manual Neon snapshot was created immediately before this phase began, to be treated as the pre-change recovery point.
- **Did the migration succeed?** Yes — confirmed on the first attempt, no retry, no failure.
- **Was rollback needed?** No.
- **Is the database now in the expected state?** Yes — confirmed exhaustively in Task C: the new table matches the audited design in every column, constraint, and index; it is empty; and every pre-existing table/row this phase checked (including the canary pair specifically) is byte-for-byte unchanged.

**The manual Neon snapshot and the 6-hour PITR window must both remain untouched until this phase's work has been independently reviewed by you.** This phase did not restore, modify, or interact with either recovery mechanism in any way — they exist purely as an unused safety net behind a change that itself required no recovery.

---

## Task F — Final Decision

### **A) MIGRATION SUCCESSFULLY APPLIED AND VERIFIED — READY FOR A SEPARATE APPROVAL-WORKFLOW AUDIT**

Every pre-write gate passed on fresh, live evidence. The application succeeded on the first attempt via the repository's real, unmodified mechanism. Post-application verification found the new schema to be an exact, complete match to the audited design — including the one detail that mattered most, `LoserPaperID`'s deliberate absence of a foreign key. Every existing table and the canary pair specifically were independently confirmed unchanged. The real, unmodified `merge_approval.py` code was proven — not assumed — to work correctly against the real, live table. All 224 tests remain green.

**Per your explicit instruction, even having reached verdict A:**
- No pending approval was created.
- Nothing was approved, rejected, or revoked.
- No merge was executed.
- **Phase 4P is not started.**

---

## Exact Accounting

- **Code files modified**: **0.**
- **Code files created**: **0.**
- **Migration files modified**: **0** — `sprint11_merge_approval.sql` was applied exactly as audited, not edited.
- **Report files created**: **2** — this file and `backend/reports/phase4o_migration_application.json`.
- **DB writes**: **1 DDL transaction** — the migration's `CREATE TABLE` + 2× `CREATE INDEX`, applied atomically as a single unit via `apply_migration.py`'s `transaction.atomic()`. No other write of any kind (no `INSERT`/`UPDATE`/`DELETE` anywhere, confirmed by the existing-data safety checks in Task C).
- **DB schema changes**: **1** — the addition of the `MergeApproval` table, its 2 explicit indexes (plus the 2 implicit ones Postgres always creates for its `PRIMARY KEY`/`UNIQUE` constraint), and its constraints, exactly as designed.
- **`MergeApproval` rows created**: **0.**
- **`ResearchPaper` rows changed**: **0** — row count and canary-pair contents independently confirmed unchanged.
- **Records merged**: **0.**
- **DOI changes**: **0.**
- **`--apply` executions**: **0.**
- **Network calls**: **0** beyond the one production-database connection this entire phase used throughout (no external HTTP/API call of any kind).
- **Tests run and results**: `test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 43/43, `test_merge_execution_safety.py` 68/68, `test_merge_approval.py` 45/45, `test_merge_executor.py` 39/39, `test_fk_lifecycle.py` 11/11 — **224/224 total, zero regressions.**

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Identical to every phase since 4E — zero changes this phase. (Applying the migration changes the live database, not the local `sprint11_merge_approval.sql` file itself — `git` correctly shows no diff for it, since its *content* was never edited, only *executed*.)

### `git status --short` (relevant paths)

```
 M backend/tools/dedup_papers.py                 <- pre-existing, unchanged this phase
 M backend/tools/test_dedup_papers.py             <- pre-existing, unchanged this phase
?? backend/analytics/migrations/sprint11_merge_approval.sql   <- pre-existing (Phase 4K.1), unchanged this phase; NOW APPLIED to production
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

**This phase's only repository changes are the two new report files.** The one real, consequential change this phase made exists in the **database**, not the **repository** — `MergeApproval` now exists in production, verified exhaustively above. Several other untracked files elsewhere in the repository predate this entire duplicate-merge project and are unrelated to it.

Per your instructions, I am stopping here. Phase 4P is not started. No approval was created. No merge was executed. No DOI was changed.
