# Phase 4S — Post-Execution Canary Review & Next-Batch Readiness (STRICTLY READ-ONLY)

## 1. Scope and Safety Accounting

- Code files modified: **0**
- Production DB writes: **0**
- Records merged: **0**
- Records deleted: **0**
- Approvals created: **0**
- Approval state changes: **0**
- DOI changes: **0**
- Migrations applied: **0**
- Restores performed: **0**
- Automatic retries: **0**
- Network calls: **0** beyond the production-database connections used for read-only investigation throughout.

Every finding below was independently re-derived, live, this phase — the Phase 4R summary you provided was treated as a claim to verify, not a fact to assume, exactly as instructed.

---

## 2. Independent Reconstruction (Task A)

**Directly observed live evidence, this phase:**

| # | Check | Result |
|---|---|---|
| 1 | Existence of both `ResearchPaper` IDs | `5232` exists; `5482` does not (`SELECT` returns zero rows) |
| 2 | Current state of `ApprovalID=1` | `SurvivorPaperID=5232`, `LoserPaperID=5482`, `Status=EXECUTED`, `ExecutedAt=2026-08-22 13:27:20 UTC`, `ExecutionAuditLogID=1539`, `PlanFingerprint=2298ea25...` unchanged from creation |
| 3 | Exact current `AuditLog` record for `LogID=1539` | `Action='paper.merge.dedup'`, `TargetType='ResearchPaper'`, `TargetID=5482`, `Metadata={'loser_doi': None, 'loser_title': 'Research Article Optimal Deep Learning Model...', 'loser_source': 'Scholar', 'merged_total': 61, 'kept_paper_id': 5232, 'loser_citations': 0}`, `CreatedAt=2026-08-22 13:27:20 UTC` |
| 4 | Any additional `AuditLog` records referencing this pair | Exactly one merge record (`LogID=1539`) plus one pre-existing approval-decision record (`LogID=1538`, `Action='merge_approval.approved'`, from Phase 4P) — no others, of any `Action`, reference either PaperID |
| 5 | Any additional merge attempt after the canary | **None** — `LogID=1539` is the single newest `paper.merge.dedup` row; the next-most-recent (`LogID=1354`) is dated `2026-06-07`, a historical merge that predates this entire real-execution sequence by weeks |
| 6 | Does idempotency currently block re-execution? | **Yes** — `idempotency_verdict()` (the real function, called read-only) returns `ALREADY_EXECUTED`, reason: "loser 5482 was already merged into winner 5232 (AuditLog LogID=1539)" |

**Reconstructed from artifacts, not directly observed as raw data (labeled as such):** the loser's pre-merge `Title`/`Source`/`citations` values are known only via the `AuditLog.Metadata` snapshot (`LogID=1539`) written at merge time — this is a stored artifact of the merge itself, not an independent, separately-verifiable pre-merge observation, though it was written by the same trusted, unmodified `merge_group()` code this whole project has audited extensively.

**No inference is presented as observed fact anywhere in this section.**

---

## 3. Expected vs. Actual — Field-Level Comparison (Task B)

Survivor `ResearchPaper` `PaperID=5232`, every field relevant to the merge:

| Field | Pre-merge value (evidence source) | Post-merge value (this phase, live) | Classification | Evidence basis |
|---|---|---|---|---|
| `DOI` | `10.1155/2022/8531213` (directly captured, Phase 4Q, live query minutes before execution) | `10.1155/2022/8531213` | **PRESERVED_AS_EXPECTED** | Direct before/after value diff |
| `Title` | `"Optimal deep learning model for olive disease diagnosis based on an adaptive genetic algorithm"` (Phase 4Q, live) | Identical | **PRESERVED_AS_EXPECTED** | Direct before/after value diff |
| `JournalID` | `1803` (Phase 4Q, live) | `1803` | **PRESERVED_AS_EXPECTED** | Direct before/after value diff — also structurally guaranteed: `journal_state=WINNER_ONLY` meant the executor's conditional `JournalID` backfill `UPDATE` was never issued at all (only fires for `LOSER_ONLY_BACKFILL`) |
| `PubYear` | `2022` (Phase 4Q, live) | `2022` | **PRESERVED_AS_EXPECTED** | Direct before/after value diff |
| `TenantID` | `1` (Phase 4Q, live) | `1` | **PRESERVED_AS_EXPECTED** | Direct before/after value diff |
| `CitationsByYear` | `{"2022":5,"2023":13,"2024":12,"2025":8,"2026":2}` (Phase 4Q, live) | Identical | **DETERMINISTICALLY_RESOLVED** | Direct before/after value diff — unchanged because the loser contributed `0` citations (`merge_citation_fields()`'s element-wise `MAX` correctly added nothing) |
| `Abstract`, `Abstract_En`, `PublicationType`, `VenueType`, `Language`, `Source`, `OpenAlexWorkID`, `PdfUrl`, `PdfAccessType`, `IsVerified`, `NormalizedTitle`, `Indexing` | Not individually captured as plaintext before execution this session (only folded into the pre-execution fingerprint hash, which is one-way and cannot be reversed into per-field values) | Current live values read this phase (§full dump in the raw investigation) | **PRESERVED_AS_EXPECTED** — *but on a different evidentiary basis than the fields above, stated explicitly* | **Source-code write-path analysis, not a literal value diff**: `merge_group()` (re-read this phase, unmodified since Phase 4B) issues exactly two `UPDATE "ResearchPaper"` statements in its entire body — one via `merge_citation_fields()` targeting only `"CitationsByYear"`/`"RawData_Log"`, and `merge_executor.py`'s own conditional `JournalID` backfill (which never fired for this pair, per the row above). **No code path in the entire authorized execution chain ever writes to any of these twelve columns on the survivor.** This is real, direct evidence — not a guess — but it is evidence of a different *kind* (absence of any write capability) than a literal pre/post diff, and is labeled as such per your explicit instruction not to present one kind of evidence as the other |
| `RawData_Log` | Not captured as plaintext pre-execution | Read this phase | **DETERMINISTICALLY_RESOLVED** | Same write-path basis as `CitationsByYear` — `merge_citation_fields()` updates this column's `cited_by_count` sub-key deterministically (`GREATEST` of both totals); the loser's `0` citations meant no change to the resulting value |
| `Title_En` | Not applicable to this pair (`NULL` both before, per Phase 4Q's fingerprint field list showing it unset) | `NULL` | **NOT_APPLICABLE** | Field was never populated on either side |

**No field in this table is marked `UNEXPECTED_DIFFERENCE`.** No field required `NOT_VERIFIABLE_FROM_AVAILABLE_EVIDENCE` — every field either has a direct diff or a direct, source-code-verified absence-of-write-path, and both bases are stated explicitly rather than conflated.

---

## 4. Dependency Reconciliation (Task C)

**Every real FK relationship referencing `ResearchPaper.PaperID` was independently discovered from the live schema this phase** (not assumed from any prior report's list):

```
AuthorReviewQueue.PaperID                        ON DELETE CASCADE
Authors.PaperID                                  ON DELETE NO ACTION
Citations.PaperID                                ON DELETE NO ACTION
CitationsHistory.PaperID                         ON DELETE NO ACTION
ExternalAuthors.PaperID                          ON DELETE NO ACTION
MergeApproval.SurvivorPaperID                    ON DELETE NO ACTION
PaperKeywords.PaperID                            ON DELETE NO ACTION
ReportPaperDecision.MissingResolvedToPaperID     ON DELETE SET NULL
ReportPaperDecision.PaperID                      ON DELETE SET NULL
```

This is exactly the 9-relationship set every prior phase (4F, 4K, 4L, 4M, 4N, 4Q) independently derived — the ninth re-derivation, all identical. `MergeApproval.LoserPaperID` correctly has **no** entry — confirmed absent again, this phase, directly from `information_schema`.

| Relationship | Expected action | Loser (5482) rows now | Survivor (5232) rows now | Result |
|---|---|---|---|---|
| `Authors.PaperID` | Remapped | `0` | `1` | Correct — the shared author (`UserID=97`) now links only to the survivor |
| `Citations.PaperID` | Not applicable (0 rows either side, before or after) | `0` | `0` | No-op, as expected |
| `ExternalAuthors.PaperID` | Not applicable | `0` | `0` | No-op |
| `CitationsHistory.PaperID` | Not applicable | `0` | `0` | No-op |
| `PaperKeywords.PaperID` | Not applicable | `0` | `0` | No-op — the Phase 4K schema-drift table remains harmless for this pair |
| `ReportPaperDecision.PaperID` | Not applicable | `0` | `0` | No-op |
| `ReportPaperDecision.MissingResolvedToPaperID` | Not applicable | `0` | `0` | No-op — the previously-flagged gap was never exercised, since 0 rows existed for this pair |
| `AuthorReviewQueue.PaperID` | Not applicable | `0` | `0` | No-op — the other previously-flagged gap, likewise never exercised |
| `MergeApproval.SurvivorPaperID` | Preserved | `0` | `1` | Correct — the approval row itself references the survivor |
| `MergeApproval.LoserPaperID` (no FK, by design) | **Intentionally retained historically** | `1` (`ApprovalID=1` still reads `5482`) | — | **Correct and expected — this is the Phase 4K.1 design working as intended, not an orphan.** Explicitly not classified as an integrity failure, per your instruction |

**No orphaned reference, no unexpected row loss, no unexpected duplication, and no row incorrectly still pointing at `5482` through any real FK was found.** The one place `5482` remains readable — `MergeApproval.LoserPaperID` — is exactly where it is supposed to remain.

---

## 5. Approval and Audit Lifecycle (Task D)

1. **No illegal state transition occurred**: `is_legal_transition(PENDING, APPROVED)=True`, `is_legal_transition(APPROVED, EXECUTED)=True` — both transitions this approval actually made are legal moves in the real, unmodified state machine.
2. **Correct pair still associated**: `SurvivorPaperID=5232`, `LoserPaperID=5482` — unchanged since creation.
3. **Fingerprint remains historically identifiable**: `PlanFingerprint=2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb`, unchanged since the row was created in Phase 4P.
4. **`ExecutionAuditLogID` correctly points to `1539`**: confirmed directly; `AuditLog` `LogID=1539` exists and describes exactly this merge.
5. **No duplicate approval for the same executed merge**: `MergeApproval` contains exactly one row, total, in the entire table.
6. **No second execution can currently proceed**: `idempotency_verdict()=ALREADY_EXECUTED`, checked read-only.
7. **`AuditLog` and `MergeApproval` tell a mutually consistent story**: `MergeApproval.ExecutionAuditLogID` (`1539`) matches the real `AuditLog.LogID` (`1539`); that row's `Metadata.kept_paper_id` (`5232`) matches `MergeApproval.SurvivorPaperID`; its `TargetID` (`5482`) matches `MergeApproval.LoserPaperID`. No contradiction anywhere.

`RevokedByUserID`/`RevokedAt`/`RevocationReason` are `NULL` — accurately reported as expected-by-design (a row that was never revoked has no revocation data; this is not a defect), not silently treated as one or the other without comment.

---

## 6. Blast-Radius Findings (Task E) — Exact Scope Checked

**What was checked, and what it proves:**

- **`AuditLog` activity in a ±7.5-minute window around the execution timestamp (`13:20:00`–`13:35:00 UTC`)**: exactly one row (`LogID=1539`, the merge itself). No other action of any kind — by any user, on any table — was logged in that window. This directly proves no other write-producing action (that this project's own code would log) occurred concurrently.
- **Aggregate row counts**: `ResearchPaper` `2031 → 2030` (`−1`, exactly the one authorized deletion); `MergeApproval` unchanged at `1`; `AuditLog` `957 → 958` (`+1`, exactly the one new record). These counts are internally consistent with exactly one deletion and no other insertion/deletion anywhere in these three tables.
- **Spot-check of 18 unrelated `ResearchPaper` rows** (every paper in the other 9 forensic candidate pairs, §7 below): all still present, none accidentally deleted or touched.
- **The survivor's own protected fields**: individually verified unchanged (§3).

**What was explicitly NOT exhaustively checked, and is stated as such rather than overclaimed**: this phase did not perform a full byte-for-byte scan of the content of all 2,030 remaining `ResearchPaper` rows, nor a full scan of every row in `Authors`/`Citations`/etc. beyond the ones directly relevant to this pair. The aggregate-count consistency plus the zero-concurrent-activity finding together constitute strong, direct evidence that nothing else was touched — but the *exact scope* checked is: the executed pair's own rows and dependencies, the aggregate counts of the three directly-affected tables, and 18 spot-checked unrelated papers. No claim is made beyond that scope.

**No cross-tenant modification occurred**: the executed pair was `TenantID=1` on both sides throughout; `TenantID` was never a target of any write in this execution.

---

## 7. Real-World Executor Validation Matrix (Task F)

| Assumption | Classification | Basis |
|---|---|---|
| Transaction ordering (remaps → `AuditLog` → delete → `MergeApproval` `EXECUTED`) | **CONFIRMED_BY_REAL_EXECUTION** | The exact sequence committed atomically in production, exactly as designed since Phase 4J |
| Row locking — lock acquisition itself | **CONFIRMED_BY_REAL_EXECUTION** | `lock_pair_rows()` succeeded with no error inside the real transaction |
| Row locking — protection under real concurrent contention | **NOT_TESTED_BY_THIS_SUCCESSFUL_CANARY** | Only a single, uncontended execution occurred; no concurrent workload existed to actually test lock contention or deadlock avoidance |
| Fingerprint stability (Django vs. `psycopg2` JSON normalization, the Phase 4P.1 fix) | **CONFIRMED_BY_REAL_EXECUTION** | This is precisely what Phase 4P found broken and Phase 4P.1 fixed — the real execution's own internal preflight recomputed the fingerprint via Django's connection and it matched, or the execution could not have succeeded at all |
| Approval lifecycle (`PENDING → APPROVED → EXECUTED`) | **CONFIRMED_BY_REAL_EXECUTION** | All three states were real, live, production transitions, not simulated |
| Idempotency — detection mechanism | **CONFIRMED_BY_REAL_EXECUTION** | A real historical `AuditLog` row now exists, and `idempotency_verdict()` correctly classifies it `ALREADY_EXECUTED` |
| Idempotency — a live second `execute_approved_merge()` call is actually refused end-to-end | **STILL_REQUIRES_SEPARATE_TESTING** | Deliberately never attempted, per explicit instruction — the detection mechanism is proven, but the full refusal path was not exercised against a real second call |
| `AuditLog` behavior | **CONFIRMED_BY_REAL_EXECUTION** | Real row written with correct `Action`/`TargetID`/`Metadata`, correctly cross-referenced from `MergeApproval.ExecutionAuditLogID` |
| FK lifecycle / no-FK `LoserPaperID` design | **CONFIRMED_BY_REAL_EXECUTION** | This is the headline result: `MergeApproval` survived the real `DELETE` of its loser, `LoserPaperID=5482` remains directly readable — exactly what Phase 4K found impossible and Phase 4K.1 fixed, now proven under real execution rather than simulation |
| `JournalID` handling — `WINNER_ONLY` (no-op) path | **CONFIRMED_BY_REAL_EXECUTION** | Exercised, correctly resulted in zero backfill write |
| `JournalID` handling — `LOSER_ONLY_BACKFILL` (actual backfill `UPDATE`) path | **NOT_TESTED_BY_THIS_SUCCESSFUL_CANARY** | This specific pair never required a backfill; the conditional `UPDATE "ResearchPaper" SET "JournalID"` statement itself has never yet executed against production |
| `AuthorNameRaw` handling — zero-conflict pass-through | **CONFIRMED_BY_REAL_EXECUTION** | This pair had none; the pass-through path was exercised |
| `AuthorNameRaw` handling — conflict-detected blocking path | **NOT_TESTED_BY_THIS_SUCCESSFUL_CANARY** | Never exercised against production (only in mocked tests) — §8 below identifies real candidates that would exercise it |
| Dependency-gap guards (`AuthorReviewQueue`/`ReportPaperDecision.MissingResolvedToPaperID`) — zero-rows pass-through | **CONFIRMED_BY_REAL_EXECUTION** | Both were `0` for this pair; the pass-through was exercised |
| Dependency-gap guards — actual blocking behavior | **NOT_TESTED_BY_THIS_SUCCESSFUL_CANARY** | Neither gap has ever been populated for any pair audited across this entire project; the `BLOCK` path itself remains unexercised against production |
| Rollback behavior | **NOT_TESTED_BY_THIS_SUCCESSFUL_CANARY** | The execution succeeded; no rollback occurred. Per your explicit instruction, success does not prove rollback — that remains proven only by the mocked failure-injection tests (Phase 4J/4K.1), not by this real execution |

---

## 8. Next-Batch Candidate Table (Task G) — Recommendation Only, No Approvals Created

All 9 remaining pairs from the original forensic set were re-checked live, fresh, this phase — not reused from Phase 4C's original (pre-fingerprint-era) classification.

| Pair (survivor→loser) | Both exist? | `pair_confidence` | DOI state | Fresh fingerprint (no prior approval exists to compare against — this is the first-ever computation for these pairs) | `JournalID` decision | `AuthorNameRaw` conflicts | Dependency risk | Tenant | Classification |
|---|---|---|---|---|---|---|---|---|---|
| 5207 → 5481 | Yes | high | winner has DOI, not claimed elsewhere | `b608b3d4...` | `WINNER_ONLY` | **1 conflict** (`UserID=97`, minor punctuation variant) | none | 1/1 | **HUMAN_REVIEW_REQUIRED** |
| 5548 → 5549 | Yes | high | winner has DOI, not claimed elsewhere | `27f3ad7c...` | `LOSER_ONLY_BACKFILL` (deterministic) | **1 conflict** (`UserID=104`) | none | 1/1 | **HUMAN_REVIEW_REQUIRED** |
| 6086 → 6088 | Yes | high | winner has DOI, not claimed elsewhere | `028fa030...` | `LOSER_ONLY_BACKFILL` (deterministic) | **1 conflict** (`UserID=105`, capitalization variant) | none | 1/1 | **HUMAN_REVIEW_REQUIRED** |
| 6153 → 6189 | Yes | high | winner has DOI, not claimed elsewhere | `8df151f4...` | `WINNER_ONLY` | **1 conflict** (`UserID=112`) | none | 1/1 | **HUMAN_REVIEW_REQUIRED** |
| 5329 → 5434 | Yes | high | winner has DOI, not claimed elsewhere | `da340186...` | `WINNER_ONLY` | **none** | none | 1/1 | **READY_FOR_NEW_APPROVAL** |
| 3875 → 6091 | Yes | high | winner has DOI, not claimed elsewhere | `c1221c70...` | `WINNER_ONLY` | **none** | none | 1/1 | **READY_FOR_NEW_APPROVAL** |
| 6645 → 7572 | Yes | high | winner has DOI, not claimed elsewhere | `cb690780...` | `WINNER_ONLY` | **none** | none | 1/1 | **READY_FOR_NEW_APPROVAL** |
| 5289 → 5392 | Yes | high | winner has DOI, not claimed elsewhere | `02aefcda...` | `WINNER_ONLY` | **none** | none | 1/1 | **READY_FOR_NEW_APPROVAL** |
| 6107 → 6109 | Yes | high | winner has DOI, not claimed elsewhere | `88c1418c...` | `LOSER_ONLY_BACKFILL` (deterministic) | **1 conflict** (`UserID=69`) — **note: this one is not a minor formatting variant; the loser's `AuthorNameRaw` value contains what appears to be multiple additional co-author names folded into one field, substantively different from the winner's single-name value, not just capitalization/spacing** | none | 1/1 | **HUMAN_REVIEW_REQUIRED** — flagged as needing closer attention than the other four conflict cases |

**Idempotency**: all 9 pairs — `NOT_PREVIOUSLY_EXECUTED`. **Dependency-table risk**: all 9 pairs — `check_unhandled_dependency_gaps()` returns `[]`. **`hard_exclusion_reason`**: `None` for all 9. **Human approval required**: yes, for every single one of these — including the `READY_FOR_NEW_APPROVAL` four — technical cleanliness is a precondition for approval, never a substitute for it; nothing here proposes skipping the approval workflow for any candidate. **`BLOCKED`**: none of the 9 — no hard safety or integrity blocker was found for any pair.

**4 candidates are genuinely `READY_FOR_NEW_APPROVAL`: `(5329, 5434)`, `(3875, 6091)`, `(6645, 7572)`, `(5289, 5392)`.** This is not a target quota being filled — it is the exact, honest count of pairs with zero unresolved conflicts found this phase; the other 5 are real, unresolved semantic conflicts requiring a human decision on which `AuthorNameRaw` variant to keep, not a technical defect. **No approval was created for any of these.**

---

## 9. Final Verdict

### **A) CANARY FULLY VALIDATED — READY TO PLAN A SEPARATE SMALL CONTROLLED BATCH**

Every claim in the Phase 4R summary was independently re-verified against fresh, live evidence this phase and found accurate, with zero discrepancy. The dependency reconciliation, approval/audit lifecycle, and blast-radius checks all confirm a clean, fully-contained, single-pair execution with no orphaned references, no unrelated changes, and a mutually consistent audit trail. The real-world validation matrix (§7) is precise about what this one execution did and did not prove — several real assumptions (concurrent locking, `JournalID` backfill, `AuthorNameRaw`-conflict blocking, dependency-gap blocking, rollback) remain genuinely untested against production and are named explicitly, not glossed over. Four candidate pairs were found technically ready for a future approval workflow, and five were found to have genuine, unresolved author-identity conflicts requiring human judgment — an honest split, not a manufactured one.

This is not verdict **C** — no integrity issue of any kind was found. This is not verdict **B** in the sense of blocking further planning — every question this phase was asked to answer was answered with direct evidence; what remains open (§7's `NOT_TESTED` items) is material for a *future* controlled test, not an unresolved question about *this* canary's own correctness.

---

Per your instructions, I am stopping here. Phase 4T is not started. No approval was created for any candidate. No additional merge was executed. Waiting for your explicit review and authorization.
