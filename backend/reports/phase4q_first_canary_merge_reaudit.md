# Phase 4Q — First Canary Merge Re-Audit (STRICTLY READ-ONLY)

## Result Summary

Every check this phase re-derived, live, this moment, against production — none reused from any prior phase's cached values. The canary pair, `ApprovalID=1`, the corrected fingerprint mechanism, the schema, and the environment are all confirmed clean and consistent. **Final decision: A) READY FOR ONE CONTROLLED LIVE CANARY MERGE.** No merge was executed. `ApprovalID=1` was read repeatedly and never modified.

## Scope Confirmation

Zero locks were acquired this phase (`lock_pair_rows()` was deliberately never called — the expected lock order was verified via the pure, DB-free `build_lock_order()` function instead, per your explicit instruction not to write any lock or open an execution transaction). Zero `INSERT`/`UPDATE`/`DELETE` of any kind. `execute_approved_merge()` and `merge_group()` were never imported or called. No `ResearchPaper`/child-table/`DOI` change. No new approval created. No approve/reject/revoke on `ApprovalID=1`. Zero network calls beyond the production-database connections used for read-only investigation.

---

## 1. `ApprovalID=1` Live State

Read directly, no assumption from any prior report:

```
ApprovalID=1, SurvivorPaperID=5232, LoserPaperID=5482,
PlanID='phase4p-canary-5232-5482',
PlanFingerprint='2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb',
ApprovalVersion=1, TenantID=1, Status='APPROVED',
ReviewedByUserID=221, ReviewedAt=2026-08-22 10:24:11 UTC
```

Byte-for-byte identical to its state at the end of Phase 4P and Phase 4P.1. Total `MergeApproval` rows in production: **1**.

## 2. Survivor/Loser Live Identity

Read live from the row itself, not assumed from direction described in any earlier phase: **`SurvivorPaperID=5232`, `LoserPaperID=5482`** — unreversed, matching the original canary direction throughout this entire project. `reject_self_merge(5232, 5482)` → `(True, None)`.

## 3. Stored Fingerprint

`2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb` — matches the expected reference value exactly.

## 4/5/6. Fingerprint Re-Audit — Both Real Paths, Live, This Moment

```
Django live fingerprint:       2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb
raw psycopg2 live fingerprint: 2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb
```

**Byte-identical: `True`.** Both equal the stored `ApprovalID=1` fingerprint: `True`, `True`. Both equal the expected reference value: `True`, `True`. This is a fresh recomputation performed this phase — not a reuse of Phase 4P.1's own numbers — confirming the Phase 4P.1 fix continues to hold under live, current conditions.

## 7. Drift Audit Result

| Check | Live result | Drift? |
|---|---|---|
| Both papers exist | Yes | No |
| `TenantID` compatibility | `1` == `1` | No |
| `Title`/`DOI`/`JournalID` (winner) | `"Optimal deep learning model..."`, `10.1155/2022/8531213`, `1803` | Unchanged |
| `Title`/`DOI`/`JournalID` (loser) | `"Research Article Optimal Deep Learning Model..."`, `NULL`, `NULL` | Unchanged |
| `pair_confidence` | `high` | No |
| `hard_exclusion_reason` | `None` | No |
| DOI safety (`is_doi_claimed_elsewhere`) | `False` | No |
| `JournalID` decision | `WINNER_ONLY`, `execution_permitted=True` | No |
| `AuthorNameRaw` conflicts | `[]` | No |
| Relevant `Authors` rows | 1 each side, `UserID=97`, identical raw name | Unchanged |

**No drift found anywhere this fingerprint or preflight depends on.**

## 8. Every Executor Precondition, and Its Result

