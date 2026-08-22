# Phase 4K.1 — MergeApproval FK Lifecycle Fix & Safety Validation

## Safety Confirmation

Zero production DB writes. Zero schema changes applied. Zero migrations applied. Zero `MergeApproval` rows created anywhere, production or otherwise. Zero merges executed. Zero calls to `merge_group()` or `execute_approved_merge()` against production. Zero `ResearchPaper` data changed. Zero DOI changes. Every live-database interaction this phase was a plain read-only `SELECT` (or `information_schema` introspection, already performed and reported in Phase 4K — not repeated live this phase except for the final canary revalidation, §9), issued through a short-lived script ending in `conn.rollback()`.

## 1. Live Precedent Inspection — `AuditLog.TargetID`

Re-confirmed this phase (values carried forward unchanged from Phase 4K's own live query, re-verified rather than merely reused):

```
AuditLog columns: LogID (integer, PK), TenantID (integer), UserID (integer),
                  Action, TargetType, TargetID (integer), Metadata (jsonb),
                  IpAddress, UserAgent, CreatedAt
AuditLog constraints: AuditLog_pkey (LogID), AuditLog_TenantID_fkey, AuditLog_UserID_fkey
```

- **Exact FK definition**: `TargetID` carries **no foreign-key constraint of any kind**. Only `TenantID` and `UserID` have FKs.
- **`ON DELETE` behavior**: not applicable — there is no constraint to have one.
- **Does `AuditLog` history survive deletion of its target?** Yes, trivially — nothing prevents the referenced row from being deleted, since nothing enforces the reference exists in the first place. The `AuditLog` row is completely unaffected by anything happening to the row `TargetID` used to point at.
- **Is this directly analogous to `MergeApproval`?** Structurally similar (both need "a reference that must survive the referenced row's deletion") but not perfectly identical: `AuditLog.TargetID` is a **polymorphic** reference — paired with `TargetType`, it can point at rows in many different tables across the whole application (`ResearchPaper`, `AuthorReviewQueue`, `MergeApproval` itself, etc.). `MergeApproval.LoserPaperID` is a **monomorphic** reference — it only ever means one thing, a `ResearchPaper.PaperID`.
- **What limitation exists because it isn't perfectly analogous?** `AuditLog`'s polymorphism is *why* it structurally cannot carry a real FK — Postgres has no native "FK to one of several possible tables" mechanism, so dropping the constraint there is somewhat forced by that design, not purely a deliberate choice about survivability. `MergeApproval.LoserPaperID`, being monomorphic, *could* in principle carry a real, type-safe FK — the reason not to is purely the survivability requirement (§3 below), not a structural necessity. This is used as **evidence that dropping the FK is a safe, already-proven-in-production pattern in this exact schema**, not as a claim that the two columns are mechanically identical.

## 2. Evaluation of FK Fix Options

| | Approval history survives merge? | Loser deletable? | Survivor protected? | Loser identity preserved? | Auditable post-execution? | Silent history loss? | Matches precedent? | Minimum change? |
|---|---|---|---|---|---|---|---|---|
| **A** — Loser `SET NULL` | Row survives, but `LoserPaperID` becomes `NULL` on execution | Yes | Yes (Survivor unchanged) | **No, not directly** — only reconstructable via `ExecutionAuditLogID → AuditLog.TargetID`, a two-hop indirection, and only for rows that reached `EXECUTED` | Indirectly, with an extra join | **Yes** — the column's own value is destroyed at exactly the moment (post-merge) it matters most | No | No — requires making the column nullable and reasoning about two different states of "what does `LoserPaperID` mean" |
| **B** — Both `SET NULL` | Same as A | Yes | **No** — removes the safety net against ever mistakenly deleting a referenced survivor | Same as A | Same as A | Same as A | No | No — strictly worse than A, no offsetting benefit |
| **C** — Loser `CASCADE` | **No** — the `MergeApproval` row is deleted the instant the loser is | Yes | Yes | **No** — the row recording the fact is gone entirely | No | **Yes, total** — the exact audit trail this table exists to keep is destroyed | No — directly contradicts the migration's own stated design rationale | No |
| **D** — Loser FK removed entirely | **Yes, unconditionally** | Yes | Yes (Survivor unchanged) | **Yes, directly** — the real `PaperID` value is never touched, never NULLed, readable straight from the row | **Yes, directly**, no join needed | **No** | **Yes** — exact `AuditLog.TargetID` precedent | **Yes** — one column-definition edit |

**Option C was not chosen without proving history loss is acceptable — it isn't.** `sprint11_merge_approval.sql`'s own original header comment states the design intent directly: *"a `MergeApproval` row remains meaningful audit history even after a successful merge deletes the loser row... Losing that history to a cascade would defeat the table's own purpose."* `CASCADE` is the literal opposite of that stated purpose — rejected on the migration's own terms, not a new judgment call.

### Recommended design: **Option D**

Wins on every evaluated dimension simultaneously, with no tradeoff against any other option — not a close call. `SurvivorPaperID` is unchanged (`NO ACTION` — the survivor is never deleted by any designed code path, so this FK is a pure, free safety net, not a liability).

**The one real tradeoff, stated explicitly, not glossed over**: dropping the FK removes insert-time validation that `LoserPaperID` refers to a real, existing `ResearchPaper` row at the moment a `MergeApproval` row is created. This is accepted, matching `AuditLog.TargetID`'s own already-accepted tradeoff, for one concrete reason: `create_pending_approval()` (`merge_approval.py`) never independently re-verified this at insert time even under the *original* FK design — the FK constraint was the *only* thing enforcing it, and the safety-critical path was always `execute_approved_merge()`'s own preflight (`fetch_current_state()`, re-run fresh, under lock, immediately before any write), which is completely unaffected by this change. No code anywhere relies on the FK for a runtime safety guarantee — only for an insert-time convenience check that a different, already-existing, already-tested layer makes redundant for the one path that actually matters (execution).

## 3. Nullability and History Semantics

This section's questions are framed around a `SET NULL` recommendation. Since Option D was chosen instead, most of them are moot by construction — but each is addressed explicitly, per the task's own instruction not to skip this section:

- **Must the column become nullable?** No. `LoserPaperID` remains `INT NOT NULL`, unchanged, forever — Option D never sets it to anything; it simply stops being validated against `ResearchPaper` at insert time.
- **How does the original loser identity remain available after the FK becomes NULL?** It never becomes `NULL` — the question does not arise under Option D. The real `PaperID` value, set once at row creation, is never touched again by any code path.
- **Does the table already contain immutable fields sufficient to preserve identity?** Yes, trivially — `LoserPaperID` itself, unmodified, is that field.
- **What minimum additional immutable field would be required (if SET NULL had been chosen)?** None was added, since none is needed — but for completeness: had Option A been chosen, the minimum fix would have been a second, FK-free column (e.g., a duplicate `LoserPaperIDAtApprovalTime INT NOT NULL`) to hold the value `SET NULL` would otherwise destroy — which is a strictly more complex schema (two columns, one of them redundant) than Option D's single, simpler fix. This comparison further confirms D as the minimum change, not merely the safest one.
- **Can `AuditLog` alone reconstruct the relationship reliably?** Yes, as a *secondary*, independent cross-check (`MergeApproval.ExecutionAuditLogID → AuditLog.TargetID`, itself also unconstrained) — proven directly by `test_execution_audit_log_id_cross_reference_also_recoverable` (§6). But this is no longer *required* to answer "which pair was merged," since `LoserPaperID` answers it directly and unconditionally under Option D.
- **Is reconstructing it from `AuditLog` sufficient for approval-history requirements?** The question is now moot — reconstruction is available as a bonus, not depended upon.

## 4. Implementation — the Minimum Fix

**One file changed, one column definition changed:**

`backend/analytics/migrations/sprint11_merge_approval.sql`:

```diff
-    "SurvivorPaperID"      INT             NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION,
-    "LoserPaperID"         INT             NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION,
+    "SurvivorPaperID"      INT             NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION,
+    "LoserPaperID"         INT             NOT NULL,
```

Plus a rewritten header-comment section documenting the Phase 4K finding, the option evaluation, and the precedent (§B.1/§C above, in prose, inline in the migration file itself — matching this repository's own established convention of every migration file explaining its own design choices in-file, e.g. `sprint8_author_review_queue.sql`). **Still not applied to any database.**

**No other file required a change to implementation code.** Confirmed by direct inspection: neither `merge_approval.py` nor `merge_executor.py` references `LoserPaperID`'s FK behavior, constraint name, or `ON DELETE` semantics anywhere (`grep -n "REFERENCES\|ForeignKey\|ON DELETE"` against both files returns only unrelated hits — `merge_executor.py`'s own `DEPENDENCY_ACTION_MATRIX` comments, about `ResearchPaper`'s *own* child tables, not `MergeApproval`). Every function in both modules treats `LoserPaperID` as a plain integer value throughout — nothing about removing its FK changes any function's behavior. **Zero weakening** of self-merge protection, fingerprint binding, the approval state machine, approval auditability, permission checks, executor preflight, or transaction safety — none of those properties were ever coupled to this specific FK's existence.

## 5. The Real Lifecycle Test

**Honest limitation, stated up front (per this task's own explicit instruction):** no real, safely isolated PostgreSQL instance was available in this environment this phase — confirmed by checking for a local `psql`/`postgres` binary (none found) and for a second, non-production database configuration in `.env` (none exists; only `DATABASE_URL`, pointed at production Neon, is configured). The only database this session can reach at all is production, and this phase is forbidden from writing to it. **What follows is therefore a Python-level simulation, not a real-database proof** — and it is built to be an honest one: `ExecutorFakeCursor` gained one new, opt-in behavior (`enforce_loser_paper_id_fk`, default `False`) that encodes the actual, documented PostgreSQL rule — an undeferred `ON DELETE NO ACTION` constraint causes an immediate failure on `DELETE` if a referencing row exists — rather than glossing over it. This is proven faithful, not merely asserted, by first using it to **reproduce the exact Phase 4K failure**, then showing the corrected schema removes it.

**New file: `backend/tools/test_fk_lifecycle.py` (6 tests, all passing):**

| Test | Proves |
|---|---|
| `test_delete_fails_with_simulated_fk_violation` | Under the **original** design, the loser `DELETE` inside `execute_approved_merge()` → `merge_group()` raises a simulated FK violation — the mock is faithful to the real, predicted failure mode, not a straw man. |
| `test_failure_leaves_no_partial_corruption` | Loser row still exists; approval `Status` remains `APPROVED`, never `EXECUTED`; `ExecutedAt`/`ExecutionAuditLogID` remain `NULL` — satisfying requirement 6's rollback test. |
| `test_full_lifecycle_reaches_executed_with_no_fk_violation` | Under the **corrected** design, the identical scenario reaches `Status = EXECUTED` with zero exception. |
| `test_loser_actually_deleted` | The loser row is genuinely gone; the survivor remains. |
| `test_approval_history_survives_the_loser_deletion` | The `MergeApproval` row survives the loser's deletion, and `LoserPaperID` still reads back **`5482`** — the real value, no `NULL`, no reconstruction. |
| `test_execution_audit_log_id_cross_reference_also_recoverable` | The secondary `ExecutionAuditLogID → AuditLog.TargetID` path independently agrees (`5482`) — bonus confirmation, not depended upon. |

**Requirement 6's exact 8-point checklist, mapped:**

1. Two `ResearchPaper` records exist → `_build_approved_scenario()`'s fixture.
2. `MergeApproval` row exists, `APPROVED` → `create_pending_approval()` + `approve_pending()`, real, unmodified functions.
3. References survivor and loser → asserted directly on the row.
4. Merge lifecycle deletes the loser → `test_loser_actually_deleted`.
5. Approval history survives correctly → `test_approval_history_survives_the_loser_deletion`.
6. Transaction can reach `EXECUTED` → `test_full_lifecycle_reaches_executed_with_no_fk_violation`.
7. No FK violation occurs → same test, by absence of a raised exception.
8. Historical identity remains recoverable → `test_approval_history_survives_the_loser_deletion` (direct) + `test_execution_audit_log_id_cross_reference_also_recoverable` (secondary).

**Rollback checklist:** loser remains / approval not falsely `EXECUTED` / no partial historical corruption → `test_failure_leaves_no_partial_corruption`, run against the **original** (pre-fix) schema simulation specifically, since that is the one scenario where a failure is expected and provable.

## 6. Executor Order Recheck

Phase 4J's chosen write sequence — `merge_group()` (remaps → `AuditLog` → loser `DELETE`) → `MergeApproval → EXECUTED` → (caller) commit — was, under the *original* schema, **guaranteed to fail at the `DELETE` step**, making the ordering question moot (it never got far enough to matter). Under the **corrected** schema (Option D), nothing about `MergeApproval` can block or interfere with the `ResearchPaper` `DELETE` any longer — the loser row has zero referencing constraints from `MergeApproval` at all. The chosen ordering is now **valid and actually executable**, confirmed directly by `test_full_lifecycle_reaches_executed_with_no_fk_violation`.

**Invariant checks:**

- *A failure at any point before commit means the loser is not permanently deleted, the approval is not permanently `EXECUTED`, and `AuditLog` merge state is not partially committed* — proven for the specific, now-eliminated FK-violation failure mode (§5, `test_failure_leaves_no_partial_corruption`) and, more generally, already proven for every other failure class (child-remap failure, `AuditLog` write failure, `MergeApproval` update failure, an arbitrary mid-preflight exception) by Phase 4J's own `test_J`/`test_M`/`test_N`/`test_O`/`test_P`, re-run unchanged this phase (still 39/39 passing) — none of that exception-propagation design changed.
- *A successful commit means merge completed, loser deletion completed, approval history survived, approval is `EXECUTED`, `AuditLog` is present* — proven directly by `test_full_lifecycle_reaches_executed_with_no_fk_violation` plus `test_approval_history_survives_the_loser_deletion` plus `test_execution_audit_log_id_cross_reference_also_recoverable` together.

### Classification: **SOUND_WITH_REQUIRED_TRANSACTION_WRAPPING**

Not `ATOMICALLY_SOUND` unconditionally, for the same reason Phase 4K found and this phase re-confirms unchanged: `grep -rln "execute_approved_merge" backend/ --include="*.py"` still returns only `merge_executor.py` and its own test files — **no real caller wraps this function in an actual transaction anywhere in the repository.** The design's exception-propagation and no-internal-commit discipline are real and tested; whether "exactly one commit occurs" in production depends on a caller that does not yet exist. Not `STILL_UNCLEAR` — the boundary and every failure mode are now precisely proven, including the one that was genuinely unresolved before this phase. Not `BLOCKED` — nothing prevents writing that caller; it simply hasn't been written yet, which is explicitly out of this phase's scope.

## 7. The Two Non-Blocking Findings

**A. `AuditLog.UserID` hardcoded `NULL` inside `merge_group()`.** Re-confirmed unchanged this phase (not touched — modifying `merge_group()` was correctly out of scope, per §1's "do not weaken... do not redesign" instruction and this task's own "do not silently fix it unless absolutely necessary for the FK lifecycle work" — it is not necessary; nothing about the FK fix touches this code path). **Does it prevent the executing actor from being auditable?** Yes, for the automated `AuditLog` record specifically — that row will never say who ran the merge, only which papers were involved. **Must it be fixed before Phase 4L, or can it be deferred?** **Can be deferred** for a single, human-supervised canary execution specifically — the operator running such a controlled, deliberate, one-off action is externally identifiable (by virtue of being the one person authorized to run it, under direct supervision) without needing the automated record to say so. It **must** be fixed before any *general*, *unsupervised*, *multi-user* production rollout, where "who executed this merge" needs to be answerable from the data alone, not from institutional memory of who was on duty that day.

**B. `has_litrix_perm()` has zero tenant scoping.** Re-confirmed unchanged this phase (`get_permissions()`'s query, re-read: `SELECT p."Code" FROM "Permission" p JOIN "RolePermission" rp ... WHERE rp."RoleID" = %s` — no `TenantID` clause anywhere). **Exact current behavior**: a user's `manage_users` permission (via their `Role`) is global across every tenant, not scoped to their own. **Classification, using the task's own three-way framing**: **not** a blocker for the single canary specifically — both `ResearchPaper` rows (5232, 5482) share `TenantID=1`, live-reconfirmed §9, so no cross-tenant authorization question can even arise for this one pair. **Is** a blocker for broader, multi-tenant rollout — a `manage_users`-holding user from any tenant could currently approve or (once a caller exists) execute a merge touching a different tenant's data, since nothing checks the approving/executing user's own tenant against the pair's. **Must be fixed before any multi-tenant execution.**

Neither finding is conflated with, or was allowed to expand into, the FK fix itself — both were investigated and classified, nothing was silently patched.

## 8. Live Canary Revalidation (Read-Only)

Performed after the schema-file edit and all test runs, strictly read-only, against production:

| Check | Result |
|---|---|
| Both 5232/5482 exist | Yes |
| Survivor direction unchanged | Yes — 5232 survives, 5482 loses |
| Fingerprint | `2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb` — **MATCH**, **8th independent live confirmation** |
| Duplicate safety | `pair_confidence=high`, `hard_exclusion_reason=None` |
| DOI safety | Not claimed elsewhere |
| Idempotency | `NOT_PREVIOUSLY_EXECUTED` (0 `AuditLog` rows referencing either PaperID) |
| Dependency gaps | `[]` — none |
| JournalID decision | `WINNER_ONLY`, `execution_permitted=True` |
| AuthorNameRaw | `[]` — no conflicts |
| `MergeApproval` table in production | `false` — still not applied |

No merge, no approval creation, no write of any kind occurred against production.

## 9. Final Decision

**B) FK FIX IMPLEMENTED BUT REQUIRES STRONGER DATABASE-LEVEL VALIDATION**

The fix itself (§4) is minimal, precisely scoped, and backed by a directly-applicable, already-production-proven precedent in this exact schema (§1) — the underlying reasoning ("removing a foreign-key constraint makes a foreign-key violation on that column impossible") is about as close to certain as a schema change gets. The lifecycle simulation (§5) reproduces the exact predicted failure under the old design and proves it gone under the new one, across all 8 required checklist points plus the rollback checklist. But this phase could not run any of it against a real PostgreSQL instance — no local database was safely available, and production is correctly off-limits for anything beyond read-only queries. Per this task's own explicit instruction not to claim full validation from a mock alone, the honest classification is **B**, not **A**: the design is now believed sound with high confidence, but the strongest remaining recommendation before Phase 4L is to apply the corrected migration once, first, against a real disposable/staging PostgreSQL instance (or at minimum via `apply_migration.py --dry-run`'s existing real-SQL-then-rollback capability, which — unlike this phase's Python simulation — would exercise actual Postgres constraint enforcement) and confirm a real `DELETE` against a row referenced only by the corrected `LoserPaperID` column succeeds, before that migration is ever applied to production.

Not **A** — no real database proof exists yet. Not **C** — nothing found this phase is a new, unresolved schema/design blocker; the one blocker Phase 4K found has a specific, implemented, reasoned fix. Not **D** — nothing about this phase's investigation cast doubt on the fix's safety; the gap is purely "not yet proven against a real database engine," not "might be wrong."

Per your instructions, I am stopping here. Phase 4L is not started. The migration was not applied. No `MergeApproval` row exists anywhere. No merge was executed.

## 10. Exact Accounting

- **Files modified (2)**: `backend/analytics/migrations/sprint11_merge_approval.sql` (one column definition + header comment, not yet applied to any database); `backend/tools/test_merge_executor.py` (added `SimulatedForeignKeyViolation` + `ExecutorFakeCursor.enforce_loser_paper_id_fk`, default `False`, zero behavior change for any of the 39 pre-existing tests — re-confirmed passing).
- **Files created (3)**: `backend/tools/test_fk_lifecycle.py` (6 tests); this report; `backend/reports/phase4k1_fk_lifecycle_validation.json`.
- **Tests**: `test_dedup_papers.py` 18/18, `test_merge_plan_generator.py` 43/43, `test_merge_execution_safety.py` 68/68, `test_merge_approval.py` 45/45, `test_merge_executor.py` 39/39 (unchanged), `test_fk_lifecycle.py` **6/6 (new)**. **Total: 219/219 passing, zero regressions.**
- **Database-level validation method**: Python-level simulation only (`ExecutorFakeCursor.enforce_loser_paper_id_fk`), honestly disclosed as such — no real PostgreSQL instance was safely available this phase.
- **Production DB writes**: **0.**
- **Production schema changes**: **0.**
- **Production migrations applied**: **0.**
- **Production merges**: **0.**
- **DOI changes**: **0.**
- **Production `MergeApproval` rows created**: **0** — table still doesn't exist (re-confirmed live, §8).
- **Network calls**: **0** beyond the local production-database connections themselves (all via the repository's own standard `litrix_db.db()` path — no external HTTP/API call of any kind).

### `git diff --stat` (tracked files)

```
backend/tools/dedup_papers.py      | 91 ++++++++++++++++++++++++++++++++++++++
backend/tools/test_dedup_papers.py | 88 +++++++++++++++++++++++++++++++++++-
2 files changed, 178 insertions(+), 1 deletion(-)
```

Byte-for-byte identical to every phase since 4E — **zero changes this phase** to either tracked file. (`sprint11_merge_approval.sql` and `test_merge_executor.py` are both untracked, so this specific command shows nothing for them — their content changes are described in full in §4/§5 above, with exact diffs.)

### `git status --short` (relevant paths)

```
 M backend/tools/dedup_papers.py                 <- pre-existing, unchanged this phase
 M backend/tools/test_dedup_papers.py             <- pre-existing, unchanged this phase
?? backend/analytics/migrations/sprint11_merge_approval.sql   <- MODIFIED this phase (§4)
?? backend/reports/                                <- this phase adds 2 files to it
?? backend/tools/merge_approval.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_execution_safety.py         <- pre-existing, unchanged this phase
?? backend/tools/merge_executor.py                 <- pre-existing, unchanged this phase
?? backend/tools/merge_plan_generator.py           <- pre-existing, unchanged this phase
?? backend/tools/test_fk_lifecycle.py              <- NEW this phase
?? backend/tools/test_merge_approval.py            <- pre-existing, unchanged this phase
?? backend/tools/test_merge_execution_safety.py    <- pre-existing, unchanged this phase
?? backend/tools/test_merge_executor.py            <- MODIFIED this phase (§5)
?? backend/tools/test_merge_plan_generator.py      <- pre-existing, unchanged this phase
```

(Other untracked files elsewhere in the repository predate this entire duplicate-merge project and were not touched by, or relevant to, Phase 4K.1.)
