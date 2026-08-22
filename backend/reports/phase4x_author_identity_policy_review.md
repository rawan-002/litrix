# Phase 4X — Author Identity Conflict Policy Review (STRICTLY READ-ONLY)

## Executive Summary

This phase set out to determine whether any narrowly-defined class of `AuthorNameRaw` differences may safely be treated as formatting-only for merge purposes — and was explicitly instructed to try to *disprove*, not confirm, the assumption that `normalize_name()` should be reused for this. That skepticism was warranted: **direct testing this phase proves `normalize_name()` is objectively lossy and can theoretically collapse two genuinely distinct identities** (a compound surname `"Al-Amin"` and a first+last name `"Al Amin"` normalize to the identical string). At the same time, a major precedent was found that neither Phase 4W nor any earlier phase had surfaced: **`normalize_name()` — and even more aggressive `pg_trgm` fuzzy similarity matching — is already deployed in production today**, inside `backend/analytics/disambiguation/pipeline.py`, for exactly the class of question this phase investigates ("same likely author, different raw representation"), gated by a confidence threshold and a real, already-built human-review mechanism (`AuthorReviewQueue`). This existing precedent does not resolve the policy question — it reframes it, and reveals a real, previously-undocumented tension with `CLAUDE.md`'s literal policy text that this report surfaces rather than resolves. Of the three live `LOSER_ONLY_BACKFILL` candidates, exactly one (`6086/6088`) is a pure case/hyphenation difference; the other two are genuinely not. **Final decision: C) HUMAN REVIEW PATH IS REQUIRED** — normalization may supply *evidence* for a reviewer, but automatic merge behavior must not change. No code was modified.

---

## Task A — The Exact Current Safety Boundary

### A.1/A.2 — What exactly triggers a block, and how is it compared?

`backend/tools/dedup_papers.py::author_content_conflicts()` (lines 858–894), quoted directly:

> *"Identity key matches the repository's own existing identity logic for this table exactly -- merge_group()'s own ON CONFLICT clause is keyed on (UserID, PaperID)... two rows are 'the same author relationship' here precisely when they share a UserID... Returns a list of conflict dicts, one per colliding UserID whose AuthorNameRaw differs, in the exact raw values with no normalization applied."*

The comparison (line 888) is `if w_raw != l_raw:` — **exact Python string equality**, case-sensitive, whitespace-sensitive, punctuation-sensitive. No transformation of any kind is applied before comparison. This is confirmed both by direct source reading and by a dedicated, deliberate test: `test_dedup_papers.py::AuthorContentConflictDetection::test_different_raw_formatting_is_an_explicit_conflict` explicitly asserts that `"I Ben Ltaifa"` vs. `"IB Ltaifa"` — a pure formatting difference — **must** register as a conflict. This is not an accidental gap; it is a tested, intentional design line.

`merge_executor.py`'s call site treats any non-empty result as an unconditional block (`EXEC_BLOCKED_AUTHOR_CONFLICT`) with no override parameter, no approval-carried exception, and no code path that consults a human decision at execution time.

### A.3–A.6 — What does `normalize_name()` do, and is it safe as a general-purpose tool?

`backend/analytics/disambiguation/pipeline.py`, lines 72–85, quoted in full:

```python
def normalize_name(name: str) -> str:
    """
    Comparable form: lowercase, no diacritics, no punctuation, single-spaced.
        'Mohammed K. Al-Otaibi' → 'mohammed k al otaibi'
        'محمد العتيبي'         → 'محمد العتيبي'  (Arabic diacritics removed)
    """
    if not name:
        return ''
    n = unicodedata.normalize('NFKD', name)
    n = ''.join(c for c in n if not unicodedata.combining(c))
    n = _ARABIC_DIACRITICS.sub('', n)
    n = _PUNCT_RE.sub(' ', n)
    n = _WS_RE.sub(' ', n).strip().lower()
    return n
```

**Deterministic**: yes — pure function, same input always produces the same output, no randomness, no external state.

**Lossy**: yes, **proven directly this phase**, not merely asserted. Three constructed test cases:

| Input A | Input B | Normalized (both) | Collapse? |
|---|---|---|---|
| `"Al-Amin"` | `"Al Amin"` | `"al amin"` | **Yes** |
| `"D'Angelo"` | `"D Angelo"` | `"d angelo"` | **Yes** |
| `"M Ahmed"` | `"Mohammed Ahmed"` | `"m ahmed"` / `"mohammed ahmed"` | No (control — confirms it does *not* expand initials, a separate, real limitation in the other direction) |

