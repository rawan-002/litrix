# Phase 4R — Controlled Live Canary Merge Execution

## Result Summary

**The first real, production duplicate-paper merge in this project's history was executed successfully.** `ApprovalID=1` transitioned `APPROVED → EXECUTED`. `ResearchPaper` 5482 (the loser) was deleted; `ResearchPaper` 5232 (the survivor) remains, unchanged in every field that mattered, with its citation data correctly merged. Exactly one `AuditLog` row (`LogID=1539`) was written. Every dependency table was independently re-checked, live, after the fact, and found exactly as expected — zero orphans, zero unexpected loss, zero unrelated change. **Final verdict: A) CANARY MERGE SUCCESSFUL — SAFE TO BEGIN SEPARATE POST-EXECUTION REVIEW.**

---

## 1. Authorization Scope

**Only one pair was authorized and only one pair was executed: `SurvivorPaperID=5232`, `LoserPaperID=5482`, `ApprovalID=1`.** No other `MergeApproval` row exists or was touched. No batch was run. `dedup_papers.py --apply` was never invoked. No second merge was attempted at any point, before or after the one authorized execution.

---

## 2. Pre-Execution Gates (Task A) — All Checked Fresh, Live, Before the Write

| # | Gate | Result | Fresh evidence |
|---|---|---|---|
| 1 | `ApprovalID=1` still exists | **PASS** | `SELECT * FROM "MergeApproval" WHERE "ApprovalID"=1` returned exactly one row |
| 2 | `SurvivorPaperID=5232`, `LoserPaperID=5482`, `Status=APPROVED` | **PASS** | Read directly from that row, this moment |
| 3 | Stored `PlanFingerprint` matches a freshly computed fingerprint | **PASS** | Stored: `2298ea25...`; fresh Django-path: `2298ea25...`; fresh raw-psycopg2-path: `2298ea25...` |
| 4 | Both connection paths byte-identical, both equal the stored value | **PASS** | Confirmed by direct string comparison, this phase, independent of Phase 4P.1/4Q's own numbers |
| 5 | `validate_against_plan()` fresh | **PASS** — `status=OK`, `passed=True` | `checks={'both_rows_exist': True, 'winner_loser_unreversed': True, 'fingerprint_match': True, 'pair_confidence': 'high', 'hard_exclusion_reason': None, 'doi_claimed_elsewhere': False}` |
| 6a | Duplicate safety | **PASS** | `pair_confidence=high`, `hard_exclusion_reason=None` |
| 6b | DOI safety | **PASS** | Not claimed elsewhere |
| 6c | `fingerprint_match` | **PASS** | `True` |
| 6d | `JournalID` decision permits execution | **PASS** | `state=WINNER_ONLY`, `execution_permitted=True` |
| 6e | `AuthorNameRaw` conflicts | **PASS** | `[]` |
| 6f | Dependency-gap checks | **PASS** | `[]` |
| 6g | `idempotency_verdict()` | **PASS** | `NOT_PREVIOUSLY_EXECUTED` |
| 7 | Executing user has required permission | **PASS** | `can_approve_merge(admin@litrix.com)=True` |
| 8 | Not a self-merge | **PASS** | `reject_self_merge(5232, 5482) → (True, None)` |
| 9 | Both `ResearchPaper` rows exist | **PASS** | Both fetched successfully |
| 10 | Same tenant, no cross-tenant condition | **PASS** | `TenantID=1` on both sides |
| 11 | No concurrent activity/blocking lock | **PASS** | `pg_stat_activity` and `pg_locks` on relevant tables, excluding this session, both empty |
| 12 | No prior successful-merge `AuditLog` entry for this pair | **PASS** | 0 matching rows |
| 13 | `ApprovalID=1` not revoked/rejected/executed/changed | **PASS** | `Status=APPROVED`, `ExecutedAt=NULL`, `ExecutionAuditLogID=NULL` at check time |

**All 13 gates (20 individual sub-checks) passed, every one checked live, this phase, before the write — none reused from Phase 4Q or any earlier report.**

---

## Task B — Snapshot/Recovery Boundary

No snapshot was created, modified, or restored this phase. Recorded, not acted upon: a manual Neon snapshot exists (created before Phase 4O), and Neon's PITR/history window is available per its current configuration, per your own stated context. This capability was treated strictly as an existing safety net, never as a reason to skip or shortcut any of the 13 pre-execution gates above.

---

## 3. Exact Execution Result (Task C)

**Committed successfully, on the first and only attempt. No exception. No rollback. No retry.**

```python
with transaction.atomic():
    with connection.cursor() as cur:
        result = execute_approved_merge(cur, admin_user, 5232, 5482, expected_plan_fingerprint)
    # result.ok == True -> transaction committed normally at context exit
```

```
outcome: SUCCESS -- committed
approval_id: 1
audit_log_id: 1539
```

The real, unmodified `merge_executor.py::execute_approved_merge()` and the real, unmodified `dedup_papers.py::merge_group()` were used, exactly as designed since Phase 4J — no executor logic was changed, no safety check was patched around, no manual `DELETE`/`UPDATE` was issued outside this one call path.

