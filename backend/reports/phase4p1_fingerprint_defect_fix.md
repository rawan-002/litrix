# Phase 4P.1 — Deterministic Fingerprint Defect Investigation & Fix

## Result Summary

**The defect is fixed, and `ApprovalID=1` remains valid without any database modification.** After the fix, both real database-connection mechanisms this repository uses — a bare `psycopg2` connection (`litrix_db.db()`) and Django's own `connection.cursor()` — now compute the **identical** fingerprint for the canary pair's live, unchanged data: `2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb`. This is, byte-for-byte, the exact value already stored on `ApprovalID=1` since Phase 4P. Re-running the full executor preflight sequence (read-only, rolled back) now returns `PREFLIGHT_OK` where it previously returned `STALE_FINGERPRINT`.

**Final decision: A) DEFECT FIXED — READY TO RE-AUDIT FIRST CANARY EXECUTION** (§13).

---

## Task A — Reproduction and Bounding

### A.1/A.2 — Reproducing the mismatch, both real paths

```
Django connection fingerprint:       b74d4675e3069fe979720db3bcad7e2c20e1dfd92dc83656d38b1a6d71dbf4f9
Raw psycopg2 connection fingerprint: 2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb
Reference (ApprovalID=1):            2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb
Django matches reference: False   |   Raw matches reference: True
```

A full field-by-field type+value diff across all 27 `FINGERPRINT_RP_FIELDS`, both winner and loser, both `Authors` rows, and both citation counts found **exactly two differing fields, both on the winner row**:

```
WINNER CitationsByYear:     django_type=str  raw_type=dict  (equal after str-cast: False)
WINNER VerificationDetails: django_type=str  raw_type=dict  (equal after str-cast: False)
```

Every other field — every text, integer, boolean, and null-valued column, plus both `Authors` rows in full — matched identically in both type and value between the two connections.

### A.3 — Proving the underlying values are semantically identical

```
CitationsByYear:     json.loads(django_str) == raw_dict                     : True
                      canonical(django, sort_keys) == canonical(raw, sort_keys): True
VerificationDetails: json.loads(django_str) == raw_dict                     : True
                      canonical(django, sort_keys) == canonical(raw, sort_keys): True
```

**This is not data drift.** The two connections are reading the exact same row, at the exact same moment, and disagree only in how they represent one Postgres data type in Python — proven directly, not inferred.

### A.4 — The exact code path

```
database fetch (merge_plan_generator.fetch_paper_row(), a plain SELECT)
  → value representation: str (Django connection.cursor(), raw SQL, no ORM decoding)
                        vs. dict (bare psycopg2.connect(), auto-decoded by its default jsonb adapter)
  → merge_execution_safety._normalize_scalar()  [THE DEFECT WAS HERE]
  → canonical payload dict, per-field
  → json.dumps(payload, sort_keys=True, ...)      [compute_plan_fingerprint()]
  → hashlib.sha256(...).hexdigest()
```

### A.5 — Exact scope of what is affected

| Category | Affected? | Evidence |
|---|---|---|
| `jsonb` columns (`CitationsByYear`, `VerificationDetails`) | **Yes — confirmed, the only affected category** | §A.2's field diff |
| Ordinary text/varchar fields (`Title`, `DOI`, `Source`, ...) | No | Identical type (`str`) and value across both connections for every such field |
| Integers (`PubYear`, `JournalID`, `TenantID`) | No | Identical type (`int`) and value across both connections |
| Booleans (`IsVerified`) | No | Identical type (`bool`) and value |
| Null values | No | Identical (`None`) across both connections for every null-valued field checked |
| Nested dicts (inside `VerificationDetails.evidence_trail`) | Yes, in principle — same root cause, since the whole column round-trips as one string | Directly exercised by the fix's tests (§D) |
| Lists (`evidence_trail` itself) | Yes, in principle — same root cause | Directly exercised by the fix's tests (§D) |
| Key ordering within a `dict` | Was already handled correctly for native `dict` inputs (`sort_keys=True`); the defect was specifically that a `str`-represented dict never reached that canonicalization at all | §A.2 |

`CitationsByYear` did not visibly change the *final fingerprint* in Phase 4P's original discovery — not because it's immune, but because that field's keys (`"2022"`–`"2026"`) already happen to sort in ascending order, so the sorted and unsorted representations coincide for this specific pair's data. **Both jsonb fields carry the identical underlying defect**, confirmed directly this phase (§A.2 shows both differing in type).

