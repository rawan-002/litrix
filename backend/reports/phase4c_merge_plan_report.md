# Phase 4C — Read-Only Safe Merge Plan Generator

## 1. Scope and Safety Confirmation

- Source/execution code modified: **0** (`dedup_papers.py` untouched — confirmed by re-running its existing test suite unchanged, §9)
- New code files created: **2** (`backend/tools/merge_plan_generator.py`, `backend/tools/test_merge_plan_generator.py`)
- Report files created: **2** (`backend/reports/phase4c_merge_plans.json`, this file)
- Database writes (INSERT/UPDATE/DELETE/TRUNCATE/ALTER/migration): **0**
- Merges executed: **0**
- DOI values assigned/changed/cleared: **0**
- Network calls: **0**
- `--apply` invocations: **0**
- Calls to `dedup_papers.py`'s `merge_group()`: **0** — enforced structurally, not just by convention: `test_merge_plan_generator.py::test_no_execution_hook_exists_in_module_source` statically parses every `cur.execute(...)` call in `merge_plan_generator.py` and fails the build if any contains `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`/`MERGE`/`ALTER`/`DROP`, and separately asserts `merge_group` is never imported.

## 2. Files Modified / Created

**Created (new, untracked):**
- `backend/tools/merge_plan_generator.py` — the plan generator itself
- `backend/tools/test_merge_plan_generator.py` — 35 unit tests, no DB/network
- `backend/reports/phase4c_merge_plans.json` — generated plan output (11 plans: 10 forensic pairs + 1 validation fixture)
- `backend/reports/phase4c_merge_plan_report.md` — this file

**Modified: 0.** `dedup_papers.py` was read and imported from (its pure functions `choose_keep`, `choose_keep_reason`, `pair_confidence`, `hard_exclusion_reason`, `_is_distinct_record`, `norm_title`) but never edited. Its own test suite (`backend/tools/test_dedup_papers.py`) was re-run unchanged and still passes 8/8 (§9).

**Pre-existing, unrelated changes already in the working tree** (from earlier, separate work in this repo — not touched, not authored, not reviewed by this phase): `backend/accounts/sync_views.py`, `backend/affiliation_verifier.py`, `backend/analytics/ai_tools.py`, `backend/analytics/ai_views.py`, `backend/analytics/management/commands/backfill_dois.py`, `backend/analytics/views.py`, `backend/backfill_missing_dois.py`, `backend/find_missing_dois.py`, `backend/scrapers/orcid.py`, `frontend/src/app/app.routes.ts`, plus several new untracked files (`backend/scrapers/openalex_new_papers.py`, `backend/tools/ai_eval.py`, various `backend/tools/*.py`, migration SQL files). None of these are part of Phase 4C; see §10/§11 (`git status --short` / `git diff --stat`) for the complete, unfiltered picture.

## 3. Schema and Dependency Map (fresh evidence, gathered read-only this phase)

**All 33 `ResearchPaper` columns**, with nullability, from `information_schema.columns`:

| Column | Nullable | Type | Column | Nullable | Type |
|---|---|---|---|---|---|
| PaperID | NO | integer | CitationsByYear | YES | jsonb |
| JournalID | YES | integer | TenantID | YES | integer |
| Title | **NO** | text | AffiliationVerified | YES | boolean |
| Title_En | YES | text | VerificationSource | YES | varchar |
| Abstract | YES | text | VerifiedAt | YES | timestamp |
| Abstract_En | YES | text | VerificationDetails | YES | jsonb |
| Language | YES | varchar | VenueType | YES | varchar |
| DOI | YES | varchar | DoiResolvedBy | YES | varchar |
| PubYear | YES | integer | DoiResolvedAt | YES | timestamptz |
| Volume | YES | varchar | OpenAlexWorkID | YES | varchar |
| Issue | YES | varchar | AbstractSource | YES | varchar |
| Pages | YES | varchar | PdfUrl | YES | text |
| IsVerified | YES | boolean | PdfAccessType | YES | varchar |
| ScrapedAt | YES | timestamptz | PublicationType | YES | varchar |
| Source | YES | varchar | | | |
| RawData_Log | YES | jsonb | | | |
| SearchVector_En | YES | tsvector | | | |
| SearchVector_Ar | YES | tsvector | | | |
| NormalizedTitle | YES | text | | | |
| Indexing | YES | varchar | | | |

Only `PaperID` and `Title` are `NOT NULL`. Every other column can legitimately be empty on either side of a pair — this is why the plan generator's `EMPTY_BOTH`/`ONE_SIDED` handling matters: almost every field can take that shape.