---

## 4. Before/After State

| | Before | After | Delta |
|---|---|---|---|
| `ResearchPaper` total rows | 2,031 | **2,030** | **−1** (exactly the one loser, no other) |
| `ResearchPaper` 5232 (survivor) | Exists, `DOI=10.1155/2022/8531213`, `JournalID=1803` | **Exists, identical DOI/JournalID/Title/TenantID** | Unchanged where it should be unchanged |
| `ResearchPaper` 5482 (loser) | Exists | **Gone** | Deleted, as intended |
| `MergeApproval` total rows | 1 | **1** | Unchanged — no new approval created |
| `MergeApproval` `ApprovalID=1` | `Status=APPROVED`, `ExecutedAt=NULL` | **`Status=EXECUTED`, `ExecutedAt=2026-08-22 13:27:20 UTC`, `ExecutionAuditLogID=1539`** | Transitioned exactly once, as designed |
| `AuditLog` total rows | 957 | **958** | **+1**, exactly one new row |
| `AuditLog` `paper.merge.dedup` total (all-time) | 59 | **60** | **+1** |
| `Authors` rows for the pair | 2 (1 each side, same `UserID=97`) | **1** (`PaperID=5232` only) | Correctly unified, no data lost — the shared author's link survives on the kept paper |
| `Citations`/`ExternalAuthors`/`CitationsHistory`/`ReportPaperDecision`/`AuthorReviewQueue`/`PaperKeywords` for the pair | 0 across the board | **0 across the board** | Unchanged — nothing to remap, matching the pre-execution dependency audit exactly |
| Survivor `CitationsByYear` | `{"2022":5,"2023":13,"2024":12,"2025":8,"2026":2}` | **Identical** | Unchanged — loser had 0 citations, `GREATEST()`/element-wise-max merge correctly contributed nothing new |

---

## 5. Integrity Verification (Task D/E)

