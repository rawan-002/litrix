# Phase 4Y — Case-Only AuthorNameRaw Policy Decision & Human-Review Integration

## Executive Summary

Building directly on Phase 4X's read-only investigation (final decision: C — HUMAN REVIEW
PATH IS REQUIRED — and its addendum's five precisely-answered follow-up questions), this
phase makes the case-only policy decision explicit and implements the minimum safe behavior
that decision justifies.

**Selected policy: Option B — CASE_ONLY_ALLOWED_FOR_HUMAN_REVIEW_ONLY.** A new, narrow, pure
classifier (`is_case_only_difference()`) was implemented and wired into the plan/reason
reporting layer only. It never grants execution permission, never triggers `AUTO_MATCH`, and
`author_content_conflicts()`'s own exact-equality blocking behavior in `dedup_papers.py` is
completely untouched. 18 new tests added (259 → **277/277**, zero regressions). Zero
production writes. `(6086,6088)` is now correctly *labeled* as a formatting-only difference in
the plan output — but a genuinely important gap was discovered in the process: **even a human
approval today has no mechanism to bypass `execute_approved_merge()`'s unconditional live
author-conflict re-check**, so `(6086,6088)` is not yet actually executable through the real
production path. This gap is reported, not fixed, per this phase's own minimal-implementation
instruction.

**Final decision: A) CASE_ONLY_POLICY_APPROVED_FOR_HUMAN_REVIEW.**

---

## Task A — Policy Option Comparison

| Criterion | Option 1: AUTO_MATCH | Option 2: HUMAN_REVIEW-only (selected) | Option 3: hard conflict (status quo) |
|---|---|---|---|
| False-positive risk | Low but nonzero — no real collision found in this dataset, but `str.lower()` has narrow theoretical locale edge cases not exercised here | None — a human sees every case, decides every time | None |
| Cross-attribution risk | None (UserID already fixed both sides in every case examined) | None | None |
| `CLAUDE.md` compatibility | Debatable — first automatic name-based decision inside the merge-safety path itself, even though narrower than "fuzzy matching" | Fully compatible — no automatic decision is made, only a label | Fully compatible |
| Consistency with `AuthorReviewQueue` precedent | Weak — that mechanism exists specifically because the repository's real precedent is confidence-gated automation *with* a human fallback below threshold; a pure AUTO_MATCH skips the fallback entirely | Strong — mirrors the "surface a suggestion, let a human confirm" pattern directly | N/A — no suggestion surfaced at all |
| Consistency with `journal_id_decision()` conflict handling | Weak — that function never auto-resolves `CONFLICT`, even for narrower cases (e.g. it doesn't have a "trivial" backfill exception) | Strong — same never-auto-resolve posture, just adds a label | Strong but loses the label |
| Auditability | Weaker — an automatic decision leaves a thinner trail than an explicit human confirmation | Strong — every case-only merge that ever happens will have gone through the same explicit human-confirmation step as any other conflict | Strong but undifferentiated |
| Reversibility | An executed AUTO_MATCH merge is a real, executed merge like any other — not reversible without the same manual recovery path Phase 4R/4V already documented | N/A — nothing executes automatically | N/A |
| Operational complexity | Would additionally require a new bypass path in `merge_executor.py` (does not exist today — see Task H) | None beyond the labeling already implemented this phase | None |
| Production candidates unlocked | 1 immediately (`6086/6088`), if an executor bypass were also built (out of scope, not built) | 0 immediately unlocked for execution; 1 (`6086/6088`) gets a clearer, evidence-backed label for a human to act on | 0 |
| Changes the meaning of `AUTO_MATCH` | Yes — would be the first name-based `AUTO_MATCH` anywhere in merge safety | No | No |

**Conclusion of Task A**: Option 1 is not disqualified by any single criterion in isolation, but
it is the weakest option on four independent axes (`CLAUDE.md` compatibility, `AuthorReviewQueue`
consistency, `journal_id_decision()` consistency, auditability) and requires additional,
unbuilt infrastructure (an executor-level bypass — see Task H) to have any practical effect at
all. Option 3 (do nothing) discards real, tested, deterministic information a human reviewer
would find useful. Option 2 captures the classifier's genuine value (a correctly-labeled,
evidence-backed distinction) with none of Option 1's costs.

---

## Task B — Recommended Policy

**Selected: B) CASE_ONLY_ALLOWED_FOR_HUMAN_REVIEW_ONLY.**

