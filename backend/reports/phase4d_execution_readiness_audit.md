# Phase 4D — Safe Merge Plan Execution-Readiness Audit (Read-Only)

## 1. Safety Confirmation

- Code files modified: **0** (`dedup_papers.py` and `merge_plan_generator.py` both untouched — this phase only *imports* `merge_plan_generator.py`'s existing pure functions, never edits them)
- Code files created: **0** (the analysis script for this phase lives in the session scratchpad, not the repo — this phase's only repository footprint is the two report files below, per the task's own suggested scope)
- Report files created: **2** — `backend/reports/phase4d_execution_readiness.json`, `backend/reports/phase4d_execution_readiness_audit.md`
- DB writes: **0**
- Network calls: **0**
- Records merged: **0**
- `execution_permitted`: remains `false` everywhere — no plan in this phase's output sets it any other way, and nothing in this phase's code path is capable of flipping it (it was never even re-derived; this audit reads Phase 4C's plans and the live DB, it does not regenerate `execution_permitted`).

## 2. Exact Scope

The 5 `SAFE_PLAN_CANDIDATE` pairs from Phase 4C, and **only** these 5:

`5207/5481` · `5232/5482` · `5548/5549` · `6086/6088` · `6153/6189`

(Winner listed first, per Phase 4C's `choose_keep()` output — all five via the `has_doi` tiebreak.) The 5 `PLAN_REQUIRES_HUMAN_APPROVAL` pairs and the 1 validation fixture from Phase 4C are explicitly out of scope for this audit.

**Answering the precise question posed** ("Could these 5 SAFE plans be handed to a future executor without discovering a new structural data-loss or referential-integrity problem?"): **No.** One new, previously-undetected data-loss pattern was found, present in 4 of the 5 pairs (§4). No referential-integrity problem (constraint violation, orphaned FK, unresolvable collision) was found in any of the 5.

## 3. Method

Every finding below comes from re-running Phase 4C's own pure functions (`fetch_paper_row`, `build_field_actions`, `build_doi_state`, `fetch_dependency_counts`, `build_child_table_actions`, `CHILD_TABLE_SPECS`) against fresh, live, read-only queries — not a re-statement of Phase 4C's cached JSON. Two checks genuinely new to this phase were added, both SELECT-only: (a) row-*content* comparison for `Authors` where winner and loser share a `UserID` (Phase 4C only ever checked row *counts* for child tables, never column-level content within a shared row); (b) existence check of the `Journals` row a hypothetical `JournalID` backfill would point to, plus a fresh, independent re-derivation of every FK referencing `ResearchPaper` (not assumed from the already-known list) to satisfy "do not rely only on `SIMPLE_CHILDREN`."

## 4. Pair-by-Pair Dry Simulation (Task A) — Full Field Preservation Audit

All 32 non-`PaperID` `ResearchPaper` columns were classified for every pair; none were omitted. Only non-trivial rows (excluding `EMPTY_BOTH`, which applies uniformly to `Title_En`, `Abstract_En`, `Volume`, `Issue`, `Pages`, `Indexing`, `DoiResolvedBy`, `DoiResolvedAt`, `OpenAlexWorkID`, `JournalID` when both are empty, and any other column empty on both sides for a given pair — genuinely nothing to plan there) are shown below; the full 32-column table for every pair is in `phase4d_execution_readiness.json`.

**Legend used below:** EQUAL (identical, no action) · KEEP_WINNER (loser has nothing to contribute, no loss) · CONFLICT_EXPECTED (Title/NormalizedTitle/RawData_Log differing is the duplicate signal itself, not a data-loss risk — loser's value stays recoverable via a pre-merge snapshot) · MERGE (existing, repo-backed `merge_citation_fields()` reconciliation) · BACKFILL_FROM_LOSER (winner empty, loser populated — deterministic, no ambiguity about the value, but not implemented today) · DERIVED (tsvector, not diffed) · NOT_APPLICABLE (`DOI` — governed by `doi_state`, never a plain field verdict).

### Pair 5207 / 5481
- `KEEP_WINNER`: JournalID (440/–), Abstract, ScrapedAt (process timestamp), AffiliationVerified, VerificationSource, VerifiedAt, VerificationDetails, AbstractSource, PdfUrl, PdfAccessType — 10 fields, all information already present on the winner, loser contributes nothing.
- `EQUAL`: Language, PubYear, IsVerified, Source, TenantID, VenueType, PublicationType — 7 fields.
- `CONFLICT_EXPECTED`: Title, NormalizedTitle, RawData_Log — 3 fields, by design.
- `MERGE`: CitationsByYear (winner has real data, loser empty — `merge_citation_fields()` correctly reduces to the winner's own values, no loss).
- `DERIVED`: SearchVector_En, SearchVector_Ar.
- `NOT_APPLICABLE`: DOI → `doi_state.action = KEEP_EXISTING_WINNER_DOI`.
- **No `CONFLICT` or `UNKNOWN` field verdict anywhere in this pair's ResearchPaper columns.** Information loss at the `ResearchPaper` level: **none.**

### Pair 5232 / 5482
Identical shape to 5207/5481 — `KEEP_WINNER` × 10 (incl. JournalID 1803/–), `EQUAL` × 7, `CONFLICT_EXPECTED` × 3, `MERGE` × 1 (CitationsByYear), `DERIVED` × 2, DOI → `KEEP_EXISTING_WINNER_DOI`. **No `ResearchPaper`-level information loss.**

### Pair 5548 / 5549
- `BACKFILL_FROM_LOSER`: **JournalID** (winner `NULL`, loser `676`) — the one field genuinely needing an operation that doesn't exist in `merge_group()` today.
- `KEEP_WINNER`: Abstract, VerifiedAt (process timestamp), AbstractSource, PdfUrl, PdfAccessType — 5 fields.
- `EQUAL`: Language, PubYear, IsVerified, ScrapedAt, Source, TenantID, AffiliationVerified, VerificationSource, VerificationDetails, VenueType, PublicationType, **CitationsByYear** — 12 fields (this pair's two rows were independently affiliation-verified with byte-identical results, unlike the pair above).
- `CONFLICT_EXPECTED`: Title, NormalizedTitle, RawData_Log.
- `DERIVED`: SearchVector_En, SearchVector_Ar.
- `NOT_APPLICABLE`: DOI → `KEEP_EXISTING_WINNER_DOI`.

### Pair 6086 / 6088
Same shape as 5548/5549: `BACKFILL_FROM_LOSER` × 1 (**JournalID**, winner `NULL`, loser `771`), `KEEP_WINNER` × 5, `EQUAL` × 12 (incl. identical `CitationsByYear`), `CONFLICT_EXPECTED` × 3, `DERIVED` × 2, DOI → `KEEP_EXISTING_WINNER_DOI`.

### Pair 6153 / 6189
- `KEEP_WINNER`: JournalID (676/–), Abstract, AffiliationVerified, VerificationSource, VerifiedAt, VerificationDetails, AbstractSource, PdfUrl, PdfAccessType — 9 fields.
- `EQUAL`: Language, PubYear, IsVerified, ScrapedAt, Source, TenantID, VenueType, PublicationType — 8 fields.
- `CONFLICT_EXPECTED`: Title, NormalizedTitle, RawData_Log.
- `MERGE`: CitationsByYear (winner populated, loser empty).
- `DERIVED`: SearchVector_En, SearchVector_Ar.
- `NOT_APPLICABLE`: DOI → `KEEP_EXISTING_WINNER_DOI`.
- **No `ResearchPaper`-level information loss.**

**Summary: zero genuine `CONFLICT` or `UNKNOWN` field verdicts across all 5 pairs, at the `ResearchPaper` column level.** The only unimplemented-but-deterministic gap is `JournalID` in 2 of the 5 pairs, exactly as Phase 4C found. **The child-row-level finding in §5 below is new to this phase and was not visible at the `ResearchPaper` column level at all.**

## 5. New Finding: Authors Row-Content Conflicts (not caught by Phase 4C)

Phase 4C's `child_table_actions` only ever checked `COUNT(*)` per table — sufficient to prove *whether* a remap is needed, but not *what* would happen to a shared row's own columns when winner and loser share a `UserID` (which all 5 pairs do — each pair has exactly one shared author). This phase queried `Authors` row content directly for that shared `UserID` on both sides, across all its columns (`AuthorOrder`, `IsCorrespondingAuthor`, `MappingConfidence`, `MappingCriteria`, `AuthorNameRaw`, `Is_Verified`).

**Result: 4 of the 5 pairs have a genuine, real content difference — always in `AuthorNameRaw` only, never in the other columns:**

| Pair | Shared UserID | Winner `AuthorNameRaw` | Loser `AuthorNameRaw` |
|---|---|---|---|
| 5207/5481 | 97 | `O Hrizi, K Gasmi, I Ben Ltaifa, H Alshammari, H Karamti, M Krichen, ...` | `O Hrizi, K Gasmi, IB Ltaifa, H Alshammari, H Karamti, M Krichen, ...` |
| 5232/5482 | 97 | *(identical on both sides — no conflict)* | |
| 5548/5549 | 104 | `S Ahmad, NB Aoun, MA El Affendi, MS Anwar, S Abbas, AA Abd El Latif` | `S Ahmad, N Ben Aoun, MAE Affendi, MS Anwar, S Abbas, AAAE Latif` |
| 6086/6088 | 105 | `A Chakrabarty, N Mansoor, MI Uddin, MH Al-adaileh, N Alsharif, ...` | `A Chakrabarty, N Mansoor, MI Uddin, MH Al-Adaileh, N Alsharif, ...` |
| 6153/6189 | 112 | `IE Fattoh, F Kamal Alsheref, WM Ead, AM Youssef` | `IE Fattoh, FK Alsheref, WM Ead, AM Youssef` |

**Why `merge_group()` doesn't crash or collide on this:** `Authors` has a `uq_authors_user_paper` unique index on `(UserID, PaperID)`. The remap step is `INSERT ... SELECT ... FROM "Authors" WHERE "PaperID"=loser ON CONFLICT ("UserID","PaperID") DO NOTHING`, followed by `DELETE FROM "Authors" WHERE "PaperID"=loser`. Since the winner already has a row for `(UserID=97, PaperID=winner)`, the loser's row for the same `UserID` hits the unique-index collision, `ON CONFLICT DO NOTHING` silently skips the insert, and the loser's row (with its own `AuthorNameRaw` variant) is then deleted outright. **No error, no crash, no duplicate row — but the loser's `AuthorNameRaw` string is permanently discarded with zero detection, zero logging, and zero snapshot specific to this content difference** (the pre-merge snapshot in `merge_group()`'s `--apply` path does capture the loser's full `Authors` row before deletion, so the value is *recoverable by hand*, same recovery story as every other silently-dropped field — just never *flagged*).

This is structurally the same class of problem as the `ResearchPaper.JournalID` gap Phase 4C found — a deterministic, low-ambiguity value that's simply never compared or logged before being discarded — except it happens *inside* a table `merge_group()` already believes it "handles correctly" (`Authors` is explicitly special-cased, not a `SIMPLE_CHILDREN` gap). The severity is low (a formatting/abbreviation variant of a name string, not used for attribution matching — per this project's data-attribution rules, `UserID` is the authoritative link, not the raw name string), but it is real, provable, occurs in 4 of 5 pairs, and was invisible to Phase 4C's count-only dependency check.

## 6. JournalID Readiness (Task B)

| Pair | Winner JournalID | Loser JournalID | Planned final value | Backfill preserves relationship? | Referenced Journal exists? | Constraint risk |
|---|---|---|---|---|---|---|
| 5207/5481 | 440 | `NULL` | 440 (`KEEP_WINNER`, no backfill needed) | n/a | n/a | none |
| 5232/5482 | 1803 | `NULL` | 1803 (`KEEP_WINNER`) | n/a | n/a | none |
| 5548/5549 | `NULL` | 676 | 676 (`BACKFILL_FROM_LOSER`) | **Yes** | **Yes** — `Journals.JournalID=676`, "Computational Intelligence and Neuroscience 2022 (1), 7897669, 2022" | none |
| 6086/6088 | `NULL` | 771 | 771 (`BACKFILL_FROM_LOSER`) | **Yes** | **Yes** — `Journals.JournalID=771`, "Complexity" | none |
| 6153/6189 | 676 | `NULL` | 676 (`KEEP_WINNER`) | n/a | n/a | none |

`ResearchPaper.JournalID` has exactly one FK: `ResearchPaper_JournalID_fkey → Journals.JournalID`, `ON DELETE NO ACTION`. For both backfill cases (5548/5549, 6086/6088), the target `JournalID` value already satisfies this FK on the *loser's own row today* (it was already a valid, committed reference) — copying the identical integer onto the winner's row cannot introduce a new FK violation, since it's the exact same valid target, just referenced by a different `ResearchPaper` row. No other constraint touches `JournalID` (re-confirmed this phase: the only constraints on `ResearchPaper` are its primary key, the exact-`Title` unique index, and the two partial-unique DOI/NormalizedTitle indexes — none involve `JournalID`). **`JournalID` backfill is schema-safe for both pairs that need it — the only missing thing is that `merge_group()` has no code path that performs it.**

## 7. Full Dependency Collision Audit (Task C)

Every FK referencing `ResearchPaper` was independently re-derived this phase directly from `information_schema` (not assumed from the existing 8-row list) — the fresh query returned exactly the same 8 rows, confirming completeness: `AuthorReviewQueue.PaperID`, `Authors.PaperID`, `Citations.PaperID`, `CitationsHistory.PaperID`, `ExternalAuthors.PaperID`, `PaperKeywords.PaperID`, `ReportPaperDecision.PaperID`, `ReportPaperDecision.MissingResolvedToPaperID`. No table beyond `SIMPLE_CHILDREN` ∪ {Authors, Citations, AuthorReviewQueue, ReportPaperDecision.MissingResolvedToPaperID} references `ResearchPaper`.

**Fresh row counts for all 10 PaperIDs across all 8 dependency slots (re-queried this phase, not reused from Phase 4C):**

| Table.FK | Winner rows | Loser rows (any of the 5 pairs) |
|---|---|---|
| Authors.PaperID | 1 each | 1 each |
| Citations.PaperID | 0 | 0 |
| ExternalAuthors.PaperID | 0 | 0 |
| PaperKeywords.PaperID | 0 (table has 0 rows DB-wide) | 0 |
| CitationsHistory.PaperID | 0 | 0 |
| ReportPaperDecision.PaperID | 0 | 0 |
| ReportPaperDecision.MissingResolvedToPaperID | 0 | 0 |
| AuthorReviewQueue.PaperID | 0 | 0 |

Per dependency, for all 5 pairs:

- **`Authors`** — winner=1, loser=1, always the *same* `UserID` (single shared author on every one of these 5 pairs, confirmed §5). Unique-index collision: `(UserID, PaperID)` — the loser's row *would* collide on insert-with-new-PaperID if attempted directly, but `merge_group()`'s actual remap is `ON CONFLICT DO NOTHING`, which handles it gracefully (no error). **Composite-key collision risk: real, but proven non-fatal** (existing code already handles it correctly, mechanically). **Content-loss risk: real** (§5) — remap alone is *not* fully sufficient to preserve all information; true child-row *content merging* (not just FK reassignment) would be needed to avoid it, confirming the task's suspicion that "remap is sufficient" cannot be assumed uniformly.
- **`Citations`, `ExternalAuthors`, `PaperKeywords`, `CitationsHistory`, `ReportPaperDecision` (both FK columns), `AuthorReviewQueue`** — 0 loser rows in every one of the 5 pairs. **No remap is triggered, no collision is possible, nothing is orphaned, nulled, or cascaded** — proven directly from real counts, not inferred. These dependencies are fully inert for this specific 5-pair set (their underlying code gaps — unhandled `AuthorReviewQueue` CASCADE, unremapped `MissingResolvedToPaperID` — remain latent, real gaps for *other*, future pairs, but have zero effect on these 5).

No table required "child-row merging" in the sense of combining two *different* underlying entities — every dependency table's shared-key story reduces to either "0 rows on the loser side" (trivially safe) or "the exact same `UserID` on both sides" (`Authors` — safe from a collision standpoint, not fully safe from a content standpoint, per §5).

## 8. Orphan/Null Side-Effect Audit (Task D)

| Dependency | ON DELETE rule | Loser rows (any of the 5 pairs) | Consequence of deleting the loser today |
|---|---|---|---|
| `AuthorReviewQueue.PaperID` | CASCADE | 0 | **None** — nothing exists to cascade-delete for any of these 5 pairs. |
| `ReportPaperDecision.PaperID` | SET NULL | 0 | **None** — `remap_simple_child()` would run first regardless (this table is in `SIMPLE_CHILDREN`), and there are 0 rows to remap or orphan. |
| `ReportPaperDecision.MissingResolvedToPaperID` | SET NULL | 0 | **None** — 0 rows reference any of these 10 PaperIDs via this column. |
| `Authors.PaperID`, `Citations.PaperID`, `ExternalAuthors.PaperID`, `CitationsHistory.PaperID`, `PaperKeywords.PaperID` | NO ACTION | 1 (Authors only) / 0 (rest) | Authors: handled by explicit remap before delete, not by the FK rule firing at all (remap removes the FK's referencing row before `ResearchPaper` delete, so `NO ACTION` never gets tested). Rest: 0 rows, nothing to orphan. |

**Conclusion: for these specific 5 pairs, no delete-time side effect (orphaning, silent nulling, cascading, or constraint violation) would occur**, because every dependency table with a nonzero-risk `ON DELETE` rule has zero rows for every one of these 10 PaperIDs. This is a **pair-specific** conclusion, not a general claim about `dedup_papers.py`'s safety on other pairs — the `AuthorReviewQueue`/`MissingResolvedToPaperID` gaps remain real, latent risks for any future pair where those tables are populated.

## 9. `merge_group()` Gap Table (Task E) — Audit Only, Not Fixed

| Required operation (per this phase's plan) | Current `merge_group()` behavior | Safe for these 5 pairs? | Missing implementation? |
|---|---|---|---|
| Keep survivor's `ResearchPaper` scalar columns where winner is populated | Untouched — winner row is never written to except `CitationsByYear`/`RawData_Log` | Yes | No |
| `CitationsByYear` reconciliation | `merge_citation_fields()` — element-wise `MAX` per year, GREATEST total | Yes | No |
| `Authors` FK remap (different UserIDs) | `INSERT...ON CONFLICT DO NOTHING` + `DELETE` | Yes (no case in this 5-pair set) | No |
| `Authors` FK remap (**same** UserID on both sides) | Same `INSERT...ON CONFLICT DO NOTHING` — silently skips the loser's row, including any differing non-key columns | **Mechanically safe (no error), but silently lossy for `AuthorNameRaw`** | **Yes** — no detection/logging of a same-key content difference before discarding it |
| `Citations` table GREATEST-merge | `INSERT...ON CONFLICT DO UPDATE...GREATEST()` | Yes (0 rows in this set, untested by these 5) | No |
| `SIMPLE_CHILDREN` remap (`ExternalAuthors`, `PaperKeywords`, `CitationsHistory`, `ReportPaperDecision`) | `remap_simple_child()` bulk UPDATE + per-row SAVEPOINT fallback | Yes (0 rows in this set, untested by these 5) | No |
| **`JournalID` backfill from loser when winner is `NULL`** | **No code path exists** — `merge_group()` never touches `JournalID` | **No** (for 5548/5549, 6086/6088 specifically) | **Yes** |
| `ReportPaperDecision.MissingResolvedToPaperID` remap | **No code path exists** | Yes for these 5 (0 rows) | Yes, in general (not exercised here) |
| `AuthorReviewQueue` handling before CASCADE-delete | **No code path exists** — relies on the DB's own CASCADE | Yes for these 5 (0 rows) | Yes, in general (not exercised here) |
| Pre-merge child-row **content**-level conflict detection (any table, not just `ResearchPaper` columns) | **Does not exist at all** — Phase 4C and prior phases only ever diffed `ResearchPaper` columns | **No** (proven lossy for `Authors.AuthorNameRaw` in 4/5 pairs) | **Yes** — this is a category of check that has never existed in any phase of this project until this audit |

## 10. Execution-Readiness Verdicts (Task F)

| Pair | Verdict | Reasoning |
|---|---|---|
| **5207 / 5481** | **READY_AFTER_IMPLEMENTATION** | Zero `ResearchPaper`-level issues, zero JournalID gap (winner already has one), zero nonzero-row dependency risk. Blocked from `EXECUTION_READY` only by the new `Authors.AuthorNameRaw` finding (§5) — a deterministic, low-severity, but real, undetected value discard. |
| **5232 / 5482** | **EXECUTION_READY** | The only pair with zero findings of any kind at any layer checked in this audit — `ResearchPaper` columns, `JournalID`, dependency counts, and `Authors` row content (this pair's shared author's `AuthorNameRaw` is byte-identical on both sides). All fields, dependencies, collisions, and delete side effects are proven and explicitly planned; nothing is missing. |
| **5548 / 5549** | **READY_AFTER_IMPLEMENTATION** | Two stacked gaps: `JournalID` backfill (schema-safe, target Journal confirmed to exist, but no code path implements it) *and* the `Authors.AuthorNameRaw` finding. Neither is a genuine ambiguity — both have a clear, deterministic correct action — they are simply not implemented. |
| **6086 / 6088** | **READY_AFTER_IMPLEMENTATION** | Same two-gap pattern as 5548/5549 (JournalID backfill to a confirmed-existing Journal, plus `AuthorNameRaw`). |
| **6153 / 6189** | **READY_AFTER_IMPLEMENTATION** | Same single-gap pattern as 5207/5481 — only the `Authors.AuthorNameRaw` finding blocks it from `EXECUTION_READY`. |

**No pair was automatically promoted.** `SAFE_PLAN_CANDIDATE` from Phase 4C meant "the `ResearchPaper`-level data plan was safe enough to investigate further" — this phase's deeper, child-row-content-level investigation found a real gap in 4 of 5 pairs that Phase 4C's methodology could not see, exactly the scenario this audit exists to catch.

## 11. Aggregate Verdict (Task G)

1. **EXECUTION_READY: 1** (5232/5482)
2. **READY_AFTER_IMPLEMENTATION: 4** (5207/5481, 5548/5549, 6086/6088, 6153/6189)
3. **HUMAN_REVIEW_REQUIRED: 0**
4. **BLOCKED: 0**

5. **Exact operations a future Phase 4E executor would need to implement**, to bring all 5 pairs to `EXECUTION_READY` (in addition to whatever wiring is needed to actually invoke a merge at all, which remains entirely unbuilt and out of scope everywhere in this project so far):
   - **`JournalID` backfill**: when winner's `JournalID` is `NULL` and loser's is populated, `UPDATE "ResearchPaper" SET "JournalID" = <loser's value> WHERE "PaperID" = <winner>` before the loser row is deleted. Needed for 2 of the 5 pairs (5548/5549, 6086/6088); both target `Journals` rows are confirmed to exist, so this is schema-safe today, contingent on nothing else changing.
   - **`Authors` same-`UserID` content-conflict detection**: before the existing `INSERT...ON CONFLICT DO NOTHING` remap runs, compare the winner's and loser's rows for any shared `UserID` across `AuthorOrder`, `IsCorrespondingAuthor`, `MappingConfidence`, `MappingCriteria`, `AuthorNameRaw`, `Is_Verified`; if any differ, either log it explicitly (minimum bar — makes today's already-safe-in-practice default an *auditable* one rather than a silent one) or apply an explicit, documented tiebreak (e.g., keep the longer/more complete string) rather than relying on `ON CONFLICT DO NOTHING`'s incidental behavior. Needed for 4 of the 5 pairs.
   - Both operations are narrow, additive, and do not require touching `dedup_papers.py`'s detection/confidence logic — they belong entirely inside `merge_group()`'s execution path (or a wrapper around it), consistent with everything else this phase found.

6. **Can a narrow executor be built for only these approved pairs, or is a broader architectural change required?**
   A **narrow** executor is sufficient for this specific 5-pair set — no broader architectural change is indicated. The two missing operations above are both small, additive, well-scoped, and independently testable (mirroring exactly the kind of pure-function test coverage `merge_plan_generator.py` already has for `JournalID` in Phase 4C, §9's test suite). Nothing found in this audit requires touching `dedup_papers.py`'s detection logic, its blocking/threshold behavior, its plan-file format, or its transaction/snapshot/`AuditLog` design (all confirmed still sound and unchanged). The `AuthorReviewQueue`/`MissingResolvedToPaperID` gaps remain real but are **provably inert for this specific 5-pair set** (§8) — a narrow executor scoped to exactly these pairs does not need to solve them, though any executor intended for a *broader* pair population (the 5 `PLAN_REQUIRES_HUMAN_APPROVAL` pairs, or the wider 55-case population) would.

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

**Unchanged from Phase 4C** — this phase added no new file outside `backend/reports/` (already untracked as a whole directory since Phase 3F) and modified nothing. `git diff --stat` is therefore identical to Phase 4C's (10 pre-existing, unrelated files — see Phase 4C's report for the full stat table); none of those 10 files were read, touched, or are relevant to this phase.