**Every FK referencing `ResearchPaper.PaperID`**, from `information_schema.table_constraints` + `referential_constraints` (independently re-derived this phase — not assumed from `dedup_papers.py`'s `SIMPLE_CHILDREN` list):

| Table | FK column | ON DELETE | In `SIMPLE_CHILDREN`? | Remap-able by simple FK reassignment, or needs merge/dedup? |
|---|---|---|---|---|
| `Authors` | PaperID | NO ACTION | No (special-cased) | Needs merge/dedup — `(UserID,PaperID)` unique index means a naive reassignment could collide; `merge_group()` already handles this via `ON CONFLICT DO NOTHING` |
| `Citations` | PaperID | NO ACTION | No (special-cased) | Needs merge/dedup — one row per paper (`PaperID` alone is the key); `merge_group()` already handles this via `GREATEST()` |
| `ExternalAuthors` | PaperID | NO ACTION | Yes | Simple FK reassignment is structurally safe (no unique constraint blocking it observed) |
| `PaperKeywords` | PaperID | NO ACTION | Yes | N/A — table does not exist in this database (confirmed via `information_schema.tables`); dead list entry |
| `CitationsHistory` | PaperID | NO ACTION | Yes | Simple FK reassignment, structurally safe |
| `ReportPaperDecision` | PaperID | **SET NULL** | Yes | Simple FK reassignment; **correction to Phase 4B**, which reported this as `NO ACTION` — fresh evidence this phase shows `SET NULL`. In practice low-impact since `SIMPLE_CHILDREN`'s `remap_simple_child()` runs *before* the loser row is deleted, so the `SET NULL` fallback shouldn't normally fire — but it means a remap failure here fails *silently* (orphaned-but-still-present row with `PaperID=NULL`) rather than blocking the delete. |
| `ReportPaperDecision` | **MissingResolvedToPaperID** (2nd, separate FK on the same table) | SET NULL | **No** | Not remapped by any existing logic. Same silent-orphan failure mode as above, but with zero existing remap code to run first. |
| `AuthorReviewQueue` | PaperID | **CASCADE** | **No** | Not remapped, not special-cased, **not included in `snapshot_paper()`'s captured child tables either** — a real merge today would delete these rows with zero recovery snapshot. |

Row-count facts carried forward from Phase 4B (both tables are 0-impact today, re-confirmed as still relevant since neither the schema nor these counts change without a write, which this phase never performs): `AuthorReviewQueue` = 0 rows DB-wide; `ReportPaperDecision` = 44 rows, 0 with `MissingResolvedToPaperID` set.

## 4. All 10 Pair Summaries

Every plan below was produced by `merge_plan_generator.py` run once, read-only, against the live DB (`python tools/merge_plan_generator.py`). Full detail: `backend/reports/phase4c_merge_plans.json`.

| Pair (winner/loser) | Classification | `confidence` | Unresolved conflicts | Data-loss risks (JournalID etc.) |
|---|---|---|---|---|
| 5207 / 5481 | **SAFE_PLAN_CANDIDATE** | high | none | none |
| 5232 / 5482 | **SAFE_PLAN_CANDIDATE** | high | none | none |
| 5548 / 5549 | **SAFE_PLAN_CANDIDATE** | high | none | JournalID: loser has `676`, winner `NULL` — COPY_LOSER, deterministic, not implemented by `merge_group()` today |
| 6086 / 6088 | **SAFE_PLAN_CANDIDATE** | high | none | JournalID: loser has `771`, winner `NULL` — same as above |
| 6153 / 6189 | **SAFE_PLAN_CANDIDATE** | high | none | none |
| 5329 / 5434 | **PLAN_REQUIRES_HUMAN_APPROVAL** | high | PubYear (2019 vs 2020); PublicationType ("Conference Paper" vs "Research Article") | none |
| 3875 / 6091 | **PLAN_REQUIRES_HUMAN_APPROVAL** | high | Language ("English" vs "en"); Source ("Scopus" vs "Scholar") | none |
| 6645 / 7572 | **PLAN_REQUIRES_HUMAN_APPROVAL** | high | PubYear (2026 vs 2025); VenueType ("Journal" vs "Preprint") | none |
| 5289 / 5392 | **PLAN_REQUIRES_HUMAN_APPROVAL** | high | PublicationType; VenueType; VerificationDetails (differing evidence trails) | none |
| 6107 / 6109 | **PLAN_REQUIRES_HUMAN_APPROVAL** | high | PubYear (2018 vs 2017) | JournalID: loser has `1104`, winner `NULL` |

This reproduces Phase 4B.1's manually-derived 5 SAFE / 5 HUMAN_APPROVAL split **exactly**, field-for-field, with zero disagreements — an independent, code-based cross-validation of that forensic work, not a re-statement of it. No pair was reclassified. No pair evaluated as `BLOCKED`.

Every `survivor_reason` across all 10 pairs is `has_doi` — the generator's `build_survivor_reason()` never invents a new heuristic; it only labels the existing `choose_keep()` decision, and in this set that decision is always driven by the DOI-presence tiebreak (matching the "do not invent any new survivor-selection heuristic" instruction).

## 5. Full Unresolved-Conflict List

Every field that produced a `CONFLICT` verdict across the 10 pairs, with counts:

| Field | Pairs affected | Deterministic rule exists? |
|---|---|---|
| PublicationType | 5329/5434, 5289/5392 (2) | No |
| VenueType | 6645/7572, 5289/5392 (2) | No |
| PubYear | 5329/5434, 6645/7572, 6107/6109 (3) | No — `_years_compatible()` only governs whether the pair still groups (gap ≤1), not which year the merged record should keep |
| Language | 3875/6091 (1) | No |
| Source | 3875/6091 (1) | No |
| VerificationDetails | 5289/5392 (1) | No — both sides reached the same confidence tier via different underlying evidence URLs |

No field conflict in this set has a repository-backed deterministic resolution rule. None were invented for this phase, per the explicit instruction not to invent rules for `PublicationType`, `VenueType`, `PubYear`, `Language`, `Source`, or `JournalID`.

## 6. JournalID Findings

`JournalID` is reported for every one of the 10 pairs (never silently omitted) and correctly classified in all four possible shapes:

- **Winner only** (`KEEP_WINNER`): 5207/5481, 5232/5482, 6153/6189, 5329/5434, 3875/6091, 6645/7572, 5289/5392 — 7 pairs
- **Loser only** (`COPY_LOSER`): 5548/5549 (676), 6086/6088 (771), 6107/6109 (1104) — 3 pairs, all flagged explicitly as `data_loss_risks`
- **Equal**: 0 pairs in this set
- **Conflict** (both populated, different): 0 pairs in this set

This directly closes the Phase 4B.1 planning gap: `merge_group()` today has no mechanism to backfill `JournalID` from the loser, so 3 of the 10 pairs (including 2 of the 5 `SAFE_PLAN_CANDIDATE` pairs) would silently lose a real, populated `JournalID` value if merged with the current, unmodified code. The plan schema now makes this explicit and deterministic (`BACKFILL_FROM_LOSER`) rather than silently discarded.

## 7. Nonzero-Child Validation Case

None of the 10 forensic pairs' **losers** have nonzero rows in any FK-dependent table (confirmed independently again this phase — same finding as Phase 4B.1). To validate that `child_table_actions` correctly reports real nonzero dependency data (not just zero-row no-ops), the generator was additionally run once against a **synthetic validation fixture**:

**PaperID 3875 (a real winner from the 10 pairs, DOI `10.1155/2019/4568368`) paired against PaperID 3898** (a real, unrelated row — "Teaching and learning computer science at Al Baha University..." DOI `10.1109/latice.2015.50`, 2015 — chosen specifically because it has 25 real rows in `ExternalAuthors`, the highest count of any `ResearchPaper` row in the database).

**This is not a claimed real duplicate.** No merge is implied, suggested, or performed. The pair was run through the exact same code path as the 10 real pairs, and the result is itself informative:

- `pair_confidence` = `review` (not `high`)
- `hard_exclusion_reason` = `different_doi_and_year_gap_gt_2` (both sides have real, different DOIs, and the year gap is 4 — 2015 vs 2019)
- **Overall classification: `BLOCKED`** — the generator correctly refused to treat two unrelated real papers as a safe merge candidate, precisely because both carry independent DOIs with an incompatible year gap. This is a stronger validation than a passive "it produced some output" check: it demonstrates the classifier's structural safety logic (§ hard_exclusion_reason) fires correctly even when child-table data alone might otherwise look mergeable.
- **`child_table_actions` for `ExternalAuthors` still reports correctly, independent of the pair-level `BLOCKED` verdict**: `winner_rows: 5` (3875's real rows), `loser_rows: 25` (3898's real rows), `planned_action: "REMAP_TO_SURVIVOR"`, `handled_by_merge_group_today: true`, `risk: "low -- existing remap_simple_child() bulk UPDATE with a per-row SAVEPOINT/conflict-drop fallback"`.

This proves the dependency-reporting mechanics work correctly against real, nonzero DB data — exact counts, correct FK column, correct planned action — while also proving the generator doesn't rubber-stamp a pair just because its dependency tables look remappable.

No child rows were changed, read, or touched beyond a `COUNT(*)` query. No row from either table was modified.

## 8. Every Discovered Data-Loss Risk

Consolidated from all 11 generated plans:

1. **`JournalID`** silently discarded from the loser in 3/10 pairs (5548/5549, 6086/6088, 6107/6109) — the single most common concrete data-loss finding, present in 2 of the 5 `SAFE_PLAN_CANDIDATE` pairs. Deterministic fix available (`BACKFILL_FROM_LOSER`), not implemented in `merge_group()` today.
2. **`AuthorReviewQueue`** — `ON DELETE CASCADE`, zero remap logic, zero recovery snapshot. Zero current rows DB-wide means zero current impact, but this is a structural gap that would fire silently and irrecoverably the moment this table is populated.
3. **`ReportPaperDecision.MissingResolvedToPaperID`** — `ON DELETE SET NULL`, zero remap logic. Zero current rows with this column set means zero current impact, but a real "resolved to" link would be silently cleared (not erred, not cascaded — just quietly forgotten) if this ever has data.
4. Every field in §5's conflict table represents data that is **not lost** outright but **requires a human decision** the current codebase cannot make deterministically — listed separately from data-loss risks because a human decision, once made, fully resolves them; they are not silent failures.

No new data-loss risk categories beyond JournalID were found in the specific 10-pair set for the reasons given in Phase 4B.1 (all loser dependency-row counts were 0 for tables other than the fixture case in §7).

## 9. Test Results

**New tests** (`backend/tools/test_merge_plan_generator.py`, pure functions, no DB/network):

```
Ran 35 tests in 0.001s
OK
```

Coverage against the Task G minimum list: DOI-present survivor selection (`SurvivorSelectionReporting`, 2 tests) · equal field values (`test_equal_values`) · winner-only value (`test_winner_only_value_is_kept_no_loser_contribution`) · loser-only value (`test_loser_only_value_is_copy_candidate`) · genuine unresolved conflict (`test_genuine_conflict_has_no_deterministic_rule`) · JournalID loser-only and conflict cases (`JournalIdSpecificCases`, 5 tests) · a child-table dependency with nonzero loser rows (`test_simple_children_remap_with_nonzero_loser_rows`, loser_rows=25) · no execution path / `execution_permitted` always `false` (`FullPlanNeverPermitsExecution`, 4 tests including the static-source-scan guard).

**Existing test suite re-run unchanged** (`backend/tools/test_dedup_papers.py` — confirms `dedup_papers.py` itself was not touched or affected):

```
Ran 8 tests in 0.001s
OK
```

**Live, read-only run against the real DB** (`python tools/merge_plan_generator.py`): 11 plans generated (10 forensic pairs + 1 fixture), written to `backend/reports/phase4c_merge_plans.json`. Zero errors, zero writes (connection closed immediately after the last SELECT).

## 10. Whether Any Plan Could Theoretically Be Executed Later

**No plan produced by this phase is executable as-is, by design** — every plan (all 11) carries `"execution_permitted": false`, and there is no code path anywhere in `merge_plan_generator.py` capable of flipping it (verified structurally, not just by inspection — §1).

Looking ahead, hypothetically: the 5 `SAFE_PLAN_CANDIDATE` plans have no unresolved field conflicts and no unhandled dependency risk in this specific set, **but 2 of them (5548/5549, 6086/6088) still require a `JournalID` backfill capability that does not exist in `merge_group()` today.** So even the "safest" plans in this batch are not executable by the current, unmodified merge code without first adding that backfill — a future Phase 4D-or-later implementation decision, explicitly out of scope here. The 5 `PLAN_REQUIRES_HUMAN_APPROVAL` plans additionally need a human to resolve the specific listed field conflicts (§5) before they could even be considered. No `BLOCKED` classification occurred among the 10 real forensic pairs (only the synthetic fixture triggered it, correctly).

## 11. Explicit Statement

**This phase did not execute any merge.** Zero `ResearchPaper` rows, zero child-table rows, and zero DOI values were read for any purpose other than SELECT-based reporting, and none were written, updated, or deleted. `dedup_papers.py`'s `merge_group()` was never called, `--apply` was never invoked, and no migration or schema change was made. All 11 generated plans are inert JSON artifacts for human review.

---

## Appendix: `git status --short`

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

**Only `backend/tools/merge_plan_generator.py`, `backend/tools/test_merge_plan_generator.py`, and everything under `backend/reports/` (untracked as a whole directory, spanning every phase since Phase 3F) are this phase's output.** Every `M` line and every other `??` line pre-dates this phase and belongs to separate, earlier work in this repository — not reviewed, not modified, not attributable to Phase 4C.

## Appendix: `git diff --stat`

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
 frontend/src/app/app.routes.ts                     | 141 +++---
 10 files changed, 1199 insertions(+), 385 deletions(-)
```

**None of these 10 files were touched by Phase 4C** — `git diff --stat` only shows tracked files with unstaged modifications, and this phase created only new, untracked files (which `diff --stat` does not list). This diff is entirely pre-existing, unrelated work.
