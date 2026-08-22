# Phase 4M — Controlled Migration Application Readiness Audit (STRICTLY READ-ONLY)

## 1. Scope and Safety Accounting

Zero production DB writes. Zero DDL of any kind executed against production. Zero `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`/`ALTER`/`CREATE`/`DROP` against the live database. Zero migration execution. Zero `--apply`. Zero `MergeApproval` rows created anywhere. Zero merges executed. Zero network calls beyond the local production-database connection itself (every interaction a plain read-only `SELECT`/catalog query, each script ending in `conn.rollback()` and `conn.close()`). Zero code files modified or created this phase — the audit found no defect requiring one (confirmed in every task below).

## 2. Live Schema Evidence (Task A)

All of the following was queried fresh this phase, directly against production — not reused from any prior phase's report.

### 2.1 Connection and privilege context

```
current_user = neondb_owner   current_database = neondb   search_path = "$user", public
has_schema_privilege(neondb_owner, 'public', 'CREATE')   = True
has_database_privilege(neondb_owner, 'neondb', 'CREATE') = True
rolsuper=False  rolcreatedb=True  rolcreaterole=True
```

### 2.2 `MergeApproval` — definitive existence check

```
to_regclass('public."MergeApproval"')  → NULL   (does not exist)
information_schema.tables ILIKE '%mergeapproval%'  → []
pg_indexes    ILIKE '%merge_approval%'  → []
pg_constraint ILIKE '%merge_approval%'  → []
pg_class      ILIKE '%mergeapproval%'   → []
information_schema.sequences ILIKE '%mergeapproval%' → []
```

**The table does not exist, and zero partial artifacts exist anywhere** (no orphaned index, sequence, or constraint bearing any name the migration would create). There is nothing to classify as drift for `MergeApproval` itself — this is the expected clean state, confirmed rather than assumed.

### 2.3 `ResearchPaper`, `AuditLog`, `AuthorReviewQueue`, `ReportPaperDecision`, `Tenant`, `Users` — full column/constraint dump

