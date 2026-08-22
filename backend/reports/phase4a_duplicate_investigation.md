# Phase 4A — Duplicate ResearchPaper Investigation (Read-Only)

Population: the 55 unique duplicate-DOI hard-gate cases from `backend/reports/phase3f_duplicate_classification.md` (primary source of truth — parsed directly from that file, not re-derived from memory).

Method: for every pair, real metadata was pulled from `ResearchPaper`/`Authors` (SELECT only) and scored on multiple independent signals — title similarity (two metrics: `doi_pipeline.title_token_jaccard` and `dedup_papers.py`'s own `SequenceMatcher` on its `norm_title()`/`fuzzy_key`), publication year compatibility, DOI-presence asymmetry, shared Litrix-researcher authorship (`Authors.UserID` overlap — the same signal `dedup_papers.py` itself uses), shared raw scraped author names, bibliographic field overlap (volume/issue/pages), and `dedup_papers.py`'s own real `pair_confidence()` / `hard_exclusion_reason()` functions, imported and called directly (no modification to that file). No single threshold decided any case alone.

## 1. Executive Summary

```
Total investigated:      55
CONFIRMED_DUPLICATE:     10
LIKELY_DUPLICATE:        30
NOT_DUPLICATE:            0
UNCLEAR_MANUAL_REVIEW:   15

Classified consistently with dedup_papers.py's own high/review split: 55/55 (0 disagreements)
```

**On `NOT_DUPLICATE = 0`, stated plainly rather than as a clean result**: this population is structurally biased against ever producing a `NOT_DUPLICATE` verdict, for two concrete reasons found in the data, not because every pair is truly a duplicate:
1. `dedup_papers.py`'s own `hard_exclusion_reason()` — its explicit "these are different papers" guard — can only fire when **both** sides already carry a DOI and those DOIs differ. By construction, every one of these 55 cases is DOI-asymmetric (the current paper has none; only the candidate-owner does), so this guard's precondition is never met here. It provides zero discriminating power on this specific population, not because it agrees they're duplicates.
2. `Authors.UserID` overlap is `True` for **all 55/55 pairs** — every duplicate-DOI candidate pair shares at least one common Litrix-attributed researcher. This is itself informative (consistent with "the same researcher's paper got scraped twice from two sources"), but it also means my own `NOT_DUPLICATE` rule (which required *no* author overlap) could never fire either. The lowest-similarity cases (e.g. `#5379`, jaccard=0.0) were routed to `UNCLEAR_MANUAL_REVIEW` instead, not `NOT_DUPLICATE`, precisely because author overlap alone isn't proof of *anything* when the title itself turns out to be corrupted (see §6).

## 2. Per-case table

Full 55-row table with every raw signal (`seq_sim`, `jaccard`, `year_rel`, `uid_overlap`, `raw_overlap`, `bib_match`, `ded_conf`, `ded_hard`) is at `backend/reports/phase4a_raw_analysis.json`. Condensed view:

| Current | Owner | DOI | Classification | Confidence basis | dedup_papers.py |
|---|---|---|---|---|---|
| 5392 | 5289 | 10.5220/0009592102610268 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=0.964 | high |
| 5434 | 5329 | 10.1109/aiccsa47632.2019.9035301 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=0.958 | high |
| 5481 | 5207 | 10.1155/2022/8950243 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=1.000 | high |
| 5482 | 5232 | 10.1155/2022/8531213 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=1.000 | high |
| 5549 | 5548 | 10.1155/2022/3183492 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=1.000 | high |
| 6088 | 6086 | 10.1155/2021/5534379 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=1.000 | high |
| 6091 | 3875 | 10.1155/2019/4568368 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=1.000 | high |
| 6109 | 6107 | 10.1007/s11276-017-1493-2 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=0.972 | high |
| 6189 | 6153 | 10.1155/2022/6354543 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=1.000 | high |
| 7572 | 6645 | 10.1109/jstars.2026.3683462 | CONFIRMED_DUPLICATE | ded_conf=high, seq_sim=0.973 | high |
| 4950,4997,5323,5332,5359,5387,5426,5428,5463,5494,5805,6006,6015,6137,6150,6185,6188,6689(→review, low sim),6695,7630,3868,3984,4589,4710,4717,4718,4770,4807,4854,4868,4912 | (see JSON) | — | LIKELY_DUPLICATE (30 total) | seq_sim 0.56–0.97 / jaccard 0.42–0.89, compatible year, same-source | review |
| 5006,5059,5298,5379,5513,5565,5706,5811,5860,5867,5875,6689,7620,4817,4913 | (see JSON) | — | UNCLEAR_MANUAL_REVIEW (15 total) | conflicting/missing year or low similarity despite author overlap | review |

(The condensed rows collapse for readability — every individual case's exact numbers are in the JSON; nothing was omitted from the investigation, only from this table's prose.)

## 3. Confirmed Duplicate Groups

10 independent 2-paper groups (no transitive chains — no PaperID appears in more than one CONFIRMED pair):

1. **{5289, 5392}** — "Formal verification and model-based testing..." / "Adopting formal verification and model-based testing..." (2020, same author)
2. **{5329, 5434}** — "Towards optimizing the placement of security testing components..." / "Optimizing the placement..." (2019/2020)
3. **{5207, 5481}** — "Tuberculosis disease diagnosis..." / "Research Article Tuberculosis Disease Diagnosis..." (2022)
4. **{5232, 5482}** — "Optimal deep learning model for olive disease diagnosis..." / "Research Article Optimal Deep Learning Model..." (2022)
5. **{5548, 5549}** — "Research Article Optimization of Students' Performance Prediction..." / "Optimization of students' performance prediction..." (2022)
6. **{6086, 6088}** — "Research Article Prediction Approaches for Smart Cultivation..." / "Prediction approaches for smart cultivation..." (2021) — the exact pair `dedup_papers.py`'s own docstring cites as its motivating case.
7. **{3875, 6091}** — "Investigating brute force attack patterns in IoT network" (Scopus) / "Research Article Investigating Brute Force Attack Patterns..." (Scholar) (2019)
8. **{6107, 6109}** — "Path Planning Models for Mobile Anchor-Assisted Localization..." / "New path planning model for mobile anchor-assisted localization..." (2017/2018)
9. **{6153, 6189}** — "Semantic Sentiment Classification for COVID‑19 Tweets..." / "Research Article Semantic Sentiment Classification for COVID-19 Tweets..." (2022)
10. **{6645, 7572}** — "LiDAR, GNSS, and IMU Sensor Fine Alignment..." / "LiDAR, GNSS and IMU Sensor Alignment..." (2025/2026, likely preprint→published drift)

Manually reviewed for over-classification risk (§5): none of the 10 show a red flag — no case relies on the 1-year year-tolerance *and* weak title similarity simultaneously; the DOI-asymmetry + near-identical title + shared author combination holds for all 10.

## 4. Unclear Cases

The 15 `UNCLEAR_MANUAL_REVIEW` cases split into two real sub-patterns, not one generic bucket:

- **Missing year on one side removes the year-compatibility signal entirely** (`#5006, #5059, #5513, #5565, #5706, #5867`): the DOI-less side's `PubYear` is `NULL` in `ResearchPaper`, so `_years_compatible()` trivially passes (can't contradict what isn't there) but provides no positive evidence either — these need a human to check the actual paper, not more automated signal.
- **Low/moderate similarity despite shared authorship, often because the "title" is itself corrupted** (`#5298, #5379, #5875, #6689, #7620, #4913`): several of these (`#5298`, `#5379`) are cases where the stored title is literally an author-citation string, not a real title — see §6. This isn't ambiguity about paper identity; it's that the similarity signal itself is measuring the wrong thing. `#6689` (adenoids/tonsils vs. inflammatory-bowel-disease) and `#7620` (Spontaneous Networked Organization) look like genuinely weaker candidate matches that happened to still pass through to the duplicate-DOI gate — plausible but not confirmable without reading the actual papers.
- **Year gap >1** (`#5811` gap_2, `#5860` gap_3, `#4817` gap_2): `dedup_papers.py`'s own `_years_compatible()` already treats gap>1 as incompatible for its `high` tier, correctly routing these to review; a >1-year gap combined with real title differences (not just formatting) is exactly the "evolutionary papers" pattern the tool's design docstring warns about.

## 5. dedup_papers.py Comparison

- **Cases it catches correctly**: for all 10 cases I marked `CONFIRMED_DUPLICATE`, `dedup_papers.py`'s real `pair_confidence()` (called directly, unmodified) independently returns `high`. **8 of these 10 were also independently found by running the tool's actual `--dry-run` mode against the full 2031-paper database** (not just a simulated pairwise call) — see the fresh merge plan at `data/dedup_audit/merge_plan_20260821_120013.json` (24 real duplicate groups found DB-wide, 13 high/11 review).
- **Cases it misses (real, evidence-based limitation)**: 2 of the 10 CONFIRMED pairs — **{5289,5392}** and **{6107,6109}** — do NOT appear in the full-corpus `--dry-run` output, even though direct pairwise `pair_confidence()` returns `high` for both. Root cause traced precisely: `dedup_papers.py`'s `block_key()` groups candidates by their first 3 significant (non-stopword) words before ever calling `pair_confidence()`. "Formal verification model..." vs "Adopting formal verification..." land in different blocks (`adopting` vs `formal`); "New path planning..." vs "Path planning models..." likewise (`new` vs `path`). This is the exact same class of bug the tool's own docstring says it was built to fix for the boilerplate-prefix case (`"Research Article X"` vs `"X"`) — but `block_key()` only strips a fixed, known boilerplate-phrase list (`_BOILERPLATE_PREFIXES`), not arbitrary leading words like "New" or "Adopting" or "Towards". **No code change is proposed here** — flagged as a factual finding only, per scope.
- **Cases where it would over-classify**: none identified among the 55 investigated. `dedup_papers.py`'s DOI-asymmetry requirement (`bool(k["doi"]) == bool(l["doi"]) → "review"`) is a real, structural guard against merging two independently-DOI'd papers, and it held correctly across every case reviewed.
- **Scope note**: the fresh full-corpus run found **24 real duplicate groups** total DB-wide, of which only **8 overlap with our 55**-case population. The other **16 groups are entirely invisible to the DOI-pipeline's duplicate-gate signal** — because that gate only fires when external retrieval (Crossref/OpenAlex) happens to return the sibling paper's own already-known DOI as the top candidate, which is indirect and retrieval-dependent, whereas `dedup_papers.py` works directly off internal title/author/DOI data. Notably, **group 14 in that fresh run is `{5645, 7618}`** — PaperID 7618 is the exact case that originally motivated this entire DOI-pipeline hardening project (the forensic investigation that found `paper_by_title()`'s 0.72 threshold could attach a wrong DOI). `dedup_papers.py` independently flags it as a likely duplicate of #5645 (`high` confidence), a fact the DOI-pipeline work never surfaced on its own.

## 6. Data Quality Findings (separate from DOI matching)

Recurring ingestion-artifact patterns found across the 55 cases, distinct from the earlier Phase 3E/3F findings:

1. **"Research Article"/boilerplate-prefix duplication** — the single most common pattern (10+ of 55 pairs), already known to and handled by `dedup_papers.py`'s existing `_BOILERPLATE_PREFIXES` stripping.
2. **Author-citation string stored as Title, not the real title** — a distinct, newly-quantified pattern within this 55-case subset: `#5379` (title is literally `"Noora Fetais, and Kamel Barkaoui"`, no title content at all), `#5298` (title begins with a full author list glued onto the real title), `#5565` (`"'ARBI, choki ben Amar (2010),"..."`). At least 3 separate instances — same underlying category as the `#5875`/`#5950` cases flagged live during Phase 3F Batch C, now confirmed as a real recurring pattern, not a one-off.
3. **Trailing bibliographic citation appended to Title** — one side's title has journal name + volume + page range + year glued onto the end (`#5359`: "...Computers 2022, 11, 121"; `#6006`: "...Computers 2022, 11, 123"; `#4770`: "...CMC-Computers, Materials & Continua, 70(2), 2022, 3189–3204"; `#4868`, `#5426`, `#5428`, `#6185`, `#4589` show the same shape). Consistently on the *other* side of a DOI-asymmetric pair from a shorter, cleaner title — suggests one specific ingestion path (appears correlated with `Source='Scholar'`) concatenates a full citation string into the title field for some records.
4. **Character-encoding artifacts**: `#4854`'s title contains `"363â€“385"` — a mis-decoded en-dash (UTF-8 bytes read as Latin-1/cp1252), a distinct data-quality issue from title contamination, worth its own note.
5. **Truncated/ellipsis-terminated titles**: several titles end in `"…"` (a scrape-time truncation marker stored raw, e.g. `#5811`, `#4913`), suggesting a page-scraping step cut off at a fixed character count without capturing the full title.

None of these were fixed. None require DOI-pipeline changes — they are `ResearchPaper.Title`-column ingestion issues, orthogonal to `doi_pipeline`/`find_missing_dois.py`.

## 7. Safety Conclusion

- **DB data modified**: NO. Every query this investigation ran was `SELECT`-only (verified: no `UPDATE`/`INSERT`/`DELETE` statement exists anywhere in the investigation scripts).
- **Network calls**: 0. No Crossref/OpenAlex/HTTP call was made at any point in Phase 4A.
- **DOI assigned**: 0. No `ResearchPaper.DOI` value was written.
- **Records merged/deleted**: 0. `dedup_papers.py` was only run in its own documented `--dry-run` mode, which is DB-read-only by design and only writes a **local plan file** (`data/dedup_audit/merge_plan_20260821_120013.json`/`.csv`) for human review — its `--apply` path was never invoked, and no `--plan` argument was ever passed to it.
- **Files created this phase**: `backend/reports/phase4a_duplicate_investigation.md` (this file), `backend/reports/phase4a_raw_analysis.json` (full 55-row data), `data/dedup_audit/merge_plan_20260821_120013.json`/`.csv` (dedup_papers.py's own standard dry-run output — not merged, not applied).
- **Files modified**: NONE. `backend/reports/phase3f_duplicate_classification.md` was read only, never rewritten.

## Final Decision

**B) DUPLICATE PROBLEM IS REAL BUT REQUIRES A SEPARATE DEDUP PHASE**

Evidence: 10/55 cases are CONFIRMED_DUPLICATE by both an independent multi-signal analysis and `dedup_papers.py`'s own real, unmodified classification logic (8 of which the tool already surfaces automatically end-to-end via its own `--dry-run`; the other 2 are hidden from it only by a blocking-key limitation, not a scoring disagreement). A further 30/55 are LIKELY_DUPLICATE with real but incomplete corroboration. This is a genuine, non-trivial pattern (18% of the 360-paper DOI=NULL backlog touches a duplicate-DOI hard-gate case) — not isolated, and not something to leave unaddressed indefinitely — but it is completely independent of `doi_pipeline`'s correctness (which remains 0/360 AUTO_MATCH, fully validated) and requires its own reviewed, `--dry-run`-first, human-approved dedup phase using the existing `dedup_papers.py` tool (already proven safe: two-stage plan/apply, snapshot + AuditLog before any merge, `[HIGH]`-only auto-merge with `[REVIEW]` always requiring a human). No implementation proposed here, per scope.

---

Files created: `backend/reports/phase4a_duplicate_investigation.md`, `backend/reports/phase4a_raw_analysis.json`, `data/dedup_audit/merge_plan_20260821_120013.json`, `data/dedup_audit/merge_plan_20260821_120013.csv`
Files modified: NONE
DB writes: 0
Network calls: 0
Records merged/deleted: 0
