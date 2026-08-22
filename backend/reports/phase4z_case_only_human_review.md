# Phase 4Z — Human Review Workflow for Case-Only Author Conflict

## Executive Summary

Phase 4Y implemented a narrow, pure `CASE_ONLY_FORMATTING_DIFFERENCE` classification and wired
it into the plan-reporting layer only, discovering that `execute_approved_merge()` has no
awareness of it and no bypass mechanism at all. This phase asked whether the *existing*
`AuthorReviewQueue` human-review infrastructure could represent and review this classification
**without touching merge execution safety**.

**Finding**: `AuthorReviewQueue` cannot represent this use case directly — its schema is shaped
for a structurally different question (does one scraped name on one paper belong to one
suggested `UserID`), and its `CONFIRMED` decision path performs an immediate, unrelated
`Authors` table write, coupling decision and execution in a way this phase's safety boundary
cannot reuse. Rather than adding a new production table/migration/approval system, this phase
implemented the **narrower representation the actual question needs**: two small, pure functions
(a reviewer-facing data contract and a state machine) with zero DB access, zero writes, and zero
reference to `MergeApproval` or the executor. A dedicated integration test proves — not merely
documents — that even after the pure review state machine independently reaches
`REVIEWED_APPROVED`, `execute_approved_merge()` still blocks the identical case-only conflict,
because the two systems are provably disconnected.

12 new tests added (277 → **289/289**, zero regressions). Zero production writes. Zero code
changes to `dedup_papers.py`, `merge_executor.py`, or `merge_approval.py`.

**Final decision: A) CASE-ONLY REVIEW WORKFLOW READY — NO EXECUTION AUTHORIZED.**
`(6086,6088)` remains non-executable until a separate, explicitly authorized policy decision and
merge-authorization step (the same gap Phase 4Y already flagged and did not fix).

---

## Task A — Audit of the Existing Human-Review Path

**Files inspected**: `backend/analytics/migrations/sprint8_author_review_queue.sql` (schema),
`backend/analytics/reconciliation_views.py` (the only consumer — list/decide/stats endpoints),
`backend/accounts/views.py::audit()` (the audit helper it calls), and, for context,
`backend/analytics/disambiguation/pipeline.py` (the producer that writes rows into this queue)
and `backend/analytics/management/commands/link_coauthors.py`.

**Schema** (`AuthorReviewQueue`): `ReviewID`, `PaperID` (single, FK to `ResearchPaper`),
`ScrapedName`, `ScrapedAffiliation`, `SuggestedUserID` (FK to `Users`), `SuggestedConfidence`,
`SuggestedCriteria`, `Status` (`PENDING`/`CONFIRMED`/`REJECTED`/`SKIPPED`, `CHECK` constraint),
`ReviewedByUserID`, `ReviewedAt`, `ReviewerNotes`, `CreatedAt`. Unique on `(PaperID,
ScrapedName)`.

**Reviewer identity / permission**: `reconciliation_views.py::_can_reconcile()` — Django
`IsAuthenticated` plus `manage_users` permission or `Admin` user type. `review_queue_decide()`
records `ReviewedByUserID = request.user.user_id`, `ReviewedAt = NOW()`, and optional
`ReviewerNotes`.

**Status transitions**: `PENDING → CONFIRMED / REJECTED / SKIPPED`, guarded by a `FOR UPDATE`
row lock and an explicit `409 Conflict` if the row is no longer `PENDING` — a real
already-decided guard, not a race condition.

**Critical finding — decision and write are coupled**: on `decision == 'CONFIRMED'`,
`review_queue_decide()` does not merely flip a status column. In the *same* atomic transaction
it runs:

```python
INSERT INTO "Authors" ("UserID", "PaperID", ..., "MappingCriteria", "AuthorNameRaw", "Is_Verified")
VALUES (%s, %s, ..., 'admin_reconcile', %s, TRUE)
ON CONFLICT ("UserID", "PaperID") DO UPDATE ...
```

`CONFIRMED` **is** the write, not a signal that authorizes a write elsewhere. This is a
deliberate, correct design for its actual purpose (co-author linkage) but is exactly the pattern
Phase 4Z's hard safety boundary (a review decision must never change execution permission)
cannot reuse for a merge-pair conflict.

**Answers to Task A's five questions**:

1. **Can `CASE_ONLY_FORMATTING_DIFFERENCE` be represented using the existing review model
   without changing schema?** **No.** The table's grain is `(PaperID, ScrapedName)` → one
   suggested `UserID` — an identity-search shape. A case-only conflict's grain is
   `(SurvivorPaperID, LoserPaperID, UserID)` → two already-fixed `AuthorNameRaw` variants — a
   pair-comparison shape. There is no column that could hold a second `PaperID`, no column for
   "which of two raw strings," and no natural place for a merge-plan fingerprint or
   `JournalID`/`DOI`/tenant context the reviewer needs (Task C).
2. **Can a reviewer distinguish exact conflict / case-only / tokenization / cardinality
   difference?** No — no such field exists anywhere in `AuthorReviewQueue`; it was never
   designed along this dimension, because its own conflicts are single-name-vs-candidate-pool,
   not two-raw-strings-already-agreed-identity.
3. **Can the reviewer explicitly approve or reject the case?** The `Status` enum supports
   `CONFIRMED`/`REJECTED` generically, but "approve" here is inseparable from the specific
   `Authors` INSERT described above — it cannot be repurposed as a content-neutral yes/no without
   also repurposing (and thereby corrupting the meaning of) that write.
4. **Does reviewer approval currently mean "approve data attribution" or "approve merge
   execution"?** Neither, precisely — it means "approve **and immediately write** a specific
   co-author link." It is closer to "approve execution of a narrow, specific action" than to a
   pure evidentiary decision. This is exactly the coupling this phase's safety boundary requires
   avoiding for the merge-conflict question.
5. **Is there any existing field/status that would dangerously conflate the two meanings?**
   Yes — `Status='CONFIRMED'` itself, by design, for its actual use case. Reusing it unmodified
   for a merge-pair case-only conflict would either (a) silently attempt an incorrect `Authors`
   INSERT keyed on a `SuggestedUserID`/`ScrapedName` shape that doesn't exist for this question,
   or (b) require enough surrounding reinterpretation that it stops being "the existing
   mechanism" and becomes a new, ad hoc one anyway — the exact outcome Task E instructs against.

**No semantics were invented to force a fit.** The audit's conclusion is a genuine mismatch, not
a workaround.

---

## Task B — Required State Machine

Implemented in `backend/tools/merge_plan_generator.py`, function `advance_case_only_review()`,
as a pure state machine with **no `cur`/DB parameter at all**:

```
PLAN_GENERATED
    -> CASE_ONLY_FORMATTING_DIFFERENCE
        -> HUMAN_REVIEW_REQUIRED
            -> REVIEWED_APPROVED   (terminal — decision/evidence only)
            -> REVIEWED_REJECTED   (terminal)
```

Every transition returns `(new_state, reason)`; any invalid transition (missing/invalid decision,
advancing from a terminal state) raises `ValueError` — there is no silent fallthrough or default
state. `REVIEWED_APPROVED`'s reason string states explicitly: *"this is a decision/evidence
record only; it does NOT authorize merge execution and does not modify MergeApproval,
ResearchPaper, Authors, or DOI."*

**Smallest missing capability, stated per Task B's own instruction**: the existing
`AuthorReviewQueue` state model cannot represent this safely (Task A, above) because its terminal
`CONFIRMED` state is bound to a specific write. The smallest capability actually missing is: *a
review-decision representation whose terminal states are pure data, not paired with any write* —
which is exactly, and only, what this phase's two new functions provide. No new database table
was created to hold this state; it exists purely as an in-memory/function-level representation
this phase, deliberately (see Task E).

---

## Task C — Case-Only Review Display / Data Contract

Implemented as `build_case_only_review_display(plan, conflict, tenant_id=None)` in
`merge_plan_generator.py`. Given a plan dict (as `generate_pair_plan()`/
`generate_plan_for_real_pair()` already produce) and one conflict dict (as Phase 4Y's
`build_author_conflict_report()` annotation already produces), it returns exactly:

| Field | Source |
|---|---|
| `survivor_paper_id` / `loser_paper_id` | `plan["survivor"]` / `plan["loser"]` |
| `survivor_author_name_raw` / `loser_author_name_raw` | the conflict dict, unmodified |
| `case_only_comparison_result` | `conflict["author_conflict_type"]` (Phase 4Y) |
| `case_only_comparison_reason` | `conflict["author_conflict_type_reason"]` (Phase 4Y) |
| `explanation` | fixed, plain-language statement that only letter case differs |
| `journal_state` | `plan["journal_state"]` (existing `build_journal_state()` output) |
| `tenant_id` | caller-supplied (no new query — reuses whatever the caller already has) |
| `doi_state` | `plan["doi_state"]` (existing `build_doi_state()` output) |
| `plan_fingerprint` | `plan["plan_fingerprint"]` |
| `plan_classification` | `plan["classification"]` |
| `disclaimer` | fixed string: `"This review does NOT authorize merge execution."` |

No unrelated sensitive data is exposed — every field is drawn from data the plan generator
already computes for this exact pair, nothing additional is fetched.

**Safety guard, not merely a display detail**: the function raises `ValueError` if the supplied
conflict's `author_conflict_type` is not `CASE_ONLY_FORMATTING_DIFFERENCE` — it refuses to build
a review display for a genuine conflict at all, directly satisfying Task F item 8 (case-only
review must not affect `5548/5549` or `6107/6109`) at the code level, not only by convention.

---

## Task D — Review Decision Semantics

- **`APPROVED`**: a human reviewer has recorded that, having seen the display contract above
  (including the explicit disclaimer), they agree the difference is formatting-only. This is
  **evidence**, nothing more.
- **`REJECTED`**: a human reviewer disagrees, or judges the difference is not safely
  formatting-only despite the classifier's label. Progression stops; no further state exists
  (`advance_case_only_review()` raises on any attempt to continue from `REVIEWED_REJECTED`).
- **`PENDING`** (i.e., `HUMAN_REVIEW_REQUIRED`): no decision has been recorded yet.

**Critical rule, verified by direct test, not by inspection alone** (Task F item 3): a positive
review decision does **not** automatically change `MergeApproval.Status`, executor authorization,
`AUTO_MATCH`, or `execution_permitted` anywhere in the codebase. `advance_case_only_review()`
takes no cursor and touches no shared state; `execute_approved_merge()` never imports or calls
it. `test_phase_4z_reviewed_approved_state_still_does_not_unblock_executor` proves this directly:
it reaches `REVIEWED_APPROVED` via the pure state machine, snapshots the mock cursor's
`MergeApproval`/`ResearchPaper` state before and after that call (byte-identical), then calls
`execute_approved_merge()` on the identical case-only-conflicted fixture and confirms it still
returns `EXEC_BLOCKED_AUTHOR_CONFLICT`.

**Human Review = evidence/decision. `MergeApproval` = execution authorization. These remain two
separate systems after this phase, with no bridge between them.**

---

## Task E — Implementation Justification

**Existing infrastructure does not already support this workflow** (Task A). Per Task E's
instruction, the exact minimal change is stated before being made:

- **File**: `backend/tools/merge_plan_generator.py`.
- **Functions added**: `build_case_only_review_display()`, `advance_case_only_review()`, plus
  four state constants and one disclaimer string constant.
- **Reason**: the existing `AuthorReviewQueue` table/endpoint is schematically incompatible
  (wrong grain, decision-coupled-to-write) for a survivor/loser-pair case-only conflict; the
  smallest safe representation is two new pure functions with no DB access, appended after the
  existing `generate_plan_for_real_pair()` (the function whose output they consume), reusing the
  plan/conflict dict shapes Phase 4E/4Y already established rather than inventing new ones.
- **No new production table, no migration, no new approval system**: the review state exists
  only as values passed between pure functions in this phase — there is no persisted
  `CaseOnlyReview` table. Persisting this state durably (so a real reviewer's decision survives
  across requests) would require a schema decision this phase does not authorize — see Task H.
- **No change to `dedup_papers.py`, `merge_executor.py`, or `merge_approval.py`** — confirmed by
  `git status` (no diff on those files) and by every existing test in those files' suites
  remaining green, unchanged.

---

## Task F — Tests

12 new tests, all passing (277 → **289/289**, 0 regressions):

**`backend/tools/test_merge_plan_generator.py` — `CaseOnlyReviewRepresentationTests`** (10 tests):