---

## Task B — Blast Radius

```
Real (non-test) callers of compute_plan_fingerprint():
  merge_execution_safety.py:313  (inside validate_against_plan())
  merge_execution_safety.py:713  (inside run_canary_simulation())

Definition of _normalize_scalar(): exactly one, merge_execution_safety.py
Real callers of _normalize_scalar():
  merge_execution_safety.py:127  (inside _normalize_authors(), for AuthorNameRaw)
  merge_execution_safety.py:162-163  (the FINGERPRINT_RP_FIELDS loop)

Other canonical JSON serialization for merge plans anywhere in the repository: none found
  (grep for "json.dumps.*sort_keys" across backend/ returns only the 3 hits inside
  merge_execution_safety.py itself)
```

- **A) Confirmed affected paths**: `compute_plan_fingerprint()` (both real call sites) via `_normalize_scalar()`'s field-normalization loop — this is the entire mechanism this project's fingerprint-binding safety property depends on.
- **B) Theoretically affected paths**: none beyond A — there is exactly one definition of each function, and no parallel/duplicate implementation exists anywhere else in the repository.
- **C) Unaffected paths**: `_normalize_authors()`'s use of `_normalize_scalar()` (for `AuthorNameRaw`) is technically routed through the same fixed function, but `AuthorNameRaw` is always a plain text column, never `jsonb` — confirmed unaffected in practice by Phase 4P's own diff (Authors rows matched identically across both connections) and unaffected in principle (a text column round-trips as `str` under both connection types identically). `merge_plan_generator.py`'s field classification (`classify_field()`) and `dedup_papers.py`'s `merge_citation_fields()` both read/compare these same columns but never hash or fingerprint them — unrelated to this defect. `merge_approval.py` never computes a fingerprint itself; it only stores and compares fingerprint values as opaque strings.

No unrelated code was touched or broadened.

---

## Task C — The Minimal Fix

### Exact file/function changed

**One file, `backend/tools/merge_execution_safety.py`**: added one small helper, `_looks_like_json_container(s)`, and extended `_normalize_scalar()`'s existing `str` branch. No other function, file, or module was touched. `dedup_papers.py`, `merge_plan_generator.py`, `merge_approval.py`, `merge_executor.py` — all confirmed untouched via `git status`/`git diff` (§14).

### The normalization rule implemented

1. A string value is only ever considered for JSON-parsing if it structurally looks like an object or array — `_looks_like_json_container()` checks (after stripping whitespace) that it both starts with `{`/`[` **and** ends with the matching `}`/`]`. An ordinary `Title`, `DOI`, `Abstract`, or any other real text field essentially never has this shape, so this check alone excludes the overwhelming majority of string values from ever being touched.
2. Only strings that pass that check are handed to `json.loads()`. If parsing fails (`ValueError`/`TypeError`), the value falls straight through to the existing plain-string handling — **no exception ever escapes `_normalize_scalar()`**.
3. If parsing *succeeds* and the result is a `dict` or `list`, the function recurses into itself with the parsed value, which routes to the (now-shared) `dict`/`list` canonicalization branch — producing the exact same canonical `json.dumps(value, sort_keys=True, ...)` output a native `dict`/`list` input would.
4. If parsing succeeds but the result is a bare scalar (a number, `true`/`false`, `null`, or a quoted string) — this can only happen for a string like `"123"` or `"null"`, and only if such a value ALSO happened to be wrapped in a way that passed check #1, which a bare scalar never is (a JSON scalar never starts with `{`/`[`) — so this case cannot actually occur; **`_looks_like_json_container()` structurally excludes it before `json.loads()` is ever called on a scalar-shaped string.**
5. `dict`/`list` inputs (whether arriving natively or via the new string-parsing path) are both handled by one shared branch: `json.dumps(value, sort_keys=True, ...)`. `sort_keys=True` sorts dictionary keys **recursively**, at every nesting level, while never touching list/array element order — Postgres JSON arrays and Python lists are both inherently ordered, and `sort_keys` only ever affects object-key ordering, never array-element ordering. This directly satisfies requirement 4 (key order never matters) and requirement 5 (list order stays significant) simultaneously, with no special-casing needed for either.
6. Empty `dict`/`list` (arriving either natively or via string-parsing) continue to normalize to `None`, matching the pre-existing rule and this project's established `NULL`/empty equivalence.

### Can ordinary strings be accidentally transformed? (§Report item 7)

