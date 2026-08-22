# Phase 4U — Cross-Tenant Safety Enforcement Audit & Minimal Fix

## Result Summary

**Task A confirmed the defect Phase 4T identified is real, not manufactured**: no schema-level mechanism (FK, CHECK constraint, trigger, RLS policy, database function) and no code-level check anywhere in `merge_approval.py`, `merge_executor.py`, `merge_execution_safety.py`, or `dedup_papers.py` enforced that `SurvivorPaperID` and `LoserPaperID` belong to the same `TenantID`. **Verdict: B) DEFECT_CONFIRMED.** A minimal fix was designed and implemented at exactly the two boundaries that matter — approval creation and the executor immediately before any write. 259/259 tests pass (235 pre-existing + 24 new), zero regressions. No merge was executed. `ApprovalID=1` was read repeatedly and never modified.

---

## Task A — Fresh Ground-Up Verification (Not Assumed From Phase 4T)

Every item below was independently re-derived this phase, by reading the actual current files and querying the live schema directly:

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Every call path that can create a `MergeApproval` | Exactly one: `merge_approval.py::create_pending_approval()` — confirmed by re-reading the module in full; no other `INSERT INTO "MergeApproval"` exists anywhere in the repository | Direct code read |
| 2 | Every call path from `APPROVED` to `execute_approved_merge()` | Exactly one: `merge_executor.py::execute_approved_merge()` — the sole function that transitions `MergeApproval` to `EXECUTED` | Direct code read |
| 3 | Does `create_pending_approval()` check `TenantID` equality? | **No** — the function never queries `ResearchPaper` at all; `tenant_id` is a caller-supplied parameter stored verbatim on the new row, never validated against either paper's actual tenant | Direct code read, full function body |
| 4 | Does `approve_pending()` check `TenantID` equality? | **No** — `_transition()` (the shared machinery `approve_pending`/`reject_pending`/`revoke_approved` all use) never queries `ResearchPaper`; it only checks permission, legal-transition, and fingerprint match | Direct code read |
| 5 | Does `execute_approved_merge()` check `TenantID` equality? | **No** — exhaustive `grep -in "tenant"` across `merge_executor.py` returned **zero matches** before this phase's fix | Direct `grep`, this phase |
| 6 | Does `merge_group()` check `TenantID` equality? | **No** — its `DELETE FROM "ResearchPaper" WHERE "PaperID" = %s` and all remap statements filter only by `PaperID`, never by `TenantID` | Direct code read |
| 7 | Does any FK/trigger/CHECK/RLS/DB function make cross-tenant merging impossible anyway? | **No.** Live schema query, this phase: zero triggers on `ResearchPaper`/`MergeApproval`/`Authors`; Row-Level Security disabled (`rowsecurity=False`) on both tables with zero policies defined; `MergeApproval`'s only relevant constraints are `MergeApproval_TenantID_fkey` (validates the row's own `TenantID` points at a real `Tenant` — says nothing about matching the two papers' tenants to each other) and `chk_merge_approval_not_self` (checks `SurvivorPaperID != LoserPaperID`, not tenant); zero database functions matching `%tenant%` exist | Live `information_schema.triggers`/`pg_tables`/`pg_policies`/`pg_constraint`/`pg_proc` queries, this phase |
| 8 | Can the executor reach a write after receiving a cross-tenant pair? | **Yes, before this phase's fix** — traced the full preflight chain (`fetch_current_state()` → `build_papers_dict_for_pure_functions()` → `pair_confidence()`/`hard_exclusion_reason()`) and confirmed none of them fetches or checks `TenantID` at all; nothing in the chain would have stopped a cross-tenant pair from reaching `merge_group()` | Direct code read of every function in the actual preflight call chain |

`merge_plan_generator.py::compute_classification()`'s `tenant_blocked` check exists (confirmed, line 453–454) but was confirmed, by direct `grep`, **never called from `merge_executor.py`** — it only runs during plan generation, a step `execute_approved_merge()` never invokes.

**No existing `validate_same_tenant()`-shaped helper was found anywhere in the repository** (`grep -rn "def validate_same_tenant\|def.*same_tenant\|def.*tenant_match\|def.*check_tenant"` — zero matches).

