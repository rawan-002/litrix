# Phase 4G — Merge Executor Preconditions Prototype

## Context Note

This request covers the same four Phase 4F blockers (fingerprinting, row-locking, idempotency, approval-recording) as the immediately preceding turn's work (`backend/reports/phase4g_execution_safety_primitives.md` / `phase4g_canary_simulation.json`, using `backend/tools/merge_execution_safety.py`). Rather than duplicate that module from scratch, this phase **extends it** with the specific new primitives this request adds that weren't built before — self-merge rejection, deterministic ascending lock ordering, and the two named 3-way verdict contracts (`NOT_PREVIOUSLY_EXECUTED`/`ALREADY_EXECUTED`/`HISTORICAL_STATE_AMBIGUOUS` and `EXISTING_APPROVAL_MECHANISM_USABLE`/`EXISTING_MECHANISM_REQUIRES_EXTENSION`/`NEW_APPROVAL_STORAGE_REQUIRED`) — and re-validates everything, including a fresh live canary run, against the extended module. This keeps the diff minimal (per this phase's own instruction against broad refactors) rather than producing a second, parallel, near-duplicate module.

## 1. Exact Files Modified / Created

**Modified (1, this phase):** `backend/tools/merge_execution_safety.py` — additive only. Added: `reject_self_merge()`, `build_lock_order()`, a self-merge check + ascending-order lock inside `lock_pair_rows()` (its existing bool-return contract for the two prior cases — both-locked / one-missing — is unchanged; only a new `ValueError` path for the self-merge case was added), `idempotency_verdict()` + 3 new constants, `approval_storage_verdict()` + 3 new constants. No existing function's prior behavior was altered for any input it previously accepted (re-run all 3 test suites below to confirm). File length: 762 lines (was ~610 before this phase's additions).

**Modified (1, this phase):** `backend/tools/test_merge_execution_safety.py` — added `SelfMergeAndLockOrderingTests` (6 tests), `IdempotencyVerdictThreeWayTests` (6 tests), `ApprovalStorageVerdictTests` (2 tests) — 14 new tests, 54 pre-existing tests untouched. File length: 687 lines.

**Not modified (0)**: `backend/tools/dedup_papers.py`, `backend/tools/test_dedup_papers.py` — `git diff --stat` for both is byte-for-byte identical to Phase 4E/4F/the prior turn (91/88 insertions, 0/1 deletions) — nothing changed. `backend/tools/merge_plan_generator.py`, `backend/tools/test_merge_plan_generator.py` — untouched, both re-run unchanged (§12). `merge_group()`, `pair_confidence()`, `detect_groups()`, `doi_pipeline/*`, database schema, migrations — none read for writing, none touched.

**Report files created (1):** `backend/reports/phase4g_executor_preconditions_prototype.md` (this file). `backend/reports/phase4g_canary_simulation.json` already exists from the prior turn and was not required to be recreated by this specific request; a fresh live re-run this phase (§11) is reported inline here instead, with its raw JSON also saved to `backend/reports/_phase4g_canary_live_run.json` for inspection.

## 2. Exact Code Changes

```python
# New in merge_execution_safety.py:

def reject_self_merge(winner_id, loser_id):
    """Pure, no DB access. Returns (ok, reason)."""
    if winner_id == loser_id:
        return False, f"winner_id and loser_id are both {winner_id} -- a PaperID cannot merge with itself"
    return True, None

def build_lock_order(id_a, id_b):
    """Pure, no DB access. Deterministic lock-acquisition order: always
    ascending by PaperID, independent of winner/loser role."""
    return tuple(sorted((id_a, id_b)))

def lock_pair_rows(cur, winner_id, loser_id):
    ok, reason = reject_self_merge(winner_id, loser_id)
    if not ok:
        raise ValueError(reason)
    ordered_ids = list(build_lock_order(winner_id, loser_id))
    cur.execute(
        'SELECT "PaperID" FROM "ResearchPaper" WHERE "PaperID" = ANY(%s) FOR UPDATE',
        (ordered_ids,),
    )
    locked = {r[0] for r in cur.fetchall()}
    return winner_id in locked and loser_id in locked

def idempotency_verdict(audit_rows, winner_id, loser_id):
    """Coarse 3-way wrapper over the existing, already-tested
    check_idempotency(). Returns (verdict, detailed_result)."""
    detailed = check_idempotency(audit_rows, winner_id, loser_id)
    if detailed.status == IDEMPOTENCY_ELIGIBLE:
        return IDEMPOTENCY_NOT_PREVIOUSLY_EXECUTED, detailed
    if detailed.status == IDEMPOTENCY_UNKNOWN_HISTORY_BLOCKED:
        return IDEMPOTENCY_HISTORICAL_STATE_AMBIGUOUS, detailed
    return IDEMPOTENCY_ALREADY_EXECUTED, detailed  # exact/reversed/contradictory

def approval_storage_verdict():
    """Pure. Returns (APPROVAL_VERDICT_NEW_STORAGE_REQUIRED, evidence_dict)
    -- see §10 for the full reasoning per candidate table."""
    ...
```