**No — proven, not merely argued, by dedicated tests (§D).** A string is only ever re-interpreted if it (a) is brace/bracket-wrapped *and* (b) successfully parses as JSON *and* (c) parses specifically to a `dict` or `list` (never a bare scalar, which is structurally impossible to reach per point 4 above). `test_ordinary_string_that_is_a_bare_json_scalar_is_not_reinterpreted` and `test_ordinary_object_shaped_text_field_is_unaffected_when_not_valid_json` both directly exercise this. The one honestly-acknowledged theoretical edge case: a genuine text field whose entire content happens to be brace-wrapped *and* happens to also be syntactically valid JSON (e.g., a `VerificationDetails`-style field, but hypothetically also true of an `Abstract` that literally quotes valid JSON syntax in full) would be canonicalized rather than left as raw text. This is judged acceptable and not worth a field-name allowlist instead, for two reasons: (1) it cannot occur for any of the genuinely `jsonb`-typed fields this defect is about, since those are supposed to be treated as structured data regardless of representation; (2) for the genuinely-text-typed fields in `FINGERPRINT_RP_FIELDS` (`Title`, `Abstract`, `DOI`, etc.), real academic paper metadata being *simultaneously* brace-wrapped, edge-to-edge, and syntactically valid JSON is not a realistic occurrence — and even if it somehow were, the fingerprint would still be *deterministic* (just canonicalized differently than raw-text comparison would produce), not *incorrect* or *crash-prone*.

### Requirements 7–11, confirmed not violated

- Merge semantics: unchanged — `dedup_papers.py` was not touched.
- Duplicate-detection thresholds: unchanged — `merge_plan_generator.py`/`dedup_papers.py`'s `pair_confidence()`/`hard_exclusion_reason()` were not touched.
- Pair selection: unchanged — `choose_keep()` was not touched.
- Approval state-machine semantics: unchanged — `merge_approval.py` was not touched.
- Dedup logic unrelated to fingerprint normalization: unchanged — nothing outside `_normalize_scalar()`/the new helper was modified.

The fix was made at the normalization boundary itself (`_normalize_scalar()`), exactly as instructed, not as an adapter-specific patch in either real call site or in any caller.

---

## Task D — Tests