### Verdict: **B) DEFECT_CONFIRMED**

**Earliest correct enforcement boundary**: `create_pending_approval()` — this is the earliest point at which a `SurvivorPaperID`/`LoserPaperID` pair is committed to a persistent, actionable record. A second, independent boundary is also required at `execute_approved_merge()` — see Task B.

---

## Task B — Threat Model (Read-Only Trace)

Assuming a caller supplies `survivor` from Tenant A, `loser` from Tenant B:

```
create_pending_approval()  →  (BEFORE THIS FIX) no check at all — a MergeApproval
                                row is created, Status=PENDING, representing an
                                invalid cross-tenant pair as if it were legitimate.

approve_pending()          →  (BEFORE THIS FIX) no check — Status becomes APPROVED.
                                The invalid pair is now a fully "approved" merge.

execute_approved_merge()   →  fetch_current_approval() finds it (nothing about the
                                lookup itself is tenant-aware)
                            →  approval_matches_pair() passes (checks direction only)
                            →  Status == APPROVED passes
                            →  lock_pair_rows() LOCKS BOTH ROWS (no tenant filter)
                            →  fetch_current_state() FETCHES BOTH ROWS (no tenant filter)
                            →  (BEFORE THIS FIX) nothing stops here
                            →  validate_against_plan() — fingerprint/duplicate/DOI
                                checks, NONE of which compare TenantID between the
                                two sides (TenantID is only a FINGERPRINT INPUT,
                                meaning a cross-tenant pair's fingerprint would
                                simply be internally consistent with itself, not
                                flagged as invalid)
                            →  idempotency / JournalID / AuthorNameRaw / dependency
                                checks — none reference tenant
                            →  merge_group() — CHILD REMAPPING BEGINS. This is the
                                first point that would have WRITTEN cross-tenant
                                data (an Authors/Citations/etc. row moved across a
                                tenant boundary), followed by the AuditLog INSERT
                                and the ResearchPaper DELETE.
```

**Findings:**

- **The first point where the operation currently stops, before this fix**: nowhere. No point in the entire chain, from approval creation through to `merge_group()`'s actual writes, contained a tenant check.
- **The first point where it could write cross-tenant data**: `merge_group()`'s child-table remap statements (`Authors`, then `Citations`, etc.) — the very first `INSERT`/`UPDATE`/`DELETE` in the entire chain.
- **Could an approval itself be incorrectly created for a cross-tenant pair?** Yes, before this fix — confirmed directly (Task A, item 3).
- **Must a same-tenant check exist in one place or multiple layers?** **Multiple** — confirmed necessary, not merely cautious: a check only at `execute_approved_merge()` would still allow an invalid `MergeApproval` row to exist (visible to reviewers, occupying the `uq_merge_approval_identity` namespace, auditable as if legitimate) even though it could never execute; a check only at `create_pending_approval()` would leave `execute_approved_merge()` with no independent defense against a row that predates the fix, was created by a future caller that bypasses `create_pending_approval()`, or was manually inserted. Each layer protects a genuinely distinct entry path — this is not redundant defense-in-depth for its own sake.

---

## Task C — Minimal Fix Design

**Proposed files, stated before implementation, exactly as required:**

1. **`backend/tools/merge_execution_safety.py`** — add two new primitives, placed immediately after `reject_self_merge()` (the closest existing analog in both shape and purpose):
   - `validate_same_tenant(survivor_tenant_id, loser_tenant_id)` — **pure, no DB access**, mirroring `reject_self_merge()`'s exact `(ok, reason)` return shape, so it is trivially reusable at both boundaries without either boundary needing to know how the other fetches its data.
   - `fetch_paper_tenant_ids(cur, survivor_id, loser_id)` — a thin, `SELECT`-only DB wrapper, needed only by `create_pending_approval()` (which has no `ResearchPaper` row in hand yet). `execute_approved_merge()` needs no equivalent new query — `TenantID` is already part of `RP_COLUMNS`, already fetched onto `winner_row`/`loser_row` by the existing `fetch_current_state()` call.

   *Why this file*: it is the established, existing home for every other pure safety primitive this project has built (`reject_self_merge`, `build_lock_order`, `is_doi_claimed_elsewhere`) — the natural, minimal-surprise location, not a new module.