| Table | PK | Relevant FKs (this phase's live read) | Relevant CHECK/UNIQUE |
|---|---|---|---|
| `ResearchPaper` | `PaperID` (integer, `SERIAL`) | `JournalID→Journals.JournalID` (NO ACTION); `TenantID→Tenant.TenantID` (NO ACTION) | `chk_researchpaper_pubyear_range`; `researchpaper_title_unique` |
| `AuditLog` | `LogID` (integer) | `TenantID→Tenant.TenantID` (NO ACTION); `UserID→Users.UserID` (SET NULL) | none beyond NOT NULL on `Action` — **`TargetID` carries no FK, re-confirmed a fourth time across Phases 4K/4K.1/4L/4M** |
| `AuthorReviewQueue` | `ReviewID` (integer) | `PaperID→ResearchPaper.PaperID` (**CASCADE**); `ReviewedByUserID→Users.UserID` (SET NULL); `SuggestedUserID→Users.UserID` (SET NULL) | `AuthorReviewQueue_Status_check`; `uq_queue_paper_scraped_name` |
| `ReportPaperDecision` | `DecisionID` (integer) | `PaperID→ResearchPaper.PaperID` (SET NULL); `MissingResolvedToPaperID→ResearchPaper.PaperID` (SET NULL); `SubmissionID→ReportSubmission.SubmissionID` (CASCADE) | `chk_decision_shape`; `chk_decision_value`; `uq_decision_submission_paper` |
| `Tenant` | `TenantID` (integer) | — | `Tenant_Slug_key`, `Tenant_Subdomain_key`, `Tenant_CustomDomain_key` |
| `Users` | `UserID` (integer) | `RoleID→Role.RoleID` (NO ACTION); `TenantID→Tenant.TenantID` (NO ACTION) | `Users_Email_key`, `Users_Litrix_ID_key`, `Users_ORCID_key`, `Users_Scholar_ID_key` |

**Every column type the migration references (`INT`/`integer`) matches the live column it points at exactly** — `ResearchPaper.PaperID`, `Tenant.TenantID`, `Users.UserID`, `AuditLog.LogID` are all `integer`. No type mismatch anywhere.

### 2.4 Dependency/child tables relevant to merge execution

| Table | Exists | FK | ON DELETE |
|---|---|---|---|
| `Authors` | Yes | `PaperID→ResearchPaper.PaperID` | NO ACTION |
| `Citations` | Yes | `PaperID→ResearchPaper.PaperID` | NO ACTION |
| `ExternalAuthors` | Yes | `PaperID→ResearchPaper.PaperID` | NO ACTION |
| `CitationsHistory` | Yes | `PaperID→ResearchPaper.PaperID` | NO ACTION |
| `PaperKeywords` | Yes | `PaperID→ResearchPaper.PaperID` | NO ACTION |

All five match what Phase 4K/4L already established — no further drift beyond the already-known, already-classified `PaperKeywords` existence (§9).

### 2.5 Permission tables (`has_litrix_perm()`'s dependencies)

`Role`, `Permission`, `RolePermission` all confirmed to exist live this phase.

## 3. Migration Static Audit (Task B)

Read `sprint11_merge_approval.sql` fresh this phase (lines 96–135, the actual DDL). Checked against §2's live evidence, item by item:

| # | Check | Result |
|---|---|---|
| 1 | Syntactically coherent with live schema | Yes — standard, double-quoted PascalCase identifiers, matching every other migration in this repository |
| 2 | Every referenced table/column exists | Yes — `ResearchPaper.PaperID`, `Tenant.TenantID`, `Users.UserID`, `AuditLog.LogID`, all confirmed live (§2.3) |
| 3 | Every referenced data type compatible | Yes — `INT` (alias for `integer`) matches every referenced column exactly; `VARCHAR(64)` for `PlanFingerprint` is exactly sized for a 64-hex-character SHA-256 digest (the canary's real, live fingerprint is 64 characters, confirmed by direct count) |
| 4 | `SurvivorPaperID` integrity behavior intentional and valid | Yes — `INT NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION`, unchanged since Phase 4H, re-confirmed correct and untouched by Phase 4K.1/4L |
| 5 | `LoserPaperID` intentionally has no FK, documented as historical identifier | Yes — confirmed present, the migration's own header comment (rewritten in Phase 4K.1, re-read fresh this phase) explains this exactly, matching your Phase 4M context statement word for word |
| 6 | `LoserPaperID` nullability matches intended historical design | Yes — `INT NOT NULL`, and genuinely never set to anything after row creation by any code path (confirmed by source read of `merge_approval.py`/`merge_executor.py` — no function writes to this column after `INSERT`) |
| 7 | Self-merge protection enforceable | Yes — `CHECK ("SurvivorPaperID" != "LoserPaperID")` is ordinary, unexceptional Postgres `CHECK` syntax; will be enforced by Postgres itself at every `INSERT`, independent of and beneath the application-level `reject_self_merge()` guard |
| 8 | `Status` CHECK/state model valid | Yes — `CHECK ("Status" IN ('PENDING','APPROVED','REJECTED','REVOKED','EXECUTED'))` matches `merge_approval.py`'s `ALL_STATUSES`/`STATUS_*` constants exactly, character for character |
| 9 | `PlanFingerprint` uniqueness semantics correct | Yes — `UNIQUE ("SurvivorPaperID","LoserPaperID","PlanFingerprint","ApprovalVersion")` matches `create_pending_approval()`'s version-bumping logic exactly (already tested, 224 passing tests exercise this) |
| 10 | Reviewer fields compatible with actual user/auth schema | Yes — `ReviewedByUserID`/`RevokedByUserID` (`INT REFERENCES "Users"("UserID") ON DELETE SET NULL`) exactly mirrors `AuthorReviewQueue.ReviewedByUserID`'s own live, real, already-shipped `SET NULL` behavior (§2.3) |
| 11 | Indexes creatable without name conflicts | Yes — `idx_merge_approval_status_created`, `idx_merge_approval_lookup` both confirmed absent from `pg_indexes` live this phase |
| 12 | Constraint names don't collide | Yes — `chk_merge_approval_not_self`, `uq_merge_approval_identity` both confirmed absent from `pg_constraint` live this phase; Postgres's own auto-generated names (`MergeApproval_pkey`, `MergeApproval_SurvivorPaperID_fkey`, etc.) are likewise free, since no object with that prefix exists |
| 13 | Migration doesn't depend on missing application code changes | Yes — the migration is pure, self-contained DDL; `merge_approval.py`/`merge_executor.py` already exist and were written against this exact column shape (224 tests already exercise it via mocks) — nothing in the application layer needs to change for this DDL to apply cleanly |

**Result: the migration is ready exactly as written. No static defect was found. No change to the migration file was made or is recommended.**

## 4. Live Data Compatibility (Task C)

- **Object name collisions**: none, for the table, both indexes, both explicit constraints, and every implicit auto-named constraint Postgres would generate (§3, item 11/12) — verified via direct `pg_indexes`/`pg_constraint`/`pg_class` queries, not inferred.
- **FK target type/data mismatch**: none — every referenced column's live type matches the migration's assumption exactly (§2.3/§3 item 3).
- **`search_path`/namespace issue**: none — `search_path = "$user", public`; the migration's unqualified `"MergeApproval"`/`"ResearchPaper"`/etc. references resolve unambiguously to `public`, the schema every other table in this project already lives in.
- **Partially-created `MergeApproval` artifact**: none (§2.2) — a genuinely clean slate.
- **Roles/permissions preventing application**: none found — `neondb_owner` (the connection identity this repository's own `litrix_db.db()` always uses) has `CREATE` on both the `public` schema and the `neondb` database, confirmed directly via `has_schema_privilege`/`has_database_privilege`, not inferred from role name. **This was established read-only, not by attempting the migration** — per the task's explicit instruction, privilege verification here is a definitive `has_*_privilege()` catalog check, not a guess.
- **Application database user privileges to create the table/indexes/constraints**: confirmed sufficient (above) — `neondb_owner` is not a Postgres superuser, but superuser is not required for `CREATE TABLE`/`CREATE INDEX`/`ADD CONSTRAINT` in a schema you have `CREATE` privilege on, which this role does.
- **Row-count / lock-contention context** (not a blocker, offered as operational context for whoever runs the actual application phase): `ResearchPaper` has 2,031 rows, `Users` 107, `Tenant` 1, `AuditLog` 956 — all small. A `CREATE TABLE ... REFERENCES` briefly takes a `SHARE ROW EXCLUSIVE`-class lock on each referenced table while validating the new FK constraints; at this scale, that validation is effectively instantaneous and poses no realistic contention risk even under concurrent application traffic.

## 5. Migration Framework / Idempotency Analysis (Task D)

Re-read `backend/apply_migration.py` fresh this phase (the actual runner for `backend/analytics/migrations/*.sql` files — confirmed by path, `sprint11_merge_approval.sql` lives here, not under `backend/migrations/`, so `run_migration.py` is not the relevant mechanism for this specific file):

```python
with transaction.atomic():
    with connection.cursor() as cur:
        cur.execute(sql)
    if args.dry_run:
        transaction.set_rollback(True)
```

1. **Is this migration expected to run inside a transaction?** Yes, explicitly — `transaction.atomic()` wraps the entire file's SQL (the `CREATE TABLE` plus both `CREATE INDEX` statements, sent as one string to `cur.execute()`) as a single atomic unit.
2. **Does the migration runner track applied migrations?** No — re-confirmed by reading the file: no ledger, no "migrations applied" table, no record written anywhere. This matches every prior phase's finding for this repository, unchanged.
3. **How are failed migrations represented?** An exception from `cur.execute(sql)` propagates out of `transaction.atomic()`, which triggers Django's automatic rollback, then continues propagating with **no surrounding `try`/`except` in `apply_migration.py` itself** — the operator would see a raw Python traceback, not a friendly message. Worth noting for whoever runs the real application (the failure *mode* is safe — nothing commits — but the failure *reporting* is unpolished).
4. **Could a partial schema state survive an interruption?** No — `transaction.atomic()` guarantees the whole statement set commits together or not at all; a process kill mid-run leaves an uncommitted transaction that Postgres itself rolls back automatically when the connection drops. This is standard, well-established Postgres behavior, not something this phase needed to test live to trust.
5. **If rerun after failure, what exact behavior occurs?** Identical to a first run — nothing would have persisted (§4 above), so a rerun starts from the same clean state.
6. **Does the migration need explicit `IF NOT EXISTS`, or is atomic tracking the standard?** No ledger exists (item 2) — `IF NOT EXISTS` is therefore this repository's real, load-bearing idempotency mechanism, not a redundant addition. The migration already uses it correctly and consistently (`CREATE TABLE IF NOT EXISTS "MergeApproval"`, both `CREATE INDEX IF NOT EXISTS` statements) — matching the exact same pattern `sprint8_author_review_queue.sql` (the repository's own closest precedent) already uses. No change needed.
7. **Would `IF NOT EXISTS` weaken drift detection?** In the abstract, yes — if `MergeApproval` already existed with a *different* shape than intended, `CREATE TABLE IF NOT EXISTS` would silently no-op rather than error. In practice, this phase's own live check (§2.2) already independently proves that scenario doesn't currently apply — the table doesn't exist at all, in any form. This is the same accepted tradeoff every other migration in this repository already lives with; not something to change here.

## 6. Deployment Order (Task E)

Traced via actual `grep` against real imports and Django wiring — not inferred from filenames:

```
grep -rln "import merge_approval\b|from merge_approval\b|import merge_executor\b|from merge_executor\b|..." backend/ --include="*.py"
→ backend/tools/merge_approval.py, merge_execution_safety.py, merge_executor.py,
  and their own four test files. Nothing else.

grep for "MergeApproval"/"merge_approval"/"merge_executor" in:
  backend/litrix_backend/ (settings/urls/wsgi)
  backend/analytics/urls.py, backend/accounts/urls.py
  backend/analytics/apps.py, backend/accounts/apps.py
  backend/analytics/models.py, backend/accounts/models.py
→ zero matches, all of them.
```

**A.** Can the migration safely be applied before the executor is exposed? **Yes** — nothing "exposes" the executor at all today (no endpoint, no management command, no CLI entry point calls `execute_approved_merge()` against a real connection); applying the migration adds a table that nothing in the running application currently queries.

**B.** Can the application code be deployed before the migration? **Yes — this is literally the current, live state of production right now.** `merge_approval.py`/`merge_executor.py` have existed in this repository since Phase 4I/4J; the migration has never been applied; the application has been running the entire time with zero issue, which is itself the strongest possible evidence for this answer.

**C.** Does any currently-imported module immediately query `MergeApproval` and therefore fail if the table is absent? **No** — confirmed by the grep above: zero references anywhere Django actually loads (`models.py`, `urls.py`, `apps.py`, `settings.py`). Django starts up correctly today with the table absent, which is the live, continuous proof.

**D.** Is a feature flag or deployment gate required before exposing execution functionality? **Not yet, and not by this migration.** There is currently nothing to gate — no code path reaches `execute_approved_merge()` from outside its own module and tests. A gate becomes a real question only once a future phase builds an actual endpoint or CLI entry point; that is out of this phase's scope and not blocked by anything found here.

**E.** Could normal application startup break after applying the migration? **No evidence suggests it would.** The migration adds a new, standalone table; it does not alter any existing table's schema, does not touch any Django-managed migration Django's own `manage.py migrate` processes (per this repository's own two-write-path architecture — domain schema is entirely outside Django's migration system), and does not change any FK target's shape. The lock-contention note (§4) is the only operational nuance worth carrying into the actual application phase, and it is negligible at this data scale.

## 7. Rollback / Recovery Matrix (Task F — Designed, Not Executed)

| State | Rollback safety | Rollback action | Reasoning |
|---|---|---|---|
| 1. Migration not yet applied | **N/A — nothing to roll back** | none | This is the current, confirmed state (§2.2) |
| 2. Migration applied, no approval rows exist | **Safe** | `DROP TABLE "MergeApproval"` (which itself drops both indexes and both explicit constraints automatically) | Zero rows means zero data of any kind is destroyed; a clean, fully reversible undo |
| 3. Migration applied, `PENDING`/`APPROVED` rows exist | **Conditionally safe** | Recommend archiving the table's contents (e.g. a `pg_dump` of just this table, or a plain export query) before `DROP TABLE`, though not strictly required for *data-integrity* safety | Safe from a `ResearchPaper` integrity perspective — by definition, no merge has executed for a `PENDING`/`APPROVED` row, so no `ResearchPaper`/`Authors`/child-table data was ever touched. **Not** safe from a work-loss perspective: a human reviewer's in-progress decision would be silently discarded, requiring them to re-review from scratch |
| 4. Migration applied, `EXECUTED` historical rows exist | **Unsafe without archival/review — stated explicitly, not softened** | Archive the table's full contents first, in a form that preserves `LoserPaperID`, `PlanFingerprint`, `ReviewedByUserID`/`ReviewedAt`, and `ExecutionAuditLogID` per row, before considering any drop | Dropping the table at this point destroys the *approval-specific* context of every completed merge (who approved it, under what exact fingerprint, when) permanently. The bare *fact* that a merge happened would still be recoverable from `AuditLog` alone (`Action='paper.merge.dedup'`, `TargetID`=loser, `Metadata.kept_paper_id`=survivor — unconstrained, would survive regardless), since `AuditLog` remains this project's independently-established sole source of truth for "did the merge happen" (Phase 4G/4H). But the *approval* record — the human-decision layer this entire multi-phase project exists to build — would not be |

**Production is currently in State 1.** This matrix exists so that whoever runs the actual, future migration-application phase knows exactly what "rollback" means at every subsequent state before deploying anything — per your explicit instruction, this phase designed it without executing any part of it.

## 8. Canary Revalidation (Task G)

Performed strictly read-only, this phase:

| Check | Result |
|---|---|
| Both 5232/5482 exist | Yes |
| Duplicate classification still valid | Yes — `choose_keep`→survivor=5232/loser=5482 (unreversed), `pair_confidence=high`, `hard_exclusion_reason=None` |
| Fingerprint vs. established reference | `2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb` — **MATCH**, **10th independent live confirmation** |
| Prior successful merge exists? | No — `idempotency_verdict()=NOT_PREVIOUSLY_EXECUTED`, 0 `AuditLog` rows referencing either PaperID |
| Approval row exists? | Impossible — `MergeApproval` table confirmed absent (§2.2), so by construction zero approval rows can exist anywhere |
| Drift relevant to the planned migration/executor | None found beyond the already-known, already-classified `PaperKeywords` existence (§9) — carried forward, not new |

## 9. Full Regression Results (Task H)

```
test_dedup_papers.py             18/18 passing
test_merge_plan_generator.py     43/43 passing
test_merge_execution_safety.py   68/68 passing
test_merge_approval.py           45/45 passing
test_merge_executor.py           39/39 passing
test_fk_lifecycle.py             11/11 passing
```

**Total: 224/224 passing.** Identical to Phase 4L's 224/224 — **the total is unchanged because no code change was needed this phase**: Tasks A–G found zero defects requiring an implementation fix, zero test gaps requiring a new test, and the migration file itself required no edit. This is the expected, correct outcome for a purely read-only audit that confirms an already-sound design, not a sign anything was skipped.

## 10. Carried-Forward Findings

Re-classified per your instruction — investigated only as far as needed to confirm none blocks migration application, not expanded or fixed:

| Finding | Classification | Does it block migration application? |
|---|---|---|
| `PaperKeywords` schema drift (exists; three earlier phase reports said it didn't) | Non-blocking known issue | No — it has no relationship to `MergeApproval`'s schema, FKs, or the migration file at all; it is a pre-existing table unaffected by anything in this migration |
| `AuditLog.UserID` hardcoded `NULL` in `merge_group()` | Non-blocking known issue for a single supervised canary; future hardening for general rollout | No — `merge_group()` is not part of this migration, and the migration's own `ExecutionAuditLogID` FK works correctly regardless of what `UserID` value a given `AuditLog` row carries |
| `has_litrix_perm()` lacks tenant scoping | Future hardening work; blocker for multi-tenant rollout, not for the single canary (`TenantID=1` on both sides, re-confirmed) | No — permission enforcement is an application-layer concern entirely separate from whether the DDL itself can be safely applied; the migration creates a table, it does not grant or check any permission |

## 11. Exact Blockers

**None found.** Every task (A through H) reached a definite, evidence-backed "no blocker" conclusion:

- Live schema: clean, fully compatible, zero drift relevant to this migration.
- Static migration audit: ready exactly as written, all 13 checklist items pass.
- Data compatibility: zero name collisions, correct privileges confirmed via direct catalog check (not inferred), negligible lock-contention risk at current scale.
- Idempotency/framework: transaction-wrapped, `IF NOT EXISTS` used correctly and consistently with repository convention, no ledger dependency to worry about.
- Deployment order: code-before-migration is not just theoretically safe, it is the literal, continuously-proven current state of production.
- Rollback: fully designed and understood at every future state, before anything is deployed.
- Canary: 10th consecutive clean confirmation, zero drift.
- Regression: 224/224, unchanged, zero weakened tests.

## 12. Final Decision

### **A) SAFE TO BEGIN A SEPARATE, EXPLICITLY AUTHORIZED MIGRATION-APPLICATION PHASE**

Every question this phase was asked to answer resolved to a definite, evidence-backed "no blocker" — not by absence of investigation, but by direct, fresh, live catalog inspection at every step (§2–§9). The migration is syntactically and semantically ready exactly as currently written; no further schema edit is recommended or needed. This decision is scoped precisely to *readiness for a migration-application phase* — it does not authorize applying the migration itself, executing any merge, or creating any approval row. Those remain exactly what your task text framed them as: the *next*, separate, explicitly-authorized phase's job.

## 13. Exact Next Recommended Phase

**A dedicated, narrowly-scoped Migration Application phase** whose entire job is: apply `sprint11_merge_approval.sql` to production via `apply_migration.py` (optionally `--dry-run` first, exercising Django's real `transaction.atomic()`/rollback path against the real database rather than this phase's read-only queries), verify the table now exists with the exact shape audited here, and then **stop** — explicitly not creating any approval row, not wiring an endpoint, and not executing any merge in that same phase. This keeps the same one-controlled-step-at-a-time discipline this entire project has followed since Phase 4A.

Per your instructions, I am stopping here. Phase 4N is not started. The migration was not applied. No `MergeApproval` row exists anywhere. No merge was executed.

---

## Safety Accounting (Exact)

- **Code files modified**: **0.**
- **Code files created**: **0.**
- **Migration files modified**: **0** — `sprint11_merge_approval.sql` was read and audited this phase, not edited.
- **Test files modified**: **0.**
- **Report files created**: **1** — this file (`backend/reports/phase4m_migration_application_readiness_audit.md`).
- **Production DB writes**: **0.**
- **Test DB writes**: **0** — Task H's regression runs entirely against `ExecutorFakeCursor`/`InMemoryApprovalCursor`/pure-function tests; no real DB connection is opened by any automated test.
- **Production DDL executed**: **0.**
- **Network calls**: **0** beyond the local production-database connection itself (read-only catalog/`SELECT` queries via `litrix_db.db()`, each script explicitly `conn.rollback()`-ed and closed).
- **Records merged**: **0.**
- **Approval rows created**: **0** — impossible in any case, since the table itself does not exist (§2.2).
- **DOI changes**: **0.**
- **`--apply` executions**: **0.**
- **Production migrations applied**: **0.**
- **Exact test total**: **224/224 passing** (unchanged from Phase 4L — no code change was needed).

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Byte-for-byte identical to every phase since 4E — **zero changes this phase.**

### `git status --short` (relevant paths)

```
 M backend/tools/dedup_papers.py                 <- pre-existing, unchanged this phase
 M backend/tools/test_dedup_papers.py             <- pre-existing, unchanged this phase
?? backend/analytics/migrations/sprint11_merge_approval.sql   <- pre-existing (Phase 4K.1), unchanged this phase
?? backend/reports/                                <- this phase adds 1 file to it
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

**This phase (4M) changed exactly one thing in the repository: it added this one report file.** Every other line in both `git status`/`git diff` output above reflects work from Phase 4E through Phase 4L, carried forward untouched. Several other untracked files elsewhere in the repository (`ai_eval.py`, `backfill_abstracts.py`, `classify_publication_type.py`, `discover_csv_identifiers.py`, `discover_missing_identifiers.py`, `merge_identifiers.py`, `summarize_staging.py`, `sync_all_researchers.py`, and files outside `backend/tools/`/`backend/analytics/migrations/`) predate this entire duplicate-merge project and are unrelated to it.