**11 new tests**, added to `backend/tools/test_merge_execution_safety.py` as `JsonRepresentationEquivalenceTests` (all against literal, hand-constructed Python values — no live DB connection, matching this file's own established convention):

| Requirement | Test |
|---|---|
| 1. dict-vs-Django-string-representation equivalence | `test_dict_and_equivalent_json_string_produce_same_fingerprint` |
| 2. Nested objects normalize identically | `test_nested_objects_normalize_identically_regardless_of_source_representation` |
| 3. Key-order variants normalize identically | `test_key_order_variants_normalize_identically_via_the_string_path`, `test_list_nested_dict_key_order_does_not_affect_fingerprint` |
| 4. Lists preserve order significance | `test_list_element_order_remains_significant_through_the_string_path` |
| 5. Ordinary JSON-resembling strings handled safely | `test_ordinary_string_that_is_a_bare_json_scalar_is_not_reinterpreted`, `test_ordinary_object_shaped_text_field_is_unaffected_when_not_valid_json` |
| 6. Malformed JSON does not crash | `test_malformed_json_like_string_does_not_crash_fingerprint_computation` |
| (empty-value equivalence, matching the pre-existing rule) | `test_empty_json_object_and_array_strings_normalize_to_none` |
| 7. Known canary fingerprint reproducible via both representations | `test_real_canary_verification_details_dict_and_json_string_forms_match` — uses the **real, exact** `VerificationDetails` shape captured live from the canary pair in Task A, not a simplified stand-in |
| 8. Approval fingerprint lookup/binding still works | Existing `test_merge_approval.py` (45 tests, unchanged, re-run — §D.10) plus the live proof in §E |
| 9. A genuinely changed value still produces a different fingerprint | `test_genuinely_changed_value_still_produces_different_fingerprint_via_string_path` |
| 10. Existing suites remain green | See below |

```
test_dedup_papers.py             18/18 passing (unchanged)
test_merge_plan_generator.py     43/43 passing (unchanged)
test_merge_execution_safety.py   79/79 passing (68 pre-existing + 11 new)
test_merge_approval.py           45/45 passing (unchanged)
test_merge_executor.py           39/39 passing (unchanged)
test_fk_lifecycle.py             11/11 passing (unchanged)
```

**Total: 235/235 passing (224 pre-existing + 11 new), zero regressions.**

All new tests use literal Python values constructed to represent "what each connection type would hand back" — no production writes of any kind occur in the automated suite.

---

## Task E — Live Read-Only Validation

Performed after the fix and after the new tests, strictly read-only, against production:

1. **`ApprovalID=1` read, unmodified**: `SurvivorPaperID=5232`, `LoserPaperID=5482`, `PlanFingerprint=2298ea25...`, `Status=APPROVED`, `ReviewedByUserID=221` — **byte-identical to its Phase 4P state**.
2. **Live fingerprint, both paths, recomputed after the fix**:
   ```
   Django path fingerprint:       2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb
   Raw psycopg2 path fingerprint: 2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb
   ```
3. **Byte-identical**: confirmed directly, `Django == raw` → `True`.
4. **Compared against `ApprovalID=1`'s stored `PlanFingerprint`**: `2298ea25...` — **matches exactly, both paths.**
5. **Is the approval now technically valid for executor preflight?** **Yes** — proven, not inferred (§6 below).
6. **Full read-only preflight sequence re-run**, individual functions only (`execute_approved_merge()` never called, matching Phase 4P's own established boundary-proof discipline), inside one transaction explicitly rolled back at the end:

   ```
   validate_against_plan(): status=OK, passed=True
     checks: {fingerprint_match: True, both_rows_exist: True, winner_loser_unreversed: True,
              pair_confidence: 'high', hard_exclusion_reason: None, doi_claimed_elsewhere: False}
   idempotency_verdict(): NOT_PREVIOUSLY_EXECUTED
   journal_state execution_permitted: True
   author_content_conflicts: []
   dependency gaps: []

   ALL PRECONDITIONS SATISFIED: True
   ```

7. **Stopped exactly where instructed**: no `JournalID` `UPDATE`, no `merge_group()` call, no `MergeApproval` status change, no `ResearchPaper` `DELETE` — the transaction was explicitly `transaction.set_rollback(True)`, releasing the lock with zero persistence, exactly mirroring Phase 4P's Task D methodology.

---

## Task F — Compatibility With the Existing Approval

### **Outcome A: the existing stored fingerprint now matches the deterministic algorithm. `ApprovalID=1` remains valid.**

This was not a coincidence requiring reconciliation — it follows directly from the fix's design. The *raw psycopg2* path was **already correct** before this fix (it always received a native `dict` and always canonicalized it via `sort_keys=True`); the *only* incorrect path was Django's, which skipped canonicalization entirely for the `str`-represented case. `ApprovalID=1`'s stored fingerprint was computed via the raw-psycopg2 path (Phase 4P's pre-write gates, §1 of that report) — i.e., it was **already the canonical value**. Fixing Django's path to *also* reach that same canonical computation necessarily converges on the value that was already stored, not a new one. **No `PlanFingerprint` was rewritten, and none needed to be.**

---

## Why the Previous 12 Fingerprint Checks Did Not Expose This

Every "live canary revalidation" from Phase 4G through the start of Phase 4P — 12 consecutive confirmations — used `litrix_db.db()` (a bare `psycopg2.connect()` connection), which always takes the already-correct `dict` branch. **Phase 4P's Task D was the first point in this entire project where the fingerprint was computed via Django's `connection.cursor()`** — deliberately, because that is the actual, real mechanism `execute_approved_merge()` is designed to run inside (`transaction.atomic()`), matching `apply_migration.py`'s and `dedup_papers.py --apply`'s own established convention. The defect was always present in the code; it was invisible to every prior check because every prior check happened to use the one connection mechanism that didn't trigger it.

---

## Files Changed and Why

| File | Change | Why |
|---|---|---|
| `backend/tools/merge_execution_safety.py` | Added `_looks_like_json_container()`; extended `_normalize_scalar()`'s `str` branch to canonicalize JSON-object/array-shaped strings identically to native `dict`/`list` inputs; generalized the existing `dict` branch to also accept `list` | This is the single function responsible for the fingerprint's determinism guarantee — the correct, minimal location for the fix, per the task's own "fix the normalization boundary, not individual callers" instruction |
| `backend/tools/test_merge_execution_safety.py` | Added `import json`; added `JsonRepresentationEquivalenceTests` (11 tests) | Direct, focused regression coverage for the exact defect and fix, using the real canary field shape captured in Task A |

No other file was modified.

---

## Exact Production Write Accounting

- **DB writes to `ResearchPaper`**: **0.**
- **DB writes to paper child tables**: **0.**
- **DB writes to `MergeApproval`**: **0** — `ApprovalID=1` was read multiple times, never updated. Row confirmed byte-identical to its Phase 4P state (§Task E, item 1).
- **Records merged**: **0.**
- **DOI changes**: **0.**
- **Merge executor executions**: **0** — `execute_approved_merge()` was never imported or called at any point this phase.
- **`--apply` executions**: **0.**
- **Network calls**: **0** beyond the production-database connections used for read-only investigation and validation throughout.
- **Second approval created**: **0** — exactly one `MergeApproval` row exists, unchanged from Phase 4P.

---

## Final Decision

### **A) DEFECT FIXED — READY TO RE-AUDIT FIRST CANARY EXECUTION**

The root cause was reproduced, bounded, and proven with concrete evidence (§Task A) — not inferred or guessed. The blast radius was fully mapped and found narrow (§Task B). The fix is minimal, scoped to the exact normalization boundary responsible for the guarantee, conservative about what strings it ever touches, and provably safe against malformed input (§Task C). 11 new, focused tests plus all 224 pre-existing tests pass — 235/235 total, zero regressions (§Task D). Live, read-only validation against production confirms both real connection mechanisms now compute the identical, reference-matching fingerprint, and that `ApprovalID=1` — preserved completely untouched throughout this entire investigation — is now genuinely, provably valid for executor preflight, with every other precondition (idempotency, duplicate safety, DOI safety, `JournalID` state, author conflicts, dependency gaps) independently re-confirmed clean at the same moment (§Task E). No approval was rewritten, revoked, or duplicated; none needed to be.

This is not verdict `B` — the existing approval did not become invalid; it was already storing the canonical value the fix converges on. This is not verdict `C` — nothing about this fix required a broader design change; it was a single, precisely-located function's canonicalization gap, closed without touching merge semantics, duplicate-detection thresholds, pair selection, the approval state machine, or any unrelated dedup logic.

**Per your instructions, I am stopping here. Phase 4Q is not started. No merge was performed. `ApprovalID=1` remains exactly as it was, now provably valid rather than merely hoped to be.**

---

## Exact Accounting

- **Code files modified**: **1** — `backend/tools/merge_execution_safety.py`.
- **Code files created**: **0.**
- **Test files modified**: **1** — `backend/tools/test_merge_execution_safety.py` (11 tests added).
- **Migration files modified**: **0.**
- **Report files created**: **2** — this file and `backend/reports/phase4p1_fingerprint_defect_fix.json`.
- **Test totals before this phase**: 224/224 passing.
- **Test totals after this phase**: **235/235 passing** (11 new, all in `test_merge_execution_safety.py::JsonRepresentationEquivalenceTests`, added specifically to cover the newly-found and newly-fixed defect — zero regressions in any pre-existing test).
- **Production DB writes**: **0.**
- **Records merged**: **0.**
- **DOI changes**: **0.**
- **`ApprovalID=1` modifications**: **0.**
- **Second approvals created**: **0.**
- **Network calls**: **0** beyond the production-database connections used for read-only investigation/validation.

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Identical to every phase since 4E — zero changes this phase to either tracked file (confirming no unrelated dedup logic was touched).

### `git status --short` (relevant paths)

```
 M backend/tools/dedup_papers.py                 <- pre-existing, unchanged this phase
 M backend/tools/test_dedup_papers.py             <- pre-existing, unchanged this phase
?? backend/reports/                                <- this phase adds 2 files to it
?? backend/tools/merge_approval.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_execution_safety.py         <- MODIFIED this phase (the fix, §Task C)
?? backend/tools/merge_executor.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_plan_generator.py           <- pre-existing, unchanged this phase
?? backend/tools/test_fk_lifecycle.py              <- pre-existing, unchanged this phase
?? backend/tools/test_merge_approval.py            <- pre-existing, unchanged this phase
?? backend/tools/test_merge_execution_safety.py    <- MODIFIED this phase (11 new tests, §Task D)
?? backend/tools/test_merge_executor.py            <- pre-existing, unchanged this phase
?? backend/tools/test_merge_plan_generator.py      <- pre-existing, unchanged this phase
```

**This phase's repository changes are exactly two files: the fix itself, and its dedicated test coverage.** The one real database interaction of consequence this phase performed was reading `ApprovalID=1` repeatedly — never writing to it.
