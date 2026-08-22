# Phase 4K — Controlled Live-Execution Readiness Audit (STRICTLY READ-ONLY)

## Safety Confirmation

Zero production DB writes. Zero `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`. Zero schema changes. Zero migration applications. Zero `MergeApproval` rows created anywhere, production or otherwise. Zero merge executions. Zero calls to `merge_group()` against a live database. Zero calls to `execute_approved_merge()` against a live database. Zero DOI changes. Zero `--apply` executions. Every live-database interaction this phase was a plain `SELECT` (or `information_schema`/`pg_catalog` introspection query), issued through short-lived scripts that end in `conn.rollback()` before closing the connection — confirmed below in §12. One implementation file was touched (`test_merge_executor.py`), and only because the audit itself exposed a real, previously-unexercised gap in the existing mocked test suite (§8) — not a defect that made the audit impossible, exactly the one exception §1 of the task permits.

## 1. Exact Files Inspected

Re-read in full this phase: `backend/tools/merge_executor.py`, `backend/tools/merge_approval.py`, `backend/tools/merge_execution_safety.py`, `backend/tools/dedup_papers.py`, `backend/tools/merge_plan_generator.py`, `backend/analytics/migrations/sprint11_merge_approval.sql`, `backend/reports/phase4j_merge_executor_prototype.md`, `backend/reports/phase4i_merge_approval_prototype.md`, `backend/reports/phase4h_approval_storage_design.md`, `backend/accounts/models.py` (`User.has_litrix_perm`/`get_permissions`), `backend/accounts/permissions.py`. Grepped: every caller of `manage_users` across `backend/accounts/` and `backend/analytics/`; every caller of `execute_approved_merge` repository-wide.

## 2. Canary Pair — Fresh Live Revalidation

