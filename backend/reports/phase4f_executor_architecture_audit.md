# Phase 4F — Merge Executor Architecture & Transaction Safety Design (Read-Only)

## Safety Confirmation

Strictly read-only. No production code was modified. No executor was created. `merge_group()` was never called. `dedup_papers.py --apply` was never run. Zero INSERT/UPDATE/DELETE/TRUNCATE/ALTER/CREATE/DROP statements were issued — every DB query this phase was a plain `SELECT` against `information_schema`, `ResearchPaper`, `Authors`, `AuditLog`, or the other dependency tables. Zero network calls. Two files were created, both under `backend/reports/`: this file and `phase4f_executor_readiness.json`.

## Task A — Existing `merge_group()` Behavior (re-read this phase, unchanged since Phase 4E)

Confirmed via `git diff` that `dedup_papers.py` has had **zero deletions** since Phase 4E's additive-only change — `merge_group()` is byte-for-byte the function first documented in Phase 4B.

**Survivor-selection behavior:** `merge_group()` itself does not select a survivor — it receives `keep`/`losers` already decided by the caller's `choose_keep()` (has_doi → citations → title_length → is_verified → lowest PaperID, unchanged).

**Exact operation sequence, in order, for each loser in the group:**
1. `SELECT DISTINCT "UserID" FROM "Authors" WHERE "PaperID" = ANY([keep] + losers)` — snapshot the expected author set, once, before any write.
2. Per loser: `INSERT INTO "Authors" (...) SELECT ... FROM "Authors" WHERE "PaperID" = %s ON CONFLICT ("UserID","PaperID") DO NOTHING` then `DELETE FROM "Authors" WHERE "PaperID" = %s`.
3. If `"Citations" in child_tables` and the loser has a row: `SELECT "CitationsCount"` then `INSERT INTO "Citations" ... ON CONFLICT ("PaperID") DO UPDATE SET "CitationsCount" = GREATEST(...), "LastUpdate" = NOW()`, then `DELETE FROM "Citations" WHERE "PaperID" = %s`.
4. For each table in `SIMPLE_CHILDREN` present in `child_tables`: `remap_simple_child()` — a bulk `UPDATE "{table}" SET "PaperID" = keep WHERE "PaperID" = loser` inside a `SAVEPOINT`, with a per-row `ctid`-based fallback (drop the conflicting row) if the bulk `UPDATE` hits a unique-constraint violation.
5. `merge_citation_fields()` — reads both rows' `CitationsByYear`/`RawData_Log`, computes an element-wise `MAX` per year plus `GREATEST()` of the totals, `UPDATE`s the kept row's `CitationsByYear`/`RawData_Log`.
6. `INSERT INTO "AuditLog" (...) VALUES (1, NULL, 'paper.merge.dedup', 'ResearchPaper', loser, <metadata jsonb>, NULL, 'script:tools/dedup_papers.py')` — **before** the delete.
7. `DELETE FROM "ResearchPaper" WHERE "PaperID" = loser`.

After the loop over all losers: `SELECT DISTINCT "UserID" FROM "Authors" WHERE "PaperID" = keep`, compare against the step-1 snapshot, and `raise RuntimeError(...)` if any `UserID` is missing — the profile-preservation assertion.

**Duplicate/conflict handling:** exactly two mechanisms exist, both silent-on-success: `ON CONFLICT ... DO NOTHING` (Authors) and `ON CONFLICT ... DO UPDATE ... GREATEST(...)` (Citations). Neither logs, flags, or reports what it discarded. `remap_simple_child()`'s `SAVEPOINT` fallback silently `DELETE`s the conflicting child row rather than merging it.

**What happens to the loser `ResearchPaper` row:** unconditionally `DELETE`d, after every dependency table has been processed for it, after its `AuditLog` entry is written. No soft-delete, no tombstone, no retirement flag — the schema has no such column (re-confirmed this phase, §Task B).

**Audit trail:** yes — `AuditLog` (`Action='paper.merge.dedup'`), one row per merged loser, written before the delete, inside the same transaction. **Proven populated**: this phase found **59 real `paper.merge.dedup` rows already in the live database** from prior, real `--apply` runs — this is not a hypothetical mechanism, it has operated in production.

**Atomicity:** the entire `--apply` invocation (every group in the run, not per-group) is wrapped in exactly one `with transaction.atomic():` block in `main()`, itself further wrapped by a full pre-write JSON snapshot (`snapshot_paper()` for every member of every group) written to `data/dedup_audit/snapshot_<ts>.json` **before** any group is processed.

