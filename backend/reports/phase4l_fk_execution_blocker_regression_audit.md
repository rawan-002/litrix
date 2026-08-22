# Phase 4L — MergeApproval FK Execution Blocker Fix & Regression Audit

## Scope Correction (Read First)

This phase's own task text was written against a stale premise: it describes the "current proposed migration" as still carrying `LoserPaperID → ResearchPaper ON DELETE NO ACTION`, and cites `AuditLog.TargetID` as a precedent for fixing it via `SET NULL`. **Task A's own re-verification (below) proved both of those statements no longer match the repository**: Phase 4K.1 (the immediately preceding phase, same session) already removed `LoserPaperID`'s foreign key entirely, and `AuditLog.TargetID` has never used `SET NULL` — it has no FK constraint of any kind, live-confirmed three times now (Phase 4K, Phase 4K.1, and again in this phase). This discrepancy was surfaced to you directly before any implementation work began; you confirmed: **keep Phase 4K.1's no-FK design, do not switch to `SET NULL`, audit the existing design end-to-end instead.** Everything below reflects that instruction. No schema reversal occurred.

## Safety Confirmation

Zero production DB writes. Zero production schema changes. Zero migrations applied. Zero `MergeApproval` rows created anywhere, production or otherwise. Zero merges executed. Zero calls to `merge_group()` or `execute_approved_merge()` against production. Zero `ResearchPaper` data changed. Zero DOI changes. Zero `--apply` executions. Zero network calls beyond the local production-database connection itself (standard `litrix_db.db()` path, every interaction a read-only `SELECT` ending in `conn.rollback()`).

---

## Task A — Re-Verification of the Exact FK Failure Path (and of Its Current Fix)

Traced fresh, this phase, from the actual code and migration file — not reused from Phase 4K's report:

1. **An `APPROVED` `MergeApproval` row is required before `execute_approved_merge()` can proceed.** `merge_executor.py`: `approval = fetch_current_approval(...)`; `if approval is None: return ExecutionResult(False, EXEC_BLOCKED_NO_APPROVAL)`; `if approval.status != ma.STATUS_APPROVED: return ExecutionResult(False, EXEC_BLOCKED_APPROVAL_NOT_APPROVED, ...)`. No code path reaches a write with anything less than a genuinely `APPROVED` row.
2. **The approval row references the loser through `LoserPaperID`.** `merge_approval.py::create_pending_approval()`'s `INSERT INTO "MergeApproval" ("SurvivorPaperID","LoserPaperID", ...)` — confirmed, `LoserPaperID` is a real, populated column on every row.
3. **`merge_group()` ultimately deletes the loser `ResearchPaper` row.** `dedup_papers.py::merge_group()`, unmodified: `cur.execute('DELETE FROM "ResearchPaper" WHERE "PaperID" = %s', (loser,))` — the final statement in its per-loser loop, after the `AuditLog` insert.
4. **Re-read `sprint11_merge_approval.sql` directly, this phase** (not from memory): line 99 currently reads

   ```sql
   "LoserPaperID"         INT             NOT NULL,
   ```

   **No `REFERENCES` clause. No `ON DELETE` clause. No FK of any kind.** `SurvivorPaperID` (line 98) is unchanged: `INT NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION`. This is the Phase 4K.1 state, not the pre-4K.1 state this phase's own task text assumed.
5. **Would the original `ON DELETE NO ACTION` constraint have blocked the DELETE?** Yes — re-confirmed by directly re-tracing PostgreSQL's documented, deterministic rule for an undeferred `ON DELETE NO ACTION` constraint: a `DELETE` against a referenced row fails immediately if a referencing row exists and is not itself being deleted/updated in the same statement. An `APPROVED` (later `EXECUTED`) `MergeApproval` row referencing the loser via `LoserPaperID` is exactly such a referencing row. This was the real, provable Phase 4K finding — re-derived independently here, not merely cited.
6. **Would the whole transaction have rolled back rather than partially committing?** Yes — `merge_group()` does not catch the exception; `execute_approved_merge()` does not catch it either; nothing in either function swallows a broad exception anywhere (confirmed by source read this phase). It propagates to whatever transaction context the caller opens, which — per Django's/Postgres's standard transaction semantics — rolls back every statement issued since the transaction began, not just the failing one.

**Conclusion of Task A**: the original failure mechanism was real (re-proven, not merely re-cited), **and it is not present in the schema as it currently exists**. `sprint11_merge_approval.sql` already reflects the fix. No further schema edit was made this phase.