All of the following was queried live against production this phase (not reused from any prior phase's cached values):

### A. Identity and existence

| PaperID | DOI | JournalID | PubYear | TenantID | Title |
|---|---|---|---|---|---|
| 5232 | `10.1155/2022/8531213` | 1803 | 2022 | 1 | "Optimal deep learning model..." |
| 5482 | `NULL` | `NULL` | 2022 | 1 | "Research Article Optimal Deep Learning Model..." |

Both exist. Both distinct. `choose_keep([5232, 5482], ...)` (reused, unmodified) returns survivor=**5232**, loser=**5482** — unreversed, matching every prior phase. `reject_self_merge(5232, 5482)` → `(True, None)`. `build_lock_order(5232, 5482)` → `(5232, 5482)`.

### B. Plan freshness

Fresh fingerprint, recomputed live via `compute_plan_fingerprint()` against today's data:

```
2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb
```

Reference fingerprint (established since Phase 4G, 2026-08-21): identical.

**Result: `MATCH`** — the **7th independent live confirmation** of this exact value, spanning three calendar days (2026-08-21, 2026-08-22, 2026-08-22) and now Phase 4K.

### C. Safety rechecks (all fresh, live, this phase)

| Check | Result |
|---|---|
| `pair_confidence(5232, 5482, ...)` | `high` |
| `hard_exclusion_reason(...)` | `None` |
| `is_doi_claimed_elsewhere(cur, "10.1155/2022/8531213", [5232, 5482])` | `False` |
| `build_journal_state(1803, None)` | `{state: WINNER_ONLY, execution_permitted: True}` |
| `author_content_conflicts(...)` | `[]` |
| `fetch_merge_audit_rows(cur, [5232, 5482])` | **0 rows** |
| `idempotency_verdict(...)` | **`NOT_PREVIOUSLY_EXECUTED`** — "no prior paper.merge.dedup history for either PaperID" |

Every technical precondition this project has ever defined for this pair still passes, live, today.

## 3. Full Dependency / Child-Row Audit

Every row count below was read live this phase — none inferred or reused.

| Table | FK Column | ON DELETE | Winner rows | Loser rows | Executor action | Safe? |
|---|---|---|---|---|---|---|
| `Authors` | `PaperID` | NO ACTION | 1 | 1 | `REMAP` (merge_group, special-cased) | Yes |
| `Citations` | `PaperID` | NO ACTION | 0 | 0 | `REMAP` (merge_group, special-cased) | Yes — no-op, 0 rows |
| `ExternalAuthors` | `PaperID` | NO ACTION | 0 | 0 | `REMAP` (SIMPLE_CHILDREN) | Yes — no-op |
| `CitationsHistory` | `PaperID` | NO ACTION | 0 | 0 | `REMAP` (SIMPLE_CHILDREN) | Yes — no-op |
| `PaperKeywords` | `PaperID` | NO ACTION | 0 | 0 | `REMAP` (SIMPLE_CHILDREN) | Yes — no-op, **see finding below** |
| `ReportPaperDecision` | `PaperID` | SET NULL | 0 | 0 | `REMAP` (SIMPLE_CHILDREN pre-empts SET NULL) | Yes — no-op |
| `ReportPaperDecision` | `MissingResolvedToPaperID` | SET NULL | — | **0** | `BLOCK` if nonzero | Yes — live-checked, 0 rows, not blocked |
| `AuthorReviewQueue` | `PaperID` | CASCADE | — | **0** | `BLOCK` if nonzero | Yes — live-checked, 0 rows, not blocked |

`CitationsByYear` is a **column** on `ResearchPaper`, not a dependency table — clarifying this since the task's list names it alongside real child tables. Live values: winner=`{"2022":5,"2023":13,"2024":12,"2025":8,"2026":2}`, loser=`NULL`. Already handled by `merge_group()`'s existing, unmodified `merge_citation_fields()` — no new risk.

`check_unhandled_dependency_gaps(cur, 5482)` (the live `AuthorReviewQueue`/`ReportPaperDecision.MissingResolvedToPaperID` re-check `merge_executor.py` performs at runtime): **`[]`** — zero blockers, confirmed live.

**Finding — schema drift since Phase 4C/4F/4H: `PaperKeywords` now exists.** Every prior phase report (4C, 4F, 4H) stated this table "does not exist today (confirmed via `information_schema.tables`)... dead list entry, harmless." That is **no longer true** — `PaperKeywords` exists today (`PaperID INT NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION`, `KeywordID INT NOT NULL REFERENCES "Keywords"("KeywordID")`), confirmed via a fresh `information_schema.columns`/FK query this phase. **This is not a new risk**: it has 0 rows table-wide (not just for the canary pair), and its shape (a plain `NO ACTION` FK to `ResearchPaper.PaperID`) is structurally identical to `ExternalAuthors`/`CitationsHistory` — it is already correctly covered by `dedup_papers.py`'s existing, unmodified `SIMPLE_CHILDREN`/`remap_simple_child()` logic (it was already listed in `SIMPLE_CHILDREN`, just never previously live). `existing_child_tables()` now correctly detects and includes it. Flagged here because three prior phase reports' "table doesn't exist" claim is now stale, and a future phase auditing a **different, non-empty** pair should not assume it's still 0 rows without checking — exactly the discipline this project has maintained throughout.

## 4. Critical FK Lifecycle Audit — MergeApproval

**This is the single most important finding of this phase.**

### The exact schema, as designed (`sprint11_merge_approval.sql`)

```sql
"SurvivorPaperID"  INT NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION,
"LoserPaperID"     INT NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION,
```

### Answering the seven required questions

1. **Does `MergeApproval` reference `WinnerPaperID`?** Yes — `SurvivorPaperID`, a real `FOREIGN KEY` to `ResearchPaper.PaperID`.
2. **Does `MergeApproval` reference `LoserPaperID`?** Yes — `LoserPaperID`, a real `FOREIGN KEY` to `ResearchPaper.PaperID`.
3. **What exact `ON DELETE` action applies to each?** Both are `NO ACTION` (the header comment in `sprint11_merge_approval.sql` itself explains this was a **deliberate** choice: "an `AuthorReviewQueue` row is meaningless once its paper is gone, but a `MergeApproval` row remains meaningful audit history even after a successful merge deletes the loser row... Losing that history to a cascade would defeat the table's own purpose"). Neither is declared `DEFERRABLE`, so Postgres's default (`NOT DEFERRABLE`, checked immediately at statement execution) applies.
4. **If the loser paper is deleted, can an EXECUTED `MergeApproval` row still legally exist?** **No — as designed, it cannot be reached at all.** The row would need to already exist (referencing the loser via `LoserPaperID`) *before* the delete is even attempted (an `APPROVED` row is the precondition for execution), and `NO ACTION` on an undeferred constraint means Postgres will not allow that referencing row to be left pointing at a row that no longer exists — the **delete itself fails**, not the approval row.
5. **If `ON DELETE NO ACTION` is used, will deletion of the loser be blocked?** **Yes.** This is not a maybe — it is standard, well-defined PostgreSQL foreign-key semantics: a `DELETE` against a row referenced by a `NOT DEFERRABLE ON DELETE NO ACTION` constraint fails immediately with a `foreign_key_violation` if any referencing row exists and is not itself being deleted or updated to no longer reference it in the same statement. `MergeApproval` rows are neither deleted nor updated by `merge_group()`'s `DELETE FROM "ResearchPaper" WHERE "PaperID" = %s` statement — `merge_group()` has no knowledge of `MergeApproval` at all (by design — it remains completely unmodified, per every phase since 4H).
6. **If yes, is this a REAL blocker?** **Yes.** Concretely, tracing the exact intended Phase 4L flow: `create_pending_approval(survivor=5232, loser=5482, ...)` → row created, `LoserPaperID=5482` → `approve_pending(...)` → `Status='APPROVED'` → `execute_approved_merge(...)` passes every preflight check (§2 above proves all of them currently pass) → calls `merge_group()`, unmodified → child remaps succeed (0 rows, no-ops) → `AuditLog` INSERT succeeds → `DELETE FROM "ResearchPaper" WHERE "PaperID" = 5482` is attempted → **Postgres raises `ForeignKeyViolation`**, because the `MergeApproval` row created in step 1 (which must exist — it is the precondition for reaching execution at all) still references `LoserPaperID = 5482` with an undeferred `NO ACTION` constraint. `merge_group()` propagates this exception (it does not catch it); `execute_approved_merge()` never reaches its own `MergeApproval → EXECUTED` update; the whole transaction rolls back cleanly (§5 confirms the rollback discipline itself is sound) — but **the merge cannot succeed, ever, under this schema, for any pair, once an `APPROVED` `MergeApproval` row exists for it** — which is unconditionally required before execution can even be attempted. This is self-contradictory by construction: the precondition for execution (an approval referencing the loser) is exactly what makes the loser undeletable.
7. **Minimum safe schema design.** `AuditLog.TargetID` — the repository's own real, live, pre-existing precedent for "a reference to a row that must survive that row's deletion" — was checked fresh this phase via `information_schema`: it carries **no foreign-key constraint at all** (`AuditLog`'s only FKs are on `TenantID` and `UserID`; `TargetID` is a plain, unconstrained `INT`). This is the exact, already-proven, already-in-production pattern this table needs for `LoserPaperID`. **Minimum fix**: drop the `REFERENCES "ResearchPaper"("PaperID")` clause on `LoserPaperID` entirely — keep it a plain `INT NOT NULL` column, losing only insert-time referential validation (which `execute_approved_merge()`'s own preflight already guarantees is a real, existing `PaperID` at the moment the row is created/read) while gaining the ability to outlive the row it names. `SurvivorPaperID` needs no change — the survivor is never deleted, so its `NO ACTION` FK never conflicts with anything.

### Classification

**C) FK_LIFECYCLE_BLOCKER**