This matches the stated default expectation; nothing in Task A's evidence rises to "strongly
proves A is preferable" — Phase 4X's own collision proof (`Al-Amin`/`Al Amin`,
`D'Angelo`/`D Angelo`) already showed how easily an *overly broad* transformation collapses
real distinctions, and even this phase's much narrower `str.lower()`-only rule inherits the
same category of risk in kind, if not in the specific proven instances. The repository's own
consistent design posture — `AuthorReviewQueue`'s confidence-threshold-plus-human-fallback,
`journal_id_decision()`'s refusal to auto-resolve `CONFLICT`, `NO_AUTO_COPY_FIELDS`,
`merge_plan_generator.py`'s explicit "no formatting preference is invented here" docstring on
`build_author_conflict_report()` — treats a human decision as the default even where the pure
logic might arguably support automation. This phase's evidence narrows the scope of the
Phase 4X policy tension without overturning the *pattern* that produced it.

**Why a deterministic formatting signal is still useful without becoming an automatic identity
decision**: the label costs nothing in safety (it never appears in any code path that grants
execution permission) and gives a human reviewer, for the first time, a precise, evidence-backed
answer to "is this AuthorNameRaw conflict a real disagreement, or just a capitalization
artifact?" — collapsing what used to be an opaque, undifferentiated `GENUINE_CONFLICT` entry
into something a reviewer can act on quickly and confidently, without asking them to trust an
automatic decision they cannot see the reasoning behind.

---

## Task C — Exact Rule Definition

Implemented in `backend/tools/merge_execution_safety.py` as `is_case_only_difference(a, b)`,
placed immediately after `fetch_paper_tenant_ids()` (Phase 4U's cross-tenant helper), matching
its `(ok, reason)` return convention.

**Behavior, exactly as specified by Task C**:
- The *only* transformation applied is `str.lower()`. No punctuation stripping, no whitespace
  collapsing, no diacritic/Unicode normalization, no fuzzy or substring matching, no initials
  expansion, no token reordering.
- `None` on either side → `(False, ...)`, explicit "missing" reason — never silently treated as
  a match.
- Empty string (`""`) on either side → `(False, ...)`, explicit reason — likewise never a match.
- Both `None`, or both `""` → `(False, ...)` — "both missing" is explicitly **not** treated as
  equivalent, per Task F item 10's requirement.
- Identical strings → `(False, ...)` — this function classifies a *difference*; if there is no
  difference, there is nothing to classify as case-only.
- A character-count mismatch → `(False, ...)` immediately, before ever calling `.lower()` — a
  direct implementation of Task C's "character count must remain identical" requirement.
- Only if `a.lower() == b.lower()` (and every prior guard has passed) → `(True, ...)`.

Pure: no DB access, no network, no side effects — verified both by direct code review and by
the module's own existing static-guard test suite (`NonGoalsStaticSourceScan`, unaffected by
this addition since the new function contains no SQL and imports nothing beyond what the module
already imports).

`dedup_papers.py::author_content_conflicts()` is **not modified** — it remains pure
exact-string-equality, exactly as Phase 4E designed it and every phase since has re-confirmed
unmodified. `is_case_only_difference()` is called only from the plan-reporting layer, described
next.

---

## Task D — Human Review Integration

**No new review system was created.** The case-only signal is integrated into the *existing*
plan/reason representation only, at exactly one point:
`merge_plan_generator.py::build_author_conflict_report()` — the function that already wraps
`author_content_conflicts()`'s output into the plan schema every `generate_pair_plan()` call
consumes.

For each conflict `author_content_conflicts()` returns, the report now adds two fields:
- `author_conflict_type`: `"CASE_ONLY_FORMATTING_DIFFERENCE"` or `"GENUINE_CONFLICT"`, from
  `is_case_only_difference()`.
- `author_conflict_type_reason`: the classifier's own reason string.

`execution_permitted` and `blocking_reason` on the report — and therefore
`compute_classification()`'s `PLAN_REQUIRES_HUMAN_APPROVAL` verdict for the whole pair — are
**completely unchanged** by this addition. A case-only conflict still blocks automatic execution
exactly like a genuine one; the new fields are visible to whatever consumes the plan (a future
reviewer UI, a report script, a human reading the JSON directly) but are never read by any
code path that decides whether to execute.

This directly reuses the plan's existing conflict-list structure (the same pattern
`field_actions`, `child_table_actions`, and `journal_state` already use — a status/reason pair
per item) rather than inventing a parallel workflow. `AuthorReviewQueue` itself was evaluated as
a literal reuse target and rejected for this specific integration point: it is keyed on
`(PaperID, UserID)` co-author-linkage decisions, a different table and a different decision
shape than a merge-time conflict report; the closer, and simpler, reuse is the plan/reason
structure that already exists for this exact purpose.

