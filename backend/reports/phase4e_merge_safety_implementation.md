# Phase 4E — Merge Safety Implementation (JournalID + AuthorNameRaw)

## 1. Exact Scope

**Files modified (2):**

| File | Reason |
|---|---|
| `backend/tools/dedup_papers.py` | Added two new, pure, additive functions at the bottom of the file, after `main()`: `journal_id_decision()` (Gap 1) and `author_content_conflicts()` (Gap 2). Named this the primary target per the task's explicit scope. Nothing existing in the file was edited — confirmed by `git diff`: **91 insertions, 0 deletions**. `detect_groups()`, `block_key()`, `pair_confidence()`, `hard_exclusion_reason()`, `choose_keep()`, `merge_group()`, and every threshold/constant are byte-for-byte unchanged. |
| `backend/tools/test_dedup_papers.py` | Added two new test classes (`JournalIdDecisionModel`, `AuthorContentConflictDetection`, 10 tests total) covering the two new functions. **88 insertions, 1 deletion** (the 1 deletion is the old import statement replaced by a wider one listing the new names). No existing test was changed or removed. |

**Files modified (2, untracked from Phase 4C, not shown by `git diff --stat` but genuinely changed this phase):**

| File | Reason |
|---|---|
| `backend/tools/merge_plan_generator.py` | Secondary target, used exactly as scoped ("only if necessary"). Added `build_journal_state()` and `build_author_conflict_report()` (thin wrappers exposing the two new `dedup_papers.py` decision functions in the plan schema, parallel to the existing `doi_state` pattern), a new `fetch_authors_rows()` SELECT-only DB helper, and wired both into `generate_pair_plan()`/`generate_plan_for_real_pair()`/`compute_classification()`. Also changed `classify_field()`'s handling of the `JournalID` column: it now defers to `journal_state` (returns `STATUS_UNKNOWN`/`SEE_JOURNAL_STATE`) instead of a generic `COPY_LOSER`/`CONFLICT` verdict — the exact same deferral pattern the module already used for `DOI` → `doi_state`, applied consistently to avoid a duplicate/inconsistent blocker message once `journal_state` also exists. |
| `backend/tools/test_merge_plan_generator.py` | Added `JournalStateDecisionShapes` (5 tests), `AuthorContentConflictPlanIntegration` (3 tests), 3 new tests in `FullPlanNeverPermitsExecution` (end-to-end blocking checks), and intentionally rewrote 2 of the 5 pre-existing `JournalIdSpecificCases` tests (the 3 that asserted the *old* generic `COPY_LOSER`/`CONFLICT` verdict for `JournalID` — now asserts the deferral to `journal_state` instead, consistent with the `classify_field()` change above) plus 1 unchanged assertion (`test_journal_id_always_present_in_field_actions`, updated only to check `recommended_action == "SEE_JOURNAL_STATE"` instead of the old status). No test was deleted; every prior guarantee (no write SQL, no `merge_group` import, `execution_permitted` always `false`) is still asserted and still passes. |

**Files explicitly untouched:** `doi_pipeline/` (all of it), `find_missing_dois.py`, `affiliation_verifier.py`, `backend/analytics/management/commands/backfill_dois.py`, `backend/backfill_missing_dois.py` — none read or written this phase. `merge_group()` itself, `detect_groups()`, `block_key()`, `pair_confidence()`, and every duplicate-classification threshold in `dedup_papers.py` are unchanged (verified via `git diff` showing 0 deletions in that file, and via the pre-existing 8-test suite passing unchanged).

