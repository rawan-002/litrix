# Phase 4B — Duplicate Records Architecture & Safety Audit

## 1. Scope and Safety Confirmation

- Code lines modified in the repo: **0**
- Database writes (INSERT/UPDATE/DELETE/TRUNCATE): **0**
- Network calls (Crossref/OpenAlex/etc.): **0**
- Records merged: **0**
- DOIs assigned/changed/cleared: **0**

Everything below comes from four read-only actions:

1. `information_schema` / `pg_indexes` SELECT queries (columns, foreign keys + `ON DELETE` rules, unique constraints, indexes, row counts) — carried over from the investigation already run this phase before the compaction.
2. Static reading of `backend/tools/dedup_papers.py` in full (807 lines) — no edits.
3. `backend/tools/dedup_papers.py --dry-run` (its own documented, DB-read-only mode) — already run in Phase 4A, output at `data/dedup_audit/merge_plan_20260821_120013.json` / `.csv`. Re-read, not re-run, this phase.
4. Two new read-only scripts, both SELECT-only and both importing `dedup_papers.py`'s pure functions (`norm_title`, `block_key`) rather than calling `detect_groups()`/`merge_group()`/`--apply`:
   - `phase4b_blocking_audit.py` — measures the blocking-key miss rate DB-wide.
   - `phase4b_field_conflict_audit.py` — measures non-reconciled-field conflicts across the 40 CONFIRMED+LIKELY Phase 4A pairs.

Neither script touches `main()`, `--apply`, or any write path. `dedup_papers.py` itself was never executed with `--apply` in this session.

## 2. Current `dedup_papers.py` Architecture

Full pipeline, stage by stage:

**Stage 1 — `load_papers(cur, user_id)`.** SELECT of `PaperID, Title, NormalizedTitle, DOI, PubYear, IsVerified, Source`, plus a citations total computed the same way the dashboard does (`COALESCE(RawData_Log->cited_by->value, RawData_Log->cited_by_count, 0)`). Also loads every `(PaperID, UserID)` author link and a first-author-by-`AuthorOrder` map. Computes `fuzzy_key = norm_title(title)` (NFKD-normalize, strip accents/punctuation, strip **one** leading boilerplate tag such as "Research Article"/"Retraction of"/"Corrigendum to" from `_BOILERPLATE_PREFIXES`).
*Safety assumption:* `NormalizedTitle` and `Title` as stored are trustworthy enough to block/compare on. *False-negative risk:* none introduced here — this stage only reads.

**Stage 2 — `detect_groups(papers, threshold)`.** Union-find over two edge types:
- **Exact edges** (cheap, always run): same `DOI`, same `NormalizedTitle`, or same lower-cased raw `Title`. These are treated as certain duplicates regardless of author/year.
- **Fuzzy edges** (bounded): papers are bucketed by `block_key(fuzzy_key)` — the first 3 non-stopword words of the normalized title. Only pairs landing in the *same* bucket are ever compared. A candidate pair is further required to share at least one author `UserID` before `SequenceMatcher(fuzzy_key_a, fuzzy_key_b).ratio()` is even computed; only if that ratio ≥ `threshold` (default 0.90) is an edge added.
*Safety assumption:* real duplicates share a first-3-word title prefix (after boilerplate stripping) and share an author. *False-negative modes measured in §3:* both assumptions fail often enough to matter. *False-positive guard:* the same-author gate is explicitly there to stop unrelated same-topic papers (the docstring cites "Acknowledgment to the Reviewers" boilerplate recurring across years) from being grouped by text similarity alone — confirmed still fires in §3's data (two acknowledgment-page pairs scored ≥0.90 but are excluded from these results because they're not same-author... actually they passed the author gate in one case, see §3).

**Stage 3 — `build_report(groups, papers)`.** For each group: `choose_keep()` picks a survivor (see §6), `choose_keep_reason()` records which tie-break factor decided it, `group_confidence()` labels the group `high` or `review` (see below), and a JSON-serializable plan entry is built (kept paper + losers, title/DOI/year/citations/source/is_verified only — not full rows).