### `ResearchPaper`
1. Survivor `5232` exists — **confirmed**.
2. Loser `5482` no longer exists — **confirmed**, `SELECT` returns zero rows.
3. Survivor DOI (`10.1155/2022/8531213`) unchanged — **confirmed**.
4. Survivor `Title`/`TenantID`/`JournalID` all match the pre-execution, approved state exactly — **confirmed**.
5. No unexpected DOI change anywhere — **confirmed**; the only `DOI`-bearing row in the pair (the survivor's) is unchanged, and the loser (which had `DOI=NULL`) is gone, not modified.

### `MergeApproval` (`ApprovalID=1`)
- `SurvivorPaperID=5232`, `LoserPaperID=5482` — **unchanged**, correct historical identity retained (the entire point of the Phase 4K.1 FK-free design — now proven in a real execution, not just simulated).
- `Status=EXECUTED` — **confirmed**.
- `PlanFingerprint` — **unchanged**, `2298ea25...`.
- `ReviewedByUserID=221`, `ReviewedAt` from the original Phase 4P approval — **unchanged**, internally consistent with `ExecutedAt`/`ExecutionAuditLogID` being set afterward, in the same row, by this execution.

### `AuditLog`
1. New merge audit record exists: `LogID=1539`.
2. Identifies the correct pair: `Action='paper.merge.dedup'`, `TargetType='ResearchPaper'`, `TargetID=5482`, `Metadata.kept_paper_id=5232`.
3. No duplicate merge audit entries — exactly one row matches `Action='paper.merge.dedup' AND TargetID=5482`.
4. Row count changed by exactly `+1` (957 → 958) — no unexpected additional writes.

### Child/Dependency Tables — Every One Re-Checked Live, Not Assumed From Any Prior Report

| Table | Before (pair-scoped) | After (pair-scoped) | Result |
|---|---|---|---|
| `Authors` | 2 rows (1 winner, 1 loser, same `UserID`) | 1 row (`PaperID=5232`) | Correctly unified |
| `Citations` | 0/0 | 0/0 | No-op, as expected |
| `CitationsByYear` (column) | winner populated, loser `NULL` | winner unchanged | Correctly merged, nothing to add |
| `ExternalAuthors` | 0/0 | 0/0 | No-op |
| `CitationsHistory` | 0/0 | 0/0 | No-op |
| `ReportPaperDecision` (both `PaperID` and `MissingResolvedToPaperID`) | 0/0 | 0/0 | No-op |
| `AuthorReviewQueue` | 0/0 | 0/0 | No-op — the previously-flagged gap (Phase 4F) was never populated for this pair, before or after |
| `PaperKeywords` | 0/0 | 0/0 | No-op — the Phase 4K schema-drift finding remains harmless for this pair |

### Global Safety
- No other `ResearchPaper` row was deleted — total count delta is exactly `−1`.
- No other merge was executed — exactly one `paper.merge.dedup` `AuditLog` row was created, for `TargetID=5482` only.
- No unrelated approval changed — `MergeApproval` still has exactly one row.
- No unrelated DOI changed — the only DOI in scope (survivor's) is unchanged.
- No unexpected cross-tenant modification — both rows were, and the survivor remains, `TenantID=1`.

### Task E — Post-Merge Integrity Checks
1. **FK violations/orphans**: every real FK-constrained table (`Authors`, `Citations`, `ExternalAuthors`, `CitationsHistory`, `ReportPaperDecision` on both its `PaperID` and `MissingResolvedToPaperID` columns, `AuthorReviewQueue`, `PaperKeywords`) was queried directly for any remaining reference to `PaperID=5482` — **zero found in every one**. This is expected and mechanically guaranteed: these are real, enforced foreign keys with `ON DELETE NO ACTION`/`SET NULL`/`CASCADE` rules; had any of them still referenced `5482`, the `DELETE` itself would have raised a `ForeignKeyViolation` and the whole transaction would have rolled back — it did not.
2. **Duplicate approval state**: exactly one row matches `(SurvivorPaperID=5232, LoserPaperID=5482, PlanFingerprint=2298ea25...)` — no duplicate.
3. **Remaining references to `LoserPaperID=5482`**: checked across every real FK-constrained table — none. The **one** place `5482` still appears is `MergeApproval.LoserPaperID` itself.
4. **`MergeApproval.LoserPaperID=5482` retention — correctly interpreted, not flagged as an orphan.** This is the intended, designed behavior (Phase 4K.1): `LoserPaperID` carries no foreign key specifically so this historical identity survives the loser's deletion. Its presence here is the fix working exactly as designed, proven for the first time under a real execution rather than a simulation.
5. **Idempotency after execution**: `idempotency_verdict(fetch_merge_audit_rows(cur, [5232, 5482]), 5232, 5482)` now returns **`ALREADY_EXECUTED`** — "loser 5482 was already merged into winner 5232 (AuditLog LogID=1539)." This was checked strictly read-only; **no second execution attempt was made or will be made this phase.**

---

## 6. Safety Accounting (Exact)

- **Production DB writes**: the one atomic transaction this phase's Task C performed (`JournalID` backfill: not needed for this pair, `WINNER_ONLY` state — no write issued for it; `Authors` remap; `CitationsByYear` merge; `AuditLog` INSERT; `MergeApproval` `EXECUTED` UPDATE; `ResearchPaper` DELETE) — **all inside one single committed transaction**, exactly the design proven in every prior phase's tests.
- **Records merged**: **1** (the pair 5232/5482).
- **Records deleted**: **1** (`ResearchPaper` `PaperID=5482`).
- **Records remapped**: **1** `Authors` row (`UserID=97`, from `PaperID=5482` to `PaperID=5232`, via `ON CONFLICT DO NOTHING` since the survivor already had the same link — net effect: the loser's now-redundant row removed, the survivor's own link untouched).
- **Approvals changed**: **1** (`ApprovalID=1`, `APPROVED → EXECUTED`).
- **`AuditLog` rows created**: **1** (`LogID=1539`).
- **DOI changes**: **0** — the loser had no DOI to lose; the survivor's DOI was never touched.
- **Network calls**: **0** beyond the production-database connection used throughout.
- **Automatic retries**: **0** — none occurred, none were needed (the single attempt succeeded).
- **Additional merges**: **0** — no second pair, no batch, no `--apply`.

---

## 7. Final Verdict

### **A) CANARY MERGE SUCCESSFUL — SAFE TO BEGIN SEPARATE POST-EXECUTION REVIEW**

Every one of the 13 pre-execution gates (20 sub-checks) passed on fresh, live evidence gathered this phase, independent of any prior report. The single authorized execution — `ApprovalID=1`, `5232` survives, `5482` loses — committed successfully on the first attempt, with no exception, no rollback, and no retry. Every post-execution check this phase performed, across `ResearchPaper`, `MergeApproval`, `AuditLog`, and all eight dependency/child tables, confirms the outcome matches exactly what the approved plan specified: one record deleted, one `Authors` link correctly unified, one `AuditLog` cross-reference written, one approval correctly transitioned to its terminal, historical state — and nothing else in the entire database was touched. The one design element this whole multi-phase investigation (Phase 4K → 4K.1 → 4L) existed to get right — a `MergeApproval` row surviving its own loser's deletion, with the historical `LoserPaperID` value still directly readable — is now confirmed working under a real execution, not merely a simulation.

Per your instructions, I am stopping here. Phase 4S is not started. No second merge was performed. No further write of any kind occurred beyond this one authorized transaction.

---

## Exact Accounting

- **Code files modified**: **0.**
- **Code files created**: **0.**
- **Test files modified**: **0.**
- **Migration files modified**: **0.**
- **Report files created**: **1** — this file (`backend/reports/phase4r_controlled_canary_execution.md`).
- **Test totals**: 235/235 passing, re-confirmed after execution, zero regressions.
- **Production DB writes**: one atomic transaction (detailed in §6).
- **Records merged**: **1.**
- **Records deleted**: **1.**
- **DOI changes**: **0.**
- **Network calls**: **0** beyond the production-database connection used throughout.
- **Automatic retries**: **0.**
- **Additional merges**: **0.**

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Identical to every phase since 4E — zero changes this phase. **This phase's real, consequential change exists entirely in the production database, not in the repository.**

Per your instructions: STOP after this report. Waiting for your review.
