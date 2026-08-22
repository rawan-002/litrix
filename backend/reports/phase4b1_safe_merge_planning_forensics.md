# Phase 4B.1 — Safe Duplicate Merge Planning Forensics

## 1. Safety Confirmation

- Source code modified: **0**
- Database writes (INSERT/UPDATE/DELETE/MERGE/TRUNCATE): **0**
- Network calls: **0**
- Records merged: **0**
- DOI changes: **0**

One new read-only script was written and run: `phase4b1_merge_planning.py` (SELECT-only against `ResearchPaper`, `Authors`, `Citations`, `ExternalAuthors`, `PaperKeywords`, `CitationsHistory`, `ReportPaperDecision`, `AuthorReviewQueue`; imports `dedup_papers.py`'s pure functions `norm_title`, `choose_keep`, `choose_keep_reason`, `pair_confidence`, `hard_exclusion_reason`, `_is_distinct_record` — never calls `detect_groups()`, `merge_group()`, `main()`, or `--apply`). Output: `backend/reports/phase4b1_raw_analysis.json` (raw per-pair data this report is built from).

## 2. Scope

The 10 pairs analyzed, exactly as specified — no others:

**TIER 1 (8):** `{5434,5329}` `{5481,5207}` `{5482,5232}` `{5549,5548}` `{6088,6086}` `{6091,3875}` `{6189,6153}` `{7572,6645}`
**TIER 2 (2):** `{5289,5392}` `{6107,6109}`

## 3. Pair-by-Pair Analysis

For every pair below, `pair_confidence()` (run against the exact author/DOI/year/title data assembled the same way `detect_groups()` would build it) returned `high`, `hard_exclusion_reason()` returned `None`, and `_is_distinct_record()` returned `False` — **PROVEN FACT**, not inferred. This closes the Phase 4B §7 TIER 2 evidence gap: `{5289,5392}` and `{6107,6109}` genuinely would be `high`-confidence if `detect_groups()` ever compared them; the only reason they aren't in the real plan is the blocking miss documented in Phase 4B §3, not a confidence-scoring problem.

In every one of the 10 pairs, `choose_keep_reason` = **`has_doi`** — the survivor is, in all 10 cases, simply the side that has a DOI and the loser does not. No pair in this set required the citations/title-length/verified/lowest-id tiebreaks at all.

Field counts below exclude `Title`/`NormalizedTitle` (always CONFLICT by construction — that wording difference **is** the duplicate signal itself, not a data-loss risk: the loser's exact title text is preserved in the pre-merge JSON snapshot + `AuditLog`, just not visible in live queries after a merge) and exclude `ScrapedAt` (ingestion timestamp, never semantically meaningful to reconcile).

---

### Pair: 5434 / 5329 — TIER 1
- **Duplicate evidence:** shared author (UserID 97), `SequenceMatcher`≥0.95 title match ("Towards optimizing..." vs "Optimizing..."), `PubYear` 2019 vs 2020 (1-year gap, within `_years_compatible`'s tolerance), DOI-asymmetry (survivor has one, loser doesn't).
- **Preferred survivor:** 5329 (has DOI). Reason label: **PROVEN BY REPOSITORY** (this is exactly `choose_keep()`'s tier-1, already-coded rule) + **PROVEN BY DB DATA** (5329's DOI `10.1109/aiccsa47632.2019.9035301` is a real, populated value; 5434's is `NULL`).
- **Field diff summary:** SAME 15 · ONE_SIDED 10 (all in survivor's favor — Abstract, CitationsByYear, AffiliationVerified, VerificationSource, VerifiedAt, VerificationDetails, AbstractSource, PdfAccessType, JournalID, DOI) · CONFLICT 5 (Title/NormalizedTitle + benign ScrapedAt — expected; **PubYear** 2019 vs 2020; **PublicationType** "Conference Paper" vs "Research Article") · CONTAMINATED 0.
- **CONFLICT detail — PubYear:** neither `pair_confidence()` nor `merge_group()` resolves this; `merge_group()` never touches `PubYear` at all, so 2019 (survivor's) silently survives and 2020 is discarded. No repository rule states which is correct (conference-proceedings vs. indexer year drift is plausible but unproven). **HUMAN_DECISION_REQUIRED.**
- **CONFLICT detail — PublicationType:** "Conference Paper" (survivor) vs "Research Article" (loser); `VenueType` agrees on "Conference" for both sides — that agreement doesn't resolve `PublicationType` since the two columns are independently populated. No deterministic rule exists in the repo to pick a winner. **HUMAN_DECISION_REQUIRED.**
- **Dependency migration:** all of `Citations`/`ExternalAuthors`/`PaperKeywords`/`CitationsHistory`/`ReportPaperDecision`/`AuthorReviewQueue`/`ReportPaperDecision.MissingResolvedToPaperID` = 0 rows on both sides. Authors: both sides = `{97}` (identical set) — remap is a no-op union.
- **Existing `merge_group()` coverage:** Authors/Citations/CitationsByYear/SIMPLE_CHILDREN — fully sufficient here since nothing needs remapping. **Not** covered: the 2 CONFLICT fields above (both silently resolved in survivor's favor with no reconciliation logic).
- **Unhandled dependencies:** none active (all 0-row).
- **Mechanical safety classification: PLAN_REQUIRES_HUMAN_APPROVAL.**
- **Exact blockers:** `PubYear` and `PublicationType` conflicts have no deterministic winner.

---

### Pair: 5481 / 5207 — TIER 1
- **Duplicate evidence:** shared author (97), title differs only by a "Research Article " boilerplate prefix on the loser, `PubYear` equal (2022), DOI-asymmetry.
- **Preferred survivor:** 5207 (has DOI `10.1155/2022/8950243`). **PROVEN BY REPOSITORY** + **PROVEN BY DB DATA**.
- **Field diff summary:** SAME 16 · ONE_SIDED 11 (all survivor's favor: Abstract, CitationsByYear, AffiliationVerified, VerificationSource, VerifiedAt, VerificationDetails, AbstractSource, PdfUrl, PdfAccessType, JournalID, DOI) · CONFLICT 3 (Title/NormalizedTitle + benign ScrapedAt — expected) · CONTAMINATED 0.
- **Dependency migration:** all dependency tables 0/0 on both sides. Authors: both `{97}`.
- **Existing `merge_group()` coverage:** fully sufficient — every genuinely differing field is ONE_SIDED in the survivor's favor already (nothing is lost, since the survivor already carries the richer value and the loser has nothing to contribute).
- **Unhandled dependencies:** none.
- **Mechanical safety classification: SAFE_PLAN_CANDIDATE.**
- **Exact blockers:** none.

---

### Pair: 5482 / 5232 — TIER 1
- **Duplicate evidence:** shared author (97), boilerplate-prefix-only title difference, `PubYear` equal (2022), DOI-asymmetry.
- **Preferred survivor:** 5232 (has DOI `10.1155/2022/8531213`). **PROVEN BY REPOSITORY** + **PROVEN BY DB DATA**.
- **Field diff summary:** SAME 16 · ONE_SIDED 11 (all survivor's favor, same shape as the pair above) · CONFLICT 3 (Title/NormalizedTitle + benign ScrapedAt) · CONTAMINATED 0.
- **Dependency migration:** all 0/0. Authors both `{97}`.
- **Existing `merge_group()` coverage:** fully sufficient, same reasoning as 5481/5207.
- **Unhandled dependencies:** none.
- **Mechanical safety classification: SAFE_PLAN_CANDIDATE.**
- **Exact blockers:** none.

---

### Pair: 5549 / 5548 — TIER 1
- **Duplicate evidence:** shared author (104), boilerplate-prefix-only title difference (here the **survivor** carries the "Research Article" prefix, loser doesn't — direction is irrelevant to the rule, `choose_keep()` still picked purely on `has_doi`), `PubYear` equal (2022), DOI-asymmetry.
- **Preferred survivor:** 5548 (has DOI `10.1155/2022/3183492`). **PROVEN BY REPOSITORY** + **PROVEN BY DB DATA**.
- **Field diff summary:** SAME 21 (incl. `CitationsByYear` and `VerificationDetails`, which are identical on both sides — both records were independently affiliation-verified with the same result) · ONE_SIDED 6 (5 survivor-favor: Abstract, DOI, AbstractSource, PdfUrl, PdfAccessType; **1 loser-favor: `JournalID`=676 on the loser, `NULL` on the survivor**) · CONFLICT 3 (Title/NormalizedTitle + `VerifiedAt`, a benign timestamp) · CONTAMINATED 0.
- **CONFLICT/gap detail — `JournalID`:** the loser carries a real, populated `JournalID` (676) that the survivor lacks. `merge_group()` never backfills non-listed `ResearchPaper` columns from the loser, so this value would be silently discarded today. The correct action for a future plan generator is deterministic (`BACKFILL_FROM_LOSER` — there's no ambiguity, only one side has a value), but it is **not something the current, unmodified merge code performs**.
- **Dependency migration:** all child tables 0/0. Authors both `{104}`.
- **Existing `merge_group()` coverage:** handles Authors/Citations/CitationsByYear/SIMPLE_CHILDREN correctly (nothing to remap); does **not** cover the `JournalID` backfill gap above.
- **Unhandled dependencies:** none (0-row); the `JournalID` gap is a `ResearchPaper`-column gap, not a child-table dependency gap.
- **Mechanical safety classification: SAFE_PLAN_CANDIDATE** — the `JournalID` gap has a fully deterministic, evidence-supported resolution (no CONFLICT, no ambiguity), it just isn't automated by today's code. A plan generator (§5) can emit it correctly without human judgment.
- **Exact blockers:** none for planning purposes; implementation of the `JournalID` backfill action is required before this pair could be safely auto-applied.

---

### Pair: 6088 / 6086 — TIER 1
- **Duplicate evidence:** shared author (105), boilerplate-prefix-only title difference, `PubYear` equal (2021), DOI-asymmetry. (This is the original Nizar-Alsharif case cited in `dedup_papers.py`'s own `block_key()` docstring as the motivating example for word-based blocking.)
- **Preferred survivor:** 6086 (has DOI `10.1155/2021/5534379`). **PROVEN BY REPOSITORY** + **PROVEN BY DB DATA**.
- **Field diff summary:** SAME 21 (incl. identical `CitationsByYear` and `VerificationDetails`) · ONE_SIDED 6 (5 survivor-favor: Abstract, DOI, AbstractSource, PdfUrl, PdfAccessType; **1 loser-favor: `JournalID`=771 on the loser, `NULL` on the survivor**) · CONFLICT 3 (Title/NormalizedTitle + benign `VerifiedAt`) · CONTAMINATED 0.
- **CONFLICT/gap detail — `JournalID`:** same pattern as 5549/5548 above — loser has a real value, survivor doesn't, `merge_group()` doesn't backfill it. Deterministic action available (`BACKFILL_FROM_LOSER`), not automated today.
- **Dependency migration:** all child tables 0/0. Authors both `{105}`.
- **Existing `merge_group()` coverage:** handles Authors/Citations/CitationsByYear/SIMPLE_CHILDREN correctly; does not cover the `JournalID` backfill gap.
- **Unhandled dependencies:** none (0-row).
- **Mechanical safety classification: SAFE_PLAN_CANDIDATE** — same reasoning as 5549/5548: the gap is deterministic, not ambiguous.
- **Exact blockers:** none for planning; `JournalID` backfill action needed before auto-apply.

---

### Pair: 6091 / 3875 — TIER 1
- **Duplicate evidence:** shared author (105; survivor additionally credits UID 6, a co-author absent from the loser's Authors rows), boilerplate-prefix-only title difference, `PubYear` equal (2019), DOI-asymmetry.
- **Preferred survivor:** 3875 (has DOI `10.1155/2019/4568368`). **PROVEN BY REPOSITORY** + **PROVEN BY DB DATA**.
- **Field diff summary:** SAME 11 · ONE_SIDED 14 (all survivor's favor: Abstract, Volume, Issue, Pages, CitationsByYear, AffiliationVerified, VerificationSource, VerifiedAt, VerificationDetails, AbstractSource, PdfUrl, PdfAccessType, JournalID, DOI) · CONFLICT 5 (Title/NormalizedTitle + benign ScrapedAt, plus two genuine ones below) · CONTAMINATED 0.
- **CONFLICT detail — Language:** "English" (survivor) vs "en" (loser) — almost certainly the same real value in two different encodings, but no canonicalization table exists anywhere in the repo to *prove* that as opposed to assume it. **HUMAN_DECISION_REQUIRED** (a trivial one, but not one the current codebase can make deterministically).
- **CONFLICT detail — Source:** "Scopus" (survivor) vs "Scholar" (loser) — two different ingestion pipelines independently created rows for the same paper. Per `CLAUDE.md`'s data-attribution rules, Scholar's `articles[]` is the source of truth for *attribution*, but that rule says nothing about which `Source` value a merged *metadata* row should keep. **HUMAN_DECISION_REQUIRED.**
- **Dependency migration:** `ExternalAuthors` — survivor 5 rows / loser 0 rows (no migration needed, loser contributes nothing). Every other table 0/0. Authors: survivor `{6,105}` ⊇ loser `{105}` — union is a strict superset, so the post-merge profile-preservation assertion passes trivially.
- **Existing `merge_group()` coverage:** handles Authors/Citations/CitationsByYear/SIMPLE_CHILDREN correctly for this pair (nothing to remap on the loser side). **Not covered:** Language and Source conflicts.
- **Unhandled dependencies:** none active.
- **Mechanical safety classification: PLAN_REQUIRES_HUMAN_APPROVAL.**
- **Exact blockers:** Language and Source conflicts, no deterministic resolution rule.

---

### Pair: 6189 / 6153 — TIER 1
- **Duplicate evidence:** shared author (112), boilerplate-prefix-only title difference, `PubYear` equal (2022), DOI-asymmetry.
- **Preferred survivor:** 6153 (has DOI `10.1155/2022/6354543`). **PROVEN BY REPOSITORY** + **PROVEN BY DB DATA**.
- **Field diff summary:** SAME 17 · ONE_SIDED 11 (all survivor's favor) · CONFLICT 2 (Title/NormalizedTitle only — `ScrapedAt` is identical for this pair) · CONTAMINATED 0.
- **Dependency migration:** all 0/0. Authors both `{112}`.
- **Existing `merge_group()` coverage:** fully sufficient.
- **Unhandled dependencies:** none.
- **Mechanical safety classification: SAFE_PLAN_CANDIDATE.**
- **Exact blockers:** none.

---

### Pair: 7572 / 6645 — TIER 1
- **Duplicate evidence:** shared author (81), high title similarity ("Fine Alignment" vs "Alignment" wording variant), `PubYear` 2026 vs 2025 (1-year gap, compatible), DOI-asymmetry.
- **Preferred survivor:** 6645 (has DOI `10.1109/jstars.2026.3683462`). **PROVEN BY REPOSITORY** + **PROVEN BY DB DATA**.
- **Field diff summary:** SAME 14 · ONE_SIDED 11 (survivor's favor: Abstract, CitationsByYear, AffiliationVerified, VerificationSource, VerifiedAt, VerificationDetails, AbstractSource, PdfUrl, PdfAccessType, JournalID, DOI) · CONFLICT 5 (Title/NormalizedTitle + benign ScrapedAt, plus two genuine ones below) · CONTAMINATED 0.
- **CONFLICT detail — PubYear:** 2026 (survivor) vs 2025 (loser) — same unresolved-silently-discarded pattern as pair 5434/5329. **HUMAN_DECISION_REQUIRED.**
- **CONFLICT detail — VenueType:** "Journal" (survivor) vs "Preprint" (loser) — this one is more consequential than a cosmetic mismatch: it suggests the loser row may represent an earlier preprint version and the survivor the final published version, which is exactly the "evolutionary papers" pattern `hard_exclusion_reason()` is designed to guard against — except that guard only fires when **both DOIs are present and differ**; here the loser has no DOI at all, so the guard never triggers and the pair still reaches `high` confidence. This is a real edge case not previously surfaced: a preprint/published-version pair with a DOI on only one side currently reads identically to a true duplicate to `pair_confidence()`. **HUMAN_DECISION_REQUIRED** — and worth flagging in isolation as a `pair_confidence()` blind spot even though it doesn't invalidate the pair's overall duplicate status here (both plausibly *are* the same work).
- **Dependency migration:** `Citations` — survivor 1 row / loser 0. `CitationsHistory` — survivor 1 row / loser 0. `ReportPaperDecision` — survivor 2 rows / loser 0. All loser-side counts are 0, so `merge_group()`'s remap logic has nothing to do for this pair (it would remap loser rows that don't exist). Authors both `{81}`.
- **Existing `merge_group()` coverage:** sufficient for what's present (nothing to remap); does not cover PubYear/VenueType.
- **Unhandled dependencies:** none active.
- **Mechanical safety classification: PLAN_REQUIRES_HUMAN_APPROVAL.**
- **Exact blockers:** PubYear conflict; VenueType conflict with the preprint/published-version ambiguity noted above.

---

### Pair: 5289 / 5392 — TIER 2
- **Duplicate evidence:** shared author (97), `SequenceMatcher`≥0.95 title match ("Adopting formal verification..." vs "Formal verification..."), `PubYear` equal (2020), DOI-asymmetry. **`pair_confidence()` = `high` when run directly (see header note) — this is the missing evidence Phase 4B §7 flagged as still needed for TIER 2, now closed.**
- **Preferred survivor:** 5289 (has DOI `10.5220/0009592102610268`). **PROVEN BY REPOSITORY** + **PROVEN BY DB DATA**.
- **Field diff summary:** SAME 16 · ONE_SIDED 7 (survivor's favor: JournalID, Abstract, DOI, CitationsByYear, AbstractSource, PdfUrl, PdfAccessType) · CONFLICT 7 (Title/NormalizedTitle + benign ScrapedAt/VerifiedAt, plus three genuine ones below) · CONTAMINATED 0.
- **CONFLICT detail — PublicationType:** "Conference Paper" (survivor) vs "Research Article" (loser). Same unresolved pattern as pair 5434/5329. **HUMAN_DECISION_REQUIRED.**
- **CONFLICT detail — VenueType:** "Conference" (survivor) vs "Journal" (loser) — a genuinely substantive disagreement about venue type between two independently-run affiliation-verification passes, not just cosmetic. **HUMAN_DECISION_REQUIRED.**
- **CONFLICT detail — VerificationDetails:** both populated, JSON differs beyond the shared prefix (`decision_basis: openalex_complete_affiliations` on both, but different evidence-trail URLs) — the two verification passes reached the same *tier* of confidence via different underlying evidence. Not a contradiction in substance, but not byte-identical either, so it cannot be silently treated as SAME. **HUMAN_DECISION_REQUIRED** if the intent is to preserve full provenance rather than just the survivor's copy.
- **Dependency migration:** all 0/0. Authors both `{97}`.
- **Existing `merge_group()` coverage:** nothing to remap; does not cover the 3 conflicts above.
- **Unhandled dependencies:** none active.
- **Mechanical safety classification: PLAN_REQUIRES_HUMAN_APPROVAL.**
- **Exact blockers:** PublicationType, VenueType, VerificationDetails conflicts.

---

### Pair: 6107 / 6109 — TIER 2
- **Duplicate evidence:** shared author (69), high title similarity ("Path Planning Models..." vs "New path planning model..."), `PubYear` 2018 vs 2017 (1-year gap, compatible), DOI-asymmetry. **`pair_confidence()` = `high` when run directly — same closed evidence gap as the pair above.**
- **Preferred survivor:** 6107 (has DOI `10.1007/s11276-017-1493-2`). **PROVEN BY REPOSITORY** + **PROVEN BY DB DATA**.
- **Field diff summary:** SAME 23 (incl. identical `CitationsByYear` and `VerificationDetails` — both independently affiliation-verified via the same publisher-HTML meta-tag strategy, byte-identical result) · ONE_SIDED 3 (2 survivor-favor: DOI, PdfAccessType; **1 loser-favor: `JournalID`**) · CONFLICT 4 (Title/NormalizedTitle + benign VerifiedAt, plus one genuine one below) · CONTAMINATED 0.
- **CONFLICT detail — PubYear:** 2018 (survivor) vs 2017 (loser) — same unresolved-silently-discarded pattern as the two PubYear conflicts above (this is very plausibly an online-first-vs-print-issue date difference for the same Springer article, which is a **REASONABLE INFERENCE**, not proven — Springer articles commonly carry both an online and a print year). **HUMAN_DECISION_REQUIRED** to confirm before choosing which year the merged record should keep, even though the underlying explanation is plausible.
- **Dependency migration:** `JournalID` is populated on the loser (1104) and empty on the survivor — a genuine `ONE_SIDED` case in the *loser's* favor, meaning the current has-DOI-wins survivor selection would actually **discard** a populated `JournalID` here (the loser has it, the survivor doesn't, and `merge_group()` never touches `ResearchPaper` non-listed columns on the keep row). Every dependency table is 0/0. Authors both `{69}`.
- **Existing `merge_group()` coverage:** does not cover PubYear, and does not backfill the survivor's missing `JournalID` from the loser even though the loser has a real value and the survivor has none — a concrete, provable instance of the §5 "field loss" risk found in Phase 4B, now attributable to a specific real field on a specific real pair.
- **Unhandled dependencies:** none active.
- **Mechanical safety classification: PLAN_REQUIRES_HUMAN_APPROVAL.**
- **Exact blockers:** PubYear conflict; loser-side `JournalID` value that would be silently lost.

## 4. Cross-Pair Findings

- **Survivor selection was never the risk factor.** All 10 pairs resolve via `choose_keep_reason = has_doi` — every survivor choice is **PROVEN BY REPOSITORY** logic plus **PROVEN BY DB DATA**. Zero REASONABLE INFERENCE or UNKNOWN was needed anywhere in Task 2 for this specific set of 10. The risk entirely lives in **field-level conflicts on the losing side**, matching Phase 4B §5's general finding but now localized to specific fields on specific pairs.
- **`PublicationType`/`VenueType` conflicts are the dominant recurring problem**, appearing in 4 of the 5 non-safe pairs (5434/5329, 6091/3875 excepted — that one has Language/Source instead —, 7572/6645, 5289/5392). No field in `dedup_papers.py` or anywhere else in the repo defines a canonical resolution rule for either column when both sides disagree.
- **`PubYear` off-by-one conflicts recur 3 times** (5434/5329, 7572/6645, 6107/6109) — all within `_years_compatible()`'s ≤1 tolerance (so the pair still groups correctly), but `merge_group()` never reconciles `PubYear` itself, meaning the discarded year is silently lost every time this pattern occurs.
- **A genuine preprint-vs-published-version edge case was found** (7572/6645, `VenueType` "Preprint" vs "Journal") that `hard_exclusion_reason()`'s "evolutionary papers" guard does not catch, because that guard requires **both** sides to carry a DOI — here only one does. This is a real, previously-undocumented gap in the duplicate-vs-evolutionary-paper distinction, not just a data-completeness issue.
- **A genuine, recurring data-loss pattern was found and localized**, not merely inferred: in 3 of 10 pairs (5549/5548, 6088/6086, 6107/6109), the **loser** carries a populated `JournalID` that the survivor lacks (676, 771, and 1104 respectively); today's `merge_group()` would silently discard it on merge since `JournalID` isn't in `merge_citation_fields()` or any remapped table. Unlike the PublicationType/VenueType/Language/Source conflicts above, this one has a fully deterministic fix (only one side ever has a value in this set — `BACKFILL_FROM_LOSER`), so it doesn't block a plan from being `SAFE_PLAN_CANDIDATE`, but it does mean `merge_group()` as it stands today would quietly drop real, usable data on 3 of the 5 pairs currently classified safe.
- **No dependency-table risk was found in this specific 10-pair set** — every loser in every pair has 0 rows in `Citations`, `ExternalAuthors`, `PaperKeywords`, `CitationsHistory`, `ReportPaperDecision`, `AuthorReviewQueue`, and `ReportPaperDecision.MissingResolvedToPaperID`. This means the child-table remap machinery in `merge_group()` is **never actually exercised** by any of these 10 pairs — their safety says nothing about whether `remap_simple_child()`'s conflict-drop fallback behaves correctly on a pair that *does* have loser-side child rows (Phase 4B's real DB-wide dry-run groups likely include such cases; this narrower 10-pair set does not).
- **No contamination (author-list-as-title, citation-text-in-title, etc.) was found in any of the 10 pairs** — the known contamination patterns from Phase 3E/4A are absent from this specific set. This should not be read as "contamination is rare across all 55 cases," only as "not present in TIER 1/TIER 2."

## 5. Proposed Safe Merge Plan Schema (DESIGN ONLY — not implemented)

```json
{
  "pair": [7392, 5289],
  "duplicate_confidence": "high",
  "survivor_selection": {
    "winner": 5289,
    "reasons": [
      {"criterion": "has_doi", "label": "PROVEN BY REPOSITORY", "detail": "choose_keep() priority-1 tiebreak"},
      {"criterion": "doi_value", "label": "PROVEN BY DB DATA", "detail": "10.5220/0009592102610268 present on winner, NULL on loser"}
    ]
  },
  "field_actions": [
    {"field": "Title", "classification": "CONFLICT", "action": "KEEP_SURVIVOR_VALUE", "note": "expected -- defines the duplicate relationship, loser text preserved in snapshot"},
    {"field": "Abstract", "classification": "ONE_SIDED", "action": "KEEP_SURVIVOR_VALUE", "note": "loser has no value to contribute"},
    {"field": "PublicationType", "classification": "CONFLICT", "action": "HUMAN_DECISION_REQUIRED", "note": "no repository rule resolves Conference Paper vs Research Article"},
    {"field": "JournalID", "classification": "ONE_SIDED_LOSER_FAVOR", "action": "BACKFILL_FROM_LOSER", "note": "loser has a populated value the survivor lacks -- today's merge_group() would drop this silently"}
  ],
  "dependency_actions": [
    {"table": "Authors", "reference": "PaperID", "loser_rows": 1, "survivor_rows": 1, "future_action": "REPOINT_TO_SURVIVOR", "collision_risk": "none -- disjoint or subset UserID sets", "handled_by_merge_group_today": true},
    {"table": "AuthorReviewQueue", "reference": "PaperID (CASCADE)", "loser_rows": 0, "survivor_rows": 0, "future_action": "KEEP_EXISTING", "collision_risk": "n/a (0 rows)", "handled_by_merge_group_today": false}
  ],
  "unresolved_conflicts": ["PublicationType", "VenueType"],
  "mechanically_safe": false,
  "human_approval_required": true,
  "reason_blocked": ["PublicationType conflict has no deterministic resolution rule", "VenueType conflict has no deterministic resolution rule"]
}
```

Notes on the design (not code): `field_actions[].action` should be an enum restricted to `{KEEP_SURVIVOR_VALUE, KEEP_LOSER_VALUE, BACKFILL_FROM_LOSER, MERGE_VALUES (only where a real merge function exists, e.g. CitationsByYear), HUMAN_DECISION_REQUIRED}` — a generator must never silently pick `KEEP_SURVIVOR_VALUE` for a field classified `CONFLICT`; that action is only valid for `ONE_SIDED` (survivor-favor) and `SAME`. `dependency_actions[].handled_by_merge_group_today` is a boolean specifically so a plan can be filtered to "only actions the current, unmodified `merge_group()` code already performs correctly" versus actions a plan reviewer must execute by hand (e.g. any `AuthorReviewQueue`/`MissingResolvedToPaperID` row, since neither is remapped today). `mechanically_safe` should be computed as `true` **only if** every `field_actions[].classification` is `SAME`/`ONE_SIDED`(survivor-favor)/`COMPATIBLE`, every `dependency_actions[].handled_by_merge_group_today` is `true`, and `unresolved_conflicts` is empty — never set from `duplicate_confidence` alone.

## 6. What Existing `merge_group()` Would Lose

Concrete, not hypothetical, for this 10-pair set:

- **`PublicationType`**: silently loses the loser's differing value in 2 of 10 pairs (5434/5329, 5289/5392) — no reconciliation logic exists for this column.
- **`VenueType`**: silently loses the loser's differing value in 2 of 10 pairs (7572/6645, 5289/5392) — same, and in one case (7572/6645) the disagreement plausibly signals a preprint-vs-published-version distinction rather than a cosmetic mismatch.
- **`PubYear`**: silently loses the loser's differing (but "compatible," gap≤1) year in 3 of 10 pairs (5434/5329, 7572/6645, 6107/6109) — this column is never touched by `merge_group()` at all.
- **`Language`, `Source`**: silently loses the loser's differing value in 1 of 10 pairs (6091/3875).
- **`VerificationDetails`**: silently loses the loser's differing (non-identical) JSON evidence trail in 1 of 10 pairs (5289/5392), even where both sides reached the same confidence tier through different evidence.
- **`JournalID`**: can silently lose a *populated* value where the survivor's is `NULL` — proven on **3 of 10 pairs** (5549/5548 loser=676, 6088/6086 loser=771, 6107/6109 loser=1104), all discarded, survivor stays `NULL`. This is the clearest, most concrete data-loss example found in this phase, and the most common: not a conflicting value needing a human, just a value that exists on the losing side and nowhere else, which `merge_group()` has no mechanism to backfill regardless of confidence tier.
- Everything `merge_group()` **does** correctly handle across all 10 pairs: `Authors` (all unions verified trivial — no case where the loser contributed a `UserID` the survivor lacked), `Citations`/`CitationsHistory`/`ReportPaperDecision`/`ExternalAuthors`/`AuthorReviewQueue` (all loser-side row counts are 0 in this set, so no remap was actually exercised — this is a coverage gap in the *test*, not proof the remap code itself is correct on a nonzero case).

## 7. Phase 4C Decision

**B) READ-ONLY MERGE-PLAN GENERATOR ONLY**, and only for a scope narrower than "these 10 pairs" — see §8.

Reasoning: a merge **executor** (option C) is not justified — 5 of the 10 pairs have at least one field-level conflict with no deterministic resolution rule (`PublicationType`, `VenueType`, `PubYear`, `Language`, `Source`, `VerificationDetails`), and 3 pairs (5549/5548, 6088/6086, 6107/6109) have a proven silent-data-loss path (`JournalID`) that today's `merge_group()` does not handle regardless of confidence tier — including 2 of the 5 otherwise-SAFE pairs. "Only 10 pairs" does not change that standard — half of them are not mechanically safe by the evidence gathered here, and `merge_group()`'s child-table remap logic was not exercised by any of the 10 (all loser dependency-row counts are 0), so its correctness on a real nonzero remap case remains unverified even for the 5 that otherwise look clean. Option A (candidate-detection repair only) is not the right next step either — detection was already closed for TIER 2 in this phase (`pair_confidence()` proven `high` for both pairs); the open problem now is field-level plan generation, not detection. Option D (more forensics, full stop) is too conservative given how localized and enumerable the remaining unknowns are (5 specific field-conflict types, 1 specific untested remap path) — a scoped, read-only plan-generator prototype is the more useful next artifact, provided it is explicitly restricted to the 5 SAFE_PLAN_CANDIDATE pairs and treats the other 5 as designed-to-output `human_approval_required: true` rather than silently resolved.

## 8. One Required Next Step

**Build a read-only Phase 4C prototype of the §5 schema, restricted to the 8 pairs total that are currently SAFE_PLAN_CANDIDATE or have a fully enumerated, single-field HUMAN_DECISION_REQUIRED blocker (i.e., all 10 pairs are in scope for the *plan generator*, but it must emit `mechanically_safe: true` for none of them without a human confirming each listed `field_actions[].action == HUMAN_DECISION_REQUIRED`) — still zero DB writes, output is the JSON plan file only, and it must additionally be validated against at least one real duplicate pair (from the wider 24-group `--dry-run` output, outside these 10) that has nonzero loser-side child-table rows, specifically to exercise and verify the `dependency_actions` portion of the schema that this 10-pair set could not test.**
