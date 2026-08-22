# Phase 4P — Approval Workflow Audit (Real Production Writes, Scoped to MergeApproval Only)

## Result Summary

The full approval workflow was tested against real production data for the canary pair (5232 survives, 5482 loses), using the real, unmodified `merge_approval.py` functions, with a real Admin user (`admin@litrix.com`, `UserID=221`). **A valid `PENDING` approval was created and successfully transitioned to `APPROVED`.** Every guard tested (self-merge rejection, fingerprint scoping, reversed-pair rejection, duplicate-creation idempotency, illegal-transition rejection, fingerprint-binding enforcement) worked correctly, with zero exceptions.

**However, Task D's executor-boundary proof — performed exactly as instructed, using Django's real `connection.cursor()` rather than a bare `psycopg2` connection, because that is how `execute_approved_merge()` is actually designed to run — surfaced a genuine, previously-undiscovered defect**: `compute_plan_fingerprint()` produces a *different* result for the *same, unchanged* data depending on which database connection mechanism fetched it. This is not data drift. It is a real correctness gap in `merge_execution_safety.py::_normalize_scalar()`, root-caused and evidenced below. **No fix was applied this phase**, per your explicit instruction not to modify the executor/dedup logic without a proven defect, and per this project's established discipline of separating "prove there is a problem" phases from "fix the problem" phases.

**Final decision: C) DEFECT FOUND — with evidence** (§12).

---

## Scope Confirmation

Every DB write this phase touched only `MergeApproval` and, via the real, unmodified `audit()` helper, `AuditLog` (the same shared audit table `approve_pending()` has always written to, per its established design — not a new write path). Zero `ResearchPaper` rows were inserted, updated, or deleted. Zero DOI values were touched. `merge_group()` and `execute_approved_merge()` were never called. `dedup_papers.py --apply` was never run. Zero network calls beyond the one production-database connection used throughout.

---

## 1. Live State Before the Test (Steps 1–6, All Read-Only, Fresh)

