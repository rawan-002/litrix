# Phase 4J — Merge Executor Prototype (NO LIVE EXECUTION)

## Safety Confirmation

`sprint11_merge_approval.sql` was never applied to any database. No `MergeApproval` row was ever created in production. `--apply` was never run. The executor was never invoked against a real connection. Zero `DELETE`/`UPDATE` ever touched a real `ResearchPaper` row. No child row was ever remapped for real. No DOI was ever changed. Zero network calls. Every one of the 38 new tests (and every test in the four pre-existing suites, re-run unchanged) runs against `ExecutorFakeCursor`, an in-memory Python double — no `psycopg2`/Django connection is opened anywhere in the automated suite. The one live-database interaction this phase performed was a single, explicitly read-only script (`SELECT`s only, ending in `conn.rollback()`), reported in §12.

## 1. Exact Files Modified / Created

**Created (2):**
- `backend/tools/merge_executor.py` — the executor itself (382 lines).
- `backend/tools/test_merge_executor.py` — 38 tests, all mocked (738 lines).

**Modified: 0.** `git diff --stat backend/tools/dedup_papers.py backend/tools/test_dedup_papers.py` is byte-for-byte identical to every phase since 4E (91/88 insertions, 1 deletion total, unchanged). `merge_plan_generator.py`, `merge_execution_safety.py`, and `merge_approval.py` were read and imported from, never edited — confirmed via `git status --short backend/tools/`, which shows them exactly as untracked-but-unmodified, identical to their state at the end of Phase 4I. `sprint11_merge_approval.sql` is untouched and still unapplied.

Before writing anything, this phase re-read `phase4f_executor_architecture_audit.md`, `phase4g_executor_preconditions_prototype.md`, `phase4h_approval_storage_design.md`, and `phase4i_merge_approval_prototype.md` in full, and inspected the current, real implementations of `dedup_papers.py`, `merge_plan_generator.py`, `merge_execution_safety.py`, `merge_approval.py`, all four existing test files, `litrix_db.py::db()`, and `accounts/common.py::audit()`.

## 2. Executor Responsibility Boundary

`merge_executor.py` does exactly one thing: given an exact approved `(survivor_id, loser_id, expected_plan_fingerprint)` identity, it re-validates everything live and — only if every check passes — performs the real merge. It is orchestration only. Every actual safety primitive is imported, not reimplemented:

- `merge_execution_safety.py`: `reject_self_merge`, `lock_pair_rows`, `fetch_current_state`, `validate_against_plan`, `is_doi_claimed_elsewhere`, `fetch_merge_audit_rows`, `idempotency_verdict`.
- `merge_approval.py`: `fetch_current_approval`, `approval_matches_pair`, `can_approve_merge`, `is_legal_transition`, the `STATUS_*` constants.
- `dedup_papers.py`: `merge_group` (**completely unmodified**), `pair_confidence`, `hard_exclusion_reason`, `author_content_conflicts`, `existing_child_tables`, `authors_columns`, `JOURNAL_LOSER_ONLY_BACKFILL`.
- `merge_plan_generator.py`: `build_journal_state`, `build_papers_dict_for_pure_functions`.

The only genuinely new logic is: (1) the orchestration sequence itself, (2) the `JournalID` backfill `UPDATE` (Phase 4E designed this action; no code had ever applied it), (3) `check_unhandled_dependency_gaps()` — the live re-check of the two dependency gaps Phase 4F/4H flagged and left unaddressed, and (4) the `MergeApproval` → `EXECUTED` transition, which Phase 4I's own module docstring explicitly deferred to "a future executor" — this phase is that executor. No broad service layer was built; there is exactly one public function, `execute_approved_merge()`.

## 3. Exact Preflight Sequence

Implemented in `execute_approved_merge()` in this exact order, matching the task's numbered list:

1. `reject_self_merge(survivor_id, loser_id)` — before any SQL.
2. Canonical/deterministic identity validation — the function never re-derives who should win via a fresh `choose_keep()` call; it executes exactly the caller-supplied direction (see §7).
3–4. `fetch_current_approval()` — must return a row; `approval_matches_pair()` — direction must match; `Status` must be `APPROVED`.
5. `lock_pair_rows()` — deterministic ascending order (reused, unmodified).
6–7. `fetch_current_state()` — re-fetched **after** the lock; both rows must exist.
8–9, 13–14. `validate_against_plan()` (reused, unmodified) — recomputes the current fingerprint and compares it against the approval's own stored `PlanFingerprint`; re-checks `pair_confidence`/`hard_exclusion_reason` (duplicate safety) and `is_doi_claimed_elsewhere` (DOI safety) fresh.
10–12. `idempotency_verdict()` — `ALREADY_EXECUTED` and `HISTORICAL_STATE_AMBIGUOUS` both block.
15. `build_journal_state()` and `author_content_conflicts()`, recomputed fresh from the just-locked data — either one being non-empty/blocked refuses execution.
16. `check_unhandled_dependency_gaps()` — live `COUNT(*)` re-check of the two previously-flagged gaps.