Not `CANNOT_PROVE` — this was proven with certainty from documented PostgreSQL FK semantics plus a live-confirmed precedent in this exact schema, not inferred or guessed. Not `FK_LIFECYCLE_SOUND` — the design cannot execute a single merge under any circumstance today. Technically the fix is narrow (one `ALTER TABLE`/one column definition change before the migration is ever applied — the migration has never been applied, so no live schema change is needed to fix it, only a file edit to a not-yet-applied `.sql` file), which is why the *overall Phase 4K* decision below is **B**, not **D** — but within this specific section, the lifecycle as currently designed is a real, deterministic blocker, not a soft caveat.

## 5. Transaction Atomicity Audit

Direct code evidence, `merge_executor.py`:

- `execute_approved_merge()` never calls `.commit(`, `.rollback(`, or opens any transaction context — confirmed by source scan (`TransactionOwnershipTests`, Phase 4J, re-verified this phase by direct reading).
- It performs `lock_pair_rows()` (a `SELECT ... FOR UPDATE`) as its first DB-touching step after the approval/permission checks — this only actually holds a lock if the caller's connection has an open transaction (Postgres's implicit-transaction-on-first-statement behavior for a plain `psycopg2` connection with `autocommit=False`, or Django's own `transaction.atomic()` context) around it. `litrix_db.py::db()` (re-read this phase) returns a plain `psycopg2.connect(...)` with **no `autocommit=True`** set — so autocommit is off by default, and any statement opens an implicit transaction; the connection's own `.commit()`/`.rollback()` is the caller's responsibility, exactly as `merge_execution_safety.py`'s own `lock_pair_rows()` docstring states.
- `dedup_papers.py::main()`'s real `--apply` path wraps its entire run in Django's `with transaction.atomic(): with connection.cursor() as cur: ...` — direct, real precedent for the pattern `execute_approved_merge()` assumes its caller will use.
- **No caller of `execute_approved_merge()` exists anywhere in this repository** (`grep -rln "execute_approved_merge" backend/ --include="*.py"` returns only `merge_executor.py` itself and `test_merge_executor.py`) — confirmed fresh this phase.