| # | Case | Test |
|---|---|---|
| 1 | `6086/6088` classified `CASE_ONLY_FORMATTING_DIFFERENCE` | `test_6086_6088_classified_case_only` |
| 2 | classification produces `HUMAN_REVIEW_REQUIRED` | `test_classification_produces_human_review_required` |
| 4 | review rejection blocks further progression | `test_rejection_reaches_reviewed_rejected_and_is_terminal` |
| — | approval reaches `REVIEWED_APPROVED`, reason states no-execution | `test_approval_reaches_reviewed_approved_state` |
| — | missing decision at `HUMAN_REVIEW_REQUIRED` raises | `test_missing_decision_at_human_review_required_raises` |
| — | `REVIEWED_APPROVED` is also terminal | `test_approved_state_is_also_terminal` |
| — | Task C data contract, all required fields + disclaimer | `test_review_display_contract_contains_required_fields_and_disclaimer` |
| 8 | case-only review does not affect `5548/5549` | `test_5548_5549_genuine_conflict_refused_by_review_display` |
| 8 | case-only review does not affect `6107/6109` | `test_6107_6109_genuine_conflict_refused_by_review_display` |
| 5/6/7 | source-level purity guard (no SQL verbs, no `cur` calls, no import of `merge_approval`/`merge_executor`) | `test_review_functions_contain_no_db_or_write_vocabulary` |

**`backend/tools/test_merge_executor.py`** (2 tests, the central proof for item 3):

| # | Case | Test |
|---|---|---|
| 3 | a case-only conflict still blocks `execute_approved_merge()` | `test_phase_4z_case_only_conflict_also_blocks_before_merge` |
| 3 (full) | **even after `REVIEWED_APPROVED`**, the executor is unaffected — `MergeApproval`/`ResearchPaper` snapshots byte-identical before/after the review call, executor still returns `EXEC_BLOCKED_AUTHOR_CONFLICT` | `test_phase_4z_reviewed_approved_state_still_does_not_unblock_executor` |

**Items 9, 10, 11** (existing exact-conflict / executor-safety / approval behavior unchanged):
confirmed by the full-suite re-run below — every pre-existing test in `test_dedup_papers.py`,
`test_merge_execution_safety.py`, `test_merge_approval.py`, and `test_fk_lifecycle.py` passes
unchanged, and Phase 4Y's own tests in `test_merge_executor.py`/`test_merge_plan_generator.py`
(including `test_L_author_name_raw_conflict_blocks_before_merge` and the three real-shape
`CaseOnlyAuthorConflictPlanIntegration` tests) are untouched and still pass.