| Precondition | Result |
|---|---|
| `reject_self_merge` | **Pass** — `(True, None)` |
| Permission precondition | **Pass, checked read-only** — `can_approve_merge()` internally issues only a `SELECT` against `Permission`/`RolePermission` (via `has_litrix_perm()`); confirmed `True` for the real reviewing admin account (`admin@litrix.com`) with zero write |
| Approval verification | **Pass** — `fetch_current_approval()` (the real function) returns the row, `Status=APPROVED`, `approval_matches_pair()=True` |
| Deterministic lock order | **Correct, verified without acquiring any lock** — `build_lock_order(5232, 5482)` and `build_lock_order(5482, 5232)` both return `(5232, 5482)`, proven via the pure, DB-free function; `lock_pair_rows()` itself was deliberately never called this phase |
| Fingerprint verification | **Pass** — §4–6 |
| `validate_against_plan()` | **`status=OK`, `passed=True`** — every sub-check (`both_rows_exist`, `winner_loser_unreversed`, `fingerprint_match`, `pair_confidence`, `hard_exclusion_reason`, `doi_claimed_elsewhere`) individually confirmed |
| Duplicate safety | **Pass** — `pair_confidence=high`, `hard_exclusion_reason=None` |
| DOI safety | **Pass** — not claimed elsewhere |
| `idempotency_verdict()` | **`NOT_PREVIOUSLY_EXECUTED`** — 0 matching `AuditLog` rows |
| `JournalID` decision | **`execution_permitted=True`** (`WINNER_ONLY` — no backfill needed) |
| `AuthorNameRaw` conflicts | **`[]`** |
| Dependency gaps | **`[]`** — `check_unhandled_dependency_gaps()`, live |
| Prior `AuditLog` merge record | **None** — 0 rows |

**Every precondition that can be checked read-only was checked, live, this phase, and every one passed.** No precondition required a write or a real lock to verify.

## 9. Idempotency Result

`NOT_PREVIOUSLY_EXECUTED` — "no prior paper.merge.dedup history for either PaperID." 0 `AuditLog` rows match `Action='paper.merge.dedup'` and `TargetID` in `{5232, 5482}`.

## 10. Dependency Results (Full Table, Live This Moment)

| Table.FK | Action | Winner rows | Loser rows |
|---|---|---|---|
| `Authors.PaperID` | REMAP | 1 | 1 |
| `Citations.PaperID` | REMAP | 0 | 0 |
| `ExternalAuthors.PaperID` | REMAP | 0 | 0 |
| `CitationsHistory.PaperID` | REMAP | 0 | 0 |
| `ReportPaperDecision.PaperID` | REMAP | 0 | 0 |
| `ReportPaperDecision.MissingResolvedToPaperID` | **BLOCK if nonzero** | — | **0** |
| `AuthorReviewQueue.PaperID` | **BLOCK if nonzero** | — | **0** |
| `PaperKeywords.PaperID` (Phase 4K schema-drift finding) | REMAP (SIMPLE_CHILDREN, unaffected — table exists, 0 rows for this pair) | — | **0** |

`check_unhandled_dependency_gaps(cur, 5482)` → `[]`. Neither previously-flagged gap (`AuthorReviewQueue`, `ReportPaperDecision.MissingResolvedToPaperID`) is populated for this pair.

## 11. Schema/FK Results

| Check | Live result |
|---|---|
| `MergeApproval` column count | 18 — unchanged since Phase 4O's application |
| `SurvivorPaperID` FK | `MergeApproval_SurvivorPaperID_fkey → ResearchPaper.PaperID, ON DELETE NO ACTION` — unchanged |
| `LoserPaperID` FK | **Confirmed absent** — 0 FK constraints on this column, matching the Phase 4K.1 design exactly |
| Other FKs | `TenantID→Tenant (NO ACTION)`, `ReviewedByUserID`/`RevokedByUserID→Users (SET NULL)`, `ExecutionAuditLogID→AuditLog (SET NULL)` — all unchanged |
| Orphaned/conflicting approval rows for this pair | **None** — exactly one row (`ApprovalID=1`, `APPROVED`) touches this pair in either role |
| `ApprovalID=1` can survive loser deletion under current schema | **Yes** — `LoserPaperID` has no FK to enforce; `SurvivorPaperID`'s FK is irrelevant to the loser's deletion (Phase 4K.1/4L, re-confirmed unchanged) |

No schema drift, no new migration, nothing since Phase 4O's application has changed this table.

## 12. `AuditLog` Prior-Execution Result

`0` rows with `Action='paper.merge.dedup'` and `TargetID` in `{5232, 5482}` — no prior execution exists for this pair, in either direction.

## Task F — Environment Safety Check

