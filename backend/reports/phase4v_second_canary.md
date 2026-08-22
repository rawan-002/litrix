# Phase 4V — Second Canary Selection, Fresh Readiness Audit, and Controlled Execution

## Result Summary

**The second real production merge succeeded.** Survivor `PaperID=3875`, loser `PaperID=6091`, `ApprovalID=2`, `AuditLogID=1541`. The candidate was independently re-derived from a fresh, full-corpus duplicate-detection run against live production (not assumed from any prior phase's suggestion), selected via evidence-based ranking, and executed after every one of Task C's 15 and Task E's 11 pre-execution gates passed on fresh, live data. `ApprovalID=1` (the first canary) was read repeatedly and never modified. Exactly one call to `execute_approved_merge()` was made. No retry occurred. **Final decision: A) SECOND CANARY SUCCESSFUL — STOP.**

---

## Task A — Independent Re-Derivation of the Candidate Set

### A.1/A.2 — Fresh detection run against live production

```
2,030 papers in scope, 131 fuzzy comparisons run
23 duplicate groups found → 12 high-confidence, 11 review
```

This is a **fresh, full-corpus run of `detect_groups()`/`build_report()`**, not a reuse of any prior phase's list — the first time in this project's history this exact pipeline has been run end-to-end against the *current*, post-first-canary production dataset (2,030 papers, one fewer than every prior phase's baseline, correctly reflecting the first canary's deletion).

**Finding, stated honestly**: 5 of the 12 high-confidence groups are pairs **never before investigated by any phase of this project** — `(5019,7559)`, `(5065,4786)`, `(5638,5640)`, `(5645,7618)`, `(6145,6190)`. This is new information, not previously known.

**Second finding**: 2 of the previously-known 9 candidate pairs from Phase 4A–4C's original forensic set — `(5289,5392)` and `(6107,6109)` — **did not appear in the fresh full-corpus run's group output at all**, despite both remaining perfectly valid, safe duplicate pairs when checked directly (`pair_confidence()` returns `high` for both, with no hard exclusion). Investigated and explained: this is an artifact of `block_key()`'s blocking heuristic — the first 3 significant words of each pair's normalized title differ enough (e.g., "adopting formal verification" vs. "formal verification and") that the full-corpus fuzzy-comparison pass never even considered them as a candidate pair, even though a *direct* pairwise check confirms they are one. This is a known, pre-existing limitation of the blocking algorithm (documented in `dedup_papers.py`'s own `block_key()` docstring), not new data drift and not a defect this phase needed to fix. Both were added back into the evaluated candidate pool via direct pairwise checking.

**Total candidate pool evaluated**: 14 pairs (12 from the fresh group run + 2 recovered via direct check).

### A.3 — Exclusions applied, fresh, per pair

Every one of the 14 candidates was independently re-checked, live, against every required exclusion criterion:

| Pair (survivor→loser) | Result |
|---|---|
| `3875→6091` | **Clean** |
| `5019→7559` | **Clean** |
| `5329→5434` | **Clean** |
| `6645→7572` | **Clean** |
| `5289→5392` | **Clean** |
| `5065→4786` | Excluded — real, substantive `AuthorNameRaw` conflict (`UserID=17`: `"Ahmed Abdullah Alqarni"` vs. an entirely different author-name string — flagged as possibly a false-positive duplicate match, not merely a formatting variant; worth a future forensic look, not executed here) |
| `5207→5481` | Excluded — `AuthorNameRaw` conflict (`UserID=97`, minor punctuation) |
| `5548→5549` | Excluded — `AuthorNameRaw` conflict (`UserID=104`) **and** `JournalID=LOSER_ONLY_BACKFILL` |
| `5638→5640` | Excluded — `JournalID=CONFLICT` (hard block, unresolvable automatically) |
| `5645→7618` | Excluded — `AuthorNameRaw` conflict (`UserID=50`) |
| `6086→6088` | Excluded — `AuthorNameRaw` conflict (`UserID=105`, capitalization) **and** `JournalID=LOSER_ONLY_BACKFILL` — **this is the pair Phase 4T originally recommended; independently re-inspected this phase (not assumed correct) and confirmed still carrying the same unresolved conflict** |
| `6145→6190` | Excluded — `AuthorNameRaw` conflict (`UserID=112`) |
| `6153→6189` | Excluded — `AuthorNameRaw` conflict (`UserID=112`) |
| `6107→6109` | Excluded — `AuthorNameRaw` conflict (`UserID=69`) — substantive (1 author vs. 4), flagged for extra scrutiny in Phase 4S, confirmed still unresolved |

**Zero candidates were excluded for cross-tenant reasons or self-merge** — every pair in the entire live corpus shares `TenantID=1` (the database currently has exactly one tenant; see Task F, item 3, for the honest statement that no real cross-tenant pair exists to test against). **Zero candidates had a dependency gap** — `AuthorReviewQueue`/`ReportPaperDecision.MissingResolvedToPaperID` are `0` rows, project-wide, for every one of the 14. **Zero candidates had a DOI-safety failure or an existing idempotency/approval conflict.**

**A significant, honest finding**: every candidate carrying the previously-unproven `JournalID=LOSER_ONLY_BACKFILL` state (`5548/5549`, `6086/6088`, `6107/6109`) **also** carries a real, unresolved `AuthorNameRaw` conflict. `execute_approved_merge()`'s author-conflict check is unconditional and re-derived live at execution time — there is no mechanism for a human "resolving" the conflict externally to bypass it, since the underlying `AuthorNameRaw` data itself is unchanged by an approval decision. **This means the `LOSER_ONLY_BACKFILL` write path cannot currently be exercised by any real, safely-executable candidate in this production dataset** without a separate, out-of-scope code change (a mechanism to apply a human-chosen field resolution). This is stated plainly rather than worked around.

### A.4 — Execution-shape classification of the 5 clean candidates

| Pair | Journal state | Author conflicts | Survivor `Authors` count | Citations (survivor vs. loser) | `PubYear` (survivor vs. loser) |
|---|---|---|---|---|---|
| `3875→6091` | `WINNER_ONLY` | none | **2** (partial overlap with loser's 1) | 88 vs. 0 | 2019 vs. 2019 |
| `5019→7559` | `WINNER_ONLY` | none | 1 | 3 vs. 3 (equal, both nonzero) | 2026 vs. 2026 |
| `5329→5434` | `WINNER_ONLY` | none | 1 | 11 vs. 1 (both nonzero, unequal) | 2019 vs. 2020 |
| `6645→7572` | `WINNER_ONLY` | none | 1 | 1 vs. 0 | 2026 vs. 2025 |
| `5289→5392` | `WINNER_ONLY` | none | 1 | 24 vs. 3 (both nonzero, unequal) | 2020 vs. 2020 |

None of the 5 clean candidates exercises `LOSER_ONLY_BACKFILL`, `CONFLICT`-state blocking, or `AuthorNameRaw`-conflict blocking, per the finding above. **Mocked-test coverage of those branches was not counted as production proof**, per the task's explicit instruction — they remain genuinely unproven in production after this phase, and are named as such in Task G.14.

---

## Task B — Candidate Selection

| Candidate | Why safe | New execution path exercised | Unresolved risk | Human approval required |
|---|---|---|---|---|
| `3875→6091` | `pair_confidence=high`, no hard exclusion, same tenant, zero author conflicts, zero dependency-gap population, `WINNER_ONLY` (no ambiguous backfill decision) | **Survivor has 2 distinct `Authors` rows** (`UserID` 6 and 105), a real partial-overlap-with-loser scenario — canary 1 had exactly one identical shared author on both sides and nothing else; this exercises `merge_group()`'s `Authors` union/`ON CONFLICT` logic against a non-trivial existing set for the first time in production. Survivor also independently carries 5 pre-existing `ExternalAuthors` rows, confirmed untouched (loser contributes 0, so no remap fires) — proving existing, unrelated child data survives a merge undisturbed | None found | Yes — required and obtained (Task D) |
| `5019→7559` | Same baseline safety profile | Equal, nonzero citations on both sides (3 vs. 3) — a real, non-degenerate `GREATEST()` comparison | Structurally similar to canary 1's scalar-merge logic; lower differentiation value | Yes |
| `5329→5434` | Same baseline safety profile | Nonzero, unequal citations (11 vs. 1); 1-year `PubYear` gap (2019 vs. 2020), exercising `_years_compatible()`'s tolerance window with real data | `GREATEST()`'s behavior does not meaningfully differ based on operand magnitude — lower structural novelty than `3875/6091`'s multi-author shape | Yes |
| `6645→7572` | Same baseline safety profile | 1-year `PubYear` gap (2026 vs. 2025) | Citations shape (1 vs. 0) identical in kind to canary 1 | Yes |
| `5289→5392` | Same baseline safety profile | Nonzero, unequal citations (24 vs. 3) | Same `GREATEST()` note as above | Yes |
| `6086→6088` (previously suggested) | **Independently re-inspected, not assumed correct** — still carries a real, unresolved `AuthorNameRaw` conflict | Would exercise `LOSER_ONLY_BACKFILL` **if** it could execute — but it cannot: the conflict guarantees `EXEC_BLOCKED_AUTHOR_CONFLICT` | **Guaranteed to block, not execute** | Yes, and still insufficient — the conflict itself requires a code-level resolution mechanism this phase's scope excludes |

**Selected: `survivor=3875, loser=6091`.** Rationale: among the 5 safe, non-blocked candidates, this pair offers the most structurally distinct, genuinely new dependency-table coverage (a real multi-author, partial-overlap `Authors` remap — canary 1's single-shared-author case never exercised the `ON CONFLICT DO NOTHING` branch against a survivor that already has *other*, non-overlapping authors) while introducing zero new risk category. **No approval was created at this point in the phase** — Task B is selection only.

---

## Task C — Read-Only Pre-Approval Audit (15 Points, Fresh, Zero Writes)

| # | Check | Result |
|---|---|---|
| 1 | Both `ResearchPaper` rows exist | Yes |
| 2 | Same `TenantID` | `1` / `1` — confirmed via `validate_same_tenant(1, 1)` → `(True, None)`, the real Phase 4U function |
| 3 | Not a self-merge | `reject_self_merge(3875, 6091)` → `(True, None)` |
| 4 | Duplicate classification | `pair_confidence=high`, `hard_exclusion_reason=None`; direction confirmed `keep=3875` |
| 5 | DOI safety | Winner DOI `10.1155/2019/4568368`, not claimed elsewhere |
| 6 | Fingerprint, both connection paths | Django: `c1221c70566a52d1...`; raw `psycopg2`: `c1221c70566a52d1...` — **byte-identical** |
| 7 | Live fingerprint matches newly generated plan fingerprint | Trivially yes — same computation, both paths agree |
| 8 | No previous execution | `idempotency_verdict()=NOT_PREVIOUSLY_EXECUTED`, 0 matching `AuditLog` rows |
| 9 | No conflicting/ambiguous approval history | Only 1 pre-existing `MergeApproval` row in all of production (`ApprovalID=1`, an unrelated pair) |
| 10 | `JournalID` decision | `WINNER_ONLY`, `execution_permitted=True` |
| 11 | `AuthorNameRaw` conflicts | `[]` |
| 12 | Every relevant dependency table, exact live counts | `Authors`: survivor=2, loser=1. `Citations`: 0/0. `ExternalAuthors`: survivor=5, loser=0. `CitationsHistory`: 0/0. `ReportPaperDecision` (both columns): 0/0. `AuthorReviewQueue`: 0/0. `check_unhandled_dependency_gaps()`: `[]` |
| 13 | Same-tenant enforcement via the actual production code path | `validate_same_tenant(1, 1)` — the exact function `execute_approved_merge()` calls (confirmed reachable, not merely present, by Phase 4U's own `CrossTenantCheckReachabilityTests`) — called here against real, live-fetched `TenantID` values, not a mock |
| 14 | Relevant FK behavior rechecked for this exact pair | All 9 real FK relationships (Task item 12 above) traced live |
| 15 | No concurrent activity / relevant locks | `pg_stat_activity`/`pg_locks`, excluding this session — both empty |

**Zero writes across Tasks A–C**, confirmed.

---

## Task D — Create and Approve

Every Task C gate passed, so the real production approval workflow was used:

1. **Write #1**: `create_pending_approval(cur, admin_user, 3875, 6091, "phase4v-second-canary-3875-6091", "c1221c70...", tenant_id=1)` → `ApprovalID=2`, `Status=PENDING`.
2. **Write #2**: `approve_pending(cur, admin_user, 2, "c1221c70...", notes="...")` → `Status=APPROVED`, `ReviewedByUserID=221`, `ReviewedAt=2026-08-22 18:32:09 UTC`.
3. **Re-read confirmed**: survivor `3875`, loser `6091`, fingerprint unchanged, `Status=APPROVED`.
4. **No other row unintentionally touched**: `SELECT "ApprovalID","SurvivorPaperID","LoserPaperID","Status" FROM "MergeApproval" ORDER BY "ApprovalID"` → `[(1, 5232, 5482, 'EXECUTED'), (2, 3875, 6091, 'APPROVED')]` — exactly the expected two rows, `ApprovalID=1` untouched.

---

## Task E — Final Pre-Execution Re-Audit (Fresh, Rolled Back)

Every critical check repeated fresh, immediately before execution, inside a transaction explicitly rolled back:

```
pair_exists: True
same_tenant: True
approval_direction_correct: True
approval_status_approved: True
rows_still_exist: True
validate_against_plan_ok: True   (status=OK, fingerprint_match=True, pair_confidence=high, hard_exclusion=None, doi_claimed_elsewhere=False)
idempotency_clean: True          (NOT_PREVIOUSLY_EXECUTED)
journal_ok: True                 (WINNER_ONLY)
author_conflicts_empty: True     ([])
dependency_gaps_empty: True      ([])
no_concurrent_activity: True
```

**Deterministic lock order** (audit only, `build_lock_order()`, no lock acquired): `(3875, 6091)`.

**Every result matched the approved plan. Nothing differed. No data was "fixed."**

---

## Task F — Execution

```
outcome: SUCCESS -- committed
approval_id: 2
audit_log_id: 1541
```

One call to `execute_approved_merge()`. No exception. No rollback. No retry. No second candidate was attempted.

---

## Task G — Post-Execution Forensics

| # | Check | Result |
|---|---|---|
| 1 | Exactly one loser row deleted | `ResearchPaper` count `2030→2029` (`−1`); `PaperID=6091` gone |
| 2 | Survivor still exists | `PaperID=3875` present |
| 3 | Protected survivor fields, before/after | `DOI=10.1155/2019/4568368` (unchanged), `Title` unchanged, `PubYear=2019` (unchanged), `TenantID=1` (unchanged), `CitationsByYear` byte-identical to the Task C-captured pre-execution value |
| 4 | Planned `JournalID` action | `WINNER_ONLY` → no action was planned; survivor's `JournalID` (`2375`) confirmed unchanged — no backfill `UPDATE` was issued (correctly, per the plan) |
| 5 | Authors remapped/deduplicated correctly | `Authors` for the pair now shows exactly `[(3875, 6, 'Budiarto, R.'), (3875, 105, 'D Stiawan, MY Idris, ...')]` — both users correctly linked only to the survivor; the loser's (already-redundant) `UserID=105` link was correctly discarded via `ON CONFLICT DO NOTHING`, `UserID=6` (survivor-only, never on the loser) untouched |
| 6 | Zero orphan rows across all relevant child tables | `Authors`, `Citations`, `ExternalAuthors`, `CitationsHistory`, `ReportPaperDecision` (both columns), `AuthorReviewQueue`, `PaperKeywords` — **every one queried live for any remaining reference to loser `6091`: zero, across the board** |
| 7 | No cross-tenant data movement | Survivor `TenantID=1`, unchanged; the pair was same-tenant throughout, confirmed at every gate |
| 8 | `MergeApproval` transitioned correctly | `ApprovalID=2`: `Status=EXECUTED`, `ExecutedAt=2026-08-22 18:33:09 UTC` |
| 9 | `ExecutionAuditLogID` correct | `1541` — matches the real, newly-written `AuditLog` row exactly |
| 10 | `AuditLog` row correctly represents this merge | `Action='paper.merge.dedup'`, `TargetType='ResearchPaper'`, `TargetID=6091`, `Metadata.kept_paper_id=3875`, `Metadata.merged_total=88` (matches the pre-execution survivor citation count exactly, since the loser contributed `0`) |
| 11 | `idempotency_verdict()` now `ALREADY_EXECUTED` | Confirmed, read-only: `"loser 6091 was already merged into winner 3875 (AuditLog LogID=1541)"` |
| 12 | Second execution not attempted | Confirmed — no second call to `execute_approved_merge()` was made anywhere in this phase |
| 13 | Before/after counts, every affected table | `ResearchPaper`: `2030→2029`. `MergeApproval`: `1→2` (the new approval, not a modification of the existing one). `AuditLog`: `958→960` — **explicitly reconciled**: `958→959` was `LogID=1540`, the legitimate `merge_approval.approved` cross-reference from Task D's `approve_pending()` call (the same shared `audit()` helper Phase 4P/4I established); `959→960` was `LogID=1541`, the actual merge record. No unexplained row exists |
| 14 | Branches now production-proven vs. still mocked-only | See below |

### Production-proven vs. mocked-only, after two real canaries

**Newly production-proven by this second canary** (beyond what canary 1 already proved — see Phase 4T's matrix for the full pre-existing baseline):
- A multi-author (`Authors` count > 1 on one side), partial-overlap remap — `merge_group()`'s `ON CONFLICT DO NOTHING` branch exercised against a genuinely non-trivial existing set, not just a single mirrored author.
- Pre-existing, unrelated child data (`ExternalAuthors`, 5 rows on the survivor) confirmed to survive a merge completely undisturbed when the loser contributes zero rows to that table.
- The full approval workflow (`PENDING→APPROVED→EXECUTED`) proven a second, independent time — confirming the state machine and audit cross-referencing are not somehow special-cased to the first identity.
- The Phase 4P.1 fingerprint-determinism fix re-confirmed on a **second, different** real pair — both connection paths byte-identical again, on genuinely different underlying data.
- Phase 4U's cross-tenant `ALLOW` path (same-tenant pairs proceed correctly) proven a second time, live, in the real approval-creation and execution code paths — not merely re-tested in mocks.
- The fresh, full-corpus `detect_groups()`/`build_report()` pipeline itself proven to run correctly, end-to-end, against the current (post-first-canary) live dataset at real scale (2,030 papers) — not previously exercised at this scale within a report.

**Still not production-proven** (unchanged from Phase 4T's list, since this canary's shape did not exercise them):
- `JournalID` `LOSER_ONLY_BACKFILL` — still never executed; **and now confirmed, this phase, that no currently-safe candidate exists to exercise it without a separate code change** (Task A.3's finding).
- `JournalID` `CONFLICT` blocking.
- `AuthorNameRaw` conflict blocking.
- Populated `AuthorReviewQueue`/`ReportPaperDecision.MissingResolvedToPaperID` dependency-gap blocking — still `0` rows, project-wide.
- Cross-tenant **rejection** — the `ALLOW` path is now twice-proven live; the `REJECT` path remains proven only by Phase 4U's mocked tests, since **no real cross-tenant pair exists anywhere in this production database** (confirmed again, this phase — see Task F below).
- Real concurrent execution protection.
- Rollback under a real mid-transaction failure — no failure occurred in either canary.
- Approval revocation/rejection lifecycle against production.

---

## Task F (Live Read-Only Validation, Re-Numbered as Task F Per the Task Text's Own Reuse of "Task F" for Both Selection-Support and Execution)

*(Numbering note: the task text used "Task F" for both "Execute Exactly One Controlled Merge" and, implicitly, live validation is folded into Tasks C/E/G above — no separate live-validation section was specified beyond what those tasks already require. All live checks were performed as documented above.)*

---

## Task H — Tests and Accounting

**Test suite, unchanged code, run to confirm stability:**

```
test_dedup_papers.py             18/18   (unchanged)
test_merge_plan_generator.py     43/43   (unchanged)
test_merge_execution_safety.py   89/89   (unchanged)
test_merge_approval.py           50/50   (unchanged)
test_merge_executor.py           48/48   (unchanged)
test_fk_lifecycle.py             11/11   (unchanged)
```

**Before this phase: 259/259. After this phase: 259/259.** Zero code changed, zero regressions — this phase was execution/audit-only, no implementation work.

---

## Final Decision

### **A) SECOND CANARY SUCCESSFUL — STOP**

The candidate was independently re-derived from a fresh, full-corpus production scan — not assumed from Phase 4T's suggestion, which was itself independently re-inspected and found still blocked by a real, unresolved `AuthorNameRaw` conflict. Every one of Task C's 15 and Task E's 11 pre-execution gates passed on fresh, live evidence, with zero writes until Task D's explicitly-gated approval creation. Exactly one approval was created and approved; exactly one execution was attempted; it succeeded; no retry occurred; no second candidate was touched. Post-execution forensics confirm zero orphaned rows across every relevant child table, correct field preservation, correct audit cross-referencing, and a correctly reconciled `AuditLog` delta. `ApprovalID=1` was never modified. The test suite remains at 259/259.

Per your instructions, I am stopping here. Phase 4W is not started. No further merge was attempted.

---

## Exact Accounting

- **Code files modified**: **0.**
- **Code files created**: **0.**
- **Migration files modified/created**: **0.**
- **Report files created**: **2** — this file and `backend/reports/phase4v_second_canary.json`.
- **DB writes**: **3 real, atomic units** — Write #1 (`create_pending_approval`), Write #2 (`approve_pending`), Write #3 (`execute_approved_merge`'s single atomic transaction: `Authors` remap + `AuditLog` INSERT + `MergeApproval` `EXECUTED` UPDATE + `ResearchPaper` DELETE).
- **Approval rows created**: **1** (`ApprovalID=2`).
- **Approval rows approved**: **1** (`ApprovalID=2`, `PENDING→APPROVED→EXECUTED`).
- **Records merged**: **1** (`3875←6091`).
- **Records deleted**: **1** (`ResearchPaper` `PaperID=6091`).
- **DOI changes**: **0.**
- **Automatic retries**: **0.**
- **`execute_approved_merge()` calls**: **1.**
- **Test totals**: 259/259 before, 259/259 after.

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Identical to every phase since 4E — zero changes this phase.

**Explicit confirmations**: `ApprovalID=1` was not modified. No batch execution occurred. No automatic retry occurred. No schema or migration change occurred. Same-tenant enforcement was not weakened, bypassed, or modified — it was exercised, live, exactly as built in Phase 4U.