---

## Task B — Independent Verification of the No-FK Design (Not a Redesign)

Per your instruction, this task treats the existing design as a *candidate to audit*, not an assumed success.

- **Does `LoserPaperID` genuinely have no FK today?** Confirmed directly by re-reading the migration file this phase (§A.4) — yes.
- **Does `SurvivorPaperID` remain protected?** Confirmed unchanged: `ON DELETE NO ACTION`, still present, line 98. **Newly tested this phase** (Phase 4K.1 never exercised this path, since no code ever deletes a survivor): `SurvivorPaperIdIntegrityProtected::test_deleting_a_referenced_survivor_is_blocked` proves a simulated `DELETE` against a referenced survivor is still rejected.
- **Is there any remaining FK/CHECK/NOT NULL interaction that could block execution?**
  - `chk_merge_approval_not_self CHECK ("SurvivorPaperID" != "LoserPaperID")` — both columns are always populated integers (neither is nullable); this constraint is unaffected by the FK removal and cannot itself block a legitimate merge (it only ever rejects a genuine self-merge attempt, which `reject_self_merge()` already refuses before any SQL is issued anyway — defense-in-depth, not a new risk).
  - `uq_merge_approval_identity UNIQUE ("SurvivorPaperID", "LoserPaperID", "PlanFingerprint", "ApprovalVersion")` — unaffected; `LoserPaperID`'s value is still always the real `PaperID`, so uniqueness scoping is unchanged.
  - `LoserPaperID INT NOT NULL` — since Option D (not `SET NULL`) was chosen, this column is **never** set to anything after creation. `NOT NULL` therefore can never be violated by any code path in this project. No conflict exists, and none was introduced.
- **Result: no remaining FK/CHECK/NOT NULL interaction blocks execution.** The design, as it currently exists, is internally coherent.

---

## Task C — Execution Flow Consistency Audit (Full 14-Step Trace)

Traced against the actual, current code — not assumed — with every FK identified at each stage:

| # | Step | Code | FK(s) touched | Status under current schema |
|---|---|---|---|---|
| 1 | Valid pair selected | Caller supplies `(survivor_id, loser_id)` | none | n/a |
| 2 | Pending approval exists | `create_pending_approval()` | `MergeApproval.SurvivorPaperID`/`LoserPaperID` — **insert-time validation of `LoserPaperID` against `ResearchPaper` no longer occurs** (Phase 4K.1's accepted tradeoff, §4 of that report); `execute_approved_merge()`'s own live preflight (step 5 below) is what actually matters for safety | Sound — insert succeeds regardless; safety is enforced later, live |
| 3 | Human approval occurs | `approve_pending()` | none new | Unchanged from Phase 4I |
| 4 | Approval locked and verified | `execute_approved_merge()`: `fetch_current_approval()` → `approval_matches_pair()` → `Status == APPROVED` check | none | Unchanged |
| 5 | Pair rows locked deterministically | `lock_pair_rows()`, `SELECT ... FOR UPDATE` | `Authors`/`Citations`/etc. are NOT touched here — only `ResearchPaper` row locks | Unchanged |
| 6 | Fingerprint + all safety checks | `validate_against_plan()`, `idempotency_verdict()` | none | Unchanged |
| 7 | Child records remapped | `merge_group()` → `Authors`/`Citations`/`SIMPLE_CHILDREN` | `Authors.PaperID`, `Citations.PaperID`, etc. — **all unaffected by the `MergeApproval` fix**, since none of them reference `MergeApproval` | Unchanged |
| 8 | `JournalID` handling | `merge_executor.py`'s own `UPDATE "ResearchPaper" SET "JournalID" = %s` (only if `LOSER_ONLY_BACKFILL`) | none | Unchanged |
| 9 | `AuthorNameRaw` conflicts blocked where required | `author_content_conflicts()` re-check | none | Unchanged |
| 10 | `AuditLog` written | `merge_group()`'s `INSERT INTO "AuditLog"` | `AuditLog.TenantID`/`UserID` FKs (unrelated) | Unchanged |
| 11 | **Loser `ResearchPaper` deleted** | `merge_group()`'s `DELETE FROM "ResearchPaper" WHERE "PaperID" = %s` | **This is the exact step §A found would have failed under the old schema.** Under the current schema: `MergeApproval.LoserPaperID` has no FK — **nothing blocks this DELETE.** `MergeApproval.SurvivorPaperID`'s FK is irrelevant here (the survivor is not being deleted). | **Fixed — proven by `CorrectedDesignSucceeds::test_loser_actually_deleted`** |
| 12 | `MergeApproval` → `EXECUTED` | `merge_executor.py`'s own `UPDATE "MergeApproval" SET "Status" = 'EXECUTED', ...` | none — this UPDATE targets `MergeApproval` itself, not a column any other table's FK references | Unchanged, now reachable |
| 13 | **Historical approval/audit data valid after loser deletion** | `MergeApproval` row: `LoserPaperID` still holds `5482` (the real value, never touched); `AuditLog` row: `TargetID` still holds `5482` (never had an FK to begin with) | none — **by design**, neither column depends on the now-deleted `ResearchPaper` row continuing to exist | **Proven — `CorrectedDesignSucceeds::test_approval_history_survives_the_loser_deletion` + the new `test_historical_approval_readable_through_the_real_lookup_function`** |
| 14 | Commit succeeds atomically | Caller's transaction context (not this module — unchanged finding from Phase 4K/4K.1: no real caller exists yet anywhere in the repository) | none | Design sound; deployment-readiness gap, not a new one (§Task G) |

**No new transaction-order problem was exposed.** The only step that changed behavior between the old and new schema is step 11 (the delete itself), exactly where §Task A predicted, and nowhere else — every other step's FK profile is identical before and after the Phase 4K.1 fix.

---

## Task D — Test Coverage (Renumbered per Your Corrected Instructions)

| # | Requirement | Test | New or existing |
|---|---|---|---|
| 1 | `LoserPaperID` retains original numeric value after loser deleted | `CorrectedDesignSucceeds::test_approval_history_survives_the_loser_deletion` | Existing (Phase 4K.1) |
| 2 | Historical approval record remains readable after execution | `CorrectedDesignSucceeds::test_historical_approval_readable_through_the_real_lookup_function` — through `fetch_current_approval()` itself, not raw dict access | **New this phase** |
| 3 | Historical lookup/audit code does not join `LoserPaperID` to a live `ResearchPaper` row unless guarded | `NoImplicitResearchPaperJoinInApprovalLookup::test_no_sql_join_against_researchpaper_in_merge_approval_module` — static proof, zero SQL `JOIN`s exist in `merge_approval.py` | **New this phase** |
| 4 | Pre-execution validation requires both `SurvivorPaperID` and `LoserPaperID` to refer to currently live rows | Loser-missing: Phase 4J's existing `test_I_missing_row_after_lock_zero_destructive_writes`. Survivor-missing: `OperationalVsHistoricalStateDistinction::test_survivor_missing_before_execution_blocks_with_zero_writes` | Existing (loser) + **New this phase** (survivor) |
| 5 | An `APPROVED` approval cannot execute if either live paper disappears before execution | Same two tests as #4 | Existing + New |
| 6 | An `EXECUTED` historical approval remains valid even though `LoserPaperID` no longer references a live row | Same as #1/#2 | Existing + New |
| 7 | `SurvivorPaperID` integrity remains protected | `SurvivorPaperIdIntegrityProtected::test_deleting_a_referenced_survivor_is_blocked` | **New this phase** |
| 8 | No orphaned `PENDING`/`APPROVED` approval can silently bypass execution safety | `OperationalVsHistoricalStateDistinction::test_orphaned_approval_cannot_silently_bypass_execution_safety` | **New this phase** |
| 9 | Rollback before commit leaves `ResearchPaper` and approval state unchanged | `OriginalDesignReproducesThePhase4KFailure::test_failure_leaves_no_partial_corruption` (the one scenario where a failure is actually expected — the loser row and approval status are both directly asserted unchanged; the honest, stated limitation of what this in-memory mock can and cannot prove about *other* rows' content is unchanged from Phase 4K.1, restated in that test's own comment) | Existing (Phase 4K.1) |
| 10 | Existing approval and executor tests remain green | Full regression, Task E | This phase |

**5 new tests added this phase**, all in `backend/tools/test_fk_lifecycle.py` (now 11 tests total in that file, up from 6), plus one supporting, unconditional addition to `ExecutorFakeCursor` in `test_merge_executor.py` (the survivor-side FK simulation, gated by nothing — it reflects the schema's real, unchanged state, so it applies to every scenario, and was confirmed to affect zero existing tests since no existing test ever deletes a survivor row).

**No `SET NULL`-specific test was added**, per your explicit instruction — none of the new tests assume, require, or check for a `NULL` `LoserPaperID` anywhere; every one either confirms the real value is preserved (operational-state tests) or confirms it stays preserved after execution (historical-state tests).

---

## Task E — Full Regression

Every suite established through Phase 4J/4K/4K.1, run this phase, exact counts:

```
test_dedup_papers.py             18/18 passing
test_merge_plan_generator.py     43/43 passing
test_merge_execution_safety.py   68/68 passing
test_merge_approval.py           45/45 passing
test_merge_executor.py           39/39 passing
test_fk_lifecycle.py             11/11 passing  (6 pre-existing + 5 new this phase)
```

**Total: 224/224 tests passing.** Before this phase: 219/219. **Zero failures, zero regressions, zero weakened assertions.**

---

## Task F — Live Read-Only Revalidation

Performed strictly read-only, this phase, against production:

| Check | Result |
|---|---|
| `MergeApproval` exists in production? | **`false`** — still not applied |
| Both 5232/5482 exist? | Yes |
| Fingerprint vs. reference | `2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb` — **MATCH**, **9th independent live confirmation** |
| Prior successful merge for the canary pair? | None — `idempotency_verdict() = NOT_PREVIOUSLY_EXECUTED`, 0 `AuditLog` rows referencing either PaperID |
| Any DB write this phase? | **0** — every query a plain `SELECT`, connection explicitly `.rollback()`-ed and closed |
| Any production migration applied this phase? | **0** |

### Carried-forward findings, re-classified per your instructions (not expanded, not fixed)

| Finding | Classification |
|---|---|
| `PaperKeywords` schema drift (now exists; Phase 4C/4F/4H said it didn't) | **Non-blocking known issue** — 0 rows table-wide, already correctly handled by existing `SIMPLE_CHILDREN` logic (Phase 4K, unchanged this phase). Does not touch `MergeApproval` or this fix in any way. |
| `AuditLog.UserID` hardcoded `NULL` in `merge_group()` | **Non-blocking known issue for a single supervised canary; future hardening work for general rollout.** Unrelated to the FK fix — `merge_group()` was not touched this phase, confirmed by `git diff --stat` (§Accounting). |
| `has_litrix_perm()` lacks tenant scoping | **Future hardening work** — not a blocker for the canary specifically (both rows share `TenantID=1`, re-confirmed live this phase indirectly via the unchanged canary identity check), **is** a blocker for multi-tenant rollout. Unrelated to the FK fix — `accounts/models.py` was not touched this phase. |

None of the three directly prevents the Phase 4L FK fix from being correct — confirmed by the fact that none of them appears anywhere in the FK-fix code path (`sprint11_merge_approval.sql`, `merge_approval.py`, `merge_executor.py`'s `LoserPaperID`-related logic) traced in Tasks A–C.

---

## Task G — Final Execution-Readiness Verdict

This decision is **not** based solely on "the FK blocker was removed." The full audit trail behind it:

- **The FK blocker itself**: proven fixed, both analytically (§Task A/B) and by simulation (§Task D, 11 tests in `test_fk_lifecycle.py`, all passing, including the previously-unexercised survivor-protection and orphan-safety paths).
- **Historical approvals**: proven to remain valid, readable through the real lookup function, and free of any implicit join risk (§Task D items 2/3/6).
- **Operational-state safety**: proven that `PENDING`/`APPROVED` approvals still strictly require both papers live, in both directions (survivor-missing was a genuine, previously-untested gap — now closed), and that an orphaned approval (a scenario the no-FK design newly makes *possible*, though not *dangerous*) cannot silently bypass execution.
- **Rollback discipline**: unchanged from Phase 4J/4K.1, re-confirmed passing this phase.
- **Uniqueness/idempotency**: `uq_merge_approval_identity` and `idempotency_verdict()` are both unaffected by the FK change (§Task B) — re-confirmed live this phase (§Task F).
- **What remains genuinely open, none of it new**: no real caller wraps `execute_approved_merge()` in a transaction anywhere in the repository (Phase 4K's finding, still unchanged — confirmed again this phase, `grep -rln "execute_approved_merge"` still returns only the module and its own tests); the migration has never been validated against a real PostgreSQL instance (Phase 4K.1's own stated limitation, still true — no local Postgres became available this phase either); the two non-blocking findings (§Task F) remain open for future hardening.

### **A) READY FOR A SEPARATE MIGRATION-APPLICATION AUDIT**

Not because the FK fix alone earns it, but because every execution-flow assumption this phase was asked to re-examine — historical-record validity, operational-state enforcement, orphan safety, rollback atomicity, uniqueness, join-safety — was independently re-verified and found sound, and the one previously-open architectural question (the FK lifecycle itself) now has both a correct design and passing tests proving it. What remains before any real execution is deliberately **out of this phase's scope**, and matches your own explicit framing: a *separate*, *controlled migration-application* audit (validating the corrected migration against a real database, and only then considering an actual `--apply`) — not live execution, and not something this phase should begin on its own initiative.

Per your instructions, I am stopping here. Phase 4M is not started. The migration was not applied. No `MergeApproval` row exists anywhere. No merge was executed.

---

## Safety Accounting

**This phase (4L):**

- Code files modified: **1** — `backend/tools/test_merge_executor.py` (added the unconditional `SurvivorPaperID` FK-protection simulation to `ExecutorFakeCursor`; zero behavioral change to any of its 39 existing tests, re-confirmed passing).
- Code files created: **1** — none beyond the test file below (no non-test implementation code was created or needed — Task A/B found the existing design already correct).
- Test files modified: **2** — `backend/tools/test_merge_executor.py` (above); `backend/tools/test_fk_lifecycle.py` (5 new tests + module docstring extension; 6 pre-existing tests untouched).
- Migration files modified: **0** — `sprint11_merge_approval.sql` was read and re-verified this phase but not edited; it already reflects the Phase 4K.1 fix.
- Report files created: **1** — this file (`backend/reports/phase4l_fk_execution_blocker_regression_audit.md`).
- Production DB writes: **0.**
- Test DB writes: **0** — every test in every suite runs against `ExecutorFakeCursor`/`InMemoryApprovalCursor`/pure-function tests; no `psycopg2`/Django DB connection is opened by any automated test.
- Production network calls: **0** beyond the local production-database connection itself (read-only `SELECT`s, `litrix_db.db()`, rolled back and closed).
- Records merged: **0.**
- DOI changes: **0.**
- `--apply` executions: **0.**
- Production migrations applied: **0.**
- Exact test totals: **224/224 passing** (219 before this phase + 5 new this phase, 0 regressions).

**Pre-existing, unrelated to this phase (carried forward, not touched):**

```
git diff --stat backend/tools/dedup_papers.py backend/tools/test_dedup_papers.py
 backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
 backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
 2 files changed, 178 insertions(+), 1 deletion(-)
```

Byte-for-byte identical to every phase since 4E — zero changes this phase to either tracked file.

```
git status --short backend/tools/ backend/analytics/migrations/ backend/reports/
 M backend/tools/dedup_papers.py                 <- pre-existing, unchanged this phase
 M backend/tools/test_dedup_papers.py             <- pre-existing, unchanged this phase
?? backend/analytics/migrations/sprint11_merge_approval.sql   <- pre-existing (Phase 4K.1), unchanged this phase
?? backend/reports/                                <- this phase adds 1 file to it
?? backend/tools/merge_approval.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_execution_safety.py         <- pre-existing, unchanged this phase
?? backend/tools/merge_executor.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_plan_generator.py           <- pre-existing, unchanged this phase
?? backend/tools/test_fk_lifecycle.py              <- MODIFIED this phase (+5 tests)
?? backend/tools/test_merge_approval.py            <- pre-existing, unchanged this phase
?? backend/tools/test_merge_execution_safety.py    <- pre-existing, unchanged this phase
?? backend/tools/test_merge_executor.py            <- MODIFIED this phase (survivor-FK simulation)
?? backend/tools/test_merge_plan_generator.py      <- pre-existing, unchanged this phase
```

Several other untracked files elsewhere in the repository (`ai_eval.py`, `backfill_abstracts.py`, `classify_publication_type.py`, `discover_csv_identifiers.py`, `discover_missing_identifiers.py`, `merge_identifiers.py`, `summarize_staging.py`, `sync_all_researchers.py`, and files outside `backend/tools/`/`backend/analytics/migrations/`) predate this entire duplicate-merge project and were not touched by, or relevant to, Phase 4L.

**Explicit statement, as required**: no production migration was applied during Phase 4L, and no merge — real or simulated-against-production — was executed. `MergeApproval` does not exist in the live database.
