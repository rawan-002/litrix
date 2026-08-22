# Phase 4G — Execution-Safety Primitives (Prototype, Read-Only, No Executor)

## Safety Confirmation

No merge executor was built. `merge_group()` was never called. `dedup_papers.py --apply` was never run. No `ResearchPaper` row was deleted, updated, or inserted. No DOI was changed. No network call was made. The one live-DB demonstration performed this phase (Task B's locking prototype) opened a real transaction, took a `SELECT ... FOR UPDATE` lock, and explicitly rolled back — before/after row states were compared and found byte-identical (§Task B). Everything else this phase ran against either mocked cursors or genuine `SELECT`-only live queries.

## Exact Files Modified / Created

**Created (2):**
- `backend/tools/merge_execution_safety.py` — the four safety primitives (Tasks A–D) plus the canary orchestration (Task E). New file, does not touch `dedup_papers.py` or `merge_plan_generator.py`.
- `backend/tools/test_merge_execution_safety.py` — 54 tests.

**Modified: 0.** `git diff --stat` for `backend/tools/dedup_papers.py` and `backend/tools/test_dedup_papers.py` is byte-for-byte identical to Phase 4E's (91/88 insertions, 0/1 deletions) — nothing changed this phase. `backend/tools/merge_plan_generator.py` and `backend/tools/test_merge_plan_generator.py` are untouched (both suites re-run unchanged, §Test Results).

**Report files created (2):** `backend/reports/phase4g_execution_safety_primitives.md` (this file), `backend/reports/phase4g_canary_simulation.json`.

## Task A — Content Fingerprint Design

**Field selection**, derived by walking Phase 4F's stale-plan analysis against `merge_plan_generator.py`'s actual decision surface, not guessed:

- **Included**: every `ResearchPaper` column `classify_field()` can return `CONFLICT`/`HUMAN_REVIEW` for, plus `DOI` and `JournalID` (each load-bearing for survivor selection / execution safety despite having their own dedicated state objects), plus a **derived `citations` integer** (the exact `COALESCE(RawData_Log->cited_by->value, RawData_Log->cited_by_count, 0)` expression `dedup_papers.py`'s own `load_papers()` already uses for `choose_keep()`'s citations tiebreak).
- **Excluded, each with code-proven justification**: `SearchVector_En`/`SearchVector_Ar` (`merge_plan_generator.DERIVED_FIELDS` — never diffed, never drives any decision); `ScrapedAt`/`VerifiedAt`/`DoiResolvedAt` (`merge_plan_generator.PROCESS_TIMESTAMP_FIELDS` — `classify_field()` always resolves these to `KEEP_WINNER`, never `CONFLICT`); the raw `RawData_Log` blob itself (superseded by the derived `citations` value — the rest of the blob, URLs and per-scrape metadata, never enters any decision, so including it would violate the task's own "irrelevant presentation details should not create accidental nondeterminism" requirement).
- **Authors rows** (`UserID`, `AuthorNameRaw`) for both sides are included even though they aren't `ResearchPaper` columns — Phase 4E proved this is exactly the silent-loss field this whole safety line of work exists to catch.

**Normalization**: `None` and an all-whitespace/empty string normalize to the *same* value (matching `merge_plan_generator._is_empty()`'s existing equivalence — a bare `NULL↔''` transition never changes any `classify_field()` verdict, so it must not change the fingerprint). `bool`/`int`/`float` pass through natively. `dict` values (`CitationsByYear`, `VerificationDetails`) are serialized via `json.dumps(sort_keys=True)` so key-insertion order can never matter. Authors lists are sorted by `UserID` so row order can never matter. DOI is **not** re-normalized with new logic — the fingerprint trusts the already-normalized value `fetch_paper_row()` produces (the same normalization `choose_keep()`/`pair_confidence()`/`classify_field()` already operate on), avoiding a second, possibly-divergent normalization scheme.

**Determinism**: SHA-256 over one canonical (`sort_keys=True`, fixed separators) JSON serialization. Same logical state → same fingerprint always; any included field's drift → a different fingerprint, no fuzzy tolerance.

### Test Evidence (13 tests, all passing)

`FingerprintDeterminism`: identical input → identical fingerprint; 5× repeated calls → identical; `Title` change → different; `DOI` change → different; `JournalID` change → different; `PublicationType` (a real conflict-producing field) change → different; `AuthorNameRaw` change → different (the Phase 4E field, explicitly tested); `None` vs `''` → **identical** (empty-normalization); `None` vs a real value → different; `CitationsByYear` dict with reordered keys → identical; Authors list reordered → identical; `ScrapedAt`/`VerifiedAt`/`DoiResolvedAt` changed → identical (proving the exclusion is real, not just documented); winner/loser IDs swapped → different; `citations` value change → different.

## Task B — Locked-State Preflight Prototype

**Repository-native locking precedent, re-confirmed this phase**: raw-cursor `SELECT ... FOR UPDATE`, inside a `transaction.atomic()`-style block, via `connection.cursor()` — the exact pattern already live in `backend/analytics/reconciliation_views.py` (and 6 other files, per Phase 4F's grep). `lock_pair_rows()` reuses this style verbatim, nothing new introduced.

**Three explicitly separated stages** (the required API boundary):
1. `fetch_current_state(cur, winner_id, loser_id)` — plain `SELECT`s, no lock, reusing `merge_plan_generator.fetch_paper_row()`/`fetch_authors_rows()` unmodified.
2. `lock_pair_rows(cur, winner_id, loser_id)` — exactly one `SELECT "PaperID" FROM "ResearchPaper" WHERE "PaperID" = ANY(%s) FOR UPDATE`. Never writes. Returns `False` if either row is missing rather than raising.
3. `validate_against_plan(...)` — pure, zero DB access, given already-fetched data. Checks (in order): both rows exist → winner/loser not reversed → fingerprint matches → `pair_confidence`/`hard_exclusion_reason` still safe → DOI not claimed elsewhere (`is_doi_claimed_elsewhere()`, a live re-check mirroring the same normalized-DOI comparison `uq_paper_doi_normalized` enforces at the DB level).

**Live validation, not just mocked**: this phase opened a real transaction against the live database, called `lock_pair_rows(cur, 5232, 5482)` (returned `True`), then called `conn.rollback()` explicitly — before/after row snapshots of both `ResearchPaper` rows were compared byte-for-byte and found **identical**, proving zero persistent writes from the lock itself, live, not by assertion alone. Full output captured in this session; the exact before/after tuples are reproduced in the transcript and summarized in `phase4g_canary_simulation.json::live_locking_demo`.

**Documented handoff** (module-level `WHAT_A_FUTURE_EXECUTOR_MUST_DO_AFTER_PREFLIGHT_SUCCESS` constant, verbatim in the code): a passing preflight only answers "is it currently safe" — a future executor holding the still-open lock must, in the *same* transaction, apply the plan's field decisions, run `merge_group()`'s existing child-migration logic unmodified, write the `AuditLog` row, delete the loser, re-run the profile-preservation assertion, then commit — any failure at any of those steps must roll back the whole thing. This module implements none of those six steps.

### Test Evidence (13 tests, all passing)
`FetchCurrentStateTests` (missing row → `None`; existing rows → correct dicts + citations), `LockPairRowsTests` (both lockable → `True`; missing row → `False`; no write SQL ever issued — checked precisely, not via a naive "UPDATE" substring match that would false-positive on the legitimate `FOR UPDATE` clause), `ValidateAgainstPlanTests` (OK-path; missing row; reversed; stale fingerprint; duplicate-safety failure via low confidence; duplicate-safety failure via hard exclusion; DOI-safety failure), `DoiClaimedElsewhereTests` (no DOI → never a conflict; claimed elsewhere → `True`; not claimed → `False`).

## Task C — Idempotency Preflight

**`AuditLog` schema, re-confirmed this phase**: `LogID` (autoincrement PK), `TenantID`, `UserID`, `Action`, `TargetType`, `TargetID`, `Metadata` (jsonb), `IpAddress`, `UserAgent`, `CreatedAt`. `merge_group()` already writes `Action='paper.merge.dedup'`, `TargetID=<loser>`, `Metadata={"kept_paper_id": <winner>, ...}` for every real merge. **59 such rows already exist in the live database** (re-confirmed this phase), from genuine historical `--apply` runs — this is proven, populated evidence, not a hypothetical.

**Not assumed sufficient without checking**: `AuditLog` has **no unique constraint** on `(Action, TargetID)` — nothing in the schema prevents a duplicate row. It is therefore evidence to reason over, not a guarantee; `check_idempotency()` is what turns that evidence into a safe decision, never trusting it blindly.

**Decision logic** (`check_idempotency(audit_rows, winner_id, loser_id)`, pure — `fetch_merge_audit_rows()` does the actual, already-scoped `SELECT`): no rows at all → `ELIGIBLE`. A row whose `Metadata` is missing or lacks `kept_paper_id` → `UNKNOWN_HISTORY_BLOCKED` (refuses to guess, per the task's explicit instruction). A row matching `TargetID=loser, kept_paper_id=winner` → `BLOCKED_EXACT_PRIOR_EXECUTION`. A row matching `TargetID=winner, kept_paper_id=loser` → `BLOCKED_REVERSED_PRIOR_PAIR`. Any remaining row where either ID appears pointing at a *third*, unrelated PaperID → `BLOCKED_CONTRADICTORY_HISTORY`. Unrelated `AuditLog` rows (other PaperIDs, other `Action`s) can never reach this function at all — `fetch_merge_audit_rows()`'s own `WHERE "Action"=%s AND "TargetID" = ANY(%s)` scoping excludes them structurally, not by trusting the pure function to ignore them.

### Test Evidence (7 tests, all passing)
No prior execution → `ELIGIBLE`; exact prior execution → blocked; reversed prior pair → blocked; ambiguous malformed `Metadata` → `UNKNOWN_HISTORY_BLOCKED`; `Metadata=None` → `UNKNOWN_HISTORY_BLOCKED`; contradictory third-party history → blocked; unrelated audit history does not falsely block (plus a dedicated mocked-cursor test proving `fetch_merge_audit_rows()`'s scoping itself excludes an unrelated pair's row before `check_idempotency()` ever sees it).

## Task D — Approval State Design

**Classification: B) No legitimate existing approval storage exists.**

`AuditLog` was evaluated in detail, not dismissed at a glance — it is the *closest* candidate in the schema (same subsystem, already proven, already populated) and was rejected only after checking it against the task's actual "exact purpose proven" bar:

- `AuditLog` is a pure **append-only action log** — no `UPDATE` path exists anywhere in this codebase for a row once inserted. Approval is inherently **mutable state** with one current status (`PENDING → APPROVED/REJECTED`) that must be queryable as a single answer. Building "current approval status" on an append-only log requires inventing "most recent row wins" logic on top — exactly the unproven, ad-hoc semantic the task instructs against.
- `AuditLog` has **no typed column** for approval status, plan fingerprint, or an immutable approval version — only its schema-free `Metadata` jsonb could hold them. Using that would be precisely "a table that can store text," the exact anti-pattern the task names.
- Task C already uses `AuditLog` for idempotency history. Overloading the *same* table for approval-state would make "is this an approval record" vs. "is this a completed-merge record" an **unenforced convention** rather than a schema guarantee — real added query-ambiguity risk in a safety-critical check, not a cosmetic objection.

**No DB table was created.** Per the task's Option B, a minimal, pure-Python approval **artifact schema** was defined instead (`ApprovalArtifact`, a frozen `dataclass` — immutable by construction, matching "immutable approval version"): `plan_id`, `winner_paper_id`, `loser_paper_id`, `plan_fingerprint`, `approval_status` (`PENDING`/`APPROVED`/`REJECTED`), `approver_identity`, `approval_timestamp`, `approval_version`, `reason_notes` — all 9 required fields present. `validate_approval_artifact(artifact, current_fingerprint)` enforces the task's explicit binding requirement: an artifact is usable only if `approval_status == APPROVED` **and** `artifact.plan_fingerprint == current_fingerprint` — an approval recorded against an older plan never authorizes a changed one. Deliberately left undecided which future storage this artifact schema should live in (a real DB table via a later migration, or a file-based workflow) — nothing in this phase's evidence favors one over the other, and choosing prematurely would be inventing an answer the task didn't ask for.

### Test Evidence (6 tests, all passing)
No artifact → invalid; `PENDING` status → invalid; `REJECTED` status → invalid; fingerprint mismatch → invalid (the exact "approval bound to an older plan must not approve a changed one" requirement); `APPROVED` + matching fingerprint → valid; all 9 required fields present on `to_dict()`.

## Task E — Canary Integration Simulation (5232/5482, live, read-only)

Ran `run_canary_simulation()` **against the real, live database** (not a mock) for the canary pair, with a reference plan whose fingerprint was computed fresh from current live data (simulating "the plan as it stands right now"), and `approval_artifact=None` (since Task D proved none can legitimately exist yet). Full result in `backend/reports/phase4g_canary_simulation.json`; summary:

| Step | Result |
|---|---|
| Load current pair | both 5232 and 5482 exist |
| Fingerprint check | current == reference — **match** |
| Duplicate safety (`choose_keep`/`pair_confidence`/`hard_exclusion_reason`) | winner=5232 (unreversed), `pair_confidence=high`, no hard exclusion |
| DOI safety | winner's DOI not claimed by any other row |
| Idempotency | `ELIGIBLE` — zero prior `paper.merge.dedup` history for either ID |
| Approval | **no artifact exists** — invalid |

**Final verdict: `BLOCKED_APPROVAL`.** Every check that *could* pass, passed — exactly reproducing Phase 4F's finding that this pair is otherwise clean — and the simulation stopped at the one gate Task D proved doesn't exist yet. `SIMULATION_READY` was never claimed for this pair, honestly, because it isn't ready: no approval mechanism exists to satisfy it. A separate mocked-cursor test (`test_valid_approval_with_matching_fingerprint_reaches_simulation_ready`) confirms the machinery *would* correctly report `SIMULATION_READY` if a valid `ApprovalArtifact` existed — proving the gate itself works, not just that it's currently closed.

## Task F — Explicit Non-Goals Verification

Five automated static-scan tests (`NonGoalsStaticSourceScan`), all passing: no `merge_group` import anywhere, and no real `merge_group(` **call** (checked by requiring every occurrence to have empty parens — a real call always has arguments, every prose mention in this file's own comments/docstrings has none); no `--apply` CLI wiring or `subprocess`/`os.system` invocation; no network client import (`requests.`/`urllib.request`/`httpx.`/`socket.`/`http.client`); zero write-verb SQL (`INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`/`ALTER`/`DROP`/`CREATE`) inside any actual `cur.execute(...)` call, checked by extracting the literal SQL strings via regex, not by scanning the whole file (which would false-positive on prose); zero bare `INSERT INTO` anywhere at all (the strongest check — since Task D concluded no legitimate approval storage exists, this file must contain literally none, not just none inside `cur.execute()`); no function named anything executor-shaped (`execute_plan`/`run_executor`/`apply_merge`) exists in the module's public surface. A second, independent, execution-time safety net exists alongside the static scan: `FakeCursor` itself (the test double) raises `AssertionError` on any write-verb SQL passed to it, so even a test that *forgot* to statically check would fail the moment the code under test tried to write.

## Test Results

| Suite | Result |
|---|---|
| `backend/tools/test_merge_execution_safety.py` (new, this phase) | **54/54 passing** |
| `backend/tools/test_dedup_papers.py` (unchanged since Phase 4E) | 18/18 passing (re-run to confirm no regression) |
| `backend/tools/test_merge_plan_generator.py` (unchanged since Phase 4E) | 43/43 passing (re-run to confirm no regression) |

## Safety Accounting

- DB writes: **0**
- Network calls: **0**
- Records merged: **0**
- DOI changes: **0**
- `--apply` executions: **0**
- Live DB interactions this phase: (a) multiple read-only `SELECT`s for evidence-gathering, (b) one live canary simulation run (`SELECT`-only throughout), (c) one live locking demonstration — `SELECT ... FOR UPDATE` followed by an explicit `conn.rollback()`, with before/after state verified identical.

## Remaining Blockers

Exactly one, and it is the expected, correct outcome of this phase rather than a new discovery: **no approval-recording mechanism exists anywhere in this repository.** Task D deliberately did not build one (no DB table permitted without proof of a legitimate existing mechanism, and none was found). This is why the canary simulation's honest final verdict is `BLOCKED_APPROVAL` rather than `SIMULATION_READY` — and why it should be: nothing in this project has ever authorized an actual merge, and this phase does not change that. Fingerprinting, locking, and idempotency are now prototyped and tested (mocked + live); approval-state storage is designed but deliberately not implemented, per the task's own strict boundary against inventing a live DB write workaround.

## Final Decision

**B) SAFETY PRIMITIVES PARTIALLY VALIDATED — specific implementation blocker remains**

Three of the four Phase 4F blockers (fingerprinting, locking, idempotency) are now prototyped, unit-tested (54 tests) against mocked fixtures, and additionally validated live against the real canary pair and the real `AuditLog` table — not merely designed on paper this time. The fourth (approval-recording) was investigated as rigorously as the task demanded and correctly found to have **no safe, legitimate existing storage** — this is not a gap in this phase's effort, it is the honest, evidence-backed answer, and the task explicitly forbade inventing a workaround for it. That single, precisely-scoped remaining blocker — where should `ApprovalArtifact` actually live (a migration, or a file-based workflow) — is a real product/architecture decision for a future phase, not a mechanical fingerprint/locking/idempotency implementation the way the other three were. **A** would overstate readiness (an executor still cannot be safely built until approval storage exists); **C** would overstate the problem (nothing found this phase suggests a *dedicated migration is required before anything else can proceed* — fingerprinting/locking/idempotency needed no migration at all and are done; only the approval piece needs a storage decision, and even that could plausibly be file-based rather than a migration). **B** is the precise, honest fit.

Per your instructions, I am stopping here. Phase 4H is not started.