2. **`backend/tools/merge_approval.py`** — `create_pending_approval()` gains one new check, placed after the existing permission check and before the duplicate-approval lookup (so an invalid pair costs the cheapest possible query, not two).

3. **`backend/tools/merge_executor.py`** — `execute_approved_merge()` gains one new check, placed immediately after the existing "both rows exist" check (step 6/7) and before `validate_against_plan()` — using data already in memory, zero new query.

**Why not `merge_plan_generator.compute_classification()`'s existing `tenant_blocked` logic, copied verbatim?** That function returns a whole-classification verdict (`"BLOCKED"`, a list of blocker strings) shaped for plan generation's own reporting needs — reusing it directly would mean either constructing a full, unrelated plan-classification call just to extract one boolean, or duplicating its comparison logic inline at both new call sites. A small, purpose-built pure function is the smaller, more directly reusable primitive, matching the task's own explicit preference.

**Why not `merge_group()`?** Traced directly (Task B): by the time `merge_group()` is ever called, `execute_approved_merge()`'s own new check has already run and would have returned before reaching that call. No evidence supports `merge_group()` as a necessary enforcement boundary — it is unreachable with a cross-tenant pair once the executor's own check is in place, so modifying it would be unnecessary, unjustified surface area.

**Design properties confirmed against the task's requirements**:
- Rejects missing paper rows safely — `validate_same_tenant()` treats `None` on either side as a rejection (`"one or both papers have no resolvable TenantID"`), not a silent pass; `None == None` is explicitly **not** treated as a match.
- Rejects cross-tenant pairs — the core comparison.
- Rejects before child-table mutation — placed before any write statement in both call sites.
- Deterministic, testable — pure function, zero DB access, zero side effects.
- Performs no writes itself — confirmed by construction.
- Does not depend on plan generation having run — confirmed; neither call site touches `merge_plan_generator.py`.

---

## Task D — Implementation

**`merge_execution_safety.py`** (excerpt):

```python
def validate_same_tenant(survivor_tenant_id, loser_tenant_id):
    if survivor_tenant_id is None or loser_tenant_id is None:
        return False, "one or both papers have no resolvable TenantID -- cannot verify tenant match"
    if survivor_tenant_id != loser_tenant_id:
        return False, (f"cross-tenant pair: survivor TenantID={survivor_tenant_id!r}, "
                        f"loser TenantID={loser_tenant_id!r} -- a merge must never cross tenants")
    return True, None

def fetch_paper_tenant_ids(cur, survivor_id, loser_id):
    cur.execute('SELECT "PaperID", "TenantID" FROM "ResearchPaper" WHERE "PaperID" = ANY(%s)',
                ([survivor_id, loser_id],))
    by_id = {pid: tid for pid, tid in cur.fetchall()}
    return by_id.get(survivor_id), by_id.get(loser_id)
```

**`merge_approval.py::create_pending_approval()`** — inserted between the permission check and the duplicate lookup:

```python
survivor_tenant_id, loser_tenant_id = fetch_paper_tenant_ids(cur, survivor_id, loser_id)
ok, reason = validate_same_tenant(survivor_tenant_id, loser_tenant_id)
if not ok:
    return OperationResult(ok=False, error=f"cross_tenant_rejected: {reason}")
```

**`merge_executor.py::execute_approved_merge()`** — inserted immediately after the existing "both rows exist" check:

```python
tenant_ok, tenant_reason = validate_same_tenant(winner_row.get("TenantID"), loser_row.get("TenantID"))
if not tenant_ok:
    return ExecutionResult(False, EXEC_BLOCKED_CROSS_TENANT, {"reason": tenant_reason})
```

Plus one new constant, `EXEC_BLOCKED_CROSS_TENANT = "CROSS_TENANT"`, added alongside the other `EXEC_BLOCKED_*` constants.

