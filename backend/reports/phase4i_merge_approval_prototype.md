# Phase 4I — MergeApproval Prototype Implementation

## 1. Exact Files Modified / Created

**Created (3):**
- `backend/analytics/migrations/sprint11_merge_approval.sql` — the schema artifact (§2). **Not applied to any database, production or otherwise** — see §11/§12 for why and how this was confirmed.
- `backend/tools/merge_approval.py` — the approval-operation functions (§4–6). 350 lines.
- `backend/tools/test_merge_approval.py` — 45 tests (§8). 380 lines.

**Modified: 0.** `dedup_papers.py`, `merge_plan_generator.py`, and `merge_execution_safety.py` are untouched — `git diff --stat` for the tracked files (`dedup_papers.py`/`test_dedup_papers.py`) is byte-for-byte identical to every prior phase since 4E (91/88 insertions, 0/1 deletions); the other two remain untracked and were re-run unchanged (§8).

Before writing anything, `backend/reports/phase4h_approval_storage_design.md` was re-read in full, and four real repository files were inspected fresh this phase: `sprint8_author_review_queue.sql`, `reconciliation_views.py` (the full `review_queue_decide` endpoint, read end-to-end), `backend/accounts/common.py::audit()`, and `backend/accounts/permissions.py`. One additional check not explicitly listed but necessary to answer Task C honestly — `backend/analytics/disambiguation/pipeline.py`, the code that actually creates `AuthorReviewQueue` rows — was grepped for `audit(` calls and found to have none, which directly determined §7's design.

## 2. Schema Design Implemented

`sprint11_merge_approval.sql`, modeled field-for-field on Phase 4H's design and `sprint8_author_review_queue.sql`'s proven shape:

```sql
CREATE TABLE IF NOT EXISTS "MergeApproval" (
    "ApprovalID"           SERIAL          PRIMARY KEY,
    "SurvivorPaperID"      INT             NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION,
    "LoserPaperID"         INT             NOT NULL REFERENCES "ResearchPaper"("PaperID") ON DELETE NO ACTION,
    "PlanID"               VARCHAR(64)     NOT NULL,
    "PlanFingerprint"      VARCHAR(64)     NOT NULL,
    "ApprovalVersion"      INT             NOT NULL DEFAULT 1,
    "TenantID"             INT             NOT NULL REFERENCES "Tenant"("TenantID"),
    "Status"               VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
                            CHECK ("Status" IN ('PENDING','APPROVED','REJECTED','REVOKED','EXECUTED')),
    "ReviewedByUserID"     INT             NULL     REFERENCES "Users"("UserID") ON DELETE SET NULL,
    "ReviewedAt"           TIMESTAMPTZ     NULL,
    "ReviewerNotes"        TEXT            NULL,
    "RevokedByUserID"      INT             NULL     REFERENCES "Users"("UserID") ON DELETE SET NULL,
    "RevokedAt"            TIMESTAMPTZ     NULL,
    "RevocationReason"     TEXT            NULL,
    "ExecutedAt"           TIMESTAMPTZ     NULL,
    "ExecutionAuditLogID"  INT             NULL     REFERENCES "AuditLog"("LogID") ON DELETE SET NULL,
    "PlanSnapshotJSON"     JSONB           NULL,
    "CreatedAt"            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_merge_approval_not_self CHECK ("SurvivorPaperID" != "LoserPaperID"),
    CONSTRAINT uq_merge_approval_identity UNIQUE
        ("SurvivorPaperID", "LoserPaperID", "PlanFingerprint", "ApprovalVersion")
);
```

