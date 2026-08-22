# Phase 4T — Post-Canary Execution Review & Coverage Gap Audit (STRICTLY READ-ONLY)

## Purpose

Determine exactly what the first real production merge (5232 ← 5482, `ApprovalID=1`, `AuditLogID=1539`) actually proved, and — more importantly — what it did **not** prove, before any consideration of scaling beyond one canary. No merge was executed. No approval was created, approved, or revoked. No code was modified (none was needed — this is a forensic/audit phase, and every finding below was reachable through inspection alone).

---

## 1. Safety Accounting

- Code files modified: **0**
- Code files created: **0**
- Report files created: **2** (this file, `backend/reports/phase4t_branch_coverage_matrix.json`)
- DB writes: **0**
- Network calls: **0** beyond the production-database connections used for read-only investigation
- Records merged: **0**
- Records deleted: **0**
- Approvals created: **0**
- Approvals changed: **0**
- DOI changes: **0**

---

## 2. Clean-Room Production Reconstruction (Task A)

Every item below was independently re-derived this phase, without assuming any prior report's conclusion:

| # | Check | Result | Classification |
|---|---|---|---|
| 1 | `PaperID 5232` exists | `SELECT` returns exactly one row | **DIRECTLY_PROVEN** |
| 2 | `PaperID 5482` does not exist | `SELECT` returns zero rows | **DIRECTLY_PROVEN** |
| 3 | `ApprovalID=1` — pair identity, `Status`, `PlanFingerprint`, `ExecutionAuditLogID`, preserved `LoserPaperID` | `SurvivorPaperID=5232`, `LoserPaperID=5482`, `PlanFingerprint=2298ea25...`, `Status=EXECUTED`, `ExecutionAuditLogID=1539` — all read directly from the live row | **DIRECTLY_PROVEN** |
| 4 | `AuditLogID=1539` content | `Action='paper.merge.dedup'`, `TargetType='ResearchPaper'`, `TargetID=5482`, `Metadata.kept_paper_id=5232` — read directly | **DIRECTLY_PROVEN** |
| 5 | Merge record semantically corresponds to `5232 ← 5482` | `Metadata.kept_paper_id (5232) == MergeApproval.SurvivorPaperID (5232)`; `AuditLog.TargetID (5482) == MergeApproval.LoserPaperID (5482)` — both cross-checked directly | **DIRECTLY_PROVEN** |
| 6 | No contradictory merge history for either ID | Exactly one `AuditLog` row (any `Action`, either `TargetID`) matches `paper.merge.dedup` for `TargetID=5482`; none for `5232`; no other row references either ID under any action | **DIRECTLY_PROVEN** |
| 7 | Current idempotency verdict | `idempotency_verdict()` (real function, read-only call) → `ALREADY_EXECUTED` | **DIRECTLY_PROVEN** |
| 8 | Current schema, re-derived fresh (not from any prior report's list) | `MergeApproval` has 18 columns; its only FK columns are `SurvivorPaperID`, `TenantID`, `ReviewedByUserID`, `RevokedByUserID`, `ExecutionAuditLogID` — `LoserPaperID` confirmed absent from the FK list | **DIRECTLY_PROVEN** |

**Zero contradictions found. Nothing in this section required code-path inference — every result above came from a direct database read this phase.**

---

## 3. What the First Canary Actually Proved

Directly, from real production evidence (§2, and the branch matrix in §5/`phase4t_branch_coverage_matrix.json`):

- The full approval lifecycle through `EXECUTED` is real and works.
- Fingerprint binding — including the Phase 4P.1 Django/`psycopg2` JSON-normalization fix — is real and works; the executor's own internal preflight recomputed the fingerprint via Django's connection during the actual execution and it matched.
- The complete preflight sequence, exercised end-to-end against real, live data, correctly permitted execution.
- Transaction ordering for the successful path (remaps → `AuditLog` → delete → `MergeApproval` `EXECUTED`) is real and committed atomically.
- The FK-free `LoserPaperID` design works under a real `DELETE`, not just a simulation — this was the single most consequential unproven assumption before this canary, and it is now proven.
- The loser row was actually deleted; the survivor actually remains.
- A real, correctly-shaped `AuditLog` row was written.
- Child-table reconciliation was exercised for the one dependency this pair actually had data in (`Authors` — 1 row remapped, correctly unified).
- Post-execution idempotency detection correctly identifies the pair as `ALREADY_EXECUTED`.

---

## 4. What Remains Unproven

Directly named, not glossed over (full detail in §5 and the JSON matrix):

- `JournalID` `LOSER_ONLY_BACKFILL` — the actual `UPDATE "ResearchPaper" SET "JournalID"` statement has never executed against production; this canary was `WINNER_ONLY`.
- `AuthorNameRaw` conflict rejection — never exercised against production; this canary had zero conflicts.
- Rollback after a failure inside a real transaction — no failure occurred; rollback remains proven only by mocked tests.
- Dependency-gap blocking with populated `AuthorReviewQueue` or `ReportPaperDecision.MissingResolvedToPaperID` — neither has ever been nonzero for any pair audited across this entire project; the `BLOCK` path itself remains unexercised against production.
- Real concurrent execution protection — only a single, uncontended execution occurred.
- **Cross-tenant rejection — a genuinely new, more serious finding than "untested."** See §5's item 20: no code anywhere in the approval-creation or execution path actually checks that `SurvivorPaperID` and `LoserPaperID` belong to the same tenant. `TenantID` is a fingerprinted field (so a cross-tenant fingerprint would differ from one computed after a tenant changed), and `merge_plan_generator.compute_classification()`'s `tenant_blocked` check exists — but `execute_approved_merge()` never calls `compute_classification()`, and neither `merge_approval.py` nor `merge_executor.py` contains any tenant-equality check of its own. This is not merely "untested" — it appears to be **unenforced** at the point that actually matters (approval creation and execution), confirmed by exhaustive `grep` across all three files. Flagged here explicitly, not softened.
- Approval revocation/rejection lifecycle against production — `PENDING → REJECTED` and `APPROVED → REVOKED` have never been driven to completion against a real `MergeApproval` row (deliberately avoided in Phase 4P to preserve the one real approval for this canary).
- A second successful merge with a materially different dependency/field shape — this canary's `Authors` remap (1 row) was its only nonzero dependency; every other dependency table was `0/0`.

---

## 5. Branch-Coverage Matrix Summary

Full detail in `backend/reports/phase4t_branch_coverage_matrix.json`. Summary:

| # | Branch | Code location | Unit-tested | Real-canary-exercised | Production-proven |
|---|---|---|---|---|---|
| 1 | Successful merge | `merge_executor.py:204` (full function) | Yes (mocked) | **Yes** | **Yes** |
| 2 | Self-merge rejection | `merge_executor.py:235` | Yes | No (n/a — real pair wasn't a self-merge) | No |
| 3 | Permission rejection | `merge_executor.py:250` | Yes | No | No |
| 4 | Missing approval | `merge_executor.py:255` | Yes | No | No |
| 5 | Reversed approval direction | `merge_executor.py:265` | Yes | No | No |
| 6 | Approval not `APPROVED` | `merge_executor.py:270-ish` (`EXEC_BLOCKED_APPROVAL_NOT_APPROVED`) | Yes | No | No |
| 7 | Stale fingerprint | `merge_execution_safety.py:359` (`validate_against_plan`) | Yes | No (n/a — fingerprint matched) | No |
| 8 | Duplicate safety failure | `merge_execution_safety.py:365` | Yes | No | No |
| 9 | DOI safety failure | `merge_execution_safety.py:370` | Yes | No | No |
| 10 | `ALREADY_EXECUTED` | `merge_executor.py` (`EXEC_BLOCKED_ALREADY_EXECUTED`) | Yes | No (n/a during execution) — **but now DIRECTLY observable read-only, post-execution** | **Partially — the detection is now proven live (§2, item 7); the executor's own refusal of a second live attempt was never exercised** |
| 11 | `HISTORICAL_STATE_AMBIGUOUS` | `merge_executor.py` (`EXEC_BLOCKED_HISTORY_AMBIGUOUS`) | Yes (`merge_execution_safety.py` level) | No | No |
| 12 | `JournalID` `EQUAL` | `dedup_papers.py:853` | Yes | No | No |
| 13 | `JournalID` `WINNER_ONLY` | `dedup_papers.py:849` | Yes | **Yes** | **Yes** |
| 14 | `JournalID` `LOSER_ONLY_BACKFILL` | `dedup_papers.py:851`; executed via `merge_executor.py:348` `UPDATE` | Yes (mocked, `test_K`) | No | No |
| 15 | `JournalID` `CONFLICT` | `dedup_papers.py:855` | Yes (`test_journal_id_conflict_state_blocks_before_merge`, Phase 4K) | No | No |
| 16 | `AuthorNameRaw` no conflict | `dedup_papers.py::author_content_conflicts()` | Yes | **Yes** | **Yes** |
| 17 | `AuthorNameRaw` formatting conflict | Same function, `EXEC_BLOCKED_AUTHOR_CONFLICT` | Yes (`test_L`) | No | No |
| 18 | Populated `AuthorReviewQueue` gap | `merge_executor.py:170` (`check_unhandled_dependency_gaps`) | Yes (`test_nonzero_authorreviewqueue_rows_block`) | No (0 rows, every pair, project-wide) | No |
| 19 | Populated `ReportPaperDecision.MissingResolvedToPaperID` gap | Same function | Yes | No (0 rows, project-wide) | No |
| 20 | Cross-tenant rejection | **No dedicated check found anywhere in the approval/execution path** — only `merge_plan_generator.compute_classification()`, which `execute_approved_merge()` never calls | Yes, at the `merge_plan_generator.py` level only (`test_merge_plan_generator.py:256`) | No | **No — and the enforcement point itself is unconfirmed to exist at execution time** |
| 21 | Transaction failure and rollback | `merge_executor.py`, multiple points (§7) | Yes (`test_M`/`test_N`/`test_O`/`test_P`, mocked) | No — the real execution succeeded | No |
| 22 | Concurrent execution attempt | `lock_pair_rows()`, `merge_execution_safety.py:308` | No dedicated test beyond a single-threaded "missing row" simulation | No | No |

**Only branches 1, 13, and 16 are production-proven.** Every other branch remains unit-tested-only or, in one case (#20), potentially unenforced at the point that matters. This is stated precisely, not rounded up.

---

## 6. Preservation-Evidence Correction (Task C)

Phase 4S's field table used the labels `PRESERVED_AS_EXPECTED` and `DETERMINISTICALLY_RESOLVED` without formally separating *how* each was known. This section supplies the missing rigor and issues one explicit correction.

| Field | Preservation evidence class | Basis |
|---|---|---|
| `DOI`, `Title`, `JournalID`, `PubYear`, `TenantID` (survivor) | **A. BEFORE_AFTER_VALUE_PROOF** | Literal values captured live pre-execution (Phase 4Q, minutes before the merge) and re-read live post-execution (this phase) — a genuine before/after diff exists for these five fields specifically, and only these five |
| `CitationsByYear`/`RawData_Log.cited_by_count` (survivor) | **C. MERGE_RECONCILIATION_PROOF** | `merge_citation_fields()` (real, unmodified code) explicitly executed an element-wise `MAX`/`GREATEST()` reconciliation; the outcome happened to be a no-op because the loser contributed `0` citations — but the reconciliation *logic* genuinely ran, this is not merely "untouched" |
| `Abstract`, `Abstract_En`, `PublicationType`, `VenueType`, `Language`, `Source`, `OpenAlexWorkID`, `PdfUrl`, `PdfAccessType`, `IsVerified`, `NormalizedTitle`, `Indexing` (survivor) | **B. SQL_PATH_NON_MODIFICATION_PROOF** | No literal pre-execution value was ever captured for these fields in any report — the evidence is exclusively that `merge_group()`/`merge_executor.py`'s write path contains no `UPDATE` statement touching any of them (source-verified, this phase, by re-reading `merge_executor.py:340-411` directly). This is real, direct evidence of a different *kind* than A — absence-of-write-capability, not a value diff — and must not be described as equivalent to A |
| `Title_En` | **D. NOT_PROVEN** (trivial case) | `NULL` on both sides, before and after — nothing to preserve or lose |
| **The loser's own distinct field values** (its own `Abstract`, `PublicationType`, `PdfUrl`, etc. — whatever they actually were) | **D. NOT_PROVEN — explicitly discarded with the loser, not preserved anywhere** | **This is the correction.** The loser row is deleted. Its own field values were never captured in full (only `Title`/`DOI`/`Source`/`citations` survive, inside `AuditLog.Metadata`, as a lossy snapshot — not the full row). No backfill, reconciliation, or transfer of any loser-side field occurred for this pair beyond the citations merge, because this canary's `journal_state` was `WINNER_ONLY` (no `JournalID` backfill needed) and its `doi_state` required no action (winner already had a DOI) — **there was, in fact, no field on this specific pair where the loser held a value the winner lacked.** Stating that the loser's fields were "preserved" would be actively misleading; the correct statement is that **nothing needed to be transferred for this pair**, and what *would* have needed transferring (a `LOSER_ONLY_BACKFILL`-shaped scenario) remains, per §4, unexercised in production |

**Corrected terminology going forward**: "preserved" should be reserved for fields with class-A evidence; class-B fields should be described as "never targeted by any write in the execution path" (a code-level guarantee, not an observed outcome); loser-side values that were never transferred should be described as "discarded with the loser," not "preserved" in any sense.

---

## 7. Rollback/Failure-Point Evidence Matrix (Task E)

Traced directly from `merge_executor.py`'s actual write sequence (re-read in full this phase, lines 340–411) — **no failure was deliberately induced against production; this section answers "what guarantees rollback if this exact step had failed," not "rollback was observed."**

| # | Write step | If this step failed after all preceding steps succeeded, what guarantees rollback? | Classification |
|---|---|---|---|
| 1 | `JournalID` backfill `UPDATE` (conditional, did not fire this canary) | The surrounding caller's `transaction.atomic()` (re-confirmed: `merge_executor.py` itself never calls `.commit()`/`.rollback()`/opens its own transaction — `TransactionOwnershipTests`, static source scan) | **PROVEN_BY_TEST** (mocked equivalent not directly present for this specific step, but the no-internal-commit guarantee that makes it safe is statically proven) + **INFERRED_FROM_CODE** for this specific line |
| 2 | `merge_group()`'s `Authors` remap (`INSERT ... ON CONFLICT`, `DELETE`) | Same `transaction.atomic()` | **PROVEN_BY_TEST** — `test_J_child_remap_failure_rolls_back` injects a failure at exactly `INSERT INTO "AUTHORS"` and asserts `RuntimeError` propagates, loser row still present |
| 3 | `merge_group()`'s `Citations`/`CitationsHistory`/etc. remaps | Same | **PROVEN_BY_TEST** — same mechanism, not independently injected per-table, but the shared exception-propagation path is what's actually being tested, not the specific SQL text |
| 4 | `merge_citation_fields()`'s `UPDATE "ResearchPaper" SET "CitationsByYear"...` | Same | **INFERRED_FROM_CODE** — no dedicated mocked test injects a failure at this exact statement, though the same propagation mechanism applies |
| 5 | `merge_group()`'s `AuditLog` `INSERT` | Same | **PROVEN_BY_TEST** — `test_N_auditlog_failure_rolls_back` |
| 6 | `merge_group()`'s `ResearchPaper` `DELETE` | Same | **PROVEN_BY_TEST** — `test_O_delete_failure_rolls_back` |
| 7 | The `SELECT "LogID" FROM "AuditLog" ...` lookup (read-only, but could theoretically raise) | Same | **INFERRED_FROM_CODE** — a read-only `SELECT` failing is not a meaningfully distinct scenario from any other exception; not separately tested |
| 8 | `MergeApproval` `EXECUTED` `UPDATE` | Same | **PROVEN_BY_TEST** — `test_M_approval_executed_update_failure_rolls_back` |
| 9 | Final invariant `SELECT`s (survivor-exists / loser-gone checks) | Same — and these themselves *raise* `RuntimeError` if the invariant is violated, which is the intended failure-detection behavior, not a bug | **PROVEN_BY_TEST** — implicitly covered by the same test suite's assertion that no false success is ever reported (`test_P`) |

**Overarching guarantee**: every one of the above ultimately rests on **PROVEN_BY_DATABASE_TRANSACTION_STRUCTURE** — Postgres's own atomicity guarantee for a transaction that is never explicitly committed until every step succeeds — combined with **PROVEN_BY_TEST** evidence (Phase 4J/4K.1's mocked failure-injection suite) that no exception anywhere in this call chain is ever caught and swallowed. **No real rollback occurred during the actual canary execution — it does not need to, since nothing failed — and this section does not claim otherwise.** The real canary proves the *happy path*'s transaction boundary is correct; it does not and cannot prove the *failure path*, which remains proven only by mocked tests.

---

## 8. Scaling-Readiness Decision (Task F)

### **A) READY FOR A SECOND, DIFFERENT-SHAPE CONTROLLED CANARY**

Not **B** — a batch (even a small one) would exercise multiple pairs simultaneously without first proving any of the specific unexercised branches (§4/§5) individually; if something in one of those branches were wrong, a batch would risk hitting it multiple times before being caught, rather than isolating exactly one new code path per controlled step, which has been this project's entire discipline since Phase 4A. Not **C**/**D** — nothing found this phase requires further forensics-only work or a design/implementation fix before proceeding; every gap identified is a *coverage* gap (a real branch that is correctly implemented and unit-tested, simply not yet exercised against production), except item #20 (cross-tenant), which is a **finding to carry forward and explicitly avoid triggering**, not a defect requiring a code change in this phase (no cross-tenant pair is anywhere near being proposed).

**The exact properties the second canary must have, to genuinely cover new ground:**

The single most valuable next test is a pair that exercises the `JournalID` `LOSER_ONLY_BACKFILL` path plus a real, live-executed `Authors`/citation reconciliation — because it is both a real, designed write path (`merge_executor.py:348`'s conditional `UPDATE`) that has **never once executed against production**, and it was explicitly investigated and forensically classified as safe back in Phase 4A–4C.

**Recommended candidate, drawn from Phase 4S's own live re-verification (not re-investigated fresh this phase — flagged as reused, pending a final live re-check immediately before any future approval)**: **`(6086, 6088)`** — survivor `6086`, loser `6088`. Phase 4S found this pair's `journal_state = LOSER_ONLY_BACKFILL` (a real, deterministic backfill would fire — the one branch this canary never touched) **and** a genuine `AuthorNameRaw` conflict (`UserID=105`, a capitalization-only variant: `"MH Al-adaileh"` vs. `"MH Al-Adaileh"`). This pair would therefore, if a human reviewer resolves the naming conflict and approves it, exercise **two** previously-unexercised branches in one controlled step: the `LOSER_ONLY_BACKFILL` write path and (assuming the conflict is judged resolved/non-blocking by a human, not silently bypassed by code) the human-decision boundary itself.

**Alternative, simpler candidate if a single-new-branch test is preferred over a two-branch one**: `(5548, 5549)` or `(6107, 6109)` — both also `LOSER_ONLY_BACKFILL`-shaped, both also carry one `AuthorNameRaw` conflict each (Phase 4S).

**No approval was created for any of these. No execution was performed. This is a recommendation only, exactly as instructed.**

---

## 9. Regression and Repository Integrity (Task G)

1. **No production writes occurred during this phase** — confirmed; every query this phase issued was a plain `SELECT`, and every connection was left uncommitted/rolled back.
2. **No merge was executed** — confirmed.
3. **No approval state changed** — confirmed; `ApprovalID=1` was read multiple times, `Status=EXECUTED` throughout, unchanged.
4. **No migration was applied** — confirmed; no DDL of any kind was issued.
5. **No DOI changed** — confirmed; the survivor's DOI was read, not written.
6. **`git status`/`git diff`**: identical to every phase since Phase 4E for the two tracked files; no untracked file in `backend/tools/` was modified this phase (verified via `git status --short`, no new `M` markers beyond the two pre-existing, unrelated ones). Pre-existing unrelated changes (the two tracked files, and every other untracked file predating this project) are unchanged; this phase's only repository additions are the two new report files.
7. **No code changed** — per your explicit instruction, no test suite was re-run merely to "inflate coverage." The one confirmation run performed (§below) exists solely to establish the accurate, current baseline this report's branch matrix depends on — not as evidence of new work.

**Test suite (baseline confirmation only, no code change preceded it):**

```
test_dedup_papers.py             18/18
test_merge_plan_generator.py     43/43
test_merge_execution_safety.py   79/79
test_merge_approval.py           45/45
test_merge_executor.py           39/39
test_fk_lifecycle.py             11/11
```

**Total: 235/235 — unchanged from Phase 4P.1/4Q/4R/4S. Zero regressions, because zero code changed.**

---

## Final Recommended Next Phase

**A narrowly-scoped Phase 4U**, gated on your explicit authorization, whose job is: (1) a final, fresh, live re-verification of the recommended candidate pair (`6086`/`6088`, or one of the alternatives), re-running every check from Phase 4Q's own methodology against current data (not reusing Phase 4S's numbers, which are now hours old); (2) a real, human review-and-decision on the specific `AuthorNameRaw` conflict found; (3) if and only if that human review resolves the conflict, creation of exactly one new `PENDING` approval for that pair; (4) stop — approval and execution remain separate, explicitly-authorized phases, exactly as this project's entire discipline has required since Phase 4H.

Per your instructions, I am stopping here. Phase 4U is not started. No merge was executed. No approval was created. Waiting for your explicit review and authorization.