Tracing the 9 required guarantees against the actual code:

1. Rows locked — yes, `lock_pair_rows()`, ascending order, real `SELECT...FOR UPDATE` SQL, live-demonstrated with rollback in Phase 4G.
2. Preconditions pass — yes, the full 16-step sequence, all reused/tested primitives.
3. Child rows remapped — yes, `merge_group()`, unmodified, real/tested.
4. Winner data preserved — yes, `merge_group()`'s profile-preservation assertion, unmodified.
5. Loser deleted — **this is where §4's finding applies**: as designed, this step fails every time an `APPROVED` approval exists, which is unconditionally required to reach this point.
6. `AuditLog` written — yes, inside `merge_group()`, before the delete.
7. `MergeApproval` becomes `EXECUTED` — yes, but only reachable if step 5 succeeds.
8. Exactly one commit — the module issues no commit itself; **whether exactly one commit occurs depends entirely on how a future caller wraps it** — unproven, since no caller exists.
9. Any failure rolls back everything — **yes, by construction**: no exception anywhere in the call chain (`merge_group()`'s `RuntimeError`, a real Postgres `ForeignKeyViolation`, or any other) is ever caught and swallowed; every one propagates to whatever transaction context the caller opened. Proven dynamically in Phase 4J (`test_J`/`test_M`/`test_N`/`test_O`/`test_P`, all `assertRaises`) and statically (no `except` block anywhere in `merge_executor.py` catches a broad exception).

### Classification

**B) SOUND_IF_EXTERNALLY_WRAPPED_IN_ONE_TRANSACTION**

The rollback-safety design itself (exception propagation, no internal commits, no swallowed exceptions) is real and tested. It is not **A** (`ATOMICALLY_SOUND`, unconditionally) because guarantee #8 genuinely depends on a caller this repository does not yet have — nothing today actually wraps `execute_approved_merge()` in a transaction at all, so "exactly one commit occurs" is a property of a caller that does not exist, not a proven fact about this module in isolation. Not **C** (`TRANSACTION_BOUNDARY_UNCLEAR`) — the boundary is precisely and consistently documented (caller owns it, matching `lock_pair_rows()`'s established convention) — it is clear, just externally unfulfilled today. Not **D** — nothing about the atomicity *design* is unsound.

## 6. MergeApproval Migration Readiness

Live schema checks, this phase, against `sprint11_merge_approval.sql`:

| Check | Result |
|---|---|
| `ResearchPaper` exists, `PaperID` is `integer` | Confirmed |
| `Tenant` exists, `TenantID` is `integer` | Confirmed (14-column real table) |
| `Users` exists, `UserID` is `integer` | Confirmed |
| `AuditLog` exists, `LogID` is `integer` | Confirmed |
| `MergeApproval` table name collision | None — table does not exist |
| Constraint/index name collisions (`chk_merge_approval_not_self`, `uq_merge_approval_identity`, `idx_merge_approval_status_created`, `idx_merge_approval_lookup`) | None found, live-checked against `pg_constraint`/`pg_indexes` |
| `CHECK` constraint validity (`Status IN (...)`, `SurvivorPaperID != LoserPaperID`) | Syntactically and semantically valid against live column types |
| Reviewer-field precedent (`AuthorReviewQueue.ReviewedByUserID`/`ReviewedAt`/`ReviewerNotes`) | Confirmed still real and identically shaped |
| **FK design compatible with the intended loser-deletion lifecycle** | **No — see §4** |