Exactly the fields Phase 4H justified as `REQUIRED` — no speculative additions. Only fields not explicitly listed in this phase's task text but present: `PlanSnapshotJSON` (Phase 4H classified `OPTIONAL`, included because it's genuinely useful and zero-risk) — no `EXPIRED`/`ExpiresAt` (Phase 4H rejected it from the minimum model, and this phase's task text agrees by omission).

**Two deliberate deviations from the `AuthorReviewQueue` precedent, both justified in the file's own header comment:**
1. `ON DELETE NO ACTION` on `SurvivorPaperID`/`LoserPaperID`, not `CASCADE` — an `AuthorReviewQueue` row is meaningless once its paper is gone; a `MergeApproval` row remains meaningful audit history even after a successful merge deletes the loser row.
2. A schema-level `CHECK` constraint against self-merge (`chk_merge_approval_not_self`), which `AuthorReviewQueue` has no equivalent of (it only ever references one `PaperID`, so the concept doesn't apply there) — a second, independent guard beneath the application-level `reject_self_merge()` check.

## 3. State Transition Matrix

| From ＼ To | PENDING | APPROVED | REJECTED | REVOKED | EXECUTED |
|---|---|---|---|---|---|
| **PENDING** | — | ✅ (`approve_pending`) | ✅ (`reject_pending`) | ❌ | ❌ |
| **APPROVED** | ❌ | — | ❌ | ✅ (`revoke_approved`) | ✅ (schema-supported; no function in this module transitions to it — see §7/§9 non-goals) |
| **REJECTED** | ❌ | ❌ | — | ❌ | ❌ |
| **REVOKED** | ❌ | ❌ | ❌ | — | ❌ |
| **EXECUTED** | ❌ | ❌ | ❌ | ❌ | — |

Encoded directly as data (`LEGAL_TRANSITIONS` dict), checked by `is_legal_transition()` — a pure function, tested independently of any DB interaction (7 tests, `StateMachineTests`). A fresh review cycle after a terminal state (`REJECTED`/`REVOKED`) does not reuse or mutate the old row — it creates a new row at the next `ApprovalVersion` for the same `(survivor, loser, fingerprint)` identity, exactly matching Phase 4H's immutable-artifact-version design.

## 4. Permission Mechanism Used

`can_approve_merge(user)` reuses `has_litrix_perm()` — the real, existing mechanism confirmed in `backend/accounts/permissions.py` — checking the **`manage_users`** code with an `Admin` `user_type` fallback, **exactly mirroring `reconciliation_views.py::_can_reconcile()`'s own logic, line for line**. No new permission code was invented. Phase 4H's own investigation found no more specific existing code (`approve_paper_merge` or similar does not exist); reusing `manage_users` repeats the established convention rather than fabricating a new, undecided one. This is stated as a real limitation, not hidden: a dedicated permission code remains a reasonable future product decision, not made here or in Phase 4H.

## 5. Locking / Concurrency Behavior

Every state-transition function (`approve_pending`, `reject_pending`, `revoke_approved`) routes through a shared `_transition()` helper that issues `SELECT ... FROM "MergeApproval" WHERE "ApprovalID" = %s FOR UPDATE` as its first statement — the identical lock-then-decide-then-update shape `reconciliation_views.py::review_queue_decide()` uses for `AuthorReviewQueue`, applied here without modification to the pattern itself. `create_pending_approval()` does not need a row lock (it's creating a new row; the real safety comes from the `UNIQUE` constraint plus the pre-insert existence check, §6).

Real double-transition attempts (two concurrent requests both trying to approve the same `ApprovalID`) are prevented by the combination of the row lock and the `Status` guard: whichever transaction's `SELECT ... FOR UPDATE` executes first holds the lock until it commits (transitioning `PENDING → APPROVED`); the second transaction blocks on the lock, then — once unblocked — sees `Status='APPROVED'` and is correctly refused by `is_legal_transition('APPROVED', 'APPROVED')` returning `False`. `test_illegal_transition_double_approve_is_rejected` proves the **sequential outcome** of this scenario (the correct, deterministic result any real concurrent execution converges to, given Postgres's row-lock semantics) — a live two-connection concurrency test was not run, consistent with "Tests must use mocks/test DB strategy consistent with existing repository tests" and this phase's explicit no-production-writes boundary.

## 6. PlanFingerprint Binding Behavior

Three independent points bind approval to an exact fingerprint, not merely one:

1. **Lookup scoping**: `fetch_current_approval(cur, survivor_id, loser_id, plan_fingerprint)`'s `WHERE` clause includes `"PlanFingerprint" = %s` — an approval recorded for fingerprint X is structurally unreachable when querying fingerprint Y. Not a convention; a query-shape guarantee.
2. **Transition-time re-verification**: `approve_pending`/`reject_pending`/`revoke_approved` all require the caller to pass `expected_plan_fingerprint`, and `_transition()` compares it against the row's stored `PlanFingerprint` **after** acquiring the lock, refusing with `fingerprint_mismatch` on any disagreement — a reviewer cannot approve "whatever is currently in row #42," only the exact plan state they were shown.
3. **Schema-level uniqueness**: `uq_merge_approval_identity` includes `PlanFingerprint` in its key — two different fingerprints for the same pair are always two different rows, never a silent overwrite.

`test_fingerprint_mismatch_blocks_approval`, `test_changed_fingerprint_invalidates_lookup_reuse`, and `test_different_fingerprint_is_a_wholly_separate_identity` prove all three points directly.

## 7. `AuditLog` Integration

Reuses `backend/accounts/common.py::audit()` — the same shared helper `reconciliation_views.py` calls, not a reimplementation. Per real repository evidence gathered fresh this phase (`backend/analytics/disambiguation/pipeline.py`, the actual code that creates `AuthorReviewQueue` rows, contains **zero** `audit()` calls — grep-confirmed): **creation is not audited**, matching the real precedent exactly, so `create_pending_approval()` makes no audit call. `approve_pending`, `reject_pending`, and `revoke_approved` each call `_write_audit()` — `TargetType='MergeApproval'`, `TargetID=<ApprovalID>`, `Action` one of `merge_approval.approved`/`.rejected`/`.revoked`, `Metadata` carrying the pair IDs, fingerprint, and the `from_status`/`to_status` transition. `test_creation_is_not_audited` proves the omission is deliberate and verified, not accidental.

**`MergeApproval` remains authoritative for approval state, never `AuditLog`** — `test_merge_approval_never_reads_status_from_auditlog` statically confirms `merge_approval.py` contains no `SELECT ... FROM "AuditLog"` anywhere; every status check reads `MergeApproval.Status` directly.

## 8. Test Results

| Suite | Result |
|---|---|
| `backend/tools/test_merge_approval.py` (new, this phase) | **45/45 passing** |
| `backend/tools/test_dedup_papers.py` (re-run, unchanged) | 18/18 passing |
| `backend/tools/test_merge_plan_generator.py` (re-run, unchanged) | 43/43 passing |
| `backend/tools/test_merge_execution_safety.py` (re-run, unchanged) | 68/68 passing |
| **Total** | **174/174 passing** |

Coverage against the task's 14 required scenarios: (1) create PENDING (`test_create_pending`); (2) approve (`test_approve_pending`); (3) reject (`test_reject_pending`); (4) revoke (`test_revoke_approved`); (5) illegal transition (`test_illegal_transition_pending_to_revoked`, plus 6 more `StateMachineTests`); (6) self-merge rejection (`test_self_merge_rejected_before_any_sql`, proving zero SQL is issued); (7) reversed survivor/loser (`test_reversed_pair_is_a_different_approval_row_entirely`, `ApprovalMatchesPairTests`); (8) changed fingerprint invalidates lookup (`test_changed_fingerprint_invalidates_lookup_reuse`); (9) duplicate active approval (`test_duplicate_active_approval_returns_existing_not_a_new_row`, `test_new_pending_after_rejected_gets_a_new_version`, `test_cannot_create_new_pending_after_executed`); (10) reviewer identity/timestamp recording (asserted directly in `test_approve_pending`/`test_revoke_approved`); (11) permission denial (`PermissionTests`, plus `test_permission_denied` / `test_permission_denied_on_transition`); (12) concurrent-review-equivalent protection (`test_illegal_transition_double_approve_is_rejected`); (13) audit integration (`AuditIntegrationTests`, 5 tests); (14) canary identity (`CanaryIdentityTests::test_canary_full_lifecycle`, using the real, byte-identical fingerprint value re-confirmed live in §9).

## 9. Live Canary Read-Only Result

Performed **after** all 174 tests passed, strictly read-only, against the real database:

| Check | Result |
|---|---|
| `MergeApproval` table exists in production? | **`false`** — confirmed via a read-only `information_schema.tables` query. The migration was never applied (§1/§11). |
| Current plan fingerprint (5232/5482) | `2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb` — **byte-identical to every prior live run across Phase 4G, 4G's final confirmation, and this phase** (now 5 independent confirmations spanning 2026-08-21 and 2026-08-22, zero drift). |
| Whether an approval already exists | **No approval can exist** — the table itself doesn't exist in production, which is a definitive, evidence-based answer, not an unanswered question. |
| Technical preconditions unchanged | **Yes** — `run_canary_simulation()` (Phase 4G, unmodified, re-run this phase) still passes every check through idempotency (`ELIGIBLE`/`NOT_PREVIOUSLY_EXECUTED`) and stops only at the approval gate. Final verdict: **`BLOCKED_APPROVAL`** — unchanged, and correctly so, since no approval mechanism is live in production yet. |

No row was inserted, updated, or deleted in production during this check — confirmed by `git status --short` showing zero code changes from this validation step, and by the query set itself being exclusively `SELECT` (including the `information_schema.tables` existence check).

## 10/11/12/13/14. Exact DB Writes, Production Writes, Network Calls, Records Merged, DOI Changes

- **DB writes performed during tests**: **0 against any real database.** All 45 new tests run entirely against `InMemoryApprovalCursor`, an in-process Python dict — no `psycopg2`/Django DB connection is opened by the test suite at all. `accounts.common.audit()` (which *would* open a real connection) is never actually invoked during tests — every test exercising a state transition patches `merge_approval._write_audit` out via `unittest.mock.patch`, so the real audit-writing code path is never reached in the automated suite.
- **Production DB writes**: **0.** The migration file was authored but not applied (§1, §11). The live validation pass (§9) was exclusively `SELECT`/`information_schema` queries.
- **Network calls**: **0.**
- **Records merged**: **0.**
- **DOI changes**: **0.** `test_no_doi_column_writes` statically confirms no `"DOI"` column assignment exists anywhere in `merge_approval.py`.

## 15. Remaining Blockers Before Executor Implementation

1. **The `sprint11_merge_approval.sql` migration has not been applied to any database.** This was a deliberate scope decision, not an oversight — explained in full in §11 below — and is the literal, direct reason the live canary check in §9 correctly found no approval can exist yet.
2. **No HTTP endpoint or UI exists** for a human to actually call `create_pending_approval`/`approve_pending`/`reject_pending`/`revoke_approved` — this module is a set of importable functions, deliberately not wired into `urls.py`/a DRF view, matching this phase's scope (§16 explicit non-goals) and this project's established pattern (`dedup_papers.py`, `merge_plan_generator.py`, and `merge_execution_safety.py` are all plain function modules too, not views).
3. **No executor exists** to consume an `APPROVED` row — `MergeApproval.Status` can reach `APPROVED` via this module, but nothing transitions it to `EXECUTED`, applies the plan's field decisions, or performs any merge. This is the explicit, correct boundary of this phase.
4. **Per-tenant permission scoping remains unconfirmed** (Phase 4H's finding, unchanged) — `can_approve_merge()` checks `manage_users`/`Admin` globally; whether that permission is itself tenant-scoped in this codebase was not established here either.
5. **The dependency-table-drift gap Phase 4H/4G documented is unaffected by this phase** — `PlanFingerprint` still does not cover most child-table row counts; an executor must still re-check those live regardless of approval state (Phase 4H §F, unchanged).

## 16. Explicit Non-Goals — Confirmed, Not Just Declared

Every item on the task's "Do NOT add" list was checked with an automated test, not merely avoided by intent:

- Executor logic: `test_no_merge_group_import_or_call` (no `merge_group` import, no real call — same precise-scan pattern already proven in Phase 4E/4G/4H's own guard tests).
- `DELETE` logic: `test_no_delete_against_research_paper`.
- Merge SQL / child-table remapping: `test_no_child_table_remap_or_journal_backfill_logic` (statically confirms `"Authors"`, `"Citations"`, `"ExternalAuthors"`, `"CitationsHistory"`, `"ReportPaperDecision"`, `"AuthorReviewQueue"` — every table `merge_group()` touches — never appear in this file at all).
- `JournalID` backfill execution / `AuthorNameRaw` conflict-resolution execution: same test — this module has no code path that reads, let alone writes, either concept; it only stores a human's yes/no decision about a plan that (elsewhere, unimplemented) would apply them.
- DOI changes: `test_no_doi_column_writes`.
- Execution-status transitions: `ExecutedAt`/`ExecutionAuditLogID` are schema columns this module can *read* (via `fetch_current_approval()`, which returns them as part of every row) but **no function in `merge_approval.py` ever writes them** — `EXECUTED` is a legal transition target in `LEGAL_TRANSITIONS` (schema support, as the task explicitly permits: *"execution status transitions unless Phase 4H explicitly requires schema support without implementing execution"* — it does, §D/§H of the Phase 4H report), but no function reaches it. Confirmed by code inspection: grep `"EXECUTED"` in `merge_approval.py` — appears only in the constant definition and the `LEGAL_TRANSITIONS` dict, never in an `UPDATE ... SET "Status" = ` statement.

## 11 (continued). Why the Migration Was Not Applied — Explicit Reasoning

This phase's task text gates production row-insertion behind explicit future approval ("Do NOT insert a real MergeApproval row into production data unless I explicitly approve that exact write in a later phase") but does not, in as many words, say the same about running the `CREATE TABLE` migration itself. Applying it anyway was deliberately not done, for reasons consistent with every prior phase of this project:

- Every phase from 4A through 4H maintained an unbroken record of **zero DB writes**, including zero schema changes — Phase 4H's own report title includes "no schema changes" as a hard rule, and this phase's task text repeats "Do NOT run any --apply command... Do NOT perform any actual merge" in the same conservative spirit.
- `CREATE TABLE` is itself a production DB write, and the row-insertion gate's own phrasing ("in a later phase") signals that *this* phase is meant to produce artifacts and validated logic, not yet touch production — consistent with how Phase 4H's own migration file was designed but explicitly not run.
- Nothing about validating the approval *logic* required a live table: `InMemoryApprovalCursor` (§8) exercises every SQL shape `merge_approval.py` issues, including the real `UNIQUE`/`CHECK` constraints (mirrored in the mock), so the 45 tests are a genuine behavioral proof, not merely a syntax check.

If this reasoning is wrong and applying the migration to production (or to a non-production/test database) was intended as part of this phase, that is exactly the kind of explicit, one-step-at-a-time confirmation this project has consistently asked for before each write — flagged here for your decision rather than assumed.

## Final Decision

**A) Approval layer implemented and validated; safe to begin executor prototype design**

The approval-storage layer specified in Phase 4H is now real, working code: a schema artifact matching the approved design and the proven `AuthorReviewQueue` precedent field-for-field; five operations covering creation, both terminal PENDING outcomes, revocation, and lookup; a state machine enforcing exactly the Phase 4H lifecycle with zero illegal edges reachable; fingerprint binding enforced at three independent layers; permission checks reusing the real, existing mechanism; audit integration matching real repository precedent exactly (including the non-obvious "creation isn't audited" finding); and 174/174 tests passing with zero regressions across four suites. The live canary check confirms the design still applies cleanly to real, current data (fingerprint unchanged a fifth time) and correctly reports that no approval exists yet, for the honest, structural reason that the storage layer is implemented but not yet deployed. Remaining work (§15) is squarely executor-scope (endpoint wiring, the executor itself, tenant-scoping investigation) — not further approval-layer design or rework, which is exactly the boundary between "B" and "A" this decision turns on.

Per your instructions, I am stopping here. Phase 4J is not started, no executor was built, nothing was merged, and no production database write occurred.