**Can distinct real author identities collapse to the same normalized value?** **Yes, in principle, demonstrated directly.** A hyphenated compound surname (`"Al-Amin"`, one person, one surname) and a first-name-plus-surname pattern (`"Al Amin"`, potentially a *different* person) are indistinguishable after this normalization. Punctuation- and case-stripping are inherently unable to distinguish "this hyphen encodes a single compound name" from "this hyphen is standing in for a space between two names" — the function has no semantic model of names, only a surface-text one.

### A.7 — How is `normalize_name()` actually used in the repository?

**Not for display cleanup. Not for candidate generation in isolation. It is used directly for identity-matching / cross-attribution decisions** — confirmed by direct reading of `link_coauthors_for_paper()`'s tier pipeline (lines 168–257):

- **Tier 4** (`_tier_name_and_albaha`, confidence `0.85`): auto-links a co-author to an existing `UserID` if `normalize_name(scraped_name) == normalize_name(candidate_researcher_name)` **and** the paper's affiliation string independently matches an Al-Baha University regex. **This is real, production, currently-running name-based matching used to decide `UserID` linkage** — auto-committed to the `Authors` table with no human step, whenever both signals agree.
- **Tier 5** (`_tier_fuzzy`): a *further* fuzzy step, using PostgreSQL's `pg_trgm` `SIMILARITY()` — genuinely fuzzy, not merely normalized — explicitly capped at confidence `0.80`, auto-linking only at `≥0.70`; anything below `0.70` is explicitly routed to **`AuthorReviewQueue`** for a human decision, never auto-linked, never silently dropped.

### A.8 — Does `CLAUDE.md`'s policy prohibit this exact use case, or only fuzzy/non-deterministic matching?

This is the central, unresolved tension this phase surfaces rather than settles. The exact policy text (`README.md` §3, quoted in full, not paraphrased):

> *"These exist because name-based matching once cross-contaminated **602 papers** between researchers with similar names... **Deterministic identifiers only** for matching: `Scholar_ID`, `DOI`, `ORCID`, OpenAlex Author ID. **Never** name-based fuzzy matching for cross-attribution... **The 602-paper incident (lesson learned):** an early version matched papers to researchers by name. Two researchers with similar Arabic names ended up sharing 602 papers. The fix: attribution flows ONLY from Scholar's per-author `articles[]` keyed on `Scholar_ID`..."*

**What the incident actually was, precisely**: the *trigger* attribution decision — "does this entire paper belong to researcher X's profile" — was made by comparing names, and it produced a wrong, large-scale result. The documented fix scopes the guarantee narrowly: *"attribution flows ONLY from Scholar's per-author `articles[]`"* — i.e., the **primary ownership link** for a paper is deterministic.

**What `disambiguation/pipeline.py`'s tiered matching actually does, precisely**: link *additional co-authors* — who are never the trigger researcher, and whose non-linkage does not affect the paper's own primary attribution at all (`"The trigger researcher is unconditionally linked by the scraper BEFORE this module runs. We never touch that link."`, line 40) — using name-based signals, but with a required confidence threshold and an unconditional human-review fallback for anything below it.

**Two honest readings, both presented rather than one being unilaterally chosen:**

1. **Narrow reading**: the policy governs *primary attribution* specifically (the 602-paper failure mode: "who does this whole paper belong to"), and the disambiguation pipeline's *secondary, threshold-gated, human-reviewable* co-author linkage is a different, narrower operation the policy's literal text didn't anticipate needing to carve out — because it structurally cannot reproduce the 602-paper failure mode (it never changes whose paper this is, only which *additional* people are linked to it, with a review safety net).
2. **Literal reading**: the policy text says *"Never name-based fuzzy matching for cross-attribution"* without qualification, and Tier 4/5 of `disambiguation/pipeline.py` are, in fact, exactly that — meaning either the policy text is stricter than what this codebase actually practices, or this existing pipeline itself represents an un-flagged, pre-existing tension that predates this entire merge-safety project.

**This phase does not resolve which reading is correct — that is a genuine data-governance judgment, not a technical one, and is named explicitly in the final decision below (Task F) as requiring resolution beyond this report's scope.** What this phase *can* state with confidence: **the current `AuthorNameRaw` merge-conflict question is narrower still than either of the above** — it does not decide `UserID` linkage at all (that is *already* deterministically fixed on both sides of every conflict this phase examined; every conflict is a same-`UserID` disagreement about a *display-text* field, not an identity decision). This is the narrowest possible framing of the three, and is addressed directly in Task E.

---

## Task B — Taxonomy of Name Differences