**Rollback on failure:** yes, via Django's `transaction.atomic()` — any unhandled exception (including the profile-preservation assertion's `RuntimeError`) rolls back **every** group processed in that `--apply` call, not just the failing one, since it is one transaction for the whole run. No `SAVEPOINT`-per-group exists at the `main()` level (only the finer-grained `SAVEPOINT`s inside `remap_simple_child()`).

**Not present anywhere in this code path** (confirmed by re-reading, not assumed): no `SELECT ... FOR UPDATE` / row locking of any kind; no plan-fingerprint or staleness check beyond "does the PaperID still exist"; no re-check of `JournalID` (the column is never referenced); no `AuthorNameRaw` content comparison (only the identity-key-level `ON CONFLICT`).

## Task B — Full Dependency Map (re-verified against live schema this phase)

Re-derived independently via `information_schema.table_constraints` + `key_column_usage` + `constraint_column_usage` + `referential_constraints`, filtered to `ccu.table_name='ResearchPaper'` — **not** assumed from `dedup_papers.py`'s own `SIMPLE_CHILDREN` list, and **not** copied from any prior phase's report. This is the third independent re-derivation across this project (Phase 4B, Phase 4D, and now Phase 4F) and all three produced the **identical 8-row result**, which is itself strong evidence of completeness (**PROVEN FACT**, not inferred).