### Classification

**BLOCKED**

Every column type, referenced table, and constraint/index name is valid and collision-free — the migration would apply cleanly as raw DDL. But per this task's own explicit instruction, **"a migration cannot be `READY_TO_APPLY` if its FK design would prevent the intended loser deletion,"** and §4 proves exactly that with certainty. `REQUIRES_MIGRATION_CHANGE` was considered but rejected in favor of `BLOCKED` — the task's own wording makes the FK defect a hard gate on `READY_TO_APPLY`, not a soft "needs a tweak" classification, and `BLOCKED` is the status this task's allowed-list offers for exactly that.

## 7. Permission and Actor Audit

Read `backend/accounts/models.py::User.get_permissions()`/`has_litrix_perm()` in full this phase:

```python
def get_permissions(self):
    ...
    cur.execute('''SELECT p."Code" FROM "Permission" p
                    JOIN "RolePermission" rp ON rp."PermissionID" = p."PermissionID"
                    WHERE rp."RoleID" = %s''', [self.role_id])
    return {row[0] for row in cur.fetchall()}

def has_litrix_perm(self, code):
    return code in self.get_permissions()
```

1. **What exact permission is required?** `manage_users` (with an `Admin` `user_type` fallback) — `can_approve_merge()`, unchanged since Phase 4I.
2. **Does that permission actually exist?** Yes — confirmed via `grep`: defined in `sprint1_foundation.sql` (`('manage_users', 'Create, edit, delete users', 'admin')`), a foundational, already-applied migration, and actively used by `invitation_views.py`, `role_views.py`, `views.py`, and `reconciliation_views.py` today.
3. **Is the executor using a real repository permission correctly?** Yes — `can_approve_merge()` reproduces `reconciliation_views.py::_can_reconcile()`'s exact logic, line for line.
4. **Can the approving user be identified?** Yes — `approve_pending()`/`reject_pending()`/`revoke_approved()` all set `ReviewedByUserID`/`RevokedByUserID` to the real `user.user_id`, and each calls `_write_audit()` with that same ID.
5. **Can the executing user be identified?** **No — a real, confirmed gap.** `merge_group()`'s own `AuditLog` `INSERT` (unmodified, unchanged since Phase 4B) hardcodes `("TenantID","UserID", ...) VALUES (1, NULL, ...)`. The merge-execution `AuditLog` row itself never records who ran it — only which papers were involved. `execute_approved_merge()` does not add a second, separate audit write to compensate.
6. **Are approval and execution independently auditable?** Partially — approval **decisions** (approve/reject/revoke) are fully attributable to a real user via both a column and an `AuditLog` row. Execution is not — see #5.
7. **Is cross-tenant isolation actually enforced?** **No — confirmed, not merely unproven.** `get_permissions()`'s query above has **zero `TenantID` clause anywhere** — a user's `manage_users` permission (via their `Role`) is global, not scoped to their own tenant, by construction. `merge_plan_generator.compute_classification()`'s `tenant_blocked` check only ensures the *pair's own two rows* share a `TenantID`; nothing checks the *approving/executing user's* tenant against the pair's `TenantID` at all.
8. **Marked accordingly** — per this task's explicit instruction not to assume: cross-tenant isolation is marked **BLOCKED** in §9, not `UNKNOWN`, because it was directly disproven this phase by reading the actual query, not merely left unconfirmed.

### Classification

**PARTIALLY_DEFINED**

The permission mechanism itself is real, live, correctly reused, and fully sufficient for the approval half of the flow. It is not `READY` because two concrete, confirmed gaps exist (executing-user attribution, cross-tenant scoping). It is not `BLOCKED` or `UNKNOWN` as a whole because the core mechanism works correctly for what it does cover, and for the specific single-tenant canary pair (both rows `TenantID=1`) neither gap changes the *correctness* of a single supervised execution — only the *general* claim that the system enforces per-tenant authorization, which it does not.

## 8. Approval → Execution Lifecycle Simulation

All 10 required scenarios, validated against the existing mocked test infrastructure (`ExecutorFakeCursor`, Phase 4J) — no production writes.