| Check | Result |
|---|---|
| Current pair state | 5232: `DOI=10.1155/2022/8531213`, `JournalID=1803`, `TenantID=1`. 5482: `DOI=NULL`, `JournalID=NULL`, `TenantID=1` |
| Survivor direction | 5232 survives, 5482 loses — **unchanged, no reversal needed** |
| Fresh fingerprint | `2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb` — **MATCH** vs. the established reference. **12th independent live confirmation** (computed via `litrix_db.db()`, the same raw-psycopg2 mechanism every prior phase's canary check used) |
| Duplicate/DOI safety | `pair_confidence=high`, `hard_exclusion_reason=None`, `doi_claimed_elsewhere=False` |
| `JournalID`/`AuthorNameRaw` | `WINNER_ONLY` (no backfill needed), zero author conflicts |
| Prior merge / `AuditLog` record | **None** — `idempotency_verdict()=NOT_PREVIOUSLY_EXECUTED`, 0 matching rows |
| Existing `MergeApproval` rows | **0 total**, 0 touching this pair in either role — a genuinely clean slate |

No assumption from any prior report was relied upon — every one of these was re-verified live at the start of this phase.

---

## 2. Every DB Write, In Order, With Reason

| # | Write | Function | Before | After | Reason |
|---|---|---|---|---|---|
| 1 | `INSERT INTO "MergeApproval"` | `create_pending_approval(cur, admin_user, 5232, 5482, 'phase4p-canary-5232-5482', '2298ea25...', tenant_id=1)` | 0 rows | 1 row, `Status='PENDING'` | Task A — create the real pending approval for the canary pair |
| 2 | `UPDATE "MergeApproval"` + `INSERT INTO "AuditLog"` | `approve_pending(cur, admin_user, 1, '2298ea25...', notes='Phase 4P approval-workflow audit...')` | `Status='PENDING'`, `AuditLog`=956 rows | `Status='APPROVED'`, `ReviewedByUserID=221`, `ReviewedAt` set, `AuditLog`=957 rows | Task C — the real human-approval decision |

**Exactly 2 writes total, both exclusively against `MergeApproval` (the second additionally, and only via the real, unmodified, pre-existing `audit()` shared helper, writes one cross-referencing row to `AuditLog` — the same helper `reconciliation_views.py`'s real endpoint already uses for the same purpose).** Every other operation this phase (11 guard tests, the full boundary proof) issued either zero SQL, a `SELECT` only, or an `UPDATE`/`INSERT` attempt that was correctly refused *before* any statement executed — confirmed individually below.

---

## 3. Every Transition Tested, and Its Result

| # | Test | Mechanism | Result | Wrote anything? |
|---|---|---|---|---|
| B.1 | Self-merge (5232→5232) | `create_pending_approval` | Refused: `self_merge_rejected` | **0 SQL statements issued** (confirmed by instrumented counter) |
| A | Create `PENDING` for the real pair | `create_pending_approval` | `ApprovalID=1`, `Status=PENDING`, correct direction/fingerprint | **Write #1** |
| B.2 | Lookup with wrong fingerprint | `fetch_current_approval` | `None` | Read-only |
| B.2 | Lookup with correct fingerprint | `fetch_current_approval` | Found, `PENDING` | Read-only |
| B.3 | Lookup with reversed pair (5482 survivor / 5232 loser) | `fetch_current_approval` | `None` | Read-only |
| B.4 | Duplicate creation attempt, same identity | `create_pending_approval` | Returns the *same* `ApprovalID=1`, row count stayed at 1 | No new `INSERT` (dedup guard) |
| C | `PENDING → APPROVED` | `approve_pending` | Success, `ReviewedByUserID=221` | **Write #2** |
| C.1 | `APPROVED → APPROVED` (double-approve) | `approve_pending` | Refused: `illegal_transition: APPROVED -> APPROVED`; row verified byte-identical before/after | 0 additional writes |
| C.2 | `APPROVED → REJECTED` | `reject_pending` | Refused: `illegal_transition: APPROVED -> REJECTED`; row verified byte-identical before/after | 0 additional writes |
| B.5 | `APPROVED → REVOKED` with a **wrong** fingerprint (state-machine-legal transition, deliberately wrong fingerprint) | `revoke_approved` | Refused: `fingerprint_mismatch` (not `illegal_transition` — proving the fingerprint guard is independently enforced, not merely coincidental with the state-machine guard); row verified byte-identical before/after | 0 additional writes |
| D | Full executor-preflight sequence (individual functions only — `execute_approved_merge()` itself never imported or called) | `fetch_current_approval`, `approval_matches_pair`, `lock_pair_rows`, `fetch_current_state`, `validate_against_plan`, `idempotency_verdict`, `build_journal_state`, `author_content_conflicts`, `check_unhandled_dependency_gaps` | **See §4 — `validate_against_plan` returned `STALE_FINGERPRINT`, the defect** | Entire block run inside one transaction, explicitly `transaction.set_rollback(True)` at the end — **zero persistence regardless of outcome** |

**`REJECTED`/`REVOKED` were deliberately not driven to completion against the real canary row.** Both are terminal states; consuming our one real, intended-to-be-kept `APPROVED` row on either would have contradicted §11's cleanup decision (keep it as the audit trail for a future canary merge) while adding nothing the 45 already-passing, unmodified `test_merge_approval.py` mocked tests don't already exhaustively prove for these exact transitions. The illegal-transition *guards themselves* (C.1/C.2) were still tested directly and live, with zero risk, since an illegal transition is refused before any write is attempted.

---

## 4. The Defect — Full Root-Cause Evidence

### What happened

Task D's executor-boundary proof used **Django's `connection.cursor()`** — deliberately, because that is the actual, real mechanism `execute_approved_merge()` is designed to run inside (`transaction.atomic()`), and every prior phase's "live canary" checks had, until now, always used `litrix_db.db()`'s bare `psycopg2` connection instead. `validate_against_plan()` reported `STALE_FINGERPRINT` — comparing the just-recomputed current fingerprint against the approval's own stored `PlanFingerprint` (`2298ea25...`, the same value re-confirmed live at the *start of this exact phase*, §1) — for a pair whose underlying `ResearchPaper` data had not changed at all.

### Isolating the cause

Recomputing the fingerprint via Django's connection, in isolation, on the identical pair:

```
fingerprint via Django connection:  b74d4675e3069fe979720db3bcad7e2c20e1dfd92dc83656d38b1a6d71dbf4f9
fingerprint via raw psycopg2:       2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb
MATCH: False
```

A field-by-field diff of every value `compute_plan_fingerprint()` consumes (all 27 `FINGERPRINT_RP_FIELDS`, both sides' citations, both sides' `Authors` rows) found **exactly one differing field**: `VerificationDetails` (a `jsonb` column):

```
WINNER FIELD DIFFERS: 'VerificationDetails'
  django = '{"ror": ..., "tier": ..., "country": ..., "confidence": ..., ...}'   (original key order)
  raw    = '{"confidence": ..., "country": ..., "decision_basis": ..., ...}'      (alphabetically sorted)
```

Direct type inspection confirms the root cause precisely:

```python
DJANGO connection -- type: <class 'str'>    # raw JSON text, key order NOT canonicalized
RAW psycopg2 connection -- type: <class 'dict'>  # auto-decoded by psycopg2's default JSONB adapter
```

**Django's `connection.cursor()`, when used for raw SQL (bypassing the ORM entirely — which is how every table in this two-write-path repository is actually queried), returns `jsonb` columns as an undecoded `str`. A bare `psycopg2.connect()` connection (`litrix_db.db()`) auto-decodes the same column into a native `dict`.**

`merge_execution_safety.py::_normalize_scalar()` branches on type:

```python
if isinstance(value, dict):
    ...
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
```

— a `dict` is canonicalized (`sort_keys=True`, guaranteeing deterministic key order). But when the same column arrives as a `str` instead, execution falls through to the earlier `isinstance(value, str)` branch and the **raw, uncanonicalized JSON text is returned as-is** — never parsed, never re-sorted. Two representations of the *exact same logical JSON object*, differing only in key order, therefore produce two *different* SHA-256 fingerprints, purely as an artifact of which connection type fetched the row.

### Why this was never caught before

- **All 224 existing tests use mocked cursors** (`ExecutorFakeCursor`, `InMemoryApprovalCursor`, `FakeCursor`) that construct `CitationsByYear`/`VerificationDetails` values as literal Python `dict`s directly in test fixtures — none of them ever exercises a real `jsonb` column round-tripping through either connection type's actual driver behavior.
- **Every prior "live canary revalidation"** across Phases 4G through 4O — 12 consecutive confirmations before this one — used `litrix_db.db()` (raw `psycopg2`), which happens to always take the `dict` branch, which happens to always be canonicalized correctly. This phase is the *first* time in the entire project the fingerprint was computed via Django's connection — precisely because Task D correctly insisted on using the real, production-realistic mechanism rather than repeating the same convenient-but-non-representative check.
- **`CitationsByYear`, the *other* dict-typed fingerprinted field, did not show up in the diff for this specific pair** — not because it's immune to the same bug, but because its keys for this specific data (`"2022"`, `"2023"`, `"2024"`, `"2025"`, `"2026"`) already happen to be in ascending/alphabetical order, so sorted and unsorted representations coincide by chance. **The defect's exposure is general to every dict-typed fingerprinted field, not specific to `VerificationDetails`** — this pair's data simply only happened to expose it through one of the two affected fields.

### Safety characterization

**This is a fail-safe defect, not a fail-open one.** Its effect is to make `validate_against_plan()` incorrectly report `STALE_FINGERPRINT` and *block* execution for data that hasn't actually gone stale — never the reverse. No unsafe merge could result from this defect as it stands today. But it is a real, proven, functional gap: **as currently implemented, an approval whose stored fingerprint was computed via one connection mechanism cannot be validated by an executor recomputing it via the other, for any pair whose fingerprinted `jsonb` fields don't happen to already sort in place.** Since `execute_approved_merge()` is designed to run inside Django's `transaction.atomic()` (matching `apply_migration.py`'s own established convention, and `dedup_papers.py --apply`'s own pattern), this would affect the *real*, eventual execution path, not merely a hypothetical one.

### Recommended fix (not applied this phase)

Make `_normalize_scalar()` robust to a `jsonb` field arriving as either a `dict` or a JSON-encoded `str`, by attempting `json.loads()` on a `str` input before the existing `dict`-canonicalization path (falling back to the current string-handling behavior only if the parse fails, since not every `str` field is JSON). This fixes the determinism guarantee at the one place responsible for it, regardless of which connection any future caller uses — the correct architectural location for the fix, not a change to how any specific caller connects to the database.

---

## 5. Can a Valid `APPROVED` Approval Be Created for the Canary Pair?

**Yes — proven directly, not hypothetically.** `ApprovalID=1` exists, live, in production, `Status='APPROVED'`, `SurvivorPaperID=5232`, `LoserPaperID=5482`, reviewed by a real Admin user, with a correctly cross-referencing `AuditLog` row. The approval-creation and approval-decision workflow itself — the state machine, the permission check, the reviewer-attribution, the audit cross-reference — all worked exactly as designed, with zero defects found in any of it.

## 6. Is There Any Path to a Merge Without a Valid Approval?

**No path was found, and the defect discovered actually reinforces this answer rather than weakening it.** `execute_approved_merge()` unconditionally requires `fetch_current_approval()` to return a matching, `APPROVED` row before any write can occur (Phase 4J/4K, unchanged, re-confirmed structurally this phase by reading the function again). The newly-discovered fingerprint defect does not open a bypass — it does the opposite: it can cause a *legitimate*, otherwise-valid approval to be spuriously refused. There is no scenario uncovered this phase in which a merge could proceed *without* a valid, matching, `APPROVED` approval.

## 7. Does Fingerprint Binding Actually Work?

**Partially — and the honest, precise answer is now more nuanced than any prior phase could state.** The *binding mechanism itself* — an approval is scoped to an exact fingerprint value, a wrong fingerprint is refused, a matching one is required — works correctly and was proven live this phase (§3, tests B.2, B.5). **But the *fingerprint computation* it depends on is not yet deterministic across this repository's own two real database-connection mechanisms** (§4). The binding logic is sound; the value it binds to is not yet guaranteed stable across every real way this codebase connects to Postgres.

## 8. Conflicting or Orphaned Records?

**None.** Exactly one `MergeApproval` row exists, for exactly the intended identity, in exactly the intended state (`APPROVED`), created and reviewed by a single, real, identifiable Admin user, with exactly one correctly cross-referencing `AuditLog` row. No duplicate, no stray `PENDING` left behind, no row referencing any other pair.

## 9. Final State of the Canary Pair and `MergeApproval`

| | Value |
|---|---|
| `ResearchPaper` row count | 2,031 — unchanged from every prior phase's baseline |
| Canary pair 5232/5482 contents | Byte-for-byte unchanged (DOI, `JournalID`, `PubYear`, `TenantID` all re-verified) |
| `MergeApproval` row count | **1** |
| That row's state | `ApprovalID=1`, `SurvivorPaperID=5232`, `LoserPaperID=5482`, `PlanFingerprint=2298ea25...`, `Status=APPROVED`, `ReviewedByUserID=221`, `ReviewedAt` set, `ExecutedAt=NULL`, `ExecutionAuditLogID=NULL` |
| `AuditLog` row count | 957 (956 baseline + 1 approval-decision cross-reference row) |
| `paper.merge.dedup` rows for this pair | 0 |

## 10. Records Merged / DOI Changes

**Records merged: 0. DOI changes: 0.** Neither `merge_group()` nor `execute_approved_merge()` was ever called. No `ResearchPaper` row was inserted, updated, or deleted at any point this phase.

## 11. Cleanup Decision (Task E) — Explained Before Any Action

**Decision: keep the real `APPROVED` approval (`ApprovalID=1`) exactly as it is. Do not revoke it, do not delete it.**

Reasoning, considered explicitly before acting:

- The approval-creation and approval-decision workflow itself has **zero defects** — the human decision it represents (this pair should be merged) is sound and remains valid.
- Phase 4P's own final-decision framework offers `A) READY FOR FIRST CONTROLLED CANARY MERGE` as a possible outcome — a framing that presupposes a real, unrevoked, `APPROVED` approval surviving this phase for a future phase to consume. Revoking it would make that outcome structurally unreachable regardless of what the rest of the audit found.
- The newly-discovered fingerprint defect (§4) does **not** invalidate the *approval decision* — it affects only whether the *executor*, specifically, can currently validate against it via Django's connection. The approval row itself is not wrong; a downstream check that would consult it is not yet reliable.
- **This is precisely why the final decision below is `C) DEFECT FOUND`, not `A) READY`**: keeping the approval intact, while being completely explicit that it cannot yet be safely or successfully consumed by a real execution attempt until the fingerprint-determinism defect (§4) is fixed and re-verified in a separate, dedicated phase — exactly mirroring how Phase 4K.1 fixed the FK-lifecycle defect Phase 4K found, in its own separate phase, before Phase 4L re-audited it.
- No approval history is deleted. This matches the design's own explicit intent (Phase 4H/4K.1): a `MergeApproval` row is meant to persist as a durable record, not be discarded merely because a downstream capability isn't ready yet.

---

## 12. Final Decision

### **C) DEFECT FOUND — with evidence**

Every part of the approval workflow itself — creation, guards, the state machine, permission enforcement, reviewer attribution, audit cross-referencing — was proven correct, live, against real production data, with a real Admin user, using the real and unmodified code. That is a genuine, positive result and is documented in full above (§2–§3, §5–§6, §8).

But Task D's boundary proof — performed exactly as instructed, using the real Django connection mechanism `execute_approved_merge()` actually runs under — found that `compute_plan_fingerprint()` is **not deterministic across this repository's two real database-connection paths**, for any `jsonb`-typed fingerprinted field whose keys don't happen to already sort alphabetically. This is a real, evidenced, root-caused defect in `merge_execution_safety.py::_normalize_scalar()`, not a data-drift finding and not a false alarm — confirmed by directly inspecting the differing values' Python types across both connection mechanisms (§4).

This is **not** verdict `A`: declaring readiness for a real canary merge attempt today would mean asserting an execution attempt could succeed, when this phase's own evidence shows it would currently, deterministically, fail at the fingerprint-validation step for a reason unrelated to genuine data staleness. This is **not** verdict `B` either — the situation is not an ambiguous or policy-level block; it is a specific, understood, precisely-located, and already-scoped-for-a-fix technical defect, which is exactly what `C` is for.

**Per your instructions, I am stopping here. Phase 4Q is not started. No real merge was executed.**

---

## Exact Accounting

- **Code files modified**: **0.**
- **Code files created**: **0.**
- **Migration files modified**: **0.**
- **Report files created**: **2** — this file and `backend/reports/phase4p_approval_workflow_audit.json`.
- **DB writes**: **2**, both against `MergeApproval` only (the second additionally writes one `AuditLog` cross-reference row via the pre-existing, unmodified `audit()` helper) — full detail in §2.
- **`MergeApproval` rows created**: **1** (`ApprovalID=1`, currently `Status=APPROVED`).
- **`ResearchPaper` rows changed**: **0** — row count and canary-pair contents independently re-verified unchanged.
- **Records merged**: **0.**
- **DOI changes**: **0.**
- **`--apply` executions**: **0.**
- **`merge_group()`/`execute_approved_merge()` calls against production**: **0.**
- **Network calls**: **0** beyond the one production-database connection used throughout.
- **Tests run**: `test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 43/43, `test_merge_execution_safety.py` 68/68, `test_merge_approval.py` 45/45, `test_merge_executor.py` 39/39, `test_fk_lifecycle.py` 11/11 — **224/224, unchanged, zero regressions.** No test was added or modified this phase — the defect found is a gap in what the existing mocked suite *can* catch (§4), not a gap this phase attempted to patch with a new test, since doing so would require touching `merge_execution_safety.py` itself, explicitly out of scope this phase.

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Identical to every phase since 4E — zero changes this phase.

### `git status --short` (relevant paths)

```
 M backend/tools/dedup_papers.py                 <- pre-existing, unchanged this phase
 M backend/tools/test_dedup_papers.py             <- pre-existing, unchanged this phase
?? backend/analytics/migrations/sprint11_merge_approval.sql   <- pre-existing, unchanged this phase (already applied to production, Phase 4O)
?? backend/reports/                                <- this phase adds 2 files to it
?? backend/tools/merge_approval.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_execution_safety.py         <- pre-existing, UNCHANGED this phase (contains the defect; not fixed here, by design)
?? backend/tools/merge_executor.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_plan_generator.py           <- pre-existing, unchanged this phase
?? backend/tools/test_fk_lifecycle.py              <- pre-existing, unchanged this phase
?? backend/tools/test_merge_approval.py            <- pre-existing, unchanged this phase
?? backend/tools/test_merge_execution_safety.py    <- pre-existing, unchanged this phase
?? backend/tools/test_merge_executor.py            <- pre-existing, unchanged this phase
?? backend/tools/test_merge_plan_generator.py      <- pre-existing, unchanged this phase
```

**This phase's only repository changes are the two new report files.** The two real, consequential changes this phase made exist in the **database**: one new `MergeApproval` row (`APPROVED`) and one new `AuditLog` row — both fully accounted for above, both deliberately preserved, neither hidden nor auto-corrected.