Full source is in the file; nothing above is abbreviated for behavior, only for length in this report.

## 3–6. Safety Accounting

- **DB writes: 0**
- **Network calls: 0**
- **Records merged: 0**
- **DOIs changed: 0**

Every live-DB interaction this phase was a `SELECT` (schema introspection queries for §10's table investigation, `fetch_current_state`/`fetch_merge_audit_rows` calls for §11's canary re-run). No transaction in this phase ever contained `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`. No `SELECT ... FOR UPDATE` was issued live this phase (that live demonstration, with an explicit rollback, was already performed and reported in the prior turn's `phase4g_execution_safety_primitives.md` §Task B — not repeated here since nothing about the locking SQL text changed, only the surrounding ID-ordering logic, which is proven by the 3 new `SelfMergeAndLockOrderingTests` mocked-cursor tests instead, per this phase's "Prototype the SQL/query-building logic only" instruction).

## 7. Full Fingerprint Design and Exact Included Fields

Unchanged from the prior turn's design (re-validated live this phase, §11 — same fingerprint value reproduced). Summary:

**Included** (deterministic SHA-256 over a canonical `sort_keys=True` JSON payload): every `ResearchPaper` column `classify_field()` (in `merge_plan_generator.py`) can return `CONFLICT`/`HUMAN_REVIEW` for — `JournalID`, `Title`, `Title_En`, `Abstract`, `Abstract_En`, `Language`, `DOI`, `PubYear`, `Volume`, `Issue`, `Pages`, `IsVerified`, `Source`, `NormalizedTitle`, `Indexing`, `CitationsByYear`, `TenantID`, `AffiliationVerified`, `VerificationSource`, `VerificationDetails`, `VenueType`, `DoiResolvedBy`, `OpenAlexWorkID`, `AbstractSource`, `PdfUrl`, `PdfAccessType`, `PublicationType` — plus a **derived `citations` integer** (the same `COALESCE(RawData_Log->cited_by->value, RawData_Log->cited_by_count, 0)` expression `dedup_papers.py`'s own `load_papers()` uses for `choose_keep()`'s tiebreak) for both sides, plus **`Authors` rows** (`UserID`, `AuthorNameRaw`) for both sides — required because Phase 4E proved `AuthorNameRaw` drift is exactly the silent-loss field this whole line of work exists to catch, and it is not a `ResearchPaper` column at all so would otherwise be invisible to a parent-row-only fingerprint. This directly satisfies this phase's "must include relevant child/dependency state where necessary" requirement — it was not omitted.

