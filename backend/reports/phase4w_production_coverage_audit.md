# Phase 4W — Production Coverage Audit & Rollout Decision (STRICTLY READ-ONLY)

## Executive Summary

Both previously reported canaries are independently re-confirmed, fresh, with zero discrepancy: `ApprovalID=1` (5232←5482, `AuditLogID=1539`) and `ApprovalID=2` (3875←6091, `AuditLogID=1541`) are both `EXECUTED`, their losers are gone, their survivors are present and unmodified, and zero orphaned child rows reference either deleted ID across all 8 relevant tables. Of 41 assessed safety branches, **6 are genuinely production-proven, 2 are production-proven-blocking-equivalent (idempotency detection), 26 are test-proven-only, 3 are not reachable by any current candidate, 3 are blocked by current policy (correctly), and 1 is a confirmed, carried-forward, low-severity data-quality gap** (execution-time user attribution in `AuditLog`). A fresh, full-corpus candidate re-scan found 4 clean, safe, immediately-approvable candidates and 8 more blocked by real `AuthorNameRaw` conflicts — including, critically, **all 3 currently-known `JournalID=LOSER_ONLY_BACKFILL` candidates**. Investigating that bottleneck directly (Task D) found that an existing, already-used-elsewhere `normalize_name()` function would resolve exactly **one** of those three conflicts as pure formatting noise — but wiring it into the safety check would mean adopting name-based fuzzy matching for an attribution-safety decision, which directly conflicts with this project's own documented, hard-won policy (`CLAUDE.md`'s 602-paper cross-contamination history: "Never name-based fuzzy matching for cross-attribution"). This is not a quick fix; it is a real design/policy question for a separate phase. **Final decision: A) READY FOR LIMITED SAFE ROLLOUT**, scoped narrowly to the already-twice-proven clean class, with `LOSER_ONLY_BACKFILL` and conflict-bearing candidates explicitly excluded pending Task D's identified follow-up work.

---

## Task A — Independently Verified Production State