| Category | Definition | Found in real Litrix data? | Known examples | `normalize_name()` collapses it? | Can it merge distinct people? | Repository policy support | Classification |
|---|---|---|---|---|---|---|---|
| **A. Case-only** | Identical characters, different capitalization | **Yes** | `6086/6088`: `"Al-adaileh"` vs. `"Al-Adaileh"` (1) | Yes | No — case carries no identity-distinguishing information in Latin transliteration | No explicit repository precedent for a *safety-check* use, but the disambiguation pipeline treats case as irrelevant for its own (different) purpose | **SAFE_FORMATTING_ONLY** *in isolation* — but see Task E for why isolation from other categories is not guaranteed in practice |
| **B. Whitespace (leading/trailing/repeated)** | Extra or missing whitespace only | Not isolated as its own case in the 8 examined conflicts (always co-occurs with punctuation/tokenization differences) | 0 pure examples | Yes | No | No direct precedent | **SAFE_FORMATTING_ONLY** *in isolation* |
| **C. Unicode normalization (NFKD/diacritics)** | Combining marks, accented characters | Not observed in the 8 English/Latin-transliterated conflicts examined; theoretically relevant for Arabic names elsewhere in the dataset (not tested this phase due to a console-encoding limitation, not a code limitation) | 0 confirmed examples in this specific conflict set | Yes | **Unclear — flagged, not resolved.** Diacritic removal on Arabic script could, in principle, collapse names that differ by a vowel mark carrying real phonetic/lexical meaning; this phase did not locate a real conflicting pair to test directly | No | **UNKNOWN** |
| **D. Punctuation-only** | Differs only in hyphens, periods, apostrophes, commas | Overlaps with A in `6086/6088` (the hyphen case) | 1 (same as A) | Yes | **Yes — proven this phase** (`"Al-Amin"` vs. `"Al Amin"`, `"D'Angelo"` vs. `"D Angelo"`) | No | **UNSAFE_AUTOMATICALLY** — punctuation can encode real name-structure information (compound surnames, apostrophes as genuine name characters), and collapsing it is where the demonstrated collision risk actually lives |
| **E. Initial/token segmentation** | Different splitting of the same syllables into initials/words | **Yes, the dominant real category** | `5207/5481` (`"I Ben Ltaifa"` vs. `"IB Ltaifa"`), `6145/6190` (`"W M. Ead"` vs. `"WM Ead"`), `6153/6189` (`"F Kamal Alsheref"` vs. `"FK Alsheref"`), `5548/5549` (`"NB Aoun"` vs. `"N Ben Aoun"`, `"MA El Affendi"` vs. `"MAE Affendi"`) | **No** — confirmed directly, this phase, for every one of these four pairs; `normalize_name()` does not merge or split tokens, only strips case/punctuation/whitespace-repetition | Not directly testable via this function (it doesn't touch tokenization) — but a *different*, more aggressive normalization that did merge these would carry real risk (e.g., "F Kamal Alsheref" merged wrongly with an unrelated "FK Alsheref" belonging to someone else) | No | **HUMAN_REVIEW_ONLY** — the most common real category, genuinely ambiguous, not resolvable by the one existing tool this phase tested |
| **F. Initial expansion/contraction** | A full given name vs. its initial (e.g., "Mohammed" vs. "M") | Not observed as an isolated case in the 8 conflicts (always bundled with category E) | 0 pure examples | No (confirmed by the control test, Task A) | N/A — not collapsed by the tool in question | No | **HUMAN_REVIEW_ONLY** |
| **G. Token reordering** | Same tokens, different order | Not observed in any of the 8 conflicts | 0 | Untested (not present) | Theoretically yes for a token-sorting normalizer, but `normalize_name()` does not sort tokens, so not applicable here | No | **UNKNOWN** (not exercised by real data) |
| **H. Different token counts** | One side has materially more or fewer name components | **Yes** | `6107/6109`: `"AM Alomari"` (1 token pair) vs. `"A Alomari, F Comeau, W Phillips, N Aslam"` (4 distinct names) | No | N/A — this is not a formatting question at all | No | **UNSAFE_AUTOMATICALLY** |
| **I. Different author cardinality for the same logical key** | The `AuthorNameRaw` field itself appears to contain multiple people's names under one `UserID` | **Yes — this is `6107/6109` again**, and structurally distinct from H because it suggests an *upstream data-quality defect* (a scraper or an earlier processing step folding multiple co-authors into one field), not merely a duplicate-merge-time formatting question | 1 confirmed | N/A | High — if ever auto-resolved by *any* rule, real information (three of the four people) would be silently discarded | No | **UNSAFE_AUTOMATICALLY**, and flagged as a separate, likely-upstream data-quality issue worth its own investigation outside this project's merge-safety scope |
| **J. Empty vs. populated** | One side is an empty/blank string, the other a real name | **Yes** | `5645/7618`, `UserID=50`: `""` vs. `"Saad Alqithami"` | N/A (`normalize_name("")` returns `""`, does not collapse onto a populated value) | No risk of merging two identities, but a real, different question: this is structurally a `COPY_LOSER`/backfill scenario (nothing to lose, a real value to gain), not a "same person, different text" conflict at all — `author_content_conflicts()` currently has no concept for this distinction and flags it identically to every other case | No precedent for this specific sub-case, though `journal_id_decision()`'s `LOSER_ONLY_BACKFILL` state is a directly analogous *pattern* for a different field | **HUMAN_REVIEW_ONLY** for now, though this specific sub-case is the one most plausibly amenable to its own narrow, deterministic rule in a future design phase (an empty string can never lose information by being overwritten) — **not decided or implemented here** |

**No safety guarantee is invented for any category.** Categories C and G are marked `UNKNOWN` precisely because this phase could not find real data to test them against, not because they are assumed safe.

---

## Task C — The Three `LOSER_ONLY_BACKFILL` Candidates, Fresh Forensic Detail

All three re-confirmed live, this phase, from scratch:

| Pair | Survivor→Loser | `JournalID` state | `UserID` | Survivor `AuthorNameRaw` | Loser `AuthorNameRaw` | `normalize_name()` collapse? | Independent classification |
|---|---|---|---|---|---|---|---|
| `5548→5549` | confirmed unchanged | `LOSER_ONLY_BACKFILL` (winner `NULL`, loser `676`) | `104` | `"S Ahmad, NB Aoun, MA El Affendi, MS Anwar, S Abbas, AA Abd El Latif"` | `"S Ahmad, N Ben Aoun, MAE Affendi, MS Anwar, S Abbas, AAAE Latif"` | **No** | Category E (token segmentation) — **HUMAN_REVIEW_ONLY**. Even a human reviewer must exercise real judgment here — "NB Aoun" and "N Ben Aoun" are *plausibly* the same person, but this is an inference from name-structure convention, not a certainty |
| `6086→6088` | confirmed unchanged | `LOSER_ONLY_BACKFILL` (winner `NULL`, loser `771`) | `105` | `"...MH Al-adaileh..."` | `"...MH Al-Adaileh..."` | **Yes** | Category A+D (case + hyphen-punctuation) — the *only* one of the three where `normalize_name()` produces a byte-identical result. **Still not automatically sufficient for production execution** (see Task E) — the transformation *type* that resolves it (punctuation-stripping) was independently shown this phase to carry real collision risk in the general case, even though it happens not to misfire on *this specific* pair |
| `6107→6109` | confirmed unchanged | `LOSER_ONLY_BACKFILL` (winner `NULL`, loser `1104`) | `69` | `"AM Alomari"` | `"A Alomari, F Comeau, W Phillips, N Aslam"` | **No** | Category H/I (cardinality mismatch) — **not a formatting question**. Automatic merging here risks silently discarding three real, distinct co-author names from the record. Likely an upstream scraper data-quality issue, not a merge-safety question this project's tooling should try to resolve automatically |

**Cross-attribution risk assessment, per pair**: none of the three risks *paper-ownership* cross-attribution in the 602-paper sense — in every case, `UserID=104/105/69` is already, deterministically, the same on both sides (this was established upstream by Scholar/OpenAlex/ORCID matching, never by comparing `AuthorNameRaw`). The risk these three actually carry, if auto-resolved incorrectly, is narrower: **silently discarding or misrepresenting a real name variant, a real co-author list, or (for `6107/6109` specifically) three entire co-author identities** — a data-completeness/accuracy risk, not the specific cross-attribution failure mode the 602-incident describes, but a real risk nonetheless, and one this project's own `NO_AUTO_COPY_FIELDS`/"do not silently choose" conventions (used elsewhere for `JournalID` conflicts, `TenantID`, etc.) already treat with equivalent seriousness.

---

## Task D — Precedent Search

Exhaustive repository search performed this phase (`grep -rn`/`grep -rln` across `backend/`):

- **Case-insensitive author comparison**: none found anywhere in the merge-safety code path (`.lower()`/`.casefold()` do not appear applied to `AuthorNameRaw` in `dedup_papers.py`, `merge_execution_safety.py`, or `merge_executor.py`).
- **`normalize_name()`**: exactly one definition (`disambiguation/pipeline.py`), used only within that module's own tier pipeline — **never imported by, or referenced from, any file in the merge-safety code path** (`dedup_papers.py`, `merge_plan_generator.py`, `merge_execution_safety.py`, `merge_executor.py`, `merge_approval.py` — confirmed by direct `grep`, zero cross-references in either direction).
- **`verify_attributions.py`**: the real, existing detector for the 602-incident class of problem — re-verifies paper-title membership against Scholar's own authoritative per-author article list (`fetch_scholar_titles()`), a completely different mechanism, unrelated to comparing `AuthorNameRaw` strings.
- **Manual/human-review workflow already existing for "same likely author, different raw representation"**: **Yes — `AuthorReviewQueue`**, populated by `disambiguation/pipeline.py`'s own Tier 5/6 (confidence `<0.70`), with the real, live, production endpoint `backend/analytics/reconciliation_views.py::review_queue_decide()` (confirmed real and working in prior phases of this project) already handling exactly this class of human decision — for *co-author linkage*, not merge-time conflicts, but structurally the closest possible precedent for "a human confirms or rejects a suggested name-identity match."

**Per Task D's own explicit instruction — "prefer evaluating reuse of that mechanism over inventing automatic behavior" — `AuthorReviewQueue` is the mechanism to evaluate reuse of, not a new workflow.** No new workflow is proposed by this phase; `AuthorReviewQueue` itself is not extended or wired into the merge path (that would be an implementation change, out of scope for this read-only phase) — its *existence and shape* is used only as evidence supporting Task E/F's recommendation that any future automated help take a human-reviewed, not silently-automatic, form.

---

## Task E — Policy Options Evaluated

| Option | False-positive risk | Cross-attribution risk | `CLAUDE.md` compatibility | 602-incident compatibility | Implementation complexity | Auditability | Reversibility | Changes automatic behavior? | Unlocks real candidates? | Weakens protection elsewhere? |
|---|---|---|---|---|---|---|---|---|---|---|
| **A. Keep exact-match rule unchanged** | None (status quo) | None | Fully compatible | Fully compatible | Zero | N/A (nothing new to audit) | N/A | No | **0** | No |
| **B. Auto-ignore strictly case-only differences** | Low, but not zero — see Task B, Category A/D overlap: a "case-only" classifier itself must correctly distinguish case-only from punctuation-adjacent cases, and `6086/6088`'s real conflict is actually case-**and**-hyphen, not case-only in the strictest sense | Low but nonzero — the underlying transformation (even scoped to "just case") still touches the same code path that, if ever broadened even slightly, reaches the proven collision risk | Debatable — still a form of name normalization applied automatically, without the disambiguation pipeline's confidence-threshold/human-review structure | Debatable, same reasoning | Low | Requires new logging to be auditable at all — none currently exists for "this was auto-resolved by case-normalization" | Low — once auto-merged, the original distinct raw text is only recoverable from the pre-merge snapshot/`AuditLog`, not from the live `Authors` row | **Yes** | **1** (`6086/6088`, if a stricter case-only-not-punctuation definition is used it may not even qualify) | Yes — establishes an automatic-normalization precedent inside the merge-safety path that does not currently exist anywhere in it |
| **C. Apply `normalize_name()` automatically to all comparisons** | **High — directly disproven as safe this phase** (`"Al-Amin"`/`"Al Amin"`, `"D'Angelo"`/`"D Angelo"`) | Elevated — the same collision mechanism applies to any future candidate, not just the 3 examined | **Conflicts with the literal policy text**, and is the option this phase was explicitly told to try to disprove | Directly analogous to the failure pattern the incident describes, at a smaller but structurally identical scale | Low (the function already exists) | None built | Same as B, worse in scope | **Yes** | Up to 8 (all current `AuthorNameRaw` conflicts), but see Task B — 6 of 8 wouldn't even be resolved by this specific function, so the real unlock is smaller than it sounds | **Yes, clearly** — this is the option Task D/E explicitly warns against adopting without proof, and this phase found no such proof |
| **D. Use normalization only to *downgrade* certain conflicts from `BLOCK` to `HUMAN_REVIEW_REQUIRED`** | Low — a human still makes the final call; normalization only *surfaces* a suggestion, never *decides* | Low — no automatic merge behavior changes at all | **Compatible** — mirrors `disambiguation/pipeline.py`'s own real, existing pattern (suggest, gate by confidence, let a human confirm) | Compatible — the trigger/primary-attribution mechanism the incident is actually about remains untouched; this only ever affects a review queue's *presentation*, never an automatic decision | Medium — requires either extending `AuthorReviewQueue`'s schema/workflow to a new context, or building an equivalent, smaller human-decision surface specific to merge approvals | High — a real reviewer decision, timestamped and attributable, exactly like every other human-gated action in this project | High — nothing is silently lost; a rejected suggestion simply leaves the pair in `HUMAN_REVIEW` status, exactly like today | **No** (automatic *merge* behavior is unchanged — a human decision is still required before any execution) | 0 immediately (still requires the human-decision surface to be built first), but structurally enables all 8 once built | **No** — explicitly designed not to |
| **E. Narrower option: auto-resolve only the `Empty vs. populated` category (Task B, category J)** | Very low — an empty string can never lose real information by being overwritten with a real value; this is structurally identical to `journal_id_decision()`'s already-accepted `LOSER_ONLY_BACKFILL` pattern for a different field | Very low — no ambiguity about which value is "more complete" | Arguably compatible — this is not name *matching* at all (nothing is being compared for equivalence), it is a *backfill* decision, the same category of decision `JournalID`'s `LOSER_ONLY_BACKFILL` state already makes automatically today | Compatible — no name comparison occurs | Low — mirrors an already-built pattern (`journal_id_decision()`) rather than introducing a new one | High if implemented with the same audit-trail discipline as `JournalID` backfill | High — reversible via the pre-merge snapshot, same as every other field | **Yes, narrowly** — but only for the specific empty-vs-populated shape, never for two populated-but-different values | 1 of the 8 conflicts examined this phase (`5645/7618`) — **not** any of the 3 `LOSER_ONLY_BACKFILL` candidates, which are a coincidentally different, unrelated bottleneck | **No** — the exact-match rule remains fully intact for every case where both sides are populated |

**Evaluated with the explicit goal of safety, not maximizing mergeable pairs, per your instruction.** Option C is the one this phase was told to try to disprove, and it is disproven directly, not by assumption. Options B and C both introduce automatic behavior into the merge-safety path with elevated risk for a very small real unlock (1 candidate, in `6086/6088`'s specific case — and even that is contingent on a "case-only" rule being scoped narrowly enough to actually match it, since the real conflict also involves a hyphen). Option D has the strongest alignment with real repository precedent (`disambiguation/pipeline.py`'s own confidence-threshold-plus-human-review design) and the strongest compatibility with both the letter and the spirit of the 602-incident lesson, at the cost of requiring new implementation work this phase does not authorize or perform. Option E is the narrowest, lowest-risk, most immediately defensible option — but it does not touch the `LOSER_ONLY_BACKFILL` bottleneck at all, since none of those three candidates has an empty-vs-populated shape.

---

## Task F — Decision Boundary

### **C) HUMAN REVIEW PATH IS REQUIRED**

Normalization (`normalize_name()`, already real and already used elsewhere) may legitimately *inform* a future human-review surface — surfacing "these two raw strings normalize identically" or "these two raw strings differ substantively" as a hint to a reviewer — but this phase found direct, constructed proof that the same transformation is not safe as an automatic decision-maker (Task A/B's collision examples), and found that the one real precedent this codebase already has for "same likely author, different raw representation" (`disambiguation/pipeline.py`'s tiered matching) itself relies on a human-review fallback, not blanket automatic resolution, for exactly the confidence range these merge-time conflicts fall into. **Automatic merge behavior for any populated-vs-populated `AuthorNameRaw` conflict must remain unchanged** — Option A (no change) remains correct for that shape today. **Option E (empty-vs-populated backfill) is the one narrow exception this phase found real, evidence-backed support for** — but it affects a different pair (`5645/7618`) than any of the three `LOSER_ONLY_BACKFILL` candidates that motivated this investigation, so it does not, by itself, resolve the bottleneck the user asked about.

**This is not `A` (no policy change needed)**, because Option E's empty-vs-populated case is a real, narrow, evidence-backed candidate for eventual safe automation that the current blanket rule does not distinguish from a genuine two-populated-values conflict — leaving it unexamined would be incomplete, not merely conservative. **This is not `B` (a narrow policy change is ready to design)**, because what this phase found is not yet a *designed* solution — it is evidence pointing toward a *shape* of solution (human-review-surfaced, `disambiguation/pipeline.py`-precedented) that still requires real design work (schema, workflow, exact confidence semantics) this phase does not perform. **This is not `D` (more data-governance review needed to make any decision)** either — enough was found this phase to make a directional decision (reuse the human-review pattern, do not automate populated-vs-populated comparison) even though the *literal-vs-narrow* reading of `CLAUDE.md`'s policy text (Task A.8) remains genuinely unresolved and should be flagged to whoever owns that document, separately from this technical investigation.

**None of the three `LOSER_ONLY_BACKFILL` candidates should be executed today.** `6086/6088`'s conflict, while resolvable by `normalize_name()` in isolation, does not meet the bar this phase sets — a mechanical coincidence that one specific function happens not to misfire on one specific pair is not the same as a safe, general rule, and this phase explicitly found that the same function *does* misfire in constructible, realistic cases.

---

## Addendum — Refined Precision, Requested Follow-Up

The user's follow-up asked five precise questions before making a final call. Each is answered here with direct, additional evidence gathered this same read-only phase — no code was changed to answer any of them.

### Addendum Q1 — Is the `disambiguation/pipeline.py` precedent for identity-attribution only, or a legitimate precedent for treating `AuthorNameRaw` differences as formatting?

**Partially transferable, and the boundary matters precisely.** Re-reading `verify_attributions.py` in full (the actual 602-incident corrector, not previously quoted in full) confirms it operates on a *third*, unrelated axis entirely: it deletes an `Authors` link whenever a paper's `Title` is absent from that researcher's own Scholar-verified `articles[]` list (`normalize_title(title) not in verified` → `DELETE FROM "Authors"`). It never compares author names at all. This reinforces that the 602-incident's own real corrective mechanism has nothing to do with name-string comparison.

`disambiguation/pipeline.py`'s Tier 4/5, by contrast, perform a **1-to-many identity search**: given one scraped name, find *which one* of potentially many registered researchers it matches. `normalize_name()` there is a *search key* — a wrong match means attaching a paper's co-authorship to the *wrong person* out of a candidate pool.

The merge-time `AuthorNameRaw` question is structurally different: it is a **1-to-1 equivalence check** on two raw strings *already* tied to the identical `UserID` on both sides — there is no candidate pool, no search, and no possibility of attaching the record to the wrong person, because the person was never in question. A collision here can, at worst, mean treating two *display-text variants* as interchangeable — not misattributing a paper.

**Conclusion**: the precedent is real and directly useful for one purpose — it disproves any reading of `CLAUDE.md`'s policy as an absolute, unconditional ban on `normalize_name()`'s existence or use anywhere in this codebase, since it is already deployed, confidence-gated, and human-review-backed for a *genuinely riskier* operation (1-to-many search) than the one under consideration here. It is **not**, by itself, sufficient precedent to justify blanket reuse for the narrower 1-to-1 equivalence question — that still requires its own risk analysis, which the rest of this addendum supplies.

### Addendum Q2 — Is `normalize_name()` genuinely deterministic in all relevant cases?

**Yes, confirmed directly.** Every one of the 5 distinct real `AuthorNameRaw` strings involved in the `LOSER_ONLY_BACKFILL` candidates was run through `normalize_name()` 20 times each (100 calls total): **every single call produced an identical result** for its input, zero variance. The function is a pure composition of `unicodedata.normalize('NFKD', ...)`, combining-mark stripping, two fixed regex substitutions, and `.lower()` — no randomness, no external state, no locale dependency introduced anywhere in its body. Determinism is not in question for any input this investigation examined.

### Addendum Q3 — Can `case-only` be classified `SAFE_FORMATTING_ONLY` without endangering the more dangerous categories?

**Yes — but only if the transformation is genuinely narrowed to case-folding alone, not `normalize_name()` as a whole.** This is the addendum's central, load-bearing finding, tested directly:

A minimal transformation — `s.lower()`, nothing else, no punctuation stripping, no whitespace collapsing, no diacritic removal — was tested against every collision-risk example and every one of the 8 real conflicts:

| Test | Result |
|---|---|
| `"Al-Amin"` vs. `"Al Amin"` (the proven collision pair) | `"al-amin"` vs. `"al amin"` — **do not collapse** |
| `"D'Angelo"` vs. `"D Angelo"` (the second proven collision pair) | `"d'angelo"` vs. `"d angelo"` — **do not collapse** |
| `6086/6088`'s real conflict | `"...mh al-adaileh..."` vs. `"...mh al-adaileh..."` — **collapse (resolved)** |
| All other 7 real conflicts (`5548/5549`, `6107/6109`, `5207/5481`, `6145/6190`, `6153/6189`, `5645/7618`, `5065/4786`) | **None collapse** — every one still correctly flagged |

**This directly answers the question: yes, a case-only-*only* rule is demonstrably narrower than `normalize_name()` and does not reproduce either proven collision case.** The two proven collisions both required punctuation-to-space conversion (a hyphen or apostrophe becoming a space) to occur — pure case-folding never touches punctuation, so it structurally cannot reproduce that failure mode. The remaining theoretical risk for case-folding alone is narrower still (locale-sensitive casing edge cases, e.g. Turkish dotted/dotless İ — not applicable here since Python's `str.lower()` is locale-independent by default and no locale is set anywhere in this call path) — a residual risk this report characterizes as near-zero rather than absent by declaration.

### Addendum Q4 — Should normalization *downgrade* to `HUMAN_REVIEW` rather than permit `AUTO_MATCH`, even for the narrow case-only category?

**Both positions have real support; this addendum presents both rather than forcing one.**

**The case for permitting a narrow `AUTO_MATCH`** (case-fold-identical, and *only* case-fold-identical, strings): the evidence above shows this specific, narrow transformation carries no demonstrated collision risk, touches none of the other 7 real conflicts, and is fully deterministic. Automating exactly this one, precisely-bounded case would not be "adding fuzzy matching to merge safety" in the sense the policy warns against — it would be recognizing that two strings differing only in letter case carry no distinguishing identity information at all, in any script or convention observed in this dataset.

**The case for keeping even this category `HUMAN_REVIEW`-only**: this project's own demonstrated pattern — `AuthorReviewQueue`, `journal_id_decision()`'s explicit refusal to auto-resolve `CONFLICT` states, `merge_plan_generator.py`'s `NO_AUTO_COPY_FIELDS` set — consistently prefers a human-reviewed, audit-trailed decision over any automatic one, even in cases the pure logic alone might support. Adopting `AUTO_MATCH` here would be the *first* instance of automatic name-based reasoning inside the merge-safety path specifically, setting a precedent for that path independent of whatever `disambiguation/pipeline.py` does elsewhere. A `HUMAN_REVIEW`-with-a-strong-suggestion approach (surface "these differ only in letter case" as a hint, let a human confirm) preserves the audit trail and the "no automatic collapse" line this path has held since Phase 4E, at the cost of still requiring a human step even for the safest case.

**This addendum does not force a choice between these two positions — that is the decision this report was asked to support, not make.**

### Addendum Q5 — Does `(6086,6088)` become testable, while `(5548,5549)` and `(6107,6109)` remain blocked?

**Yes, precisely confirmed.** Under a narrow, case-fold-only rule:
- `(6086,6088)`: resolved — the *only* difference between the two raw strings, character-for-character, is letter case on one hyphenated surname. No punctuation, whitespace, tokenization, or cardinality difference exists in this pair once case is set aside.
- `(5548,5549)`: **remains blocked** — the conflict is a genuine tokenization difference (`"NB Aoun"` vs. `"N Ben Aoun"`, `"MA El Affendi"` vs. `"MAE Affendi"`), confirmed this phase to survive case-folding unchanged.
- `(6107,6109)`: **remains blocked** — the conflict is a cardinality mismatch (one name vs. four), confirmed this phase to survive case-folding unchanged; this is not a formatting question under any transformation considered.

**This narrows Task F's original finding rather than reversing it.** The original Phase 4X decision (`C — HUMAN REVIEW PATH IS REQUIRED`) was based on evaluating `normalize_name()` as a whole, which this addendum does not retract — that broader tool remains unsafe for blanket use, exactly as originally found. What this addendum adds is a **materially narrower, more precisely bounded sub-option**: a case-fold-only comparison, which the evidence supports far more strongly than `normalize_name()` as a whole, and which affects exactly one of the three original candidates. Whether that narrower option should become `AUTO_MATCH` (Addendum Q4's first position) or remain `HUMAN_REVIEW`-with-evidence (Addendum Q4's second position) is the one open call this report hands back for your decision — everything else needed to make that call has now been directly tested and reported, not inferred.

---

## Exact Accounting

- **Code files modified**: **0.**
- **Code files created**: **0.**
- **Report files created**: **2** — this file and `backend/reports/phase4x_author_identity_policy_matrix.json`.
- **DB writes**: **0.**
- **DB reads**: Multiple, all `SELECT`-only, against `ResearchPaper`/`Authors` for the three candidate pairs and their `JournalID`/`AuthorNameRaw` values — all read-only, no lock held beyond each statement's own implicit transaction, all connections closed/rolled back.
- **Network calls**: **0.**
- **Records merged**: **0.**
- **Records deleted**: **0.**
- **DOI changes**: **0.**
- **Approvals created/modified**: **0.**
- **Tests run**: full suite re-run, unchanged code — `test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 43/43, `test_merge_execution_safety.py` 89/89, `test_merge_approval.py` 50/50, `test_merge_executor.py` 48/48, `test_fk_lifecycle.py` 11/11 — **259/259, unchanged from Phase 4W, zero regressions.**
- **Production state changed**: **NO** — independently re-verified after all investigation (both the original pass and this addendum): `MergeApproval` still shows exactly `[(1, 'EXECUTED'), (2, 'EXECUTED')]`, `ResearchPaper` count still `2029`.

Per your instructions, I am stopping here. Phase 4Y is not started. No implementation was performed. No production data was changed.