**Requirements confirmed satisfied:**
1. Cross-tenant approval creation fails before any `INSERT` — proven by `test_C_cross_tenant_rejection_zero_insert`.
2. Cross-tenant execution fails before child remapping / `AuditLog` write / `DELETE` / `EXECUTED` transition — proven by `test_G`/`test_H`/`test_I`/`test_J`.
3. Existing same-tenant behavior unchanged — all 235 pre-existing tests pass unmodified; `test_A`/`test_K` add explicit confirmation.
4. No new migration — confirmed; nothing about this fix touches schema.
5. No DOI-logic change — confirmed; neither modified file references `DOI`.
6. No change to `pair_confidence()`, `detect_groups()`, thresholds, or duplicate classification — confirmed; `dedup_papers.py` was not touched (`git diff --stat` unchanged from every prior phase since 4E).
7. No weakening of any existing check — confirmed; every insertion is purely additive, placed alongside (never replacing) existing checks; all 235 pre-existing tests pass exactly as before.

**Two enforcement points, explained**: boundary #1 (`create_pending_approval()`) protects against an invalid pair ever becoming a persistent, reviewable, "approved-looking" record at all. Boundary #2 (`execute_approved_merge()`) protects against an invalid `MergeApproval` row that predates this fix, or that some future caller creates by bypassing `create_pending_approval()` entirely — a distinct entry path boundary #1 cannot see. Task B's trace directly justifies both.

---

## Task E — Tests

**24 new tests, 0 modified, 0 removed.**

| Test | Requirement | Result |
|---|---|---|
| `ValidateSameTenantTests` (5 tests) | Pure-function correctness: same-tenant pass, different-tenant reject, either-side-`None` reject, both-`None` not-silently-matched | New this phase — `test_merge_execution_safety.py` |
| `FetchPaperTenantIdsTests` (5 tests) | Thin DB-wrapper correctness: both exist, cross-tenant, missing-survivor, missing-loser, both-missing | New this phase |
| `CrossTenantApprovalCreationTests::test_A_same_tenant_pair_existing_behavior_preserved` | Requirement A | New this phase |
| `::test_B_cross_tenant_pair_rejected` | Requirement B | New this phase |
| `::test_C_cross_tenant_rejection_zero_insert` | Requirement C | New this phase |
| `::test_D_missing_survivor_safe_failure` | Requirement D | New this phase |
| `::test_E_missing_loser_safe_failure` | Requirement E | New this phase |
| `CrossTenantExecutionTests::test_F_cross_tenant_approved_pair_blocked` | Requirement F | New this phase |
| `::test_G_block_before_any_child_table_write` | Requirement G | New this phase |
| `::test_H_block_before_auditlog_merge_insert` | Requirement H | New this phase |
| `::test_I_block_before_loser_delete` | Requirement I | New this phase |
| `::test_J_block_before_approval_executed` | Requirement J | New this phase |
| `::test_K_same_tenant_canary_shaped_pair_unchanged` | Requirement K | New this phase |
| `::test_zero_write_sql_at_all` | (supporting) | New this phase |
| `CrossTenantCheckReachabilityTests` (2 tests) | Static + behavioral proof the check is reachable from `execute_approved_merge()` itself (not merely `merge_plan_generator.py`) | New this phase — one `re.finditer`-based proof the function is actually *called*, not merely imported; one live-behavior proof via `_cross_tenant_scenario()` |

**Before this phase**: 235/235 passing. **After**: **259/259 passing** (24 new: 10 in `test_merge_execution_safety.py`, 5 in `test_merge_approval.py`, 9 in `test_merge_executor.py`). Zero regressions, zero pre-existing test modified.

```
test_dedup_papers.py             18/18   (unchanged)
test_merge_plan_generator.py     43/43   (unchanged)
test_merge_execution_safety.py   89/89   (79 + 10 new)
test_merge_approval.py           50/50   (45 + 5 new)
test_merge_executor.py           48/48   (39 + 9 new)
test_fk_lifecycle.py             11/11   (unchanged)
```

---

## Task F — Live Read-Only Validation