**Files created (2, this phase's report output):** `backend/reports/phase4e_merge_safety_implementation.md` (this file), `backend/reports/phase4e_safe_pair_reassessment.json`. `backend/reports/phase4c_merge_plans.json` was **regenerated** (not newly created — it already existed from Phase 4C) by re-running the now-updated `merge_plan_generator.py`, so every one of its 11 plans now carries the new `journal_state`/`author_content_conflicts` fields.

## 2. JournalID Fix (Gap 1)

**Previous unsafe behavior:** `merge_group()` never reads or writes `ResearchPaper.JournalID` at all — confirmed by re-reading the function this phase (unchanged since Phase 4B/4C/4D). If the loser has a populated `JournalID` and the winner does not, that value is silently discarded the instant the loser row is deleted, with no error, no log line, and no flag anywhere in the merge output. Phase 4D found this concretely in 2 of the 5 SAFE pairs (5548/5549, 6086/6088).

**Implemented decision states** (`dedup_papers.py::journal_id_decision()`, wrapped for the plan by `merge_plan_generator.py::build_journal_state()`): exactly the 5 states specified —

| State | Meaning | `execution_permitted` |
|---|---|---|
| `NO_JOURNAL` | both sides empty | `true` |
| `WINNER_ONLY` | winner has a value, loser doesn't | `true` |
| `LOSER_ONLY_BACKFILL` | winner empty, loser populated — the one deterministic case | `true` (planned action: backfill) |
| `EQUAL` | both populated, identical | `true` |
| `CONFLICT` | both populated, **different** | **`false`** — no winner is chosen, `blocking_reason` is always set |

No broader field-reconciliation policy was built — this function only ever looks at `JournalID`; it has no awareness of any other column.

**Test evidence:** `test_dedup_papers.py::JournalIdDecisionModel` — 5/5 passing (both-NULL, winner-only, loser-only-backfill, equal, conflict-not-a-silent-choice). `test_merge_plan_generator.py::JournalStateDecisionShapes` — 5/5 passing, at the plan-object level (asserting `execution_permitted`/`blocking_reason`/`winner_value`/`loser_value` are all correctly populated, not just the bare state string).

**Read-only validation results:** re-running the generator against the live DB (§4) reproduced exactly the 3 shapes Phase 4D already found among the 5 SAFE pairs — `WINNER_ONLY` for 5207/5481 (440/–), 5232/5482 (1803/–), 6153/6189 (676/–); `LOSER_ONLY_BACKFILL` for 5548/5549 (–/676) and 6086/6088 (–/771). **Zero `CONFLICT` states occurred in this 5-pair set** (no pair has two different, real `JournalID`s) — consistent with Phase 4C/4D's earlier finding that this specific set has no `JournalID` ambiguity, only a missing-backfill gap.

## 3. AuthorNameRaw Fix (Gap 2)

**Exact previous silent-loss behavior:** `merge_group()`'s `Authors` remap is `INSERT ... SELECT ... FROM "Authors" WHERE "PaperID"=%loser ON CONFLICT ("UserID","PaperID") DO NOTHING`, followed by `DELETE FROM "Authors" WHERE "PaperID"=%loser`. When winner and loser share a `UserID` (true for every one of the 5 SAFE pairs — each has exactly one shared author), the `INSERT` collides on the existing `uq_authors_user_paper` unique index, `ON CONFLICT DO NOTHING` silently no-ops the insert, and the loser's row — including whatever `AuthorNameRaw` variant it carried — is then deleted. No error, no log, no flag; the only recovery path is the generic pre-`--apply` snapshot, which nothing points a reviewer toward.

**How conflicts are now detected:** `dedup_papers.py::author_content_conflicts(winner_authors, loser_authors)` uses **exactly the same identity key `merge_group()`'s own SQL already uses** — `UserID`, matched against the `(UserID, PaperID)` unique index — no new identity concept, no fuzzy matching, no name normalization. For every `UserID` present in both lists, it compares `AuthorNameRaw` with plain `!=` (exact string comparison); a difference produces one conflict record, an exact match produces none. A `UserID` present on only one side is never a conflict (per the spec: nothing is lost from a side that never had the row).

**What data is exposed:** each conflict carries `UserID`, `winner_author_name_raw`, and `loser_author_name_raw` — the **exact, unmodified raw strings**, not a summary or a diff. `merge_plan_generator.py::build_author_conflict_report()` wraps the list into `{"conflicts": [...], "execution_permitted": bool, "blocking_reason": str|None}` and the full pair plan exposes it as a top-level `author_content_conflicts` array (never nested inside a generic field list, matching the requested "exact affected author identity/key where available" and "both conflicting AuthorNameRaw values").

**Why no automatic formatting choice was invented:** the task explicitly forbids it ("Do NOT automatically choose a preferred formatting... Do not invent one"), and no existing, repository-backed rule for picking between two raw-name variants exists anywhere in `dedup_papers.py` today. Any non-empty conflict list therefore sets `execution_permitted: false` unconditionally — there is no severity threshold, no "prefer the longer string" heuristic, nothing that could silently resolve it. This mirrors exactly how a generic `ResearchPaper` field `CONFLICT` (e.g. `PublicationType`) is already handled elsewhere in the plan.

**Test evidence:** `test_dedup_papers.py::AuthorContentConflictDetection` — 5/5 passing: identical-name-no-conflict, differing-formatting-is-a-conflict, only-the-actually-conflicting-row-is-reported (multi-author case — a shared, identical-name co-author must not appear alongside the real conflict), no-shared-UID-no-false-conflict, and a test named explicitly for the historical failure mode (`test_conflict_is_not_hidden_by_the_real_merge_paths_on_conflict_do_nothing`) asserting the detector does *not* reproduce the SQL's silence. `test_merge_plan_generator.py::AuthorContentConflictPlanIntegration` — 3/3 passing at the plan-object level. `FullPlanNeverPermitsExecution::test_author_content_conflict_blocks_the_whole_pair` — confirms an author conflict alone (with an otherwise pristine pair) correctly flips pair-level `classification` to `PLAN_REQUIRES_HUMAN_APPROVAL` and keeps `execution_permitted` `false`.

**Read-only validation results:** re-running the generator against the live DB found the **same 4 conflicts Phase 4D found by hand**, byte-for-byte identical raw strings, one per pair: `{5207,5481}` UserID 97 ("...I Ben Ltaifa..." vs "...IB Ltaifa..."), `{5548,5549}` UserID 104 ("...NB Aoun..." vs "...N Ben Aoun..."), `{6086,6088}` UserID 105 ("...Al-adaileh..." vs "...Al-Adaileh..."), `{6153,6189}` UserID 112 ("...F Kamal Alsheref..." vs "...FK Alsheref..."). `{5232,5482}` correctly produced **zero** conflicts (its shared author's `AuthorNameRaw` is byte-identical on both sides) — matching Phase 4D's finding that this is the one clean pair.

## 4. Five-Pair Reassessment

| Pair (winner/loser) | Previous Phase 4D verdict | JournalID result | AuthorNameRaw result | New Phase 4E verdict | Execution performed? |
|---|---|---|---|---|---|
| 5207 / 5481 | READY_AFTER_IMPLEMENTATION | `WINNER_ONLY` (440/–, no action needed) | 1 conflict, UserID 97 | **HUMAN_REVIEW_REQUIRED** | **NO** |
| 5232 / 5482 | EXECUTION_READY | `WINNER_ONLY` (1803/–, no action needed) | 0 conflicts | **EXECUTION_READY** | **NO** |
| 5548 / 5549 | READY_AFTER_IMPLEMENTATION | `LOSER_ONLY_BACKFILL` (–/676) | 1 conflict, UserID 104 | **HUMAN_REVIEW_REQUIRED** | **NO** |
| 6086 / 6088 | READY_AFTER_IMPLEMENTATION | `LOSER_ONLY_BACKFILL` (–/771) | 1 conflict, UserID 105 | **HUMAN_REVIEW_REQUIRED** | **NO** |
| 6153 / 6189 | READY_AFTER_IMPLEMENTATION | `WINNER_ONLY` (676/–, no action needed) | 1 conflict, UserID 112 | **HUMAN_REVIEW_REQUIRED** | **NO** |

**Why the verdict category shifted from Phase 4D's `READY_AFTER_IMPLEMENTATION` to `HUMAN_REVIEW_REQUIRED` for 4 pairs, honestly explained rather than smoothed over:** Phase 4D (my own earlier judgment call) treated the `AuthorNameRaw` gap as "a missing, obvious, low-ambiguity operation" — closer in spirit to the `JournalID` backfill. **This phase's explicit instructions are stricter and supersede that judgment**: "The pair must not be automatically executable until a deterministic resolution rule exists or explicit human approval is provided" and "Do not invent one." Implementing that instruction literally means any `AuthorNameRaw` conflict blocks execution pending a human decision — which is a `HUMAN_REVIEW_REQUIRED`-class outcome, not `READY_AFTER_IMPLEMENTATION`. This is a **deliberate policy tightening**, not a new problem discovered — no new data-loss path was found beyond what Phase 4D already identified (§5 confirms the count is still exactly 2 JournalID cases + 4 AuthorNameRaw cases, same as Phase 4D). `5232/5482` remains the sole clean pair, exactly as Phase 4D found — confirming no new issue was introduced by this implementation.

For `5548/5549` and `6086/6088` specifically: even after a human resolves the `AuthorNameRaw` conflict, these two would *still* need the `JournalID` backfill **implemented** (not just planned — `merge_group()` has zero code for it today) before being genuinely `EXECUTION_READY`. This is stated explicitly here rather than left implicit, per the instruction not to hide additional caveats behind a verdict label.

## 5. Safety Accounting

- DB writes: **0**
- Network calls: **0**
- Records merged: **0**
- DOI changes: **0**
- `--apply` executions: **0**
- `execution_permitted`: verified `false` on every one of the 11 regenerated plans (`grep '"execution_permitted": true' backend/reports/phase4c_merge_plans.json` → 0 matches) — including the one `EXECUTION_READY`-labeled pair (5232/5482): `EXECUTION_READY` is a §4 *assessment* label in this report, never a field this phase's code writes into the plan JSON itself.

## 6. Test Results

| Suite | Before this phase | After this phase | New tests added |
|---|---|---|---|
| `backend/tools/test_dedup_papers.py` | 8/8 passing | **18/18 passing** | 10 (`JournalIdDecisionModel` ×5, `AuthorContentConflictDetection` ×5) |
| `backend/tools/test_merge_plan_generator.py` | 35/35 passing | **43/43 passing** | 8 (`JournalStateDecisionShapes` ×5, `AuthorContentConflictPlanIntegration` ×3) + 2 rewritten (`JournalIdSpecificCases`, intentionally updated for the new deferral behavior — see §1) + 3 more added inline to `FullPlanNeverPermitsExecution` (journal-conflict blocks pair, author-conflict blocks pair, clean-state does not block) |

Both suites run clean together, no failures, no skips. The pre-existing static-source-scan guard (`test_no_execution_hook_exists_in_module_source` — no write-verb SQL, no `merge_group` import anywhere in `merge_plan_generator.py`) still passes unmodified against the updated file, including its new `fetch_authors_rows()` SELECT-only helper.

## 7. Diff Accounting

**`git diff --stat`** (full, unfiltered):

```
 backend/accounts/sync_views.py                     |  48 ++-
 backend/affiliation_verifier.py                    |  15 +
 backend/analytics/ai_tools.py                      | 426 ++++++++++++++++---
 backend/analytics/ai_views.py                      | 183 +++++++-
 backend/analytics/management/commands/backfill_dois.py | 26 +-
 backend/analytics/views.py                         | 153 ++++---
 backend/backfill_missing_dois.py                   |  30 ++
 backend/find_missing_dois.py                       | 472 +++++++++++++--------
 backend/scrapers/orcid.py                          |  90 +++-
 backend/tools/dedup_papers.py                      |  91 ++++
 backend/tools/test_dedup_papers.py                 |  88 +++-
 frontend/src/app/app.routes.ts                     | 141 +++---
 12 files changed, 1377 insertions(+), 386 deletions(-)
```

**`git status --short`** (full, unfiltered):

```
 M backend/accounts/sync_views.py
 M backend/affiliation_verifier.py
 M backend/analytics/ai_tools.py
 M backend/analytics/ai_views.py
 M backend/analytics/management/commands/backfill_dois.py
 M backend/analytics/views.py
 M backend/backfill_missing_dois.py
 M backend/find_missing_dois.py
 M backend/scrapers/orcid.py
 M backend/tools/dedup_papers.py
 M backend/tools/test_dedup_papers.py
 M frontend/src/app/app.routes.ts
?? LITRIX_AI_CHATBOT.md
?? backend/doi_pipeline/
?? backend/fix_uid85_wrong_openalex_id.py
?? backend/migrations/20260809_identifier_paper_evidence.sql
?? backend/migrations/20260810_publication_type.sql
?? backend/reports/
?? backend/scrapers/openalex_new_papers.py
?? backend/test_find_missing_dois.py
?? backend/tools/ai_eval.py
?? backend/tools/backfill_abstracts.py
?? backend/tools/classify_publication_type.py
?? backend/tools/discover_csv_identifiers.py
?? backend/tools/discover_missing_identifiers.py
?? backend/tools/merge_identifiers.py
?? backend/tools/merge_plan_generator.py
?? backend/tools/summarize_staging.py
?? backend/tools/sync_all_researchers.py
?? backend/tools/test_merge_plan_generator.py
```

**Files changed by this phase, explicitly separated from pre-existing modifications:**
- **This phase (Phase 4E), tracked and shown by `git diff`:** `backend/tools/dedup_papers.py` (91 insertions, 0 deletions), `backend/tools/test_dedup_papers.py` (88 insertions, 1 deletion).
- **This phase (Phase 4E), untracked so not shown by `git diff --stat`, but genuinely modified this phase:** `backend/tools/merge_plan_generator.py`, `backend/tools/test_merge_plan_generator.py` (both created in Phase 4C, extended this phase — see §1 for the exact changes). `backend/reports/` contents updated/added (already untracked as a whole directory since Phase 3F).
- **Pre-existing, unrelated to any phase of this duplicate-record project** (10 files: `sync_views.py`, `affiliation_verifier.py`, `ai_tools.py`, `ai_views.py`, `backfill_dois.py`, `views.py`, `backfill_missing_dois.py`, `find_missing_dois.py`, `orcid.py`, `app.routes.ts`, plus several untracked files like `openalex_new_papers.py`) — none read, none touched, same as every prior phase's report notes.

## Final Decision

**A) IMPLEMENTATION VALIDATED — SAFE TO DESIGN EXECUTOR NEXT**

Both Phase 4D silent-loss paths are now explicitly represented and cannot be silently lost by anything this phase built: `journal_state` always reports one of the 5 required states with an explicit `blocking_reason` when ambiguous, and `author_content_conflicts` always surfaces the exact raw values for any same-`UserID` mismatch, using the repository's own existing `(UserID,PaperID)` identity key with zero fuzzy matching or invented normalization. Re-running the (now-updated) plan generator against the same 5 Phase 4D pairs reproduced Phase 4D's findings exactly — the same 2 `JournalID` backfill cases, the same 4 `AuthorNameRaw` conflicts, the same 1 clean pair (5232/5482) — with **no new, previously-undiscovered issue surfaced**, which is itself informative: it means Phase 4D's manual audit was already complete and accurate for this 5-pair set, and this phase's implementation is a faithful, mechanical codification of it rather than a discovery of something new.

The verdict distribution changed (1 EXECUTION_READY / 4 HUMAN_REVIEW_REQUIRED, vs. Phase 4D's 1 EXECUTION_READY / 4 READY_AFTER_IMPLEMENTATION) because this phase's explicit instructions deliberately tightened the `AuthorNameRaw` policy from "an obvious operation to build" to "requires human approval, no automatic formatting choice, ever" — a policy decision stated directly in the task, not a new risk this implementation uncovered. `execution_permitted` remained hardcoded `false` on every single plan throughout, `--apply`/`merge_group()`/any write path was never invoked, and both new test suites (18/18, 43/43) plus a fresh read-only DB run confirm the representation is correct against live data, not just synthetic fixtures. That combination — narrow, tested, validated, zero regressions, zero scope creep into `merge_group()` or `detect_groups()` — is exactly the state a future executor-design phase would need to start from safely.

Per your instructions, I am stopping here. Phase 4F is not started.