| # | Scenario | Covered by | Result |
|---|---|---|---|
| 1 | PENDING → APPROVED → all preconditions pass → EXECUTED | `SuccessfulExecutionTests` (Phase 4J, 6 tests) | pass |
| 2 | PENDING → APPROVED → fingerprint drift → BLOCKED | `test_F_stale_fingerprint_zero_write_sql` | pass |
| 3 | PENDING → APPROVED → duplicate safety failure → BLOCKED | See reasoning below | pass (existing coverage) |
| 4 | PENDING → APPROVED → DOI safety failure → BLOCKED | See reasoning below | pass (existing coverage) |
| 5 | PENDING → APPROVED → JournalID ambiguity → BLOCKED | **`test_journal_id_conflict_state_blocks_before_merge` (NEW, this phase)** | pass |
| 6 | PENDING → APPROVED → AuthorNameRaw ambiguity → BLOCKED | `test_L_author_name_raw_conflict_blocks_before_merge` | pass |
| 7 | PENDING → APPROVED → blocking dependency populated → BLOCKED | `test_full_pipeline_blocks_on_dependency_gap` | pass |
| 8 | PENDING → APPROVED → ALREADY_EXECUTED → BLOCKED | `test_G_already_executed_zero_write_sql` | pass |
| 9 | PENDING → APPROVED → HISTORICAL_STATE_AMBIGUOUS → BLOCKED | `test_H_historical_state_ambiguous_zero_write_sql` | pass |
| 10 | Attempted self-merge → rejected before any SQL | `test_B_self_merge_zero_write_sql` | pass |