---

## Task E — Not Applicable

The selected policy is B, not A. Task E's requirements (invariant proof, dedicated regression
tests, fresh corpus check, separate authorization before any real merge) are Task E's own
gate for *if* A had been selected; they are not triggered. No `AUTO_MATCH` behavior exists
anywhere in the codebase after this phase. `(6086,6088)` was not executed, and no approval was
created for it (Task G/H, below).

---

## Task F — Tests

18 new tests added, matching all 17 required cases (case 9's "missing vs. non-missing" is
covered by one test method exercising both directions plus the empty-string variant — 3
assertions, standard style for this test suite):

**`backend/tools/test_merge_execution_safety.py` — `IsCaseOnlyDifferenceTests`** (13 test
methods, covering Task F items 1–12):

| # | Case | Test | Result |
|---|---|---|---|
| 1 | identical names | `test_identical_names_not_a_difference` | `(False, ...)` |
| 2 | exact case-only difference | `test_exact_case_only_difference_detected` | `(True, ...)` |
| 3 | punctuation difference | `test_punctuation_difference_not_case_only` | `(False, ...)` |
| 4 | whitespace difference | `test_whitespace_difference_not_case_only` | `(False, ...)` |
| 5 | tokenization difference | `test_tokenization_difference_not_case_only` | `(False, ...)` |
| 6 | token-count difference | `test_token_count_difference_not_case_only` | `(False, ...)` |
| 7 | diacritic difference | `test_diacritic_difference_not_case_only` | `(False, ...)` |
| 8 | reordered tokens | `test_reordered_tokens_not_case_only` | `(False, ...)` |
| 9 | missing vs. non-missing | `test_missing_vs_present_not_case_only` | `(False, ...)` both directions + empty string |
| 10 | both missing | `test_both_none_not_treated_as_equivalent` / `test_both_empty_string_not_treated_as_equivalent` | `(False, ...)` |
| 11 | `Al-Amin` vs. `Al Amin` | `test_al_amin_collision_pair_not_case_only` | `(False, ...)` — Phase 4X's proven collision pair confirmed still safe |
| 12 | `D'Angelo` vs. `D Angelo` | `test_dangelo_collision_pair_not_case_only` | `(False, ...)` — same |

**`backend/tools/test_merge_plan_generator.py`** (5 new tests, covering Task F items 13–17):

| # | Case | Test | Result |
|---|---|---|---|
| 13 | `6086/6088` real production shape | `CaseOnlyAuthorConflictPlanIntegration.test_6086_6088_real_shape_labeled_case_only_but_still_blocked` | labeled `CASE_ONLY_FORMATTING_DIFFERENCE`, `execution_permitted` still `False` |
| 14 | `5548/5549` real production shape | `test_5548_5549_real_shape_remains_blocked_and_genuine` | labeled `GENUINE_CONFLICT`, still blocked |
| 15 | `6107/6109` real production shape | `test_6107_6109_real_shape_remains_blocked_and_genuine` | labeled `GENUINE_CONFLICT`, still blocked |
| 16 | no unintended impact on `JournalID` conflict logic | `JournalStateDecisionShapes.test_phase_4y_no_unintended_impact_on_journal_id_logic` | full 5-shape matrix unchanged |
| 17 | no unintended impact on `AuthorNameRaw` conflict behavior outside case-only | `AuthorContentConflictPlanIntegration.test_phase_4y_genuine_conflict_labeled_and_still_blocks` | `I Ben Ltaifa`/`IB Ltaifa` still blocks, labeled `GENUINE_CONFLICT` |