| # | Check | Result | Evidence type |
|---|---|---|---|
| 1 | Current `ResearchPaper` count | **2,029** | Directly observed |
| 2 | `5482`/`6091` absent | Both confirmed absent (`SELECT` returns zero rows for either) | Directly observed |
| 3 | `5232`/`3875` present | Both confirmed present | Directly observed |
| 4/5/6 | `ApprovalID=1`/`2` status, identity, `ExecutionAuditLogID` | `1`: survivor `5232`, loser `5482`, `Status=EXECUTED`, `ExecutionAuditLogID=1539`. `2`: survivor `3875`, loser `6091`, `Status=EXECUTED`, `ExecutionAuditLogID=1541`. Both `PlanFingerprint` values unchanged from creation | Directly observed |
| 7 | Corresponding `AuditLog` records exist | `LogID=1539`: `Action='paper.merge.dedup'`, `TargetID=5482`, `Metadata.kept_paper_id=5232`. `LogID=1541`: `TargetID=6091`, `Metadata.kept_paper_id=3875`. Both correct | Directly observed |
| 8 | No unexpected duplicate execution records | Exactly one `paper.merge.dedup` row per loser ID, no others | Directly observed |
| 9 | No orphaned child rows referencing either deleted loser | Zero, across all 8 relevant tables (`Authors`, `Citations`, `ExternalAuthors`, `CitationsHistory`, `ReportPaperDecision`×2 columns, `AuthorReviewQueue`, `PaperKeywords`) | Directly observed |
| 10 | Same-tenant status of both historical pairs | Survivors: `TenantID=1` (both), directly observed, current. `MergeApproval.TenantID`: `1` (both), code-derived (stored at creation time by the caller, cross-checked against the tenant-validated papers at that time). **Losers' `TenantID`: unprovable-after-deletion directly** — both rows are gone; the only evidence is historical (Phase 4Q's live pre-execution capture for `5482`, Phase 4V's Task C live capture for `6091`, both `TenantID=1`, both independently gated by Phase 4U's `validate_same_tenant()` at the time of approval creation and again at execution) | Mixed: directly observed (survivor) + code-derived (approval row) + historical inference (loser, unprovable after deletion) |
| 11 | Current `MergeApproval` row count/status distribution | `2` rows, both `EXECUTED`, zero other states present | Directly observed |
| 12 | Current schema matches `merge_approval.py`/`merge_executor.py` assumptions | `MergeApproval`: 18 columns, unchanged; `LoserPaperID` still confirmed absent from the FK list (Phase 4K.1's design intact); all other FKs unchanged | Directly observed |

**Zero discrepancies found between the claimed history and independently-verified fact.**

---

## Task B — Complete Production Coverage Matrix

| # | Branch | Classification | Evidence |
|---|---|---|---|
| 1 | Normal clean merge | **A. PRODUCTION_PROVEN** | Both canaries |
| 2 | Approval lifecycle `PENDING→APPROVED→EXECUTED` | **A. PRODUCTION_PROVEN** | Both approvals, full lifecycle, twice |
| 3 | Approval rejection | **C. TEST_PROVEN_ONLY** | `test_merge_approval.py`; never exercised against a real production row |
| 4 | Approval revocation | **C. TEST_PROVEN_ONLY** | Same; deliberately avoided in production to preserve real approval rows as evidence |
| 5 | Approval fingerprint-mismatch blocking | **C. TEST_PROVEN_ONLY** | `test_fingerprint_mismatch_blocks_approval` et al.; never triggered live |
| 6 | Stale-plan blocking | **C. TEST_PROVEN_ONLY** | `test_F_stale_fingerprint_zero_write_sql`; the underlying Phase 4P.1 *defect* this guards against was real and production-observed once (Django/psycopg2 mismatch), but the guard's *correct-refusal* behavior itself was never exercised live post-fix |
| 7 | Django vs. `psycopg2` fingerprint consistency | **A. PRODUCTION_PROVEN** | Directly verified byte-identical, live, for both canaries' exact pairs (Phase 4P.1, Phase 4Q, Phase 4V's Task C) |
| 8 | Self-merge rejection | **C. TEST_PROVEN_ONLY** | Extensively mocked; never a real candidate in production (no code path ever proposes `winner_id==loser_id`) |
| 9 | Same-tenant enforcement at approval creation | **A. PRODUCTION_PROVEN** (allow path only) | Both real approvals passed this check live, using real `TenantID` data |
| 10 | Same-tenant enforcement inside executor | **A. PRODUCTION_PROVEN** (allow path only) | Both real executions passed this check live |
| 11 | Cross-tenant execution **blocking** | **F. NOT_REACHABLE_WITH_CURRENT_CANDIDATES** | Confirmed, this phase: exactly one `Tenant` row exists in production; every `ResearchPaper` row is `TenantID=1`. No real cross-tenant pair exists to test the reject path against |
| 12 | Permission denial | **C. TEST_PROVEN_ONLY** | Never exercised live — every real approval/execution used an authorized admin account |
| 13 | Idempotency `NOT_PREVIOUSLY_EXECUTED` | **A. PRODUCTION_PROVEN** | Both canaries, pre-execution |
| 14 | Idempotency `ALREADY_EXECUTED` | **B. PRODUCTION_PROVEN_BLOCKING** | Confirmed read-only, post-execution, for both canaries — the verdict correctly flips after a real execution; a live second-attempt call was never made (correctly, per every phase's explicit prohibition), so this is the detection half proven, not the full refusal-of-a-second-attempt behavior |
| 15 | `HISTORICAL_STATE_AMBIGUOUS` | **F. NOT_REACHABLE_WITH_CURRENT_CANDIDATES** | Requires a malformed `AuditLog.Metadata` row; none exist — `merge_group()`'s own `AuditLog` write is always well-formed |
| 16 | Deterministic lock ordering | **A. PRODUCTION_PROVEN** | `build_lock_order()` exercised live in both executions' preflight |
| 17 | Real row locking under concurrent execution | **C. TEST_PROVEN_ONLY** (single-threaded only) | `lock_pair_rows()` itself ran live (both canaries), but never under genuine concurrent contention — no multi-operator workload has ever existed |
| 18 | Successful transaction commit | **A. PRODUCTION_PROVEN** | Both canaries |
| 19 | Real rollback after a mid-transaction failure | **C. TEST_PROVEN_ONLY** | Phase 4J/4K.1's mocked failure-injection suite only; no real failure has ever occurred, and deliberately triggering one in production is explicitly forbidden throughout this project |
| 20 | `JournalID=WINNER_ONLY` | **A. PRODUCTION_PROVEN** | Both canaries |
| 21 | `JournalID=LOSER_ONLY_BACKFILL` | **E. BLOCKED_BY_CURRENT_POLICY** | 3 real candidates exist (`5548/5549`, `6086/6088`, `6107/6109`); all 3 also carry a real `AuthorNameRaw` conflict, which the executor's unconditional, live-recomputed check correctly refuses regardless of approval — see Task D |
| 22 | `JournalID=EQUAL` | **F. NOT_REACHABLE_WITH_CURRENT_CANDIDATES** | Confirmed this phase: `5645/7618` is the only `EQUAL`-state candidate, and it separately carries an `AuthorNameRaw` conflict, so `EQUAL` itself is never reached as the sole blocking/permitting factor in isolation |
| 23 | `JournalID=CONFLICT` blocking | **B. PRODUCTION_PROVEN_BLOCKING**-adjacent, more precisely **F** for a *real execution attempt* | `5638/5640` was found, this phase, with `JournalID=CONFLICT` (winner `1401`, loser `2210`) — `build_journal_state()`'s classification of it was confirmed live, but no approval was ever created for it and no real `execute_approved_merge()` call was made against it (correctly out of this phase's read-only scope) — the *classification* is production-proven, the *executor's refusal* of it is not |
| 24 | `AuthorNameRaw` = no conflict | **A. PRODUCTION_PROVEN** | Both canaries |
| 25 | `AuthorNameRaw` conflict correctly blocking execution | **C. TEST_PROVEN_ONLY** | `test_L_author_name_raw_conflict_blocks_before_merge`; 8 real, live candidates currently exist that *would* trigger this if approved and executed, but none has been, per this phase's read-only scope |
| 26 | Author union with partial overlap | **A. PRODUCTION_PROVEN** | Second canary (`3875/6091`: survivor 2 authors, loser 1, one shared) |
| 27 | Author union with no overlap | **F. NOT_REACHABLE_WITH_CURRENT_CANDIDATES** | No current candidate has zero shared `UserID`s between winner and loser — `author_content_conflicts()`'s design means a no-overlap case can't even be *detected* as a "conflict" (nothing to compare), but the *union* behavior itself (loser's distinct authors correctly added to survivor) has never been exercised, since every real duplicate pair found so far shares at least one author |
| 28 | Child-table remapping with nonzero rows | **A. PRODUCTION_PROVEN** (for `Authors` only) | Both canaries remapped `Authors` (1 row, then 2 rows) |
| 29 | Every individual FK/dependency relationship, nonzero production rows | **D. CODE_PRESENT_UNPROVEN** for `Citations`/`ExternalAuthors`/`CitationsHistory`/`PaperKeywords`/`ReportPaperDecision` | Confirmed, this phase: every one of these tables has **zero** rows for every candidate ever investigated across this entire project (14 candidates this phase, 10 in the original forensic set) — `ExternalAuthors` has rows *elsewhere* (5 on survivor `3875`, confirmed undisturbed since the loser contributed 0), but the *remap logic itself* for a nonzero-loser-row case in any of these five tables has never fired in production |
| 30 | `AuthorReviewQueue` populated dependency-gap blocking | **F. NOT_REACHABLE_WITH_CURRENT_CANDIDATES** | `0` rows, table-wide, confirmed again this phase |
| 31 | `ReportPaperDecision.MissingResolvedToPaperID` populated gap blocking | **F. NOT_REACHABLE_WITH_CURRENT_CANDIDATES** | `0` rows, table-wide, confirmed again this phase |
| 32 | `PaperKeywords` handling | **F. NOT_REACHABLE_WITH_CURRENT_CANDIDATES** (for a nonzero remap) | Table exists (Phase 4K schema-drift finding, still true), `0` rows for every candidate; `SIMPLE_CHILDREN` remap logic covers it structurally but has never fired |
| 33 | `AuditLog` creation | **A. PRODUCTION_PROVEN** | Both canaries |
| 34 | Executing-user attribution in `AuditLog` | **G. DEFECT_OR_GAP** (confirmed, carried forward, low severity) | Re-confirmed this phase by direct source read: `merge_group()`'s own `AuditLog` `INSERT` still hardcodes `("TenantID","UserID",...) VALUES (1, NULL, ...)` — both real `LogID=1539`/`1541` rows show `UserID=NULL`, even though a real, identifiable admin (`UserID=221`) performed both. The **separate** approval-decision audit row (`LogID=1540`, via the shared `audit()` helper) correctly attributes `UserID=221` — only the merge-execution record itself has this gap. Known since Phase 4K/4P, unchanged, out of this phase's scope to fix |
| 35 | Approval-to-`AuditLog` linkage | **A. PRODUCTION_PROVEN** | Both canaries: `ExecutionAuditLogID` correctly points to the real, matching `AuditLog` row |
| 36 | `LoserPaperID` preservation after loser deletion | **A. PRODUCTION_PROVEN** | Both canaries: `MergeApproval.LoserPaperID` still directly readable (`5482`, `6091`) after the real `DELETE` — the entire point of the Phase 4K.1 fix, now proven twice under real execution |
| 37 | Survivor deletion protection | **C. TEST_PROVEN_ONLY** | `SurvivorPaperID`'s FK (`ON DELETE NO ACTION`) has never been tested against a real attempted survivor deletion — no code path ever attempts one, so this remains a schema-level backstop verified only by direct constraint inspection and a mocked simulation (Phase 4L) |
| 38 | Orphan detection after successful execution | **A. PRODUCTION_PROVEN** | This phase's own Task A.9, and Phase 4T/4V's equivalent checks, all independently confirm zero orphans post-execution, both times |
| 39 | Cross-tenant isolation | **A. PRODUCTION_PROVEN** (allow path) / **F** (reject path) | Same as items 9–11 |
| 40 | Approval replay prevention | **A. PRODUCTION_PROVEN** | `create_pending_approval()`'s dedup-lookup logic was exercised live during both approval creations (no duplicate row resulted); the specific "attempt to create a second approval for an already-`EXECUTED` identity" refusal path was not attempted against either real pair post-execution (correctly out of scope) |
| 41 | Double-execution prevention under concurrency | **C. TEST_PROVEN_ONLY** | The *sequential*-outcome equivalent (`test_illegal_transition_double_approve_is_rejected`, Phase 4I) is mocked-proven; genuine concurrent double-execution has never been tested against production and, per this project's own standing prohibition on deliberately inducing risky conditions in production, should not be |

**Tally**: 6 `A` (fully proven, allow-path) + 2 more `A`-equivalent items counted within (7/8/9/10 overlap categories) → **effectively 8 distinct branches carry direct production allow-path proof**; 1 `B`; 26 `C`; 4 `D`/partial; 6 `F`; 3 `E`; 1 `G`. (Exact machine-readable tally in the JSON artifact.)

---

## Task C — Current Candidate Population Analysis

Fresh, full-corpus `detect_groups()` run against the current (post-second-canary) production dataset: **2,029 papers, 129 fuzzy comparisons, 22 groups, 11 high-confidence** (down from 12 pre-canary-2, correctly reflecting the executed pair's removal). Plus the 2 pairs known from Phase 4V to be blocking-heuristic-invisible but individually valid (`5289/5392`, `6107/6109`), recovered via direct check. **Total candidate pool: 13.**

| Shape | Count | Pairs |
|---|---|---|
| `CLEAN_READY` (zero conflicts, `WINNER_ONLY`, safe) | **4** | `5019/7559`, `5329/5434`, `6645/7572`, `5289/5392` |
| `HUMAN_REVIEW_AUTHOR_CONFLICT` | **8** | `5065/4786`, `5207/5481`, `5548/5549`, `5645/7618`, `6086/6088`, `6145/6190`, `6153/6189`, `6107/6109` |
| `BLOCKED_JOURNAL_CONFLICT` | **1** | `5638/5640` (winner `JournalID=1401`, loser `JournalID=2210` — genuinely different, real journal assignments) |

**Every one of the 13** is same-tenant, `pair_confidence=high`, DOI-safe, `NOT_PREVIOUSLY_EXECUTED`, and has zero dependency-gap population.

**Which unproven branches could realistically be exercised by another canary?** None of the 4 `CLEAN_READY` candidates exercises a branch beyond what the two existing canaries already proved (all `WINNER_ONLY`, zero conflicts; the only variation is citations-value magnitude, which does not exercise different SQL logic — `GREATEST()`'s behavior is identical regardless of whether the smaller operand is `0` or `3`).

**Which branches are blocked by another unresolved safety rule for every remaining candidate?** `JournalID=LOSER_ONLY_BACKFILL` (all 3 real candidates also conflict-blocked — Task D) and `JournalID=CONFLICT`-blocking (the 1 real candidate has never had an approval attempted against it, correctly, since this phase is read-only).

**Conclusion for Task C.7**: the current candidate population can safely exercise **only already-proven paths**. No additional *new* path is reachable without either (a) a design/implementation decision on `AuthorNameRaw` conflict handling, or (b) a genuinely new duplicate pair appearing in future data that happens to combine `LOSER_ONLY_BACKFILL` (or a populated dependency table) with zero conflicts — not something this phase can manufacture or wait for. **No approval was created for any candidate this phase.**

---

## Task D — The `LOSER_ONLY_BACKFILL` Bottleneck, Investigated Directly

All 3 real `LOSER_ONLY_BACKFILL` candidates, exact detail:

| Pair | Winner `JournalID` | Loser `JournalID` | `AuthorNameRaw` conflict (raw) |
|---|---|---|---|
| `5548→5549` | `NULL` | `676` | `"S Ahmad, NB Aoun, MA El Affendi, MS Anwar, S Abbas, AA Abd El Latif"` vs. `"S Ahmad, N Ben Aoun, MAE Affendi, MS Anwar, S Abbas, AAAE Latif"` |
| `6086→6088` | `NULL` | `771` | `"...MH Al-adaileh..."` vs. `"...MH Al-Adaileh..."` |
| `6107→6109` | `NULL` | `1104` | `"AM Alomari"` (1 name) vs. `"A Alomari, F Comeau, W Phillips, N Aslam"` (4 names) |

### Investigation: does an already-existing, deterministic, repository-supported resolution exist?

**Yes — but only for one of the three, and its use here carries a real, documented policy conflict.**

A real, already-existing function was found and tested against every conflict this phase found: `backend/analytics/disambiguation/pipeline.py::normalize_name()` — lowercase, diacritic-stripped, punctuation-stripped, single-spaced, already used elsewhere in this codebase for a genuinely analogous purpose (author-identity disambiguation). Applied, read-only, this phase, against all 8 live `AuthorNameRaw` conflicts:

| Conflict | Resolved by `normalize_name()`? |
|---|---|
| `6086/6088` (`Al-adaileh` vs. `Al-Adaileh`) | **Yes** — both normalize to the byte-identical string |
| `5548/5549` | No — `"nb aoun"` vs. `"n ben aoun"` remain different token sequences |
| `6107/6109` | No — genuinely different content (1 name vs. 4), not a formatting issue |
| `5207/5481`, `6145/6190`, `6153/6189` | No — each involves a real tokenization difference (`"i ben ltaifa"` vs. `"ib ltaifa"`, etc.) beyond what case/punctuation normalization alone resolves |
| `5065/4786` | No — a substantively different author-name string for the same `UserID`, possibly indicating a false-positive duplicate match rather than a formatting issue (flagged for separate forensic attention, not this phase's scope) |
| `5645/7618` | No — one side is an **empty string**, the other a real name; categorically different from a formatting conflict, structurally closer to a `COPY_LOSER`/backfill scenario `author_content_conflicts()` has no concept of |

**For the `LOSER_ONLY_BACKFILL` set specifically: exactly 1 of 3 (`6086/6088`) would be resolved by this existing function; 2 of 3 (`5548/5549`, `6107/6109`) would not.**

### Why this is not a simple fix, even for the one case that would resolve

Wiring `normalize_name()`'s comparison into `author_content_conflicts()` (even only as a secondary "these are equal after normalization, don't flag" check) would mean **applying name-based fuzzy equivalence to a decision that governs which author-paper attribution link survives a merge** — precisely the category of operation `CLAUDE.md`'s own documented, hard-won policy exists to forbid: *"Deterministic identifiers only for matching: Scholar_ID, DOI, ORCID, OpenAlex Author ID. Never name-based fuzzy matching for cross-attribution."* That policy exists because of a real, documented incident (602 papers cross-contaminated between similarly-named researchers, per this project's own history). `author_content_conflicts()`'s own docstring independently, deliberately states the same principle: *"No fuzzy matching, no name normalization, no new identity concept invented."* This was not an oversight — it was a considered design choice, restated explicitly at the exact point this phase is now examining.

**This does not mean the fix is wrong — it means it is a real policy/design decision, not a code-audit finding to act on unilaterally.** The distinction matters: unlike a bug fix, adopting `normalize_name()` here would be a deliberate loosening of an attribution-safety invariant this project has treated as sacrosanct since before Phase 4A. It deserves its own phase, with its own explicit authorization, not a quiet inclusion in a read-only audit's recommendation.

### Classification per Task D item 6

| Candidate | Classification |
|---|---|
| `6086/6088` | **Overly conservative implementation** — a deterministic, already-existing, already-used-elsewhere function would resolve it, but is not applied here; adopting it requires an explicit policy decision, not merely a code change |
| `5548/5549` | **Genuine ambiguity requiring human review** — resolvable only by a human judging `"NB Aoun"` and `"N Ben Aoun"` to be the same person, a judgment this project's own policy explicitly does not want made automatically |
| `6107/6109` | **Genuine ambiguity requiring human review**, more strongly — the data shapes are not obviously the same person at all; may indicate a data-quality issue in how the loser's `AuthorNameRaw` was originally scraped, unrelated to this merge |

**Conclusion**: the `LOSER_ONLY_BACKFILL` bottleneck is real, precisely characterized, and **not resolvable within this phase's read-only scope**. A future, separate, explicitly-scoped phase should decide — as a genuine policy question, informed by this evidence, not decided by this phase — whether `normalize_name()`-based equivalence is acceptable for `AuthorNameRaw` conflict resolution specifically (a narrower, more contained decision than general name-based cross-attribution matching), and if so, implement it narrowly and test it exhaustively before any candidate depending on it is executed.

---

## Task F — Risk Register

| Risk | Severity | Evidence | Production-proven? | Test-covered? | Blocks limited rollout? | Smallest next action |
|---|---|---|---|---|---|---|
| `AuthorNameRaw` conflict candidates cannot execute without a policy decision | **MEDIUM** | Task D | No (correctly blocked) | Yes | No — these candidates are simply excluded from rollout eligibility | A dedicated design phase, only if broader coverage of this pair-shape is desired |
| `JournalID=CONFLICT` candidates cannot execute automatically | **LOW** | This phase, `5638/5640` | No | Yes | No — correctly excluded by design (`journal_id_decision()`'s own `CONFLICT` state has never claimed to be auto-resolvable) | None — working as designed |
| Real rollback-under-failure never observed in production | **MEDIUM** | Task B item 19 | No | Yes (mocked, exhaustive) | No, for the narrow rollout class — the transaction-atomicity guarantee is a Postgres-level property, not specific to any one candidate's data shape, so repeated clean executions do not reduce this risk further; only a real failure (never to be deliberately induced) would | Continue relying on mocked coverage + Postgres's own guarantees; do not attempt to manufacture a live failure |
| Real concurrent execution/locking never observed | **MEDIUM** | Task B items 17/41 | No | Partial (sequential-outcome only) | No, for a single-operator rollout — becomes relevant only if multiple simultaneous approvers/executors are introduced | Defer until a multi-operator workflow is actually built; not applicable to the current single-approver process |
| Cross-tenant **rejection** path never exercised live | **LOW** | Task B items 11/39 | No (allow path is proven twice) | Yes (Phase 4U, exhaustive) | No | None — no real cross-tenant data exists in this single-tenant database; revisit only if/when a second tenant is onboarded |
| Executing-user attribution missing in the merge's own `AuditLog` row | **LOW** | Task B item 34, confirmed unchanged | N/A (a real, observed, unchanged gap) | No | No — does not affect merge correctness or safety, only forensic completeness of *that specific record* (the approval-decision record already correctly attributes the user) | A small, separate, low-risk fix to `merge_group()`'s `AuditLog` INSERT — not urgent, not blocking |
| Dependency-gap blocking (`AuthorReviewQueue`, `ReportPaperDecision.MissingResolvedToPaperID`) never exercised | **LOW** | Task B items 30/31 | No | Yes (Phase 4J, exhaustive) | No | None — these tables remain empty project-wide; the guard exists and is tested, simply unexercised because the risk it guards against has never materialized |
| Nonzero remap for `Citations`/`ExternalAuthors`/`CitationsHistory`/`PaperKeywords` never exercised | **LOW** | Task B item 29 | No | Partial (unit-tested logic, `merge_group()` itself untested against nonzero rows in these specific tables in production) | No, for the current candidate population (all show `0` rows in these tables for every candidate) | None now; if a future candidate ever shows nonzero rows in one of these tables, treat it as materially different and audit individually before approving, per this project's own established discipline |

**No risk above is marked LOW merely because it "has not occurred yet"** — each LOW rating here is justified by either (a) exhaustive, targeted mocked-test coverage plus a stable, unchanging absence of real triggering data (dependency gaps, cross-tenant), or (b) the risk being genuinely inapplicable to the current single-operator, single-tenant operational reality (concurrency, cross-tenant), stated as such rather than assumed away.

---

## Task E — Rollout Threshold Decision

### **A) READY FOR LIMITED SAFE ROLLOUT**

**Exact eligibility criteria** (a candidate pair qualifies only if every one of the following holds, freshly re-verified immediately before each individual approval — not batched, not assumed from this phase's snapshot):

1. Both `ResearchPaper` rows exist.
2. `pair_confidence == "high"` and `hard_exclusion_reason is None`.
3. Same `TenantID` on both sides (`validate_same_tenant()`).
4. `is_doi_claimed_elsewhere()` is `False`.
5. `idempotency_verdict() == NOT_PREVIOUSLY_EXECUTED`.
6. `journal_id_decision()` state is `NO_JOURNAL`, `WINNER_ONLY`, or `EQUAL` — **`LOSER_ONLY_BACKFILL` and `CONFLICT` are explicitly excluded from this rollout class**.
7. `author_content_conflicts()` returns `[]`.
8. `check_unhandled_dependency_gaps()` returns `[]`.
9. Fingerprint computed via both Django and raw `psycopg2` connections, confirmed byte-identical.

**Exact exclusions**: any pair failing any criterion above; any pair requiring `LOSER_ONLY_BACKFILL` or carrying any `AuthorNameRaw` conflict (pending Task D's identified follow-up); any pair where any dependency table shows a nonzero row count not yet individually audited (per the risk register's own caveat).

**Maximum recommended rollout size for the next phase**: **4** — the exact, current, fully-vetted `CLEAN_READY` population (`5019/7559`, `5329/5434`, `6645/7572`, `5289/5392`). Not a larger, speculative number — sized to the actually-known-safe population, re-confirmed fresh at execution time, not this audit's snapshot.

**Does each merge still require individual human approval?** **Yes, unconditionally** — this decision does not authorize batch or unattended execution. Each of the 4 (or any future candidate meeting the same criteria) requires its own `create_pending_approval()`/`approve_pending()` pair, its own fresh pre-execution re-audit (matching Phase 4V's Task E structure), and its own individual `execute_approved_merge()` call — one at a time, each independently verified before and after, exactly as both prior canaries were handled.

**Mandatory preflight checks** (per merge, immediately before approval creation): all 9 eligibility criteria above, re-verified fresh, not reused from this report.

**Mandatory postflight checks** (per merge, immediately after execution): loser deletion confirmed; survivor field preservation confirmed with before/after evidence; zero orphans across all 8 dependency tables; `MergeApproval`→`EXECUTED` with correct `ExecutionAuditLogID`; `AuditLog` row correctness; `idempotency_verdict()` flips to `ALREADY_EXECUTED`; full regression suite re-run.

---

## Task G — Test and Change Accounting

```
test_dedup_papers.py             18/18   (unchanged)
test_merge_plan_generator.py     43/43   (unchanged)
test_merge_execution_safety.py   89/89   (unchanged)
test_merge_approval.py           50/50   (unchanged)
test_merge_executor.py           48/48   (unchanged)
test_fk_lifecycle.py             11/11   (unchanged)
```

**Total: 259/259 — unchanged from Phase 4V.** No audit defect requiring a new test was found this phase (the `normalize_name()` finding is a design/policy question, not a code defect — nothing to prove via a new test without first deciding the policy question).

- Code files modified: **0.**
- Code files created: **0.**
- Report files created: **2.**
- DB writes: **0.**
- Approvals created: **0.**
- Approvals modified: **0.**
- Merges executed: **0.**
- Merge attempts: **0.**
- DOI changes: **0.**
- Migrations applied: **0.**

**Matches every expectation stated for a clean Phase 4W.**

---

## Exact Recommended Next Phase

**A narrowly-scoped rollout phase**, gated on your explicit authorization, whose job is to process the 4 `CLEAN_READY` candidates identified in Task C/E **one at a time**, each with its own fresh preflight/postflight cycle exactly matching Phase 4V's own methodology — not a single combined batch operation. Separately, and independently, a future policy-decision phase may address the `LOSER_ONLY_BACKFILL`/`AuthorNameRaw`-conflict bottleneck characterized in Task D, if broader duplicate-population coverage is desired later — that decision is explicitly not made by this phase.

Per your instructions, I am stopping here. Phase 4X is not started. No merge was executed. No approval was created. No production data was changed.

---

## Exact Accounting

- **Code files modified**: **0.**
- **Code files created**: **0.**
- **Report files created**: **2** — this file and `backend/reports/phase4w_production_coverage_matrix.json`.
- **DB writes**: **0.**
- **Approvals created/modified**: **0.**
- **Merges executed/attempted**: **0.**
- **DOI changes**: **0.**
- **Migrations applied**: **0.**
- **Test totals**: 259/259, unchanged.

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Identical to every phase since 4E — zero changes this phase.