| Table.FK column | ON DELETE | Nullable | Classification | Currently handled? |
|---|---|---|---|---|
| `Authors.PaperID` | NO ACTION | **NO** | `MUST_REMAP_BEFORE_DELETE` | Yes — special-cased |
| `Citations.PaperID` | NO ACTION | NO | `MUST_REMAP_BEFORE_DELETE` | Yes — special-cased |
| `ExternalAuthors.PaperID` | NO ACTION | YES | `MUST_REMAP_BEFORE_DELETE` | Yes — `SIMPLE_CHILDREN` |
| `PaperKeywords.PaperID` | NO ACTION | NO | `MUST_REMAP_BEFORE_DELETE` | Yes — `SIMPLE_CHILDREN` (table doesn't exist today, dead entry) |
| `CitationsHistory.PaperID` | NO ACTION | YES | `MUST_REMAP_BEFORE_DELETE` | Yes — `SIMPLE_CHILDREN` |
| `ReportPaperDecision.PaperID` | **SET NULL** | YES | `SET_NULL` | Yes, pre-empted — `SIMPLE_CHILDREN` remaps it before the SET NULL rule would ever fire |
| `ReportPaperDecision.MissingResolvedToPaperID` | **SET NULL** | YES | `SET_NULL` | **No** — not remapped by anything; 0 rows populated DB-wide today (re-verified), zero practical impact, real code gap |
| `AuthorReviewQueue.PaperID` | **CASCADE** | NO | `AUTOMATIC_CASCADE` | **No** — not remapped, not snapshotted; 0 rows DB-wide today (re-verified), highest-severity unaddressed gap the moment this table is populated |

Nullability was re-checked fresh this phase (not reused from a prior phase's cached value) via a direct `information_schema.columns` query per FK column — see `phase4f_executor_readiness.json::dependency_map` for the exact per-row evidence.

**Correction carried forward and re-confirmed:** `ReportPaperDecision.PaperID`'s `ON DELETE` rule is `SET NULL`, not `NO ACTION` — first corrected in Phase 4C, now confirmed a third time with an independent query.

## Task C — Transaction Boundary Design (proposal only)

The full 12-step sequence, with per-step transaction placement and rollback/invariant reasoning, is in `phase4f_executor_readiness.json::transaction_steps`. Summary of the key design decision: **two checks (`execution_permitted == True`, human approval where required) belong *outside* and *before* any transaction is opened** — they are properties of the stored plan object, not live DB state, so failing them should never cost a lock or a `SAVEPOINT`. **Every other step belongs inside one transaction**, opened only after those two cheap checks pass, beginning with `SELECT ... FOR UPDATE` on both rows. This narrows the current `--apply` design (one transaction for the *entire run*, covering every group) to one transaction *per pair* for a future executor — deliberately, so that Phase 4D/4E's evidence (real, individual pairs can carry real, blocking-worthy conflicts) is respected at the same granularity a human approval would be granted at, without needing to touch or risk `--apply`'s own existing, already-tested, coarser-grained transaction behavior for its own separate call path.

## Task D — Stale Plan Protection

**Schema evidence, checked fresh this phase:** `ResearchPaper` has **zero** columns matching `%update%` or `%modif%` across all 33 columns (`information_schema.columns` scan). There is no `UpdatedAt`/`ModifiedAt`/`LastModified` column to build a timestamp-based staleness check on.

**Recommendation:** a **content fingerprint** — a deterministic hash over exactly the field values (`ResearchPaper` columns, `Authors` rows, dependency row-counts) the plan's `field_actions`/`journal_state`/`author_content_conflicts` were computed from — stored on the plan and re-verified inside the executor's transaction, immediately after the row lock. This is recommended *because* it directly encodes the actual safety property needed ("is this the same data the plan reasoned about") rather than a proxy for it, and requires no schema migration.

**This is a recommendation, not an existing capability.** `merge_plan_generator.py`'s plans carry no fingerprint field today — building this (canonical serialization, hash function, plan-schema field, executor-side re-check) is **100% new work for a future phase**, explicitly labeled as such rather than implied to already exist.

Row-level locking (`SELECT ... FOR UPDATE`) is a separate, complementary mechanism (prevents a *new* concurrent change during the transaction; does not detect a change that already happened *before* the transaction opened) — **and this one already has direct repository precedent**: the exact raw-cursor `SELECT ... FOR UPDATE` pattern is used today in `backend/analytics/reconciliation_views.py` (confirmed by reading the actual code this phase) and 6 other files (grep-confirmed), inside `transaction.atomic()` blocks, with `connection.cursor()` — the same style `dedup_papers.py` itself already uses. Adopting it for a merge executor introduces no new architectural concept to this codebase.

## Task E — Idempotency & Double-Execution Safety

**Existing audit/execution table found:** `AuditLog`, already used for exactly this purpose (`Action='paper.merge.dedup'`), **populated with 59 real rows** from genuine prior `--apply` runs (re-verified this phase, not assumed). No unique constraint prevents a duplicate row for the same `TargetID`, so it is a usable **evidence source** for an idempotency preflight check, not a **database-enforced guarantee** — the distinction matters and is stated explicitly per the task's instruction not to invent what isn't there.

**No dedicated "execution/job" table for merges exists.** Stated explicitly rather than invented, per the task's instruction. (The repository does have an unrelated, general job-tracking table, `SyncJob`, confirmed to exist this phase for a different subsystem — noted only as precedent that such a pattern is not foreign to this codebase, not proposed for reuse here.)

**The primary, already-working idempotency mechanism** is simpler than a new table: `--apply --plan`'s existing "does this PaperID still exist in `ResearchPaper`" filter already makes a second run of the same plan a safe no-op for any already-merged group — this is real, existing code (`main()`'s `alive` check), not a proposal. Its one gap: it proves the row is *gone*, not that *this specific plan* is what removed it. Combining it with an `AuditLog` cross-check (query for an existing `TargetID`+`Action` row before proceeding) closes that gap using only tables that already exist.

**Reversal prevention:** the plan JSON already names `survivor`/`loser` by key, not position — a future executor's design principle should be to **execute** the plan's recorded decision, never **re-derive** it via a fresh `choose_keep()` call at execution time (which could disagree with the approved plan if data changed — exactly what the fingerprint check in Task D exists to catch).

**Partial child-migration repeats:** `merge_group()`'s individual operations (`ON CONFLICT DO NOTHING`, `GREATEST()`-based `UPDATE`, `remap_simple_child()`'s by-`PaperID` `UPDATE`) are each already idempotent in isolation — re-running the same call against an already-partially-migrated state would not duplicate data. The real risk is a **second, concurrent, unlocked invocation**, which is precisely what Task C's row-locking design prevents; it is not a gap in `merge_group()`'s own per-statement logic.

## Task F — 5232/5482 Canary Analysis (live, read-only re-verification this phase)