**Excluded, each with code-proven justification** (not silently invented): `SearchVector_En`/`SearchVector_Ar` (derived tsvector, never diffed, never drives a decision); `ScrapedAt`/`VerifiedAt`/`DoiResolvedAt` (`classify_field()` always resolves these to `KEEP_WINNER`, proven never to produce `CONFLICT`, and Phase 4F separately proved `ResearchPaper` has no generic "UpdatedAt" column at all — the fingerprint does not depend on any nonexistent timestamp column, satisfying this phase's explicit requirement); the raw `RawData_Log` blob (superseded by the derived `citations` integer — the rest of the blob never enters any decision).

**Normalization** (documented in code, not just prose): `None` and an all-whitespace/empty string normalize identically (matching `merge_plan_generator._is_empty()`'s own equivalence — a bare NULL↔'' transition changes no decision, so must not change the fingerprint); `dict` values (`CitationsByYear`, `VerificationDetails`) serialize via `json.dumps(sort_keys=True)` so key order never matters; `Authors` lists sort by `UserID` so row order never matters; `DOI` is trusted pre-normalized from `fetch_paper_row()` (no second, possibly-divergent normalization scheme introduced).

**Function signature**: `compute_plan_fingerprint(winner_id, loser_id, winner_row, loser_row, winner_authors, loser_authors, winner_citations, loser_citations)` — a pure function (no `conn` parameter; DB access is the caller's responsibility via `fetch_current_state()`, keeping the hash computation itself trivially unit-testable without any DB, mocked or real). This is the "equivalent design... if repository evidence supports something better" allowance this phase's task text explicitly offers over the suggested `build_merge_fingerprint(conn, survivor_id, loser_id)` signature — chosen because every other pure function in this project's Phase 4C–4G work (`classify_field`, `pair_confidence`, `choose_keep`, `journal_id_decision`, `author_content_conflicts`) follows the same "pure computation, separate DB-fetch wrapper" split, and mixing a live `conn` into a function whose entire job is "same input → same output, testable without a database" would break that established, working convention.

**Test evidence** (13 tests, `FingerprintDeterminism`, all passing): (1) identical unchanged input → identical fingerprint; (2) 5× repeated calls → identical; (3) `Title` change (a "meaningful parent-field change") → different; (4) `AuthorNameRaw` change (a "relevant child/dependency change" — the literal Phase 4E field) → different; (5) `CitationsByYear` dict with reordered keys → **identical** (ordering-only difference, semantically irrelevant, does not create false drift); (6) `Authors` list reordered → **identical** (same reasoning); (7) `None` vs `''` → **identical** (explicit null/empty normalization, tested); (8) `None` vs a real value → different; plus `DOI`/`JournalID`/`PublicationType`/`citations`/winner-loser-reversal change tests, all → different.

## 8. Row-Locking Design With Transaction Reasoning

**Existing precedent, re-confirmed**: `backend/analytics/reconciliation_views.py` (line ~124) uses raw-cursor `SELECT ... FOR UPDATE` inside a `transaction.atomic()` block via `connection.cursor()` — the same pattern is present in 6 other files across the repo (grep-confirmed in the prior turn, unchanged). This is the exact style `lock_pair_rows()` reuses.

**Connection/autocommit behavior through `litrix_db.db()`**: `db()` returns a plain `psycopg2.connect(...)` connection with keepalive settings — it does **not** set `autocommit=True`, so the default psycopg2 behavior (autocommit off, an implicit transaction begins on the first statement) applies. This means `lock_pair_rows()`'s `SELECT ... FOR UPDATE` call, issued on a `db()`-sourced cursor, is automatically inside an implicit transaction the moment it executes — but the caller is still responsible for explicit `commit()`/`rollback()` at the right point, which is why `lock_pair_rows()` deliberately does **not** call either: it documents ("Caller MUST already be inside a transaction... this function does not open or manage one") that transaction lifecycle is the executor's responsibility, not this prototype's.

**Two-ResearchPaper-row locking**: one statement, `SELECT "PaperID" FROM "ResearchPaper" WHERE "PaperID" = ANY(%s) FOR UPDATE`, with the array parameter always built in ascending order via `build_lock_order()`.

**Is ascending-ID ordering necessary?** — investigated, not assumed. Within *this one statement*, Postgres acquires both row locks as part of a single atomic step; there is no deadlock possible *between the two rows of one such call*. The real risk `build_lock_order()` guards against is a **future** executor either (a) splitting this into two sequential `SELECT ... FOR UPDATE` calls (one per row) instead of one `ANY()` call, or (b) two concurrent executor invocations racing over an overlapping pair of PaperIDs. The standard, textbook defense against that class of multi-row-lock deadlock is exactly what was implemented: always acquire locks in one consistent, deterministic order (ascending ID) regardless of each row's semantic role. Applying it now, at the query-building layer, means it's inherited automatically by any future change to *how* the lock is issued, rather than needing to be remembered later.

**Test evidence** (6 new tests, `SelfMergeAndLockOrderingTests`, all passing): self-merge (`winner_id == loser_id`) → `reject_self_merge()` returns `(False, reason)`; different IDs → `(True, None)`; `lock_pair_rows()` on a self-merge → raises `ValueError`, **and zero SQL is issued** (asserted directly against the mock cursor's executed-statement log — proving the reject happens before any DB call, not after); `build_lock_order(5482, 5232) == (5232, 5482)` and `build_lock_order(5232, 5482) == (5232, 5482)` (order-independent); `build_lock_order(100, 200) == build_lock_order(200, 100)`; and a `RecordingCursor` test proving the **exact array parameter** passed to the real `FOR UPDATE` SQL is `[5232, 5482]` even when `lock_pair_rows()` is called with the loser-first argument order `(5482, 5232)`.

## 9. Idempotency Verdict, Based on Actual `AuditLog` Evidence

**Schema, re-confirmed this phase**: `LogID` (autoincrement PK), `TenantID`, `UserID`, `Action`, `TargetType`, `TargetID`, `Metadata` (jsonb), `IpAddress`, `UserAgent`, `CreatedAt`. `merge_group()` already writes `Action='paper.merge.dedup'`, `TargetID=<loser PaperID>`, `Metadata={"kept_paper_id": <winner PaperID>, ...}` for every real merge.

**Not assumed sufficient — checked**: no unique constraint on `(Action, TargetID)` exists (`LogID` is a bare autoincrement PK), so `AuditLog` cannot *by itself* guarantee idempotency; it is evidence a preflight function must reason over.

**Verdict: `EXISTING_AUDITLOG_SUFFICIENT`.** Not because the table enforces anything automatically, but because `check_idempotency()`/`idempotency_verdict()` — built entirely from `AuditLog` fields already present (`Action`, `TargetID`, `Metadata.kept_paper_id`) — can already fully answer all three required scenarios: an exact prior execution (`TargetID`=loser, `Metadata.kept_paper_id`=winner), a reversed prior pair (`TargetID`=winner, `Metadata.kept_paper_id`=loser), and a contradictory third-party outcome (either ID's `Metadata.kept_paper_id` points elsewhere) — all three collapse to `ALREADY_EXECUTED` in the 3-way contract, while a malformed/missing `Metadata` row (can't be resolved from `AuditLog` content alone) correctly returns `HISTORICAL_STATE_AMBIGUOUS` rather than guessing. No new column, table, or migration was needed to reach a complete answer — only a decision function over existing, already-populated data (59 real rows, re-confirmed).

**Required prototype delivered**: `idempotency_verdict(audit_rows, winner_id, loser_id)` → one of `NOT_PREVIOUSLY_EXECUTED` / `ALREADY_EXECUTED` / `HISTORICAL_STATE_AMBIGUOUS`, plus the preserved finer-grained `IdempotencyResult` (exact/reversed/contradictory distinction not discarded, just not exposed at the coarse level unless needed).

**Test evidence** (6 new tests, `IdempotencyVerdictThreeWayTests`, all passing, on top of the 7 pre-existing `check_idempotency()` tests): no history → `NOT_PREVIOUSLY_EXECUTED`; exact prior execution → `ALREADY_EXECUTED`; reversed prior pair → `ALREADY_EXECUTED`; contradictory third-party history → `ALREADY_EXECUTED`; malformed `Metadata` → `HISTORICAL_STATE_AMBIGUOUS`; the detailed sub-result's evidence list is preserved, not discarded, in the coarse wrapper's output.

## 10. Approval-Recording Verdict, Based on Actual Repository/Schema Evidence

Every candidate this phase's task text names was investigated this phase via a fresh, read-only `information_schema` query (not reused from memory):

**Fresh schema sweep**: `SELECT table_name FROM information_schema.tables WHERE table_name ILIKE '%approv%' OR ILIKE '%review%' OR ILIKE '%decision%' OR ILIKE '%queue%'` → exactly `ReportPaperDecision` and `AuthorReviewQueue`. No fourth candidate exists beyond these two plus `AuditLog`.

| Candidate | Real schema evidence | Verdict |
|---|---|---|
| `AuditLog` | Append-only (no `UPDATE` path anywhere in this codebase for an existing row); `Metadata` is schema-free jsonb; no typed `approval_status`/`plan_fingerprint`/`approval_version` column; 59 real rows, already used for idempotency (§9) — overloading it for approval-state too would make "approval record" vs. "completed-merge record" an unenforced convention, not a schema guarantee. | **Rejected** |
| `ReportPaperDecision` | Real `Decision` (NOT NULL) + `DecidedAt` (NOT NULL) shape, 44 real rows — but keyed to a single `PaperID` plus a `SubmissionID` (a report-correction workflow, e.g. "this scraped paper's title/DOI/year looks wrong"), not a winner/loser pair; no fingerprint concept anywhere in its 11 columns. | **Rejected — wrong domain, not merely missing a column** |
| `AuthorReviewQueue` | `Status` (NOT NULL) + `ReviewedByUserID` + `ReviewedAt` + `ReviewerNotes` — genuinely the **closest structural analog** in the whole schema (a real "reviewer approves/rejects a suggestion with notes" pattern) — but keyed to a single `PaperID` plus an author-name-mapping suggestion, no pair/fingerprint concept, and 0 rows in production today. | **Rejected — wrong domain — but its shape independently confirms the `ApprovalArtifact` field design below rather than inventing it from nothing** |

**Verdict: `NEW_APPROVAL_STORAGE_REQUIRED`.** No schema change was made to reach this conclusion, and none is proposed as part of it. Per the task's explicit "do not invent a new table" instruction, no table was created. Instead, a minimal, pure-Python approval **artifact schema** (unchanged from the prior turn, `ApprovalArtifact` — a frozen dataclass, immutable by construction) carries the required 9 fields: `plan_id`, `winner_paper_id`, `loser_paper_id`, `plan_fingerprint`, `approval_status` (`PENDING`/`APPROVED`/`REJECTED`), `approver_identity`, `approval_timestamp`, `approval_version`, `reason_notes`. `validate_approval_artifact()` enforces the required binding: an artifact only authorizes execution if `approval_status == APPROVED` **and** its `plan_fingerprint` exactly matches the current one — an approval for an older plan can never approve a changed one.

**Test evidence** (2 new tests, `ApprovalStorageVerdictTests`; 6 pre-existing `ApprovalArtifactTests` re-run unchanged): `approval_storage_verdict()` returns `NEW_APPROVAL_STORAGE_REQUIRED`; the returned evidence dict substantively covers all three investigated tables (not a stub).

## 11. Canary Pair 5232/5482 Results (fresh, live, this phase)

All of the following was re-queried live this phase, not reused from a prior phase's cached output:

| Check | Result |
|---|---|
| Survivor selection (`choose_keep`) | 5232 wins (`has_doi`) — matches the established plan, unreversed |
| `pair_confidence` | `high` |
| `hard_exclusion_reason` | `None` |
| Content fingerprint | `2298ea25...44d705cb` — **byte-identical to the prior turn's live run**, proving zero drift across sessions |
| JournalID state | winner=1803, loser=`NULL` (`WINNER_ONLY` — no backfill needed) |
| AuthorNameRaw state | shared `UserID`=97, raw string **byte-identical** on both sides — zero conflict |
| DOI state | winner=`10.1155/2022/8531213`, loser=`NULL` — clean, unclaimed elsewhere |
| Child dependency state (all 7 FK slots, fresh query) | `Authors`=1/1 (identical content); `Citations`/`ExternalAuthors`/`CitationsHistory`/`ReportPaperDecision`×2/`AuthorReviewQueue` = 0/0 across the board |
| Self-merge check | N/A (5232 ≠ 5482) — `reject_self_merge()` → `(True, None)` |
| Lock order | `(5232, 5482)` regardless of call-argument order |
| Idempotency | `NOT_PREVIOUSLY_EXECUTED` (0 `AuditLog` rows for either ID) |
| Approval-storage verdict | `NEW_APPROVAL_STORAGE_REQUIRED` (no artifact exists or can exist without new storage) |
| **Canary simulation final verdict** | **`BLOCKED_APPROVAL`** |

**Which preconditions are already satisfiable with existing repo/database infrastructure**: survivor selection, pair-confidence/hard-exclusion re-checking, DOI-uniqueness re-checking, child-dependency counting, and row-locking (`SELECT...FOR UPDATE`) all reuse code and SQL patterns that already exist and work in this repository today, unmodified.

**Which are prototype-only** (built and tested this phase and the prior turn, not yet wired into any real executor): the fingerprint function, the locked-state preflight's three-stage separation, the idempotency verdict, and self-merge/lock-ordering — all exist as tested pure/thin-DB functions in `merge_execution_safety.py`, called nowhere except by tests and this report's manual validation runs.

**Which still require implementation**: an actual approval-recording mechanism (storage undecided — a future migration or a file-based workflow, per §10) is the sole remaining gap; and the executor itself (the six-step commit sequence documented in `WHAT_A_FUTURE_EXECUTOR_MUST_DO_AFTER_PREFLIGHT_SUCCESS`) does not exist anywhere — this phase, like the ones before it, produces preconditions and evidence, not an executor.

**The expected, honest output was not "ready to merge now," and it isn't**: `BLOCKED_APPROVAL`, for the correct and only reason (no approval mechanism exists to satisfy).

## 12. Test Results (exact counts)

| Suite | Count | Result |
|---|---|---|
| `backend/tools/test_merge_execution_safety.py` (extended this phase) | **68/68 passing** (54 pre-existing + 14 new) |
| `backend/tools/test_dedup_papers.py` (re-run, unchanged) | 18/18 passing |
| `backend/tools/test_merge_plan_generator.py` (re-run, unchanged) | 43/43 passing |
| **Total** | **129/129 passing** |

## 13. Remaining Blockers

One, unchanged from the prior turn's finding and re-confirmed rather than re-guessed: **no approval-recording storage mechanism exists anywhere in this repository**, and per this phase's explicit instruction, none was created. Fingerprinting, row-locking (including the newly-added self-merge rejection and deterministic lock ordering), and idempotency are now fully prototyped and tested — 129 tests total, live-validated against the real canary pair and the real, populated `AuditLog` table, not merely designed on paper.

## 14. Final Decision

**B) SOME PRECONDITIONS PROTOTYPED — IMPLEMENTATION PASS REQUIRED**

Three of the four original blockers are done to the standard this phase asked for: prototyped, deterministic, tested (including new self-merge/lock-ordering/3-way-verdict coverage this round), and validated live against real data — not claimed solved merely because a design was written (per the task's explicit caution). The fourth — approval-recording — was investigated exactly as rigorously as the other three (three real candidate tables checked against live schema evidence, none accepted, one artifact schema designed but deliberately not wired to any storage), and correctly found to require new implementation work that this phase was explicitly forbidden from doing (no schema changes, no new table). That is a real, precisely-scoped, single remaining gap — not an architectural blocker (nothing found suggests the existing `AuditLog`/`SIMPLE_CHILDREN`/`transaction.atomic()` architecture is unsafe or needs a dedicated migration before *anything* can proceed — quite the opposite, three of four pieces needed no migration at all), so **C** would overstate the problem. **A** would understate what's missing — an executor still cannot be safely built until an approval-storage decision is made and implemented. **B** is the precise fit.

Per your instructions, I am stopping here after the report and tests. Phase 4H is not started.

---

## 15. Final Confirmation Pass (2026-08-22) — No Code Changes

A follow-up request asked for a final live re-confirmation of the evidence and decision above, with **zero code changes**. Confirmed via `git status --short backend/tools/` before and after this pass: identical to §1 — `merge_execution_safety.py`/`test_merge_execution_safety.py` show no further changes, only the same modifications already reported. All values below were re-derived live, read-only, against the current database — not reused from §11's cached numbers, though they match exactly (proving continued zero drift, now across a date change to 2026-08-22 as well).

### 15.1 Live Canary Revalidation — PaperIDs 5232 / 5482

| Check | Result |
|---|---|
| `idempotency_verdict()` | **`NOT_PREVIOUSLY_EXECUTED`** (detailed: `ELIGIBLE` — "no prior paper.merge.dedup history for either PaperID") |
| `approval_storage_verdict()` | **`NEW_APPROVAL_STORAGE_REQUIRED`** |
| Self-merge check (`reject_self_merge(5232, 5482)`) | `(True, None)` — not a self-merge, passes |
| Deterministic lock order (`build_lock_order(5482, 5232)`) | `(5232, 5482)` — ascending, independent of call-argument order |
| Fingerprint | `2298ea25fc1c53b842809926bc72a5e0e77ec566e04b4f83f214a85544d705cb` |
| Staleness result | **Not stale** — matches the reference plan fingerprint exactly (`fingerprint_check.match = True`) |
| Prior `AuditLog` merge record for either ID? | **No** — 0 rows (`Action='paper.merge.dedup'`, `TargetID` in {5232, 5482}) |
| Passes every technical precondition except approval storage? | **Yes** — `run_canary_simulation()`'s step order is: pair exists → fingerprint match → duplicate safety (`pair_confidence=high`, unreversed, no hard exclusion) → DOI safety (unclaimed elsewhere) → idempotency (`ELIGIBLE`) → approval. Every step up to and including idempotency passed cleanly; the run stops **only** at the approval gate. Final verdict: **`BLOCKED_APPROVAL`**. |

### 15.2 The Three Idempotency Outcomes, Explained From Actual Repository Evidence

- **`NOT_PREVIOUSLY_EXECUTED`** — no `AuditLog` row exists with `Action='paper.merge.dedup'` and `TargetID` equal to either PaperID in the pair. This is the live result for 5232/5482 right now: the query `fetch_merge_audit_rows(cur, [5232, 5482])` returned zero rows. Nothing in the repository's audit trail has ever recorded either ID as a merged-away loser.
- **`ALREADY_EXECUTED`** — collapses three distinct evidence patterns `check_idempotency()` distinguishes internally, all meaning "history already has a completed, recorded outcome for this identity": (a) an exact match — a row with `TargetID=loser` and `Metadata.kept_paper_id=winner`, meaning this precise pair was already merged in this direction; (b) a reversed match — `TargetID=winner` and `Metadata.kept_paper_id=loser`, meaning the current "winner" was itself merged away *into* the current "loser" at some point in the past, so treating it as a survivor now would contradict recorded history; (c) contradictory — either ID's `Metadata.kept_paper_id` points at some *third*, unrelated PaperID, meaning that ID's merge history doesn't involve its current pairing partner at all. This repository has 59 real `paper.merge.dedup` rows today from genuine historical `--apply` runs — none of them reference 5232 or 5482, which is exactly why the live verdict above is `NOT_PREVIOUSLY_EXECUTED` rather than this.
- **`HISTORICAL_STATE_AMBIGUOUS`** — an `AuditLog` row's `TargetID` matches one of the two PaperIDs, but its `Metadata` is missing or lacks a `kept_paper_id` key, so which merge it actually represents can't be determined from the data alone. `AuditLog.Metadata` is an unconstrained jsonb column with no schema enforcement, so a malformed or legacy row of this shape is a real possibility the check must not silently wave through — it refuses to guess and blocks instead. No such row exists for 5232/5482 today (0 total matching rows either way), but the check exists specifically because `AuditLog`'s schema permits this shape to occur.

### 15.3 Every Remaining Blocker Before a Real Merge Executor May Be Implemented

| # | Blocker | Classification |
|---|---|---|
| 1 | No approval-recording storage exists (`AuditLog`/`ReportPaperDecision`/`AuthorReviewQueue` all investigated and rejected, §10) — only a pure-Python `ApprovalArtifact` *schema* exists, bound to no real storage | **schema/storage missing** |
| 2 | No mechanism exists for a human to actually grant an approval (who is authorized, through what interface, recorded how) | **human approval/policy required** |
| 3 | The executor itself — the six-step commit sequence (`WHAT_A_FUTURE_EXECUTOR_MUST_DO_AFTER_PREFLIGHT_SUCCESS`: apply field decisions, migrate children via `merge_group()`, write `AuditLog`, delete loser, re-check profile-preservation, commit) — does not exist in any form | **implementation missing** |
| 4 | `lock_pair_rows()` is never actually called inside a real transaction boundary by any caller today — it is a tested prototype function, not wired into any live code path | **implementation missing** |
| 5 | `AuthorReviewQueue` (`ON DELETE CASCADE`, unremapped by `merge_group()`) and `ReportPaperDecision.MissingResolvedToPaperID` (`ON DELETE SET NULL`, unremapped) remain real, uncorrected gaps in `merge_group()` itself — both are 0 rows DB-wide today, so untested in practice under a populated scenario (Phase 4B/4D/4F finding, unchanged) | **unresolved technical risk** |
| 6 | `JournalID` backfill (`LOSER_ONLY_BACKFILL` state) and any future `AuthorNameRaw`-conflict resolution policy are *planned* (Phase 4E) but not *implemented* in `merge_group()` — the field decisions the fingerprint/plan represent are not yet things the write path can act on | **implementation missing** |
| 7 | Whether `TenantID` isolation has ever been exercised against a real cross-tenant pair remains unverified (Phase 4F, restated) — not a defect, just untested territory | **unresolved technical risk** |

### 15.4 Are Locking, Stale-Plan Detection, Self-Merge Prevention, and Idempotency Sufficiently Prototyped?

**Yes, for moving from precondition *design* to executor *design*** (not executor *implementation* — that distinction matters and is preserved below):

- **Row locking**: the exact SQL (`SELECT ... FOR UPDATE`, deterministic ascending order via `build_lock_order()`) is prototyped, reuses proven repository precedent (`reconciliation_views.py` and 6 other files), and was validated live with an explicit rollback (§8, prior turn) — proving zero persistent writes. What's missing is only its *wiring into a real transaction inside a real executor*, which cannot exist before the executor itself does.
- **Stale-plan detection**: `compute_plan_fingerprint()` is deterministic (13 tests), field-scoped from actual `classify_field()` evidence rather than guessed, and has now been reproduced **identically across three separate live runs on three different days** (2026-08-21 twice, 2026-08-22) against the same unchanged pair — as strong a proof of stability as a read-only prototype can offer without an executor to feed it into.
- **Self-merge prevention**: `reject_self_merge()` is prototyped, tested, and wired into `lock_pair_rows()` itself (raises before any SQL is issued) — this one is arguably *already* at implementation quality, not just design quality, though it is still only reachable through direct function calls, not through any executor entry point.
- **Idempotency detection**: `idempotency_verdict()`/`check_idempotency()` are prototyped, tested against all required scenarios, and validated live against the real, populated `AuditLog` table (59 real historical rows, 0 relevant to the canary pair).

None of the four required a schema change, and none is blocked by missing evidence — every open question about *how* they should behave was answerable from what already exists in the repository. What remains before *executor design* specifically (not implementation) can begin is unrelated to these four: it's the approval-storage decision (§10, blocker #1/#2 above), since an executor's contract necessarily includes "what does it check to know it's allowed to run," and that answer doesn't exist yet even at the design level.

### 15.5 Final Decision (Reaffirmed)

**B) More precondition implementation is required before executor design**

Locking, stale-plan detection, self-merge prevention, and idempotency detection are sufficiently prototyped and validated — repeatedly, live, across multiple sessions — to be treated as settled *inputs* to an executor design. They are not, however, sufficient *on their own* to make executor design safe to begin, because an executor's design must specify its approval-checking contract, and no real approval-storage mechanism exists to design against yet — only an unattached artifact schema. Starting executor design today would mean designing around a placeholder for the one gate every other check in this pipeline (§15.1) funnels into. This is not a new architectural blocker (§14/§C of the original decision — nothing here suggests the existing `AuditLog`/`transaction.atomic()`/`SIMPLE_CHILDREN` architecture is unsafe), so **C** remains wrong; and it is not fully resolved either, so **A** remains premature. **B** stands.

### 15.6 Safety Accounting — This Confirmation Step Only

- Code files modified in this step: **0** (confirmed via `git status --short backend/tools/` before and after — identical to §1)
- DB writes: **0**
- Network calls: **0**
- Records merged: **0**
- DOI changes: **0**
- `--apply` executions: **0**

Per your instructions: Phase 4H is not started, no executor was built, nothing was merged, nothing was written to the database, and no existing code was modified.

## NEXT RECOMMENDED PHASE

**Phase 4H — Approval-Storage Decision & Design (read-only / design-only, no schema change executed).** Its job would be to decide *where* `ApprovalArtifact` should actually live — a real database migration (a small, dedicated, purpose-built table, now that Phase 4G has proven no existing table can safely serve this role) versus a file-based workflow — and to produce the exact schema/interface for whichever is chosen, without yet creating it. That decision is the one remaining input executor design needs; everything else (fingerprinting, locking, idempotency, self-merge prevention) is already settled. This recommendation is offered only, per your instruction — it has not been started.