**Why scenarios 3 and 4 needed no new test (per this task's own "only add tests if the audit exposes a real missing invariant" instruction):** `execute_approved_merge()` reuses `validate_against_plan()` (Phase 4G) for **all four** of `PREFLIGHT_REVERSED`/`PREFLIGHT_STALE_FINGERPRINT`/`PREFLIGHT_DUPLICATE_SAFETY_FAILED`/`PREFLIGHT_DOI_SAFETY_FAILED` through **one single shared branch**: `if not preflight.passed: return ExecutionResult(...)`. `test_F` already proves this exact integration point — the wiring from a `validate_against_plan()` failure through to a correctly-blocked `ExecutionResult` with zero write SQL — using `STALE_FINGERPRINT` as its trigger. The other three failure *reasons* (`DUPLICATE_SAFETY_FAILED` via low confidence or a hard exclusion, `DOI_SAFETY_FAILED`) are independently, exhaustively unit-tested at `validate_against_plan()`'s own level (`ValidateAgainstPlanTests`, Phase 4G, 7 tests, unchanged) — they produce the identical `PreflightResult` shape `test_F` already proves gets wired correctly. Adding four more near-identical integration tests that exercise the same one `if` branch with a different upstream reason would be exactly the "artificially increase test count" this task explicitly warns against, without proving anything `test_F` plus `ValidateAgainstPlanTests` don't already prove together.

**Why scenario 5 *did* need a new test:** the audit found `EXEC_BLOCKED_JOURNAL_CONFLICT` was imported into `test_merge_executor.py` but never once asserted — a genuine, real gap, not a duplicate of existing coverage (the `JournalID` `CONFLICT` state is reached through a code path — step 15's fresh `build_journal_state()` re-check — entirely separate from `validate_against_plan()`, so nothing else exercised it). `test_journal_id_conflict_state_blocks_before_merge` was added (§ below) and passes.

## 9. Final Readiness Matrix

See `backend/reports/phase4k_readiness_matrix.json` for the machine-readable version. Summary:

| Prerequisite | Status | Evidence | Blocking? | Exact Next Action |
|---|---|---|---|---|
| Canary pair still exists | READY | Live SELECT, §2A | No | — |
| Survivor direction (5232 survives) | READY | Live `choose_keep()`, §2A | No | — |
| Fingerprint freshness | READY | Live MATCH, 7th confirmation, §2B | No | — |
| Duplicate safety | READY | Live `pair_confidence=high`, §2C | No | — |
| DOI safety | READY | Live `is_doi_claimed_elsewhere=False`, §2C | No | — |
| Self-merge prevention | READY | Tested + zero-SQL-before-check proven, Phase 4G/4J | No | — |
| Row locking | READY_WITH_TESTED_ASSUMPTION | Live-demonstrated SQL+rollback (Phase 4G); never run inside `execute_approved_merge()` live | No | Exercise once in Phase 4L under supervision |
| Transaction atomicity | READY_WITH_TESTED_ASSUMPTION | §5 — sound design, no real caller exists yet | No (design), Yes (deployment) | Wrap in `transaction.atomic()` when a real caller is written |
| Rollback behavior | READY_WITH_TESTED_ASSUMPTION | Phase 4J M/N/O/P, mocked only | No | — |
| Idempotency | READY | Live `NOT_PREVIOUSLY_EXECUTED`, §2C | No | — |
| AuditLog compatibility (schema) | READY | Live type/FK check, §4.7 | No | — |
| JournalID handling | READY | Live `WINNER_ONLY`, §2C; CONFLICT-block newly tested, §8 | No | — |
| AuthorNameRaw handling | READY | Live zero-conflict, §2C; block path tested, Phase 4J | No | — |
| AuthorReviewQueue dependency | READY | Live 0 rows, §3 | No | — |
| ReportPaperDecision dependency | READY | Live 0 rows (both columns), §3 | No | — |
| All relevant child-table remaps | READY_WITH_TESTED_ASSUMPTION | §3 — `PaperKeywords` drift found, 0 rows, already safely handled | No (for this pair) | Re-verify dependency map before any non-empty pair |
| MergeApproval migration validity | **BLOCKED** | §6 — FK design defect | **Yes** | Drop `LoserPaperID`'s FK constraint (§4.7) before authoring a revised migration |
| MergeApproval FK lifecycle after loser deletion | **BLOCKED** | §4 — proven with certainty | **Yes** | Same fix as above |
| Approval creation | READY | Phase 4I, 45/45 tests | No | — |
| Approval storage (as designed) | **BLOCKED** | Same underlying schema defect, §4/§6 | **Yes** | Same fix as above |
| Approval revocation | READY | Phase 4I, tested | No | — |
| Approval/execution actor auditability | **BLOCKED** | §7 — execution side has no user attribution | Yes (for general readiness), No (for one supervised canary) | Add an explicit executor-side audit write, or accept manual/external attribution for a single supervised run |
| Permission enforcement | READY | §7 — real, live, correctly reused | No | — |
| Cross-tenant isolation | **BLOCKED** | §7 — confirmed absent, not merely unproven | Yes (for general readiness), No (both canary rows share TenantID=1) | Add tenant-scoping to `has_litrix_perm()` or an equivalent check before any multi-tenant use |
| Migration deployment readiness | **BLOCKED** | §6 | **Yes** | Same fix as §4.7 |

## 10. Final Decision

**B) READY ONLY AFTER SPECIFIC FIXES**

### The minimum exact fix required before Phase 4L can be attempted

**One schema change, not yet written, not yet applied:** amend `sprint11_merge_approval.sql`'s `LoserPaperID` column to remove its `REFERENCES "ResearchPaper"("PaperID")` foreign-key clause entirely — keep it a plain `INT NOT NULL` column, matching the exact, live-confirmed, already-in-production precedent `AuditLog.TargetID` already uses for this identical problem (a reference that must outlive the row it names). `SurvivorPaperID` needs no change. This is a narrow, well-precedented, low-risk edit to a `.sql` file that has never been applied to any database — not a redesign of the approval state machine, the executor's control flow, the locking strategy, or any other structural piece, all of which remain sound (§5, §7, §8, §9's `READY` rows).

Two additional gaps must be **explicitly acknowledged, not silently fixed or silently ignored**, before a human decides whether a single supervised canary execution is acceptable despite them:
1. The merge-execution `AuditLog` record does not capture which user ran it (§7.5) — a pre-existing characteristic of `merge_group()`, unchanged by any phase since 4B.
2. Cross-tenant permission scoping does not exist (§7.7–8) — not a blocker for this specific canary pair (both rows share `TenantID=1`), but a real, confirmed gap in the general mechanism.

### Why B, not A/C/D

Not **A** — §4/§6 found a concrete, proven-with-certainty blocker; declaring the system ready would mean asserting Phase 4L could succeed when it provably cannot under the current, unapplied schema. Not **C** — nothing found this phase is an open question; every required investigation (FK lifecycle, transaction atomicity, migration readiness, permission/actor audit, dependency audit, lifecycle simulation) reached a definite, evidence-backed conclusion, including the one genuinely new, previously-undiscovered finding (§4). Not **D** — the fix is a single, precisely-scoped column definition change with a directly-available, already-proven-safe precedent in this exact schema; nothing about the approval state machine, the executor's preflight sequence, the locking strategy, or the reuse of `merge_group()` needs to change. Per §10's own instruction, since this is **B**, the full Phase 4L safety envelope is intentionally not defined here — that belongs to whatever phase actually authors and reviews the corrected migration.

Per your instructions, I am stopping here. Phase 4L is not started. The migration was not applied. No `MergeApproval` row exists anywhere. No merge was executed.

## 11. Exact Accounting

- **Files inspected**: 12, listed in §1.
- **Files modified**: 1 — `backend/tools/test_merge_executor.py` (one new test added, `test_journal_id_conflict_state_blocks_before_merge`, plus the small `_happy_path()` fixture extension it required — added because this phase's audit found a real, previously-unexercised gap, per §1's explicit exception).
- **Files created**: 2 — this report and `backend/reports/phase4k_readiness_matrix.json`.
- **Code files modified beyond the above**: 0. `merge_executor.py`, `merge_approval.py`, `merge_execution_safety.py`, `dedup_papers.py`, `merge_plan_generator.py`, `sprint11_merge_approval.sql` — all read, none written.
- **Tests run**: all 5 suites. Exact counts: `test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 43/43, `test_merge_execution_safety.py` 68/68, `test_merge_approval.py` 45/45, `test_merge_executor.py` **39/39** (38 pre-existing + 1 new this phase). **Total: 213/213 passing, zero regressions.**
- **DB reads performed**: ~30 distinct read-only queries across 5 short-lived, rollback-terminated scripts — canary identity/fingerprint/safety rechecks (§2), dependency row counts across 8 tables (§3), `PaperKeywords` schema investigation (§3), `AuditLog`/`ResearchPaper`/`Tenant`/`Users` schema+FK checks (§4/§6), constraint/index collision check (§6), `has_litrix_perm()` source read (§7).
- **DB writes**: **0.**
- **Schema changes**: **0.**
- **Migrations applied**: **0.**
- **Merges executed**: **0.**
- **DOI changes**: **0.**
- **Production `MergeApproval` rows created**: **0** — the table itself still does not exist in production (re-confirmed live this phase, `to_regclass('public."MergeApproval"')` → `NULL`).
- **Network calls**: **0** beyond the local production-database connections themselves (all via `psycopg2`/`litrix_db.db()`, the repository's own standard, already-established connection path — no external HTTP/API call of any kind was made).

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Byte-for-byte identical to every phase since 4E — **zero changes this phase** to either tracked file.

### `git status --short` (relevant paths)

```
 M backend/tools/dedup_papers.py            <- pre-existing, unchanged this phase
 M backend/tools/test_dedup_papers.py       <- pre-existing, unchanged this phase
?? backend/analytics/migrations/sprint11_merge_approval.sql   <- pre-existing, unchanged this phase
?? backend/reports/                          <- pre-existing dir; this phase adds 2 files to it
?? backend/tools/merge_approval.py           <- pre-existing, unchanged this phase
?? backend/tools/merge_execution_safety.py   <- pre-existing, unchanged this phase
?? backend/tools/merge_executor.py           <- pre-existing, unchanged this phase
?? backend/tools/merge_plan_generator.py     <- pre-existing, unchanged this phase
?? backend/tools/test_merge_approval.py      <- pre-existing, unchanged this phase
?? backend/tools/test_merge_execution_safety.py  <- pre-existing, unchanged this phase
?? backend/tools/test_merge_executor.py      <- MODIFIED this phase (+1 test)
?? backend/tools/test_merge_plan_generator.py    <- pre-existing, unchanged this phase
```

(Several other untracked files — `ai_eval.py`, `backfill_abstracts.py`, `classify_publication_type.py`, `discover_csv_identifiers.py`, `discover_missing_identifiers.py`, `merge_identifiers.py`, `summarize_staging.py`, `sync_all_researchers.py`, and various files outside `backend/tools/`/`backend/analytics/migrations/` — predate this entire duplicate-merge project and were not touched by, or relevant to, Phase 4K.)