**Full suite result**: `259 → 277 passed, 277 total` (18 new tests, 0 regressions, 0 skipped,
0 failures). The delta is exactly the 18 tests listed above; every pre-existing test's outcome
is unchanged, confirmed by re-running the complete six-file suite
(`test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 48/48,
`test_merge_execution_safety.py` 102/102, `test_merge_approval.py` 50/50,
`test_merge_executor.py` 48/48, `test_fk_lifecycle.py` 11/11).

---

## Task G — Read-Only Production Validation

Live-read (no writes) via `litrix_db.db()`, this phase:

| Pair | `ResearchPaper.JournalID` | `Authors.AuthorNameRaw` (survivor / loser) | Matches Phase 4X? |
|---|---|---|---|
| 6086 / 6088 | `None` / `771` (`LOSER_ONLY_BACKFILL`) | `"...MH Al-adaileh..."` / `"...MH Al-Adaileh..."` | Yes, byte-identical |
| 5548 / 5549 | `None` / `676` (`LOSER_ONLY_BACKFILL`) | `"...NB Aoun, MA El Affendi..."` / `"...N Ben Aoun, MAE Affendi..."` | Yes, byte-identical |
| 6107 / 6109 | `None` / `1104` (`LOSER_ONLY_BACKFILL`) | `"AM Alomari"` / `"A Alomari, F Comeau, W Phillips, N Aslam"` | Yes, byte-identical |

The narrow classifier was re-run read-only against these exact live-fetched strings:

```
6086/6088 → (True,  "values are identical after lower() alone, ... differ only in letter case")
5548/5549 → (False, "character counts differ -- not a pure case difference")
6107/6109 → (False, "character counts differ -- not a pure case difference")
```

Identical to the addendum's predictions in the Phase 4X report — confirmed against fresh live
data, not merely recalled from the prior investigation. No approval was created, no merge was
attempted, no data was modified.

---

## Task H — Future Eligibility Decision (no approval created)

**Under Policy B**: `(6086,6088)` is now correctly labeled `CASE_ONLY_FORMATTING_DIFFERENCE`
in its plan output — it is eligible for a *future human review* to see this label and factor it
into a decision, exactly as Policy B intends.

**Important gap discovered this phase, not fixed**: labeling alone does not make `(6086,6088)`
executable. `merge_executor.py::execute_approved_merge()` re-derives `author_content_conflicts()`
live at Step 15 and unconditionally returns `EXEC_BLOCKED_AUTHOR_CONFLICT` for *any* non-empty
conflict list — it has no awareness of `author_conflict_type` (that field exists only in
`merge_plan_generator.py`'s report, which the executor does not consult) and, more fundamentally,
**no code path anywhere lets a human-confirmed exception bypass this check**, regardless of the
conflict's classification. This is the same gap Phase 4V's `significant_finding` already flagged
in a different framing ("no mechanism for a human decision to bypass it"); this phase adds the
specific detail that even a correctly-labeled case-only conflict hits the identical wall.

**Per this phase's own instruction ("If you discover that a code change would be useful, STOP
and report the proposed change instead of implementing it")**, this gap is reported, not
implemented. A future phase, if authorized, would need to design a narrow, auditable override —
for example, a `MergeApproval` field recording that a human explicitly confirmed a specific
case-only exception, checked by the executor only for conflicts `is_case_only_difference()`
independently re-confirms live (never trusting a stale label) — before `(6086,6088)` could
actually be executed through the real production path. That design is out of scope here.

`(5548,5549)` and `(6107,6109)`: unaffected by any of this — both remain `GENUINE_CONFLICT`,
correctly blocked under every policy option evaluated, exactly as Phase 4X found.

---

## Safety Accounting

- **Code files modified**: 2 — `backend/tools/merge_execution_safety.py` (added
  `is_case_only_difference()`), `backend/tools/merge_plan_generator.py` (annotated
  `build_author_conflict_report()`'s output). `dedup_papers.py`, `merge_group()`,
  `merge_executor.py`, `merge_approval.py` — **untouched**.
- **Test files modified**: 2 — `backend/tools/test_merge_execution_safety.py`,
  `backend/tools/test_merge_plan_generator.py`.
- **Report files created**: 2 — this file and `backend/reports/phase4y_case_only_author_policy.json`.
- **Production DB writes**: **0.**
- **Approvals created**: **0.**
- **Approvals changed**: **0.**
- **Merges**: **0.**
- **Deletes**: **0.**
- **DOI changes**: **0.**
- **Migrations**: **0.**
- **Network calls**: **0.**
- **Changes to `ApprovalID=1` or `ApprovalID=2`**: **none** — both still `EXECUTED`, re-verified
  live at the start and end of this phase.
- **Tests**: `259 → 277` (18 new, 0 regressions).
- **Production state changed**: **NO** — `MergeApproval` still exactly
  `[(1, 'EXECUTED'), (2, 'EXECUTED')]`, `ResearchPaper` count still `2029`, re-verified live both
  before any code change and immediately before writing this report.

## Final Decision

**A) CASE_ONLY_POLICY_APPROVED_FOR_HUMAN_REVIEW**

The classification/labeling capability is implemented, tested (18 new tests, 277/277 total),
and live-validated. Case-only `AuthorNameRaw` differences are now explicitly distinguished from
genuine conflicts in the plan output, for a human reviewer's benefit — with zero change to
execution permission anywhere in the codebase. `(6086,6088)` is correctly labeled but not yet
practically actionable without a separately-authorized executor-level override mechanism, which
this phase deliberately does not build.

Per your instructions, I am stopping here. Phase 4Z is not started. No approval was created. No
merge was executed. No production data was changed.