Fresh queries this phase (not reused from Phase 4E's output) confirm:

- **Current DB state matches Phase 4E exactly**: 5232 (`DOI=10.1155/2022/8531213`, `JournalID=1803`, `PubYear=2022`), 5482 (`DOI=NULL`, `JournalID=NULL`, same title minus the "Research Article" prefix) — no drift.
- **Re-running the plan generator fresh for just this pair** reproduces the identical result: `classification=SAFE_PLAN_CANDIDATE`, `requires_human_approval=false`, `journal_state.state=WINNER_ONLY` (non-blocking), `author_content_conflicts=[]`. **The Phase 4E plan is proven still current**, not merely assumed to be.
- **Every dependency table** re-queried fresh: `Authors` = 1/1 (shared `UserID=97`, `AuthorNameRaw` **byte-identical** on both sides, re-confirmed), every other table = 0/0.
- **Zero prior merge attempts**: no `AuditLog` row with `Action='paper.merge.dedup'` and `TargetID` in {5232, 5482} exists.

**Preconditions a future executor would need before this pair could be safely executed** (none exist today): (1) a human-approval step that flips this specific plan's `execution_permitted` to `true` — no such mechanism exists anywhere in this repository; (2) the fingerprinting mechanism from Task D; (3) row locking wired into `dedup_papers.py`'s write path (the pattern exists elsewhere in the repo, not here); (4) the `AuditLog`+existence idempotency preflight from Task E as actual code; (5) the executor itself, which does not exist. **This pair is not executed. Nothing was written.**

## Task G — Minimum Safe Executor Contract (proposal only)

Full contract is in `phase4f_executor_readiness.json::proposed_executor_contract`. Design principle stated up front: **reuse, don't rewrite.** Every dependency already classified `MUST_REMAP_BEFORE_DELETE` keeps using `merge_group()`'s existing, unmodified, already-tested logic; the only genuinely new write operations are the two things Phase 4E's plan schema newly represents but `merge_group()` still doesn't act on — `JournalID` backfill when `journal_state.state == LOSER_ONLY_BACKFILL`, and a defensive re-check-and-refuse if `author_content_conflicts` is ever non-empty (belt-and-suspenders on top of `execution_permitted` already being `false` for such a plan). No Celery, service layer, or queue is proposed — nothing in this repository's evidence (a single-process, synchronous, `psycopg2`/Django-cursor pipeline throughout) suggests one is needed for per-pair, human-approved, low-volume merges; introducing one would be scope creep the task explicitly forbids.

## Blocking Unknowns

See `phase4f_executor_readiness.json::blocking_unknowns` for the full list (5 items). Summary: fingerprinting, row-locking-in-`dedup_papers.py`, the `AuditLog` idempotency preflight, and an approval-recording mechanism **all need to be built** before any executor could safely run — none of them are blocked by unresolved *evidence* gaps (every piece of schema/behavior evidence needed to design them was gathered this phase), they are simply **unbuilt**. The one genuine evidentiary unknown is how `AuthorReviewQueue` (CASCADE) and `ReportPaperDecision.MissingResolvedToPaperID` (SET NULL, unremapped) would actually behave under a populated scenario — both are 0 rows DB-wide today, so this has never been exercised in practice; not unsafe by evidence, just untested.

## Final Decision

**B) DESIGN HAS BLOCKING UNKNOWNS — more read-only investigation required**

Not because any single piece of evidence is missing — Tasks A, B, D, E, and F all reached firm, evidence-backed conclusions, and the dependency map has now been independently re-derived three times with identical results. The reason for **B**, not **A**, is that this phase's own findings (Task D, Task E, `blocking_unknowns`) prove that **four concrete, load-bearing pieces of the executor do not exist anywhere in the repository yet**: plan fingerprinting, row locking wired into the actual merge write path, an idempotency preflight built from the existing `AuditLog`/existence data, and any mechanism at all for recording a human approval that flips `execution_permitted` to `true`. "Design ready" would mean these are specified precisely enough to build without further investigation — Task C and Task G specify *what* they should do, but Task D explicitly labels the fingerprint mechanism as requiring new implementation with a "what would need building" list, not a "here is exactly how" specification ready to hand to an implementer. The honest position, per the task's own instruction not to claim more certainty than the evidence supports, is that a future phase should resolve those four items — likely as narrow, individually-testable read-only-or-additive design/prototype passes, the same pattern every phase since 4C has used — before an implementation phase begins. **C is not correct**: nothing found this phase suggests the existing architecture (transaction-per-run, `AuditLog`, `SIMPLE_CHILDREN` remap, the profile-preservation assertion) is *unsafe* for a *narrow*, per-pair, human-approved executor — it is simply *incomplete* for one, and every gap found has a clear, repository-native, non-invasive path to closing it.

## Exact Accounting

- Code files modified: **0**
- Code files created: **0**
- Executor code created: **0** (none — explicitly forbidden and not done)
- Report files created: **2** (`backend/reports/phase4f_executor_architecture_audit.md`, `backend/reports/phase4f_executor_readiness.json`)
- DB writes: **0**
- Network calls: **0**
- Records merged: **0**
- DOI changes: **0**
- `--apply` executions: **0**
- Tests run: **0** (no code was written or changed this phase, so no test suite was affected or needed to be re-run; the last known-good state — `test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 43/43 — is unchanged since Phase 4E and was not re-verified here since nothing could have altered it)

Per your instructions, I am stopping here. Phase 4G is not started, and no executor has been implemented.