- **Concurrent activity**: `pg_stat_activity` (excluding this session, `state != 'idle'`) → **empty**.
- **Locks on relevant tables**: `pg_locks` joined against `ResearchPaper`/`MergeApproval`/`Authors`/`AuditLog` (excluding this session) → **empty**.
- **Prior merge**: confirmed none (§12).
- **No lock was written by this phase**; no execution transaction was opened.
- **Backup/PITR**: not re-checked this phase, per your explicit instruction — no evidence was found this phase of any change to the Neon configuration since the snapshot was created before Phase 4O. This is stated as "not re-verified," not "confirmed still valid" — the snapshot's continued adequacy should be confirmed by you (or re-checked explicitly) at the moment a real execution is actually authorized, since this phase found no signal either way.

## 13. Test Totals

```
test_dedup_papers.py             18/18
test_merge_plan_generator.py     43/43
test_merge_execution_safety.py   79/79
test_merge_approval.py           45/45
test_merge_executor.py           39/39
test_fk_lifecycle.py             11/11
```

**Total: 235/235 passing — exactly the expected count. Zero regressions. No test was modified.**

## 14. Exact DB Write Accounting

- Production DB writes: **0.**
- `MergeApproval` writes: **0.**
- `ResearchPaper` writes: **0.**
- Child-table writes: **0.**
- Locks acquired: **0** (deliberately — `lock_pair_rows()` never called; the expected order was proven via the pure `build_lock_order()` function instead).
- Network calls: **0** beyond the production-database connections used for read-only investigation.

## 15. Exact Merge Accounting

- Records merged: **0.**
- DOI changes: **0.**
- `execute_approved_merge()` executions: **0** — never imported or called.
- `merge_group()` executions: **0** — never imported or called.

---

## Final Decision

### **A) READY FOR ONE CONTROLLED LIVE CANARY MERGE**

Every item this phase was asked to re-verify — the approval's live identity and state, both real fingerprint computation paths (byte-identical to each other, to the stored value, and to the expected reference), every field and dependency the plan and the executor's preflight actually depend on, the schema's FK shape, and the absence of any concurrent activity or conflicting approval — was independently re-derived, live, this phase, with zero reliance on any prior phase's cached conclusion. Nothing found contradicts readiness. Nothing found is new. 235/235 tests remain green.

**Per your explicit instruction: this decision does not authorize execution.** No merge was performed. No further phase was started.

Per your instructions, I am stopping here and waiting for your explicit approval before any real execution is attempted.

---

## Exact Accounting

- **Code files modified**: **0.**
- **Code files created**: **0.**
- **Test files modified**: **0.**
- **Migration files modified**: **0.**
- **Report files created**: **2** — this file and `backend/reports/phase4q_first_canary_merge_reaudit.json`.
- **Production DB writes**: **0.**
- **`MergeApproval` writes**: **0.**
- **`ResearchPaper` writes**: **0.**
- **Child-table writes**: **0.**
- **DOI changes**: **0.**
- **Records merged**: **0.**
- **`execute_approved_merge()`/`merge_group()` executions**: **0.**
- **Network calls**: **0** beyond the production-database connections used for read-only investigation throughout.
- **Test totals**: 235/235 passing, unchanged from Phase 4P.1, zero regressions.

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
?? backend/reports/                                <- this phase adds 2 files to it
?? backend/tools/merge_approval.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_execution_safety.py         <- pre-existing (Phase 4P.1 fix), unchanged this phase
?? backend/tools/merge_executor.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_plan_generator.py           <- pre-existing, unchanged this phase
?? backend/tools/test_fk_lifecycle.py              <- pre-existing, unchanged this phase
?? backend/tools/test_merge_approval.py            <- pre-existing, unchanged this phase
?? backend/tools/test_merge_execution_safety.py    <- pre-existing (Phase 4P.1 tests), unchanged this phase
?? backend/tools/test_merge_executor.py            <- pre-existing, unchanged this phase
?? backend/tools/test_merge_plan_generator.py      <- pre-existing, unchanged this phase
```

**This phase's only repository changes are the two new report files.** No production database write of any kind occurred. `ApprovalID=1` was read many times and modified zero times.