**Full suite result**: `277 → 289 passed, 289 total` (12 new, 0 regressions, 0 failures).
Per-file: `test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 58/58,
`test_merge_execution_safety.py` 102/102, `test_merge_approval.py` 50/50,
`test_merge_executor.py` 50/50, `test_fk_lifecycle.py` 11/11.

No test was added merely to inflate coverage — each maps directly to one of the 11 required
Task F cases or to Task C/D's own stated requirements.

---

## Task G — Live Read-Only Check

Fresh live read (no writes) via `litrix_db.db()`, this phase:

```
ResearchPaper: (6086, JournalID=None,  TenantID=1, DOI='10.1155/2021/5534379')
ResearchPaper: (6088, JournalID=771,   TenantID=1, DOI=None)
Authors:       (6086, UserID=105, "...MH Al-adaileh...")
Authors:       (6088, UserID=105, "...MH Al-Adaileh...")
MergeApproval: [(1, 'EXECUTED'), (2, 'EXECUTED')]
ResearchPaper total count: 2029
AuthorReviewQueue rows referencing 6086 or 6088: 0
```

`is_case_only_difference()` re-run against these exact live-fetched strings:
`(True, "values are identical after lower() alone, ... differ only in letter case")`.

Everything matches Phase 4X/4Y's prior findings exactly, plus two new confirmations this
phase specifically needed: `TenantID=1` for both (same tenant, consistent with the rest of this
single-tenant production database), and `AuthorReviewQueue` currently holds **zero** rows for
either paper — confirming no prior, unrelated review artifact exists that could be mistaken for
one. No approval was created, no review record was created, no merge was attempted, no data was
modified.

---

## Task H — Final Policy Boundary

1. **Can the existing human-review workflow safely represent case-only conflicts?** No, not
   directly (Task A) — its schema and its `CONFIRMED`-writes-`Authors` coupling are shaped for a
   different question. This phase built the narrower representation the actual question needs,
   as pure functions with no persisted state, rather than repurposing the mismatched table.
2. **Does it preserve the distinction between human review and merge authorization?** Yes — by
   construction (`advance_case_only_review()` has no DB parameter) and by direct proof (the
   `test_phase_4z_reviewed_approved_state_still_does_not_unblock_executor` integration test).
3. **Does anything need to change before `6086/6088` could enter a future review workflow?**
   For a *display-only, in-memory* review (what this phase built), no — it works today, as the
   tests demonstrate. For a *durable, persisted* review (a real reviewer's decision surviving
   across HTTP requests, visible to other admins, auditable over time), yes: a schema decision
   is needed, since no existing table has the right shape and this phase deliberately did not
   create one. That schema design is future work, not started here.
4. **Is there any reason to change the executor now?** No. The executor's unconditional
   `author_content_conflicts()` block is exactly correct given no policy has yet authorized any
   bypass — Phase 4Y already identified this as a real, separate gap, and this phase's direct
   proof (the executor stays blocked even after an independent `REVIEWED_APPROVED`) confirms that
   gap is still exactly where Phase 4Y left it. Changing the executor was never in scope here and
   nothing discovered this phase weakens the case for leaving it alone.

**NO executor change was made in Phase 4Z**, matching the expected safety principle exactly.

---

## Safety Accounting

- **Files inspected (Task A)**: `backend/analytics/migrations/sprint8_author_review_queue.sql`,
  `backend/analytics/reconciliation_views.py`, `backend/accounts/views.py` (`audit()`),
  `backend/analytics/disambiguation/pipeline.py`, `backend/analytics/management/commands/link_coauthors.py`.
- **Code files modified**: 1 — `backend/tools/merge_plan_generator.py` (two new pure functions,
  four state constants, one disclaimer constant; nothing existing in the file was changed).
- **Code files explicitly untouched**: `dedup_papers.py`, `merge_executor.py`,
  `merge_approval.py`, `merge_execution_safety.py`, `reconciliation_views.py`,
  `analytics/migrations/sprint8_author_review_queue.sql`.
- **Test files modified**: 2 — `backend/tools/test_merge_plan_generator.py`,
  `backend/tools/test_merge_executor.py`.
- **Report files created**: 2 — this file and `backend/reports/phase4z_case_only_human_review.json`.
- **Production DB writes**: **0.**
- **Production approvals created**: **0.**
- **Review records created**: **0** (no persisted review table exists; nothing to create).
- **Merges**: **0.**
- **DOI changes**: **0.**
- **Migrations**: **0.**
- **`ApprovalID=1`/`ApprovalID=2`**: unchanged, both still `EXECUTED`.
- **Tests**: `277 → 289` (12 new, 0 regressions).
- **Production state changed**: **NO** — `MergeApproval` still exactly
  `[(1, 'EXECUTED'), (2, 'EXECUTED')]`, `ResearchPaper` count still `2029`, `AuthorReviewQueue`
  rows for `6086`/`6088` still `0`, all re-verified live immediately before writing this report.

## Final Decision

**A) CASE-ONLY REVIEW WORKFLOW READY — NO EXECUTION AUTHORIZED**

The pure display contract and pure state machine are implemented, tested (12 new tests,
289/289 total), and directly proven — not merely documented — to be disconnected from execution
authorization. `(6086,6088)` remains non-executable until a separate, explicitly authorized
policy decision AND a separate merge-authorization step (an executor-level bypass, per Phase 4Y's
still-open finding) are both built and approved. `(5548,5549)` and `(6107,6109)` are unaffected
and remain correctly blocked as genuine conflicts.

**Exact next recommended phase (not started, not authorized by this phase)**: a governance-level
decision on whether to build a *durable, persisted* case-only review record (its own small
migration, since no existing table fits) and, separately and independently, whether to design
the executor-level bypass Phase 4Y and this phase both identified but neither built. These are
two distinct, separately-approvable decisions, not one bundled change.

Per your instructions, I am stopping here. Phase 4AA is not started. No production approval was
created. No merge was executed. No production data was changed.