`group_confidence()`: a group formed **only** by exact edges (same DOI/NormalizedTitle/lower-title) is always `high`. A group containing **any** fuzzy edge is `high` only if `pair_confidence()` returns `high` for every (keep, loser) pair in it. `pair_confidence()` returns `high` only for the narrow profile: not a Corrigendum/Erratum/Retraction on either side, shared author, `SequenceMatcher ≥ 0.95` (stricter than the 0.90 grouping threshold), compatible pub year (equal or off-by-one, or one/both missing), and **exactly one side** has a DOI (the "same paper, one copy not yet enriched" asymmetry — two different real DOIs, or neither side having one, forces `review`). `hard_exclusion_reason()` is checked first and independently: if both sides have DOIs and they differ, and either the year gap exceeds 2 or the first authors differ, the pair can never be `high` and is either excluded entirely (year gap) or forced to `review` (different first author) — this is the guard against "evolutionary papers" (a conference paper and its later separately-DOI'd journal extension).

**Stage 4 — output split.** `--dry-run` writes the full plan (all groups, both confidences) to `merge_plan_<ts>.json/.csv` under `data/dedup_audit/` and stops — no DB write. `--apply --plan <file>` re-loads live `papers` fresh, drops any plan group whose members no longer exist (partial-run resumability), **discards every `review`-confidence group** (never auto-merged, ever, even by `--apply`), then wraps every remaining `high` group in one `transaction.atomic()`: full JSON snapshot of every member row + its child rows written to `data/dedup_audit/snapshot_<ts>.json` *before* any write, then per group `merge_group()` (Authors remap, `Citations` GREATEST-merge, `SIMPLE_CHILDREN` remap, `merge_citation_fields()` element-wise MAX, an `AuditLog` row per loser, then `DELETE FROM "ResearchPaper" WHERE "PaperID"=loser`), then a profile-preservation assertion (every `UserID` that was linked to any member must still be linked to the kept paper afterward) that raises and rolls back the **entire transaction** if violated.

*False-positive risk in production:* effectively zero for `high` groups within the tool's own logic — `hard_exclusion_reason()` and the DOI-asymmetry requirement are deliberately conservative. The residual risk is not "wrong pair merged" but "right pair merged, real data silently dropped" (§5).
*False-negative risk:* real duplicates never reach `detect_groups()`'s fuzzy stage at all if they land in different `block_key` buckets or don't share a `UserID` — measured precisely in §3.

## 3. Blocking / Candidate Detection Audit

Real DB-wide measurement (`phase4b_blocking_audit.py`, same-author pairs only, excluding pairs already equal by DOI/NormalizedTitle/lower-title):

| Metric | Count |
|---|---|
| Same-author candidate pairs in scope | 119,680 |
| Pairs actually compared by the real blocking step (same `block_key`) | 132 |
| Of those 132, pairs scoring `SequenceMatcher(fuzzy_key) ≥ 0.90` | 24 |
| Pairs scoring ≥ 0.90 **but in different `block_key` buckets** (never compared, never grouped) | 24 |
| **Total pairs ≥ 0.90 found by this measurement** | 48 |
| **Miss rate for genuine ≥0.90-similarity pairs** | **50%** (24/48) |

PROVEN FACT: this is not limited to the two pairs flagged in Phase 4A. `{5289,5392}` and `{6107,6109}` are members of this same 24-pair miss set, but 22 others share the identical mechanism. The blocking failure is **systematic**, driven by three recurring, distinguishable patterns in the missed set:

1. **Correction/Retraction/Erratum prefix variants `norm_title()` doesn't strip** (8/24 missed pairs) — e.g. `"Correction to: Blockchain-assisted..."` vs `"Blockchain-assisted..."`, `"RETRACTED ARTICLE: Toward..."` vs `"Retraction Note: Toward..."`, `"[Retracted] Advanced Computing..."` vs `"Advanced Computing..."`. `_BOILERPLATE_PREFIXES` only recognizes `"corrigendum to"`, `"erratum to/erratum"`, `"retraction of"`, `"retraction:"` — not `"correction to:"`, `"retraction note:"`, `"retracted article:"`, or a bracketed `"[retracted]"` tag. **Low practical stakes**: `_is_distinct_record()` (checked on the *original*, unstripped title) and `pair_confidence()` already force any pair involving a Correction/Retraction/Erratum to `review`, never `high` — so even if blocking caught these, they would not auto-merge. The blocking miss here only means a human reviewer doesn't get to see the pairing suggested at all, not that an unsafe auto-merge is possible.
2. **Genuine title rewordings with a different leading word** (10/24 missed pairs, including the 2 known Phase 4A cases) — e.g. `"Adopting formal verification and model-based testing techniques..."` vs `"Formal verification and model-based testing techniques..."` ({5289,5392}), `"Path Planning Models for Mobile Anchor-Assisted..."` vs `"New path planning model for mobile anchor-assisted..."` ({6107,6109}), `"A formal model-based testing framework..."` vs `"A model-based testing framework..."` ({5320,5350}), `"iCARII: ..."` vs `"iCAR: ..."` ({6095,6096}). These are the real, actionable blind spot: they are not distinct-record titles, several would plausibly reach `pair_confidence()=high` if only they were ever compared, and blocking prevents that entirely.
3. **False positives of the 0.90 similarity threshold itself, correctly excluded by being missed** (2/24) — `{4221,4224}`: "Overview for the first shared task..." vs "...the second shared task..." (a paper series, genuinely different papers); `{6143,6144}`: "Acknowledgment to the Reviewers of Sensors in 2022" vs "...of JMSE in 2022" (same boilerplate, different journals). These two happening to share a `UserID` (an editor/reviewer credited on both) is exactly the false-positive class the same-author gate does not fully filter, and it is only blocking (an accident of word order, not a designed safeguard) that keeps them from ever being grouped.

**Candidate-pair-explosion risk if blocking were widened or removed** (directly measured, not estimated): removing blocking entirely but keeping the same-author restriction would require scoring **119,680 pairs** instead of 132 — an ~900× increase in `SequenceMatcher` calls for this DB size alone, and that cost grows independently of any group-size limit as more same-author papers accumulate. Any blocking fix should stay a *bucketing* change (e.g. compare against the last N-2/N-1 words too, or fingerprint on a bag of significant words rather than a fixed 3-word prefix) rather than removing blocking altogether.

## 4. ResearchPaper Dependency Map

(No merge performed — this is a static map of what a future merge would have to handle, not a merge.)

Foreign keys referencing `ResearchPaper.PaperID` (from `information_schema`, gathered before this phase's compaction and unchanged since — schema is static):

| Table | FK column | ON DELETE | Handled by `dedup_papers.py` today? |
|---|---|---|---|
| `Authors` | PaperID | NO ACTION | Yes — special-cased remap respecting the `(UserID,PaperID)` unique index |
| `Citations` | PaperID | NO ACTION | Yes — special-cased `GREATEST()`-merge |
| `ExternalAuthors` | PaperID | NO ACTION | Yes — in `SIMPLE_CHILDREN`, generic remap |
| `PaperKeywords` | PaperID | NO ACTION | Yes — in `SIMPLE_CHILDREN` |
| `CitationsHistory` | PaperID | NO ACTION | Yes — in `SIMPLE_CHILDREN` |
| `ReportPaperDecision` | PaperID | NO ACTION | Yes — in `SIMPLE_CHILDREN` |
| `ReportPaperDecision` | MissingResolvedToPaperID (2nd FK column, same table) | SET NULL | **No** — not remapped by any logic; only the `PaperID` column is handled |
| `AuthorReviewQueue` | PaperID | **CASCADE** | **No** — not in `SIMPLE_CHILDREN`, not special-cased, not in `snapshot_paper()`'s captured child tables |
| `PaperGrants` | PaperID | NO ACTION | Listed in `SIMPLE_CHILDREN`, but **the table does not exist in the database** — dead list entry, harmless |

PROVEN FACT (current row counts): `AuthorReviewQueue` has **0 rows**; `ReportPaperDecision` has **44 rows**, **0** with `MissingResolvedToPaperID` set. Both gaps are real code gaps but currently **zero practical impact** — a merge run today would not lose or cascade-delete any live data through either path, because no row exists to be affected. This would change the moment either table gets populated (e.g. if the author-suggestion review workflow that presumably writes `AuthorReviewQueue` goes live), at which point a merge of a loser paper carrying a queue row would silently CASCADE-delete that review-queue entry with no snapshot and no audit trail specific to it.

Unique/PK constraints relevant to a merge: `ResearchPaper_pkey` (PaperID), `researchpaper_title_unique` (exact raw `Title`), `uq_paper_doi_normalized` (partial unique on normalized DOI — already enforced, 0 violations DB-wide per Phase 3E), `uq_paper_normalized_title` (partial unique on `NormalizedTitle` — does not catch these 55 cases; confirmed in Phase 4A that `NormalizedTitle` is populated for 107/107 involved PaperIDs yet unequal for all 55 pairs, because ingestion-time `NormalizedTitle` computation and `dedup_papers.py`'s own `norm_title()` are different algorithms).

## 5. Merge Safety Analysis

What `merge_group()` **does** reconcile across keep/loser: `Authors` links (full union, asserted afterward), the `Citations` table (`GREATEST` of counts), `CitationsByYear` + the dashboard's `RawData_Log` total (element-wise `MAX` per year, via `merge_citation_fields()`), and the five `SIMPLE_CHILDREN` tables (remapped, with a per-row conflict-drop fallback via `SAVEPOINT`).

What it does **not** reconcile: every other `ResearchPaper` column on the kept row — `Abstract`, `Abstract_En`, `Title_En`, `Language`, `PdfUrl`, `PdfAccessType`, `AbstractSource`, `Volume`, `Issue`, `Pages`, `PublicationType`, `VenueType`, `DoiResolvedBy`, `DoiResolvedAt`, `OpenAlexWorkID`, `Indexing`, `AffiliationVerified`, `VerificationSource`, `VerificationDetails`, `VerifiedAt`. The loser row is deleted outright (after `AuditLog` + full JSON snapshot, so it's *recoverable by hand*, but not automatically). Any value the loser held in one of these columns — populated or in conflict with the keep's value — is gone from live queries the instant the merge commits.

Real measurement (`phase4b_field_conflict_audit.py`) across the 40 Phase 4A pairs classified `CONFIRMED_DUPLICATE` or `LIKELY_DUPLICATE` (i.e., excluding the 15 `UNCLEAR_MANUAL_REVIEW` cases, which shouldn't be merged at all yet):

**18 of 40 pairs (45%) have at least one genuinely conflicting, both-sides-populated, non-reconciled field.** Per-field counts:

| Field | Pairs with a genuine two-sided conflict |
|---|---|
| `PublicationType` | 14 |
| `VenueType` | 7 |
| `Language` | 2 |
| `Abstract` | 1 |
| `OpenAlexWorkID` | 1 |
| `CitationsByYear` | 1 (safely reconciled by `merge_citation_fields()` — not a loss) |
| `PdfUrl`, `PdfAccessType`, `AbstractSource`, `Volume`, `Issue`, `Pages`, `DoiResolvedBy`, `DoiResolvedAt`, `Indexing`, `AffiliationVerified`, `VerificationSource` | 0 in this sample |

The zero counts above are a property of this specific 40-pair sample (most fields are simply `NULL` on both/most Scholar-sourced rows), not a structural guarantee that would hold on a different or larger pair population.

Concrete example: pair `(7630, 7617)`, classified `LIKELY_DUPLICATE`, has both an `Abstract` conflict and an `OpenAlexWorkID` conflict. Whichever side `choose_keep()` ranks as loser (per §6's has_doi→citations→title_length→is_verified→PaperID ordering) has its abstract text and OpenAlex linkage permanently discarded on merge, with nothing in `merge_group()` to catch or reconcile it — only the coarse pre-merge JSON snapshot for a human to notice and manually restore.

**Conclusion:** even the narrowest, most conservative `[HIGH]`-confidence merges are not risk-free with respect to *field completeness* — the tool's safety guarantees (§2, §7) are about not merging the *wrong pair*, not about not *losing data* from the right pair.

## 6. Survivor Selection Evidence

`choose_keep()`'s actual, already-implemented policy is a single composite sort key, in this exact priority order: **has DOI → citation count → title string length → `IsVerified` → lowest `PaperID`** (all `reverse=True`, so higher/true/longer/lower-ID wins at each tier).

Classifying the candidate criteria from repository evidence:

| Criterion | Label | Basis |
|---|---|---|
| Has a DOI | **A — directly supported** | Priority-1 tiebreak in `choose_keep()` today |
| Citation count | **A — directly supported** | Priority-2 tiebreak today |
| Title length | **A — directly supported** | Priority-3 tiebreak today (note: this measures literal string length, not field completeness — a longer title is not necessarily a more complete *record*; flagged as a latent quirk, not something Phase 4B should fix) |
| `IsVerified` | **A — directly supported** | Priority-4 tiebreak today |
| Lowest `PaperID` (earliest-created) | **A — directly supported** | Final tiebreak today |
| "Most complete record" by populated-field count (Abstract/PdfUrl/Volume/Issue/Pages/PublicationType, etc.) | **C — unsupported product decision** | Not referenced anywhere in `choose_keep()`. §5 shows this gap has measured consequences (18/40 pairs with real field conflicts). Building a genuine "richest record wins" or field-by-field composite-survivor policy is a real product decision — which field matters, whether to synthesize a merged record sourced from multiple losers rather than pick one whole winner — that nothing in the repo currently decides, and Phase 4B should not invent it. |
| Recency of `ScrapedAt` | **C — unsupported** | Not referenced anywhere in the selection logic |
| `Source` priority (e.g. Scholar over a secondary scraper) | **C — unsupported** | `Source` is loaded into the `papers` dict but never compared in `choose_keep()` |
| `VerificationSource` / manual curation | **C — unsupported** | Real column, not used in survivor selection |
| **B — reasonable engineering inference (not decided anywhere, but a plausible incremental fix):** inserting a "prefer the side with a populated `PublicationType`/`VenueType`/`Abstract`" tiebreak ahead of title-length would directly shrink the §5 data-loss surface without inventing a new confidence tier or a field-synthesis engine. This is inference for a *possible future* change, explicitly not something to implement in this phase. | | |

## 7. Risk Tiering of the 55 Cases

Cross-referencing the 55 Phase 4A cases (10 CONFIRMED_DUPLICATE / 30 LIKELY_DUPLICATE / 0 NOT_DUPLICATE / 15 UNCLEAR_MANUAL_REVIEW) against the real `dedup_papers.py --dry-run` output (24 groups DB-wide: 13 HIGH / 11 REVIEW), measured directly this phase:

**TIER 1 — mechanically detected today, `[HIGH]` confidence, zero code changes needed to detect: 8 pairs**
`(5434,5329) (5481,5207) (5482,5232) (5549,5548) (6088,6086) (6091,3875) (6189,6153) (7572,6645)` — all 8 are found by the real full-DB `--dry-run` **and** land in a `high`-confidence group in the actual plan file. These are safe from a *wrong-pair* standpoint per the tool's own conservative logic (§2). They are **not** automatically safe from a *field-loss* standpoint — each must still be checked against §5's field-conflict logic before an unattended `--apply`; this measurement was only run in aggregate over 10+30, not filtered per-tier yet.
Evidence still needed: per-pair field-conflict check restricted to just these 8 (subset of the 40 already measured in §5 — extractable from the same JSON output, not yet isolated).

**TIER 2 — real duplicates by evidence, but invisible to the current engine due to a proven, systematic cause: 2 pairs**
`(5289,5392) (6107,6109)` — both CONFIRMED_DUPLICATE in Phase 4A, both absent from the real `--dry-run` output entirely (not `review`, not present at all — §3 confirms exactly why: different `block_key` buckets from a leading-word rewording, category 2 of §3's three-pattern breakdown). These would very plausibly reach `pair_confidence()=high` if only `detect_groups()` ever compared them — but that is inference, not proof, since `pair_confidence()` was never actually run on them by the real engine end-to-end in this investigation (Phase 4A ran `pair_confidence()` directly as a library call, which is a different code path than letting `detect_groups()` discover and pass them in). Evidence still needed: confirm `pair_confidence(keep, loser, papers)` returns `high` for these two specific pairs using the *exact* `papers` dict `detect_groups()` would build (Phase 4A's script built its own `papers` dict independently — worth one more read-only cross-check before trusting this as TIER 1-equivalent).

**TIER 3 — LIKELY_DUPLICATE and UNCLEAR_MANUAL_REVIEW, not found by the real engine at all: 45 pairs (30 + 15)**
None of the 30 LIKELY_DUPLICATE or 15 UNCLEAR_MANUAL_REVIEW pairs appear together in *any* real `--dry-run` group (measured directly — 0/30 and 0/15 "same group" hits). This is expected, not a new anomaly: these 45 cases were surfaced by Phase 3F's *duplicate-DOI* detection during the DOI backlog pipeline, not by `dedup_papers.py`'s own *title-similarity* detection — a different origin population, with lower title-similarity or missing author-overlap in many cases (that's precisely why Phase 4A's own heuristic, not `pair_confidence()`, was needed to classify them at all — see the Phase 4A report §5, "dedup_papers.py Comparison": these 45 were consistently classified `review`-equivalent or not grouped at all when checked against `pair_confidence()`/`hard_exclusion_reason()` directly). None of TIER 3 should be considered for any auto-merge path; all require human review, and several (the 15 UNCLEAR cases specifically) may not be duplicates at all.

## 8. DOI Pipeline Ordering Analysis

PROVEN FACT: all 55 Phase 3F duplicate-DOI cases exist *because* a paper lacking a DOI was matched by `find_missing_dois.py` to a DOI already attached to a separate, older `ResearchPaper` row — i.e., they are a symptom of the duplicate-record problem surfacing through DOI resolution, not an unrelated issue. The pipeline's own hard duplicate-DOI gate (§ Phase 3E/3F) correctly rejected all 55 rather than double-assigning a DOI, which is why zero duplicate DOIs exist in the DB today despite the duplicate *records* existing underneath.

REASONABLE ENGINEERING INFERENCE (not a repository fact): running dedup **before** further DOI-backlog processing would likely reduce future duplicate-DOI REJECTs (because one of each pair's rows would no longer exist to collide with) and would save wasted Crossref/OpenAlex API calls against a row a merge would delete anyway. This is a plausible efficiency argument, not something demonstrated by running the pipelines in that order.

Counter-consideration, also evidence-based (§5, §7): dedup is not risk-free — TIER 1 merges can silently drop conflicting field data, and TIER 2/3 cases are either not yet provably safe to merge or require human review outright. "Dedup first" trades a *caught, safe* failure mode (the hard DOI gate correctly rejecting) for a process that, if run unattended, has its own *uncaught, silent* failure mode (field loss). Sequencing dedup before DOI enrichment is not simply "safer" in an unqualified sense.

UNKNOWN: whether the product actually wants records merged before or after enrichment — i.e., whether a "duplicate" pair should ideally be merged early (one record, enriched once) or whether keeping both until one is fully enriched and then merging preserves more signal for `choose_keep()`'s tiebreaks (a DOI or citation count picked up during enrichment could change which side wins). Nothing in the repository states this preference; it is a genuine product/UX decision outside this audit's evidence.

## 9. Destructive-Path Audit

`dedup_papers.py`'s destructive boundary, read (not executed) this phase:

- **Mode selection**: `--dry-run` and `--apply` are a `required=True` `mutually_exclusive_group` — the script always requires an explicit mode; there is no silent default that writes.
- **`--apply` requires `--plan <file>`**, enforced by `ap.error(...)` before anything runs — execution can never proceed straight from a fresh detection; it must replay a previously written, human-reviewable plan file.
- **Every write statement**, in order, inside `merge_group()`: `INSERT ... ON CONFLICT DO NOTHING` into `Authors` (remap) + `DELETE FROM "Authors" WHERE "PaperID"=loser`; conditional `INSERT ... ON CONFLICT DO UPDATE ... GREATEST(...)` + `DELETE` on `Citations`; per-`SIMPLE_CHILDREN`-table `UPDATE ... SET "PaperID"=keep` (with a `SAVEPOINT`/`ROLLBACK TO SAVEPOINT` per-row conflict-drop fallback); `UPDATE "ResearchPaper" SET "CitationsByYear"=..., "RawData_Log"=...` on the keep row; `INSERT INTO "AuditLog" (...) VALUES (...)`; `DELETE FROM "ResearchPaper" WHERE "PaperID"=loser`.
- **Transaction boundary**: the *entire* `--apply` invocation (every group in the filtered, `[HIGH]`-only report, `--limit-groups` permitting) runs inside a single `transaction.atomic()` block. There is no per-group commit.
- **Rollback behavior**: the post-loop profile-preservation assertion (every `UserID` linked to any merged member must still be linked to the kept paper) raises `RuntimeError` on violation, which rolls back the **whole transaction** — every group in that `--apply` call, not just the offending one. A mid-loop database error would do the same via Django's `atomic()` semantics.
- **Idempotency**: re-running `--apply --plan <same file>` after a prior success is safe — `main()` reloads live `papers` fresh and drops any plan group whose `kept`/loser PaperIDs no longer exist (`len(alive) > 1` check), so already-merged groups become no-ops rather than errors.
- **Partial-failure risk**: because one `--apply` call is one transaction, there is no "commit the good groups, skip the bad one" behavior within a single invocation — a failure anywhere rolls back everything from that call. The documented mitigation is `--limit-groups N` (explicitly recommended in the module docstring: "Use `--limit-groups 1` on your first `--apply`") to bisect risk across multiple smaller invocations rather than relying on partial-transaction behavior that doesn't exist.
- **Recovery artifacts**: a full pre-write JSON snapshot (`snapshot_<ts>.json`, every member row + every present child-table row via `snapshot_paper()`) is written *before* any write in the transaction, plus an `AuditLog` row per merged-away loser (`Action='paper.merge.dedup'`, includes kept-paper-id, loser title/DOI/source/citations, merged citation total). §4's gap: `AuthorReviewQueue` rows are not included in `snapshot_paper()`'s captured child tables (it only snapshots `child_tables` = `SIMPLE_CHILDREN ∩ existing tables` + `Citations`), so a CASCADE-deleted `AuthorReviewQueue` row (currently impossible — 0 rows) would not appear in the recovery snapshot either.

No destructive path was executed to produce this section — it is a static read of the code plus the row-count/constraint facts gathered via read-only SQL.

## 10. Final Decision

**C) MORE FORENSICS REQUIRED**

Reasoning: TIER 1 (§7, 8 pairs) is close to being genuinely safe for a future narrow auto-merge, but §5 proves that even `[HIGH]`-confidence merges can silently drop conflicting field data (45% of the measured 40-pair population had at least one such conflict), and that check has not yet been isolated to the TIER 1 subset specifically. TIER 2 (2 pairs) is real but still requires one more concrete check (running `pair_confidence()` against `detect_groups()`'s own `papers` dict, not Phase 4A's separately-built one) before it can be trusted as equivalent to TIER 1. TIER 3 (45 pairs) is confirmed to need human review and must not be part of any automated path. The blocking-key fix needed for TIER 2 (and for whatever undiscovered TIER-2-equivalent cases exist beyond this 55-case sample — §3's 24-pair DB-wide miss set is larger than the 2 known Phase-4A cases) has not been designed, only measured. None of this points to READY-FOR-4C on either the safer-detection or merge-plan-generator options yet, but the problem is also not so open-ended or contradictory as to warrant D (SHOULD-NOT-PROCEED) — the path forward is concrete and narrow, just not yet walked.

## 11. Required Next Step

**Phase 4C should be scoped to exactly one deliverable: a read-only, per-pair field-conflict + blocking-eligibility report restricted to the 8 TIER 1 pairs plus the 2 TIER 2 pairs (10 total), answering — for each — (a) does it have a genuine two-sided field conflict per §5's method, and (b) for the 2 TIER 2 pairs specifically, does `pair_confidence()` return `high` when fed the exact `papers` dict `detect_groups()` itself builds. No merge, no code change, no `--apply`, still strictly read-only. That report is the evidence needed to decide, in a subsequent phase, whether any of these 10 pairs can be merged as a first supervised, `--limit-groups 1`-at-a-time batch.**