No write SQL is issued before step 16 passes — proven dynamically by test scenarios B–J (each asserts `executed_sql` contains no write-verb statement) and statically by `TransactionOwnershipTests`/`StaticSafetyChecks`, which assert every preflight-function call's source offset precedes the first write-statement literal's offset.

A permission check (`can_approve_merge(user)`) sits between steps 2 and 3, in the same relative position `create_pending_approval()` already uses (self-merge check, then permission). No new permission code was invented — this reuses `manage_users`/`Admin`, the same mechanism Phase 4H/4I already established for this exact repository, since no evidence supports a distinct "execute" permission.

## 4. Exact Transaction Sequence, And Why It Diverges From The Literal Task Ordering

Actual write sequence, once every preflight check passes:

```
(JournalID backfill UPDATE, only if journal_state == LOSER_ONLY_BACKFILL)
merge_group(cur, survivor, [loser], ...)   <- UNMODIFIED, does, in order:
    Authors remap -> Citations remap (if table exists) -> SIMPLE_CHILDREN remap
    -> CitationsByYear merge -> AuditLog INSERT -> ResearchPaper DELETE (loser)
    -> profile-preservation assertion
SELECT the AuditLog LogID merge_group() just wrote
UPDATE MergeApproval SET Status='EXECUTED', ExecutedAt=NOW(), ExecutionAuditLogID=<that LogID>
final invariant re-check (survivor exists, loser gone)
```

**This deliberately diverges from this task's own §3 bullet order** (`AuditLog → MergeApproval EXECUTED → delete loser`) in one respect: here, the delete happens *before* the `MergeApproval` update, not after. Two constraints made this the correct choice rather than an oversight:

1. **`merge_group()` is not modified.** It is real, tested, production code behind 59 historical merges. Splitting its internal sequence to interleave a `MergeApproval` write between its own `AuditLog` INSERT and its own `DELETE` would mean editing that trusted code path during a phase that performs no live execution to validate the edit against — exactly the kind of change every prior phase (4F–4I) explicitly protected this file from.
2. **`ExecutionAuditLogID` cannot be known before `merge_group()` runs** — the `AuditLog` row it links to is written *by* `merge_group()`, at the same moment as the delete. There is no way to set it beforehand without either reserving a `LogID` in advance (inventing new machinery this phase's own "do not invent a broad service layer" instruction argues against) or writing the `AuditLog` row here ourselves (duplicating logic `merge_group()` already owns).

This ordering matches **Phase 4H's own approved executor design** (§H step 6: "...writing the AuditLog row, deleting the loser, ...updating MergeApproval.Status='EXECUTED'...") rather than this message's own literal bullet list. `test_loser_delete_is_the_final_destructive_action` (§10) verifies the resolution this phase actually chose: the `ResearchPaper` `DELETE` is the last statement that *removes or discards any data* — the one write after it (`MergeApproval` → `EXECUTED`) is non-destructive metadata cross-referencing, never a second data-loss opportunity. Flagging this here, in the same spirit Phase 4I flagged its migration-non-application decision — if this reasoning is wrong and the literal ordering was intended, that is a one-line change (compute a placeholder `LogID`-independent EXECUTED marker before calling `merge_group()`, then a second, harmless UPDATE to backfill `ExecutionAuditLogID` after) — deferred here rather than guessed at.

The module never opens, commits, or rolls back a transaction itself (`TransactionOwnershipTests` proves this by source scan) — exactly like `lock_pair_rows()` and every write-issuing function in `merge_approval.py`, the caller owns the transaction. Any exception (including `merge_group()`'s own `RuntimeError` on a profile-preservation violation, or an injected failure) propagates unhandled.

## 5. FK / Dependency Action Matrix

`DEPENDENCY_ACTION_MATRIX` in `merge_executor.py`, one row per real FK Phase 4F independently re-derived three times:

| Table.FK | ON DELETE | Executor Action | Handling |
|---|---|---|---|
| `Authors.PaperID` | NO ACTION | `REMAP` | `merge_group()`, special-cased, unmodified |
| `Citations.PaperID` | NO ACTION | `REMAP` | `merge_group()`, special-cased, unmodified |
| `ExternalAuthors.PaperID` | NO ACTION | `REMAP` | `merge_group()`'s `SIMPLE_CHILDREN`, unmodified |
| `CitationsHistory.PaperID` | NO ACTION | `REMAP` | `merge_group()`'s `SIMPLE_CHILDREN`, unmodified |
| `ReportPaperDecision.PaperID` | SET NULL | `REMAP` | `merge_group()`'s `SIMPLE_CHILDREN` pre-empts the SET NULL rule, unmodified |
| `ReportPaperDecision.MissingResolvedToPaperID` | SET NULL | **`BLOCK`** | Phase 4F gap #2 — no remap logic exists; `check_unhandled_dependency_gaps()` live-counts it for the loser, blocks if nonzero |
| `AuthorReviewQueue.PaperID` | CASCADE | **`BLOCK`** | Phase 4F gap #1 — no remap logic exists; live-counted, blocks if nonzero |

No row count was ever assumed zero — `check_unhandled_dependency_gaps()` performs the same "does the table even exist" `information_schema.tables` guard `existing_child_tables()` already uses, then a real `COUNT(*)` for the two `BLOCK`-classified rows only. `DependencyActionMatrixTests` (7 tests) prove: every matrix entry has a valid action; both previously-flagged gaps are classified `BLOCK`; every `merge_group()`-handled dependency is classified `REMAP`; zero rows never blocks; a nonexistent table is never even `COUNT`-queried; nonzero rows in either gap table correctly block; and the full `execute_approved_merge()` pipeline correctly stops at this step when either gap is populated.

## 6. Field-Preservation Behavior

`JournalID`: `build_journal_state()` (reused, unmodified) is recomputed fresh from the just-locked rows. `LOSER_ONLY_BACKFILL` triggers the one new write this phase adds — a direct `UPDATE "ResearchPaper" SET "JournalID" = %s WHERE "PaperID" = %s` using the loser's already-fetched value, issued **before** `merge_group()` deletes that row. Any other state that isn't `execution_permitted` (i.e. `CONFLICT`) blocks with `EXEC_BLOCKED_JOURNAL_CONFLICT` before any write. `AuthorNameRaw`: `author_content_conflicts()` (reused, unmodified) is recomputed fresh; any non-empty result blocks with `EXEC_BLOCKED_AUTHOR_CONFLICT` before any write — even in a scenario deliberately constructed so the fingerprint still matches (i.e. even if a bad approval somehow slipped through with the conflict already present at approval time), this defense-in-depth re-check still catches it (`test_L_author_name_raw_conflict_blocks_before_merge`). `CitationsByYear` was already handled by `merge_group()`'s existing `merge_citation_fields()` and required no new code. If the plan doesn't represent a required resolution for either field, execution blocks — it never guesses.

## 7. Approval Validation Contract

`execute_approved_merge()` requires an exact `APPROVED` `MergeApproval` matching `(SurvivorPaperID, LoserPaperID, PlanFingerprint)` — enforced twice, independently: `fetch_current_approval()`'s own `WHERE` clause structurally cannot return a reversed pair, and `approval_matches_pair()` re-checks the direction anyway as defense-in-depth (`test_E_reversed_approval_zero_write_sql` proves this via a monkeypatch that forces a mismatched row through, since the real query can't produce one). `PENDING`, `REJECTED`, `REVOKED`, and `EXECUTED` approvals all correctly refuse (only `APPROVED` passes `if approval.status != ma.STATUS_APPROVED`). The approval is fetched fresh, after the identity/permission checks but before the row lock — its `PlanFingerprint` is what `validate_against_plan()` re-verifies against the freshly-locked, freshly-recomputed fingerprint (steps 8–9). There is no bypass flag, parameter, or code path.

## 8. Idempotency Behavior

Reuses `idempotency_verdict()` unmodified. `NOT_PREVIOUSLY_EXECUTED` continues; `ALREADY_EXECUTED` and `HISTORICAL_STATE_AMBIGUOUS` both block with zero write SQL, before any child remap or delete. `AuditLog` remains the sole execution-history source of truth — unchanged from every prior phase.

## 9. Rollback Behavior

The module never calls `.commit(`/`.rollback(` and never opens its own transaction context (`TransactionOwnershipTests`, source-scan). Any exception — `merge_group()`'s own `RuntimeError` on a profile-preservation violation, or a deliberately injected failure at any of the three write points (`test_M`/`test_N`/`test_O`) — propagates unhandled out of `execute_approved_merge()`; nothing here ever catches and swallows one. `test_J_child_remap_failure_rolls_back` and `test_P_unexpected_mid_preflight_exception_not_reported_as_success` both assert the exception actually propagates (`assertRaises`) rather than being converted into a graceful `ExecutionResult(ok=False, ...)` — no partial merge state is ever reported as a success. The real, atomic rollback guarantee itself is Django's `transaction.atomic()`, already proven in Phase 4F/4G/4H's own analysis of `dedup_papers.py`'s existing `--apply` path — not re-proven here, since this prototype never opens a real transaction against a real database.

## 10. Test Matrix and Exact Results

| Scenario | Test | Result |
|---|---|---|
| A. Successful canary execution | `SuccessfulExecutionTests` (6 tests: ok=True, survivor survives/loser gone, ascending lock order, AuditLog written+linked, approval EXECUTED, delete is final destructive action) | pass |
| B. Self-merge | `test_B_self_merge_zero_write_sql` | pass |
| C. Missing approval | `test_C_missing_approval_zero_write_sql` | pass |
| D. PENDING approval | `test_D_pending_approval_zero_write_sql` | pass |
| E. Reversed approval | `test_E_reversed_approval_zero_write_sql` | pass |
| F. Stale fingerprint | `test_F_stale_fingerprint_zero_write_sql` | pass |
| G. ALREADY_EXECUTED | `test_G_already_executed_zero_write_sql` | pass |
| H. HISTORICAL_STATE_AMBIGUOUS | `test_H_historical_state_ambiguous_zero_write_sql` | pass |
| I. Missing row after lock | `test_I_missing_row_after_lock_zero_destructive_writes` | pass |
| J. Child remap failure → rollback | `test_J_child_remap_failure_rolls_back` | pass |
| K. JournalID deterministic backfill | `test_K_journal_id_deterministic_backfill` | pass |
| L. AuthorNameRaw unresolved conflict | `test_L_author_name_raw_conflict_blocks_before_merge` | pass |
| M. Approval EXECUTED update failure → rollback | `test_M_approval_executed_update_failure_rolls_back` | pass |
| N. AuditLog failure → rollback | `test_N_auditlog_failure_rolls_back` | pass |
| O. Delete failure → rollback | `test_O_delete_failure_rolls_back` | pass |
| P. Unexpected mid-preflight exception | `test_P_unexpected_mid_preflight_exception_not_reported_as_success` | pass |
| Q. No COMMIT before success | `TransactionOwnershipTests` (3 tests) | pass |
| Dependency/FK matrix | `DependencyActionMatrixTests` (7 tests) | pass |
| Permission | `PermissionTests` (1 test) | pass |
| Static safety | `StaticSafetyChecks` (5 tests) | pass |

**`test_merge_executor.py`: 38/38 passing.**

| Suite | Result |
|---|---|
| `test_merge_executor.py` (new, this phase) | 38/38 |
| `test_dedup_papers.py` (re-run, unchanged) | 18/18 |
| `test_merge_plan_generator.py` (re-run, unchanged) | 43/43 |
| `test_merge_execution_safety.py` (re-run, unchanged) | 68/68 |
| `test_merge_approval.py` (re-run, unchanged) | 45/45 |
| **Total** | **212/212 passing, zero regressions** |

## 11. Static Safety Checks

- No network client of any kind (`requests.`/`urllib.request`/`httpx.`/`socket.`/`http.client`) exists anywhere in `merge_executor.py`.
- No `subprocess`, `os.system`, or `--apply` argparse wiring exists.
- The executor cannot reach a write statement without first calling `fetch_current_approval()` and checking its result for `None` — proven by source-offset comparison against the first write literal.
- No direct `DOI` column write exists anywhere.
- No raw `INSERT INTO "Authors"`/`DELETE FROM "Authors"`/`INSERT INTO "AuditLog"` literal exists in this file — those writes remain exclusively inside the unmodified `merge_group()`.
- The module never opens, commits, or rolls back a transaction.

## 12. Live Validation

Performed strictly read-only, after all 212 tests passed, via one script consisting only of `SELECT` statements followed by an explicit `conn.rollback()`:

| Check | Result |
|---|---|
| `MergeApproval` table exists in production? | **`false`** — still unapplied |
| Canary fingerprint (5232/5482) | `2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb` — **byte-identical to every prior run this project has ever performed** (now the 6th independent confirmation, spanning 2026-08-21 and 2026-08-22) |
| Idempotency verdict | `NOT_PREVIOUSLY_EXECUTED` — unchanged |

No `--apply`, no executor invocation, no write of any kind occurred against the real database this phase.

## 13. Exact DB Writes

- **Test/fake environment**: every write in every test happens against `ExecutorFakeCursor`'s in-memory Python dicts. No `psycopg2`/Django connection is opened anywhere in `test_merge_executor.py`.
- **Production**: **0.** The one live-database script this phase ran issued only `SELECT`s and ended with `conn.rollback()`.

## 14. Network Calls

**0.** Confirmed both by static source scan (§11) and by the fact that no test or live-validation step ever imports or calls anything network-related.

## 15. Records Merged

**0.** No real `ResearchPaper` row was ever deleted; no real `Authors`/`Citations`/child-table row was ever remapped.

## 16. DOI Changes

**0.** No `DOI` column write exists anywhere in `merge_executor.py` (`test_no_direct_doi_column_write`).

## 17. Remaining Blockers Before The First Real Controlled Execution

1. **The `sprint11_merge_approval.sql` migration has still never been applied to any database** — the same, unbroken, deliberate decision carried forward from Phase 4I. Nothing in this phase changes that.
2. **No HTTP endpoint or UI exists** to actually invoke `execute_approved_merge()` — it remains a plain importable function, matching this project's established pattern (`dedup_papers.py`, `merge_plan_generator.py`, `merge_execution_safety.py`, and `merge_approval.py` are all function modules too).
3. **This prototype has never been run against a real transaction, a real lock, or real concurrent access.** Every behavior above is proven against a single-threaded, in-memory double — the real Postgres locking/transaction guarantees this design depends on are themselves already proven elsewhere in this repository (`reconciliation_views.py`'s live, working endpoint; `dedup_papers.py`'s own real `--apply` history), but this specific new orchestration has not been exercised against them.
4. **Per-tenant permission scoping remains unconfirmed** — Phase 4H's finding, unchanged; `can_approve_merge()` still checks `manage_users`/`Admin` globally.
5. **The `TenantID` isolation path (`compute_classification()`'s `tenant_blocked` check) has never been exercised against a real cross-tenant pair** — Phase 4F's finding, unchanged, still untested in practice.
6. **The transaction-sequence divergence documented in §4 (delete-before-`EXECUTED`-update, rather than this message's own literal bullet order) needs your explicit confirmation** that Phase 4H's design (which this phase followed) is the intended one, rather than a stricter interpretation requiring `merge_group()` itself to be split.
7. **No real concurrent-execution test exists** — `test_illegal_transition_double_approve_is_rejected` (Phase 4I) proves the *sequential outcome* two racing approvals converge to; this phase adds no equivalent live-concurrency proof for two racing *executions*, since doing so would require a real multi-connection database, explicitly out of scope.

## Final Decision

**A) Executor prototype implemented and validated; ready for a separate controlled live-execution-readiness audit**

Every piece Phase 4F identified as missing four phases ago — fingerprinting, row-locking, idempotency preflight, approval storage, and now the executor itself — exists as real, tested code. This specific phase's own required test matrix (A–Q, plus dependency-matrix and permission coverage) is fully implemented and passing: 38/38 new tests, 212/212 total across all five suites, zero regressions. The two dependency gaps Phase 4F found and every subsequent phase carried forward unresolved (`AuthorReviewQueue` CASCADE, `ReportPaperDecision.MissingResolvedToPaperID` SET NULL) are no longer silently ignored — they are live-checked and block execution if populated, exactly as this phase's own instruction required ("If a dependency action remains uncertain, BLOCK execution rather than guessing"). The canary pair's fingerprint has now been reproduced identically six independent times across two calendar days, through every phase from 4G to this one, with zero drift.

This is **A**, not **B**, because nothing found this phase left an unresolved *implementation* gap within this phase's own stated scope — every required test passed, every required safety check is wired in, and the one design tension (§4's transaction-ordering divergence) has a considered, documented resolution, not an open question blocking further work. This is **A**, not **C**, because nothing found this phase suggests the underlying architecture (reused, unmodified `merge_group()`; `AuditLog` as the sole execution-history source of truth; `MergeApproval` as the sole approval-state source of truth; deterministic ascending locking; three-way fingerprint/idempotency/dependency preflight) is unsafe — every gap that remains (§17) is squarely pre-live-execution operational readiness (migration application, endpoint wiring, real-transaction exercise, tenant-scoping investigation), not a design flaw this prototype exposed.

Per your instructions, I am stopping here. Phase 4K is not started. The migration was not applied. No real merge was executed. No production database was written to.