| # | Check | Result |
|---|---|---|
| 1 | `ApprovalID=1` reconfirmed intact and `EXECUTED` | `SurvivorPaperID=5232`, `LoserPaperID=5482`, `Status=EXECUTED`, `TenantID=1`, `ExecutionAuditLogID=1539` — byte-identical to every prior phase's read |
| 2 | Original canary result unchanged | `ResearchPaper` count `2030` (unchanged); survivor `5232` present, loser `5482` absent; `AuditLog` count `958` (unchanged) |
| 3 | Real cross-tenant pair, safely discoverable | **None exists — stated honestly, not forced.** `SELECT "TenantID", COUNT(*) FROM "ResearchPaper" GROUP BY "TenantID"` → `[(1, 2030)]`; `SELECT COUNT(*) FROM "Tenant"` → `1`. **This production database currently contains exactly one tenant.** There is no live cross-tenant pair to discover, by construction — the defect fixed this phase is a real, general-purpose correctness gap in code designed for a multi-tenant future, not a currently-exploitable condition against today's actual data |
| 4 | New enforcement verified against live data, read-only | `fetch_paper_tenant_ids(cur, 5232, 5482)` → `(1, None)` (loser correctly gone) → `validate_same_tenant()` → correctly rejects (missing-row reason, not a false tenant-mismatch claim). A real, live, same-tenant pair (`5329`, `5434`, from Phase 4S's own candidate list) → `(1, 1)` → `validate_same_tenant()` → correctly passes |
| 5 | No existing `MergeApproval` row represents a cross-tenant pair | Confirmed — the only row (`ApprovalID=1`) has `TenantID=1` stored, and the still-live survivor (`5232`) independently confirms `TenantID=1`; both Phase 4Q and Phase 4R already independently confirmed `TenantID=1` on the loser side before it was deleted |
| 6 | No unexpected data drift | `ResearchPaper` `2030`, `AuditLog` `958` — both exactly match the last known state from Phase 4S/4T, confirming zero writes occurred anywhere during this phase's investigation |

---

## Task G — Safety Accounting

- **Code files modified**: **3** — `backend/tools/merge_execution_safety.py`, `backend/tools/merge_approval.py`, `backend/tools/merge_executor.py`.
- **Code files created**: **0.**
- **Migration files modified/created**: **0.**
- **DB writes**: **0.**
- **Production DB writes**: **0.**
- **Network calls**: **0** beyond the production-database connections used for read-only investigation.
- **MergeApprovals created**: **0.**
- **Approvals changed**: **0.**
- **Papers merged**: **0.**
- **Papers deleted**: **0.**
- **DOI changes**: **0.**
- **`ApprovalID=1` changes**: **0** — read repeatedly, never written.
- **Tests added**: **24** (0 modified, 0 removed).
- **Full test-suite result**: **259/259 passing**, up from 235/235, zero regressions.
- **Live merge attempted**: **No.**

**Explicit confirmations, as required:**
- Phase 4U did **NOT** execute a second canary.
- Phase 4U did **NOT** touch the successful 5232/5482 merge state.
- Phase 4U did **NOT** perform a batch merge.

---

## Final Decision

### **A) CROSS-TENANT SAFETY ENFORCED AND VALIDATED — ready for a separate second-canary readiness audit**

Task A independently reproduced and confirmed Phase 4T's finding with direct code and live-schema evidence, not assumption. Task B traced the exact threat precisely, identifying `merge_group()`'s first write as the point of actual exposure and justifying two independent enforcement boundaries, not one. Task C's design reuses the smallest appropriate primitive shape (`reject_self_merge()`'s own pattern), avoids unnecessary surface area (`merge_group()` untouched, `merge_plan_generator.py` untouched), and satisfies every stated design constraint. Task D's implementation is minimal — two files gain one new check each, plus one shared primitive module — and every existing behavior (approval workflow, fingerprint binding, locking, idempotency, `JournalID`, `AuthorNameRaw`, dependency-gap checks, duplicate-detection logic) is confirmed unweakened by the full, unmodified pre-existing test suite still passing at 235/235. Task E adds 24 new, focused tests covering every required scenario plus an explicit reachability proof. Task F validates the fix against live data, read-only, and honestly reports that no real cross-tenant pair currently exists in production to exercise end-to-end — this is not a validation gap, since the fix's correctness was proven both by mocked tests (which can construct the scenario) and by exercising the real primitives against real, live single-tenant data (correctly passing).

Per your instructions, I am stopping here. Phase 4V is not started. No second canary was executed. `ApprovalID=1`'s state was never touched. Waiting for your explicit review and authorization.
