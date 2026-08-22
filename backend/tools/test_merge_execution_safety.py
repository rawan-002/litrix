"""Tests for merge_execution_safety.py -- Phase 4G. Pure-function tests plus
a mocked-cursor double for the thin DB-facing wrappers. No live DB
connection, no network, no writes. See the static-guard test at the bottom
for the automated proof that no write SQL / merge_group import / --apply /
executor loop exists anywhere in the module under test (Task F).

Run: cd backend && python tools/test_merge_execution_safety.py
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from merge_execution_safety import (  # noqa: E402
    ApprovalArtifact,
    APPROVAL_APPROVED, APPROVAL_PENDING, APPROVAL_REJECTED,
    IDEMPOTENCY_BLOCKED_CONTRADICTORY_HISTORY,
    IDEMPOTENCY_BLOCKED_EXACT_PRIOR_EXECUTION,
    IDEMPOTENCY_BLOCKED_REVERSED_PRIOR_PAIR,
    IDEMPOTENCY_ELIGIBLE,
    IDEMPOTENCY_UNKNOWN_HISTORY_BLOCKED,
    PREFLIGHT_DOI_SAFETY_FAILED,
    PREFLIGHT_DUPLICATE_SAFETY_FAILED,
    PREFLIGHT_MISSING_ROW,
    PREFLIGHT_OK,
    PREFLIGHT_REVERSED,
    PREFLIGHT_STALE_FINGERPRINT,
    SIM_BLOCKED_APPROVAL,
    SIM_BLOCKED_IDEMPOTENCY,
    SIM_BLOCKED_SAFETY_CONFLICT,
    SIM_BLOCKED_STALE_PLAN,
    SIM_READY,
    check_idempotency,
    compute_plan_fingerprint,
    fetch_current_state,
    is_doi_claimed_elsewhere,
    lock_pair_rows,
    run_canary_simulation,
    validate_against_plan,
    validate_approval_artifact,
    approval_storage_verdict,
    build_lock_order,
    idempotency_verdict,
    reject_self_merge,
    APPROVAL_VERDICT_NEW_STORAGE_REQUIRED,
    IDEMPOTENCY_ALREADY_EXECUTED,
    IDEMPOTENCY_HISTORICAL_STATE_AMBIGUOUS,
    IDEMPOTENCY_NOT_PREVIOUSLY_EXECUTED,
    fetch_paper_tenant_ids,
    validate_same_tenant,
    is_case_only_difference,
)
from merge_plan_generator import RP_COLUMNS  # noqa: E402


def rp_row(**overrides):
    row = {c: None for c in RP_COLUMNS}
    row["PaperID"] = overrides.pop("PaperID", 1)
    row.update(overrides)
    return row


BASE_WINNER = rp_row(PaperID=5232, DOI="10.1155/2022/8531213", JournalID=1803, Title="Optimal deep learning model",
                      PubYear=2022, TenantID=1, IsVerified=True, Source="Scholar",
                      PublicationType="Research Article")
BASE_LOSER = rp_row(PaperID=5482, DOI=None, JournalID=None, Title="Research Article Optimal deep learning model",
                     PubYear=2022, TenantID=1, IsVerified=True, Source="Scholar",
                     PublicationType="Research Article")
BASE_WINNER_AUTHORS = [{"UserID": 97, "AuthorNameRaw": "H Alshammari, K Gasmi"}]
BASE_LOSER_AUTHORS = [{"UserID": 97, "AuthorNameRaw": "H Alshammari, K Gasmi"}]


# ===========================================================================
# TASK A -- fingerprint tests
# ===========================================================================

class FingerprintDeterminism(unittest.TestCase):
    def _fp(self, winner=None, loser=None, wa=None, la=None, wc=10, lc=5):
        return compute_plan_fingerprint(
            5232, 5482, winner or BASE_WINNER, loser or BASE_LOSER,
            wa if wa is not None else BASE_WINNER_AUTHORS,
            la if la is not None else BASE_LOSER_AUTHORS, wc, lc)

    def test_identical_input_identical_fingerprint(self):
        self.assertEqual(self._fp(), self._fp())

    def test_deterministic_across_repeated_calls(self):
        fps = {self._fp() for _ in range(5)}
        self.assertEqual(len(fps), 1)

    def test_title_change_produces_different_fingerprint(self):
        changed = dict(BASE_WINNER, Title="A completely different title")
        self.assertNotEqual(self._fp(), self._fp(winner=changed))

    def test_doi_change_produces_different_fingerprint(self):
        changed = dict(BASE_WINNER, DOI="10.1155/2099/9999999")
        self.assertNotEqual(self._fp(), self._fp(winner=changed))

    def test_journal_id_change_produces_different_fingerprint(self):
        changed = dict(BASE_WINNER, JournalID=9999)
        self.assertNotEqual(self._fp(), self._fp(winner=changed))

    def test_conflict_producing_field_change_produces_different_fingerprint(self):
        changed = dict(BASE_LOSER, PublicationType="Conference Paper")
        self.assertNotEqual(self._fp(), self._fp(loser=changed))

    def test_author_name_raw_change_produces_different_fingerprint(self):
        """The exact Phase 4E silent-loss field -- must be load-bearing."""
        changed_authors = [{"UserID": 97, "AuthorNameRaw": "A totally different raw name"}]
        self.assertNotEqual(self._fp(), self._fp(la=changed_authors))

    def test_none_and_empty_string_normalize_identically(self):
        none_version = dict(BASE_WINNER, Abstract=None)
        empty_version = dict(BASE_WINNER, Abstract="   ")
        self.assertEqual(self._fp(winner=none_version), self._fp(winner=empty_version))

    def test_none_vs_real_value_differs(self):
        none_version = dict(BASE_WINNER, Abstract=None)
        real_version = dict(BASE_WINNER, Abstract="A real abstract")
        self.assertNotEqual(self._fp(winner=none_version), self._fp(winner=real_version))

    def test_dict_key_order_does_not_affect_fingerprint(self):
        a = dict(BASE_WINNER, CitationsByYear={"2020": 1, "2021": 2})
        b = dict(BASE_WINNER, CitationsByYear={"2021": 2, "2020": 1})
        self.assertEqual(self._fp(winner=a), self._fp(winner=b))

    def test_author_list_order_does_not_affect_fingerprint(self):
        two_authors_a = [{"UserID": 1, "AuthorNameRaw": "A"}, {"UserID": 2, "AuthorNameRaw": "B"}]
        two_authors_b = [{"UserID": 2, "AuthorNameRaw": "B"}, {"UserID": 1, "AuthorNameRaw": "A"}]
        self.assertEqual(self._fp(wa=two_authors_a), self._fp(wa=two_authors_b))

    def test_process_timestamp_fields_are_not_part_of_the_fingerprint_inputs(self):
        """ScrapedAt/VerifiedAt/DoiResolvedAt are excluded by design -- not
        in FINGERPRINT_RP_FIELDS, so changing them on the input row (which
        callers might still pass through) must not change the fingerprint."""
        changed = dict(BASE_WINNER, ScrapedAt="2099-01-01", VerifiedAt="2099-01-01", DoiResolvedAt="2099-01-01")
        self.assertEqual(self._fp(), self._fp(winner=changed))

    def test_winner_loser_reversal_changes_fingerprint(self):
        reversed_fp = compute_plan_fingerprint(5482, 5232, BASE_LOSER, BASE_WINNER,
                                                BASE_LOSER_AUTHORS, BASE_WINNER_AUTHORS, 5, 10)
        self.assertNotEqual(self._fp(), reversed_fp)

    def test_citations_change_produces_different_fingerprint(self):
        self.assertNotEqual(self._fp(wc=10), self._fp(wc=999))


# ===========================================================================
# Phase 4P.1 -- JSON-representation-equivalence tests. Reproduces the exact
# real-world defect (Phase 4P): Django's connection.cursor(), used for raw
# SQL, hands back a jsonb column as an undecoded JSON string; a bare
# psycopg2.connect() connection (litrix_db.db()) auto-decodes the same
# column into a native dict. Both are the SAME underlying data -- proven
# live, Phase 4P.1 Task A (json.loads(django_str) == raw_dict, for both
# CitationsByYear and VerificationDetails on the real canary pair) -- so a
# fingerprint computed from one representation must equal a fingerprint
# computed from the other. These tests use literal Python values
# constructed by hand to stand in for "what each connection type would
# have handed back", not a live DB connection -- exactly matching this
# file's own established mocked-test convention.
# ===========================================================================

class JsonRepresentationEquivalenceTests(unittest.TestCase):
    def _fp_with_field(self, field_value):
        winner = dict(BASE_WINNER, VerificationDetails=field_value)
        return compute_plan_fingerprint(5232, 5482, winner, BASE_LOSER,
                                         BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5)

    def test_dict_and_equivalent_json_string_produce_same_fingerprint(self):
        """The exact real-world case: a psycopg2-decoded dict vs. the same
        column arriving as an undecoded JSON string via Django's cursor."""
        as_dict = {"tier": "openalex", "country": "SA", "confidence": "fallback"}
        as_json_string = '{"tier": "openalex", "country": "SA", "confidence": "fallback"}'
        self.assertEqual(self._fp_with_field(as_dict), self._fp_with_field(as_json_string))

    def test_nested_objects_normalize_identically_regardless_of_source_representation(self):
        as_dict = {"tier": "openalex", "evidence_trail": [{"reason": "html_http_403", "tier": "publisher_html"},
                                                            {"tier": "pdf", "reason": "pdf_http_403"}]}
        as_json_string = json.dumps(as_dict)
        self.assertEqual(self._fp_with_field(as_dict), self._fp_with_field(as_json_string))

    def test_key_order_variants_normalize_identically_via_the_string_path(self):
        """Not just dict-vs-dict (already proven elsewhere in this file) --
        a JSON STRING with keys in a different order must also normalize
        to the same fingerprint once parsed and re-canonicalized."""
        s1 = '{"a": 1, "b": 2, "c": 3}'
        s2 = '{"c": 3, "a": 1, "b": 2}'
        self.assertEqual(self._fp_with_field(s1), self._fp_with_field(s2))

    def test_list_element_order_remains_significant_through_the_string_path(self):
        s_ordered = '[{"reason": "a"}, {"reason": "b"}]'
        s_reversed = '[{"reason": "b"}, {"reason": "a"}]'
        self.assertNotEqual(self._fp_with_field(s_ordered), self._fp_with_field(s_reversed))

    def test_list_nested_dict_key_order_does_not_affect_fingerprint(self):
        """List order matters; the order of KEYS within a dict nested
        inside that list must not -- sort_keys applies recursively."""
        s1 = '[{"a": 1, "b": 2}]'
        s2 = '[{"b": 2, "a": 1}]'
        self.assertEqual(self._fp_with_field(s1), self._fp_with_field(s2))

    def test_ordinary_string_that_is_a_bare_json_scalar_is_not_reinterpreted(self):
        """A string like "123" or "null" is valid JSON in isolation, but
        does not start with '{' or '[' -- _looks_like_json_container()
        must never even attempt to parse it, so it is treated as ordinary
        text, exactly as before this fix."""
        self.assertNotEqual(self._fp_with_field("123"), self._fp_with_field(123))
        fp_bare_null_string = self._fp_with_field("null")
        fp_none = self._fp_with_field(None)
        self.assertNotEqual(fp_bare_null_string, fp_none,
                             "the literal text 'null' must stay a string, not become Python None")

    def test_ordinary_object_shaped_text_field_is_unaffected_when_not_valid_json(self):
        """A hypothetical free-text value that happens to start and end
        with braces but is not valid JSON (e.g. a pasted code fragment in
        an Abstract) must fall back to being treated as plain text, not
        crash and not silently vanish."""
        text = "{this is not json, just a stray brace in an abstract}"
        result = self._fp_with_field(text)
        self.assertIsInstance(result, str)  # a real fingerprint was produced, not an exception
        # And it must differ from the empty/None case -- the text content still matters.
        self.assertNotEqual(result, self._fp_with_field(None))

    def test_malformed_json_like_string_does_not_crash_fingerprint_computation(self):
        malformed_but_brace_wrapped = "{not: valid, json: at, all: True,}"
        try:
            result = self._fp_with_field(malformed_but_brace_wrapped)
        except Exception as e:  # noqa: BLE001 -- explicitly asserting NO exception of any kind
            self.fail(f"malformed JSON-like string crashed fingerprint computation: {e!r}")
        self.assertIsInstance(result, str)

    def test_empty_json_object_and_array_strings_normalize_to_none(self):
        """Matches the existing dict/list-length-zero -> None rule --
        '{}' and '[]' arriving as strings must normalize the same way an
        already-empty dict/list does."""
        self.assertEqual(self._fp_with_field("{}"), self._fp_with_field({}))
        self.assertEqual(self._fp_with_field("[]"), self._fp_with_field([]))
        self.assertEqual(self._fp_with_field("{}"), self._fp_with_field(None))

    def test_real_canary_verification_details_dict_and_json_string_forms_match(self):
        """Uses the REAL field shape from the live canary pair's actual
        VerificationDetails value (Phase 4P.1 Task A), not a simplified
        stand-in -- proving the fix on the exact real-world data shape
        that originally exposed the defect."""
        real_value = {
            "ror": "https://ror.org/0403jak37", "tier": "openalex", "country": "SA",
            "confidence": "fallback", "decision_basis": "openalex_albaha_match",
            "evidence_trail": [
                {"url": "https://onlinelibrary.wiley.com/doi/10.1155/2022/8531213",
                 "tier": "publisher_html", "reason": "html_http_403"},
                {"url": "https://downloads.hindawi.com/journals/wcmc/2022/8531213.pdf",
                 "tier": "pdf", "reason": "pdf_http_403"},
                {"ror": "https://ror.org/0403jak37", "tier": "openalex", "country": "SA",
                 "matched_author": "Moez Krichen", "matched_substring": "al baha univ",
                 "matched_institution": "Al Baha University"},
                {"tier": "crossref", "reason": "crossref_incomplete_affiliations",
                 "authors": 7, "authors_with_affiliation": 0},
            ],
            "matched_author": "Moez Krichen", "official_source": False,
            "verifier_version": "3.0.0", "matched_substring": "al baha univ",
            "matched_institution": "Al Baha University",
        }
        as_json_string = json.dumps(real_value)  # simulates Django's undecoded-string representation
        self.assertEqual(self._fp_with_field(real_value), self._fp_with_field(as_json_string))

    def test_genuinely_changed_value_still_produces_different_fingerprint_via_string_path(self):
        """The fix must not make the fingerprint insensitive to real
        changes -- only to representation differences."""
        s1 = '{"tier": "openalex", "confidence": "fallback"}'
        s2 = '{"tier": "openalex", "confidence": "high"}'
        self.assertNotEqual(self._fp_with_field(s1), self._fp_with_field(s2))


# ===========================================================================
# Mocked cursor double for Task B / Task C DB-facing tests
# ===========================================================================

class FakeCursor:
    """Minimal SELECT-only fake -- routes on distinctive SQL substrings.
    Raises AssertionError if any write-verb SQL is ever passed to it, as a
    second, execution-time safety net on top of the static source scan."""

    WRITE_VERBS = ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "ALTER ", "DROP ", "CREATE ")

    def __init__(self, research_papers=None, authors=None, audit_rows=None):
        self.research_papers = research_papers or {}   # PaperID -> dict of RP_COLUMNS+PaperID
        self.authors = authors or {}                    # PaperID -> [{"UserID":..,"AuthorNameRaw":..}]
        self.audit_rows = audit_rows or []               # list of (LogID, TargetID, Metadata, CreatedAt)
        self._result = None
        self.executed_sql = []

    def execute(self, sql, params=None):
        sql_upper = sql.upper()
        for verb in self.WRITE_VERBS:
            self.assertNotWrite(verb in sql_upper, verb, sql)
        self.executed_sql.append(sql)

        if 'FROM "AUDITLOG"' in sql_upper:
            target_ids = set(params[1]) if params and len(params) > 1 else set()
            self._result = [
                (log_id, tid, meta, created)
                for (log_id, tid, meta, created) in self.audit_rows
                if tid in target_ids
            ]
            return

        if "COALESCE" in sql_upper and 'FROM "RESEARCHPAPER"' in sql_upper:
            pid = params[0]
            row = self.research_papers.get(pid)
            self._result = [(row.get("_citations", 0),)] if row else []
            return

        if "LOWER(\"DOI\")" in sql_upper:
            doi, exclude = params
            hits = [pid for pid, r in self.research_papers.items()
                    if r.get("DOI") and r["DOI"].lower() == doi.lower() and pid not in exclude]
            self._result = [(hits[0],)] if hits else []
            return

        if "FOR UPDATE" in sql_upper:
            ids = params[0]
            self._result = [(pid,) for pid in ids if pid in self.research_papers]
            return

        if 'FROM "AUTHORS"' in sql_upper:
            pid = params[0]
            rows = self.authors.get(pid, [])
            self._result = [(a["UserID"], a["AuthorNameRaw"]) for a in rows]
            return

        if 'FROM "RESEARCHPAPER"' in sql_upper and '"JOURNALID"' in sql_upper:
            pid = params[0]
            row = self.research_papers.get(pid)
            if not row:
                self._result = []
                return
            from merge_plan_generator import RP_COLUMNS as cols
            self._result = [tuple(row.get(c) for c in (["PaperID"] + cols))]
            return

        raise AssertionError(f"FakeCursor cannot route this SQL: {sql!r}")

    def assertNotWrite(self, is_write, verb, sql):
        if is_write:
            raise AssertionError(f"FakeCursor detected a write verb {verb!r} in SQL passed to it: {sql!r}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


def _paper_dict(**overrides):
    d = dict(BASE_WINNER)
    d.update(overrides)
    return d


# ===========================================================================
# TASK B -- locked-state preflight tests
# ===========================================================================

class FetchCurrentStateTests(unittest.TestCase):
    def test_missing_row_returns_none(self):
        cur = FakeCursor(research_papers={5232: _paper_dict(PaperID=5232, _citations=1)})
        w, l, wa, la, wc, lc = fetch_current_state(cur, 5232, 999999)
        self.assertIsNotNone(w)
        self.assertIsNone(l)

    def test_existing_rows_returned(self):
        cur = FakeCursor(
            research_papers={5232: _paper_dict(PaperID=5232, _citations=10),
                              5482: _paper_dict(PaperID=5482, DOI=None, _citations=5)},
            authors={5232: BASE_WINNER_AUTHORS, 5482: BASE_LOSER_AUTHORS},
        )
        w, l, wa, la, wc, lc = fetch_current_state(cur, 5232, 5482)
        self.assertEqual(w["PaperID"], 5232)
        self.assertEqual(l["PaperID"], 5482)
        self.assertEqual(wc, 10)
        self.assertEqual(lc, 5)


class LockPairRowsTests(unittest.TestCase):
    def test_both_rows_lockable_returns_true(self):
        cur = FakeCursor(research_papers={5232: _paper_dict(PaperID=5232), 5482: _paper_dict(PaperID=5482)})
        self.assertTrue(lock_pair_rows(cur, 5232, 5482))
        self.assertTrue(any("FOR UPDATE" in s.upper() for s in cur.executed_sql))

    def test_missing_row_returns_false(self):
        cur = FakeCursor(research_papers={5232: _paper_dict(PaperID=5232)})
        self.assertFalse(lock_pair_rows(cur, 5232, 999999))

    def test_no_write_sql_ever_issued(self):
        """'FOR UPDATE' (the legitimate row-lock clause) legitimately
        contains the substring 'UPDATE' -- checked precisely here (an
        UPDATE *statement* starts with 'UPDATE "TableName" SET', which
        this must never contain), not via a naive substring match."""
        cur = FakeCursor(research_papers={5232: _paper_dict(PaperID=5232), 5482: _paper_dict(PaperID=5482)})
        lock_pair_rows(cur, 5232, 5482)
        for sql in cur.executed_sql:
            self.assertNotIn("DELETE", sql.upper())
            self.assertNotIn("INSERT", sql.upper())
            self.assertNotRegex(sql.upper(), r'^\s*UPDATE\s+"')


class SelfMergeAndLockOrderingTests(unittest.TestCase):
    """Phase 4G (executor-preconditions round): self-merge rejection and
    deterministic ascending lock order, prototyped at the query-building
    layer -- see build_lock_order()'s docstring for why this matters even
    though a single ANY() statement locks both rows atomically."""

    def test_reject_self_merge_same_id(self):
        ok, reason = reject_self_merge(5232, 5232)
        self.assertFalse(ok)
        self.assertIn("5232", reason)

    def test_reject_self_merge_different_ids_is_fine(self):
        ok, reason = reject_self_merge(5232, 5482)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_lock_pair_rows_raises_on_self_merge_before_any_sql(self):
        cur = FakeCursor(research_papers={5232: _paper_dict(PaperID=5232)})
        with self.assertRaises(ValueError):
            lock_pair_rows(cur, 5232, 5232)
        self.assertEqual(cur.executed_sql, [], "no SQL may be issued for a rejected self-merge")

    def test_lock_order_is_always_ascending(self):
        self.assertEqual(build_lock_order(5482, 5232), (5232, 5482))
        self.assertEqual(build_lock_order(5232, 5482), (5232, 5482))

    def test_lock_order_independent_of_winner_loser_role(self):
        """Whichever ID is 'winner' vs 'loser' must not change the
        acquisition order -- only numeric ID value decides it."""
        self.assertEqual(build_lock_order(100, 200), build_lock_order(200, 100))

    def test_lock_pair_rows_issues_ids_in_ascending_order(self):
        """The exact array parameter passed to the FOR UPDATE query must be
        ascending regardless of (winner_id, loser_id) argument order."""
        captured = {}

        class RecordingCursor(FakeCursor):
            def execute(self, sql, params=None):
                if params and "FOR UPDATE" in sql.upper():
                    captured["ids"] = list(params[0])
                return super().execute(sql, params)

        cur = RecordingCursor(research_papers={5232: _paper_dict(PaperID=5232), 5482: _paper_dict(PaperID=5482)})
        lock_pair_rows(cur, 5482, 5232)  # loser-first argument order
        self.assertEqual(captured.get("ids"), [5232, 5482])


class ValidateSameTenantTests(unittest.TestCase):
    """Phase 4U -- cross-tenant isolation guard. validate_same_tenant() is
    pure (no DB access), mirroring reject_self_merge()'s exact shape, so it
    can be reused unmodified at both real enforcement boundaries
    (merge_approval.py::create_pending_approval(),
    merge_executor.py::execute_approved_merge())."""

    def test_same_tenant_passes(self):
        ok, reason = validate_same_tenant(1, 1)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_different_tenants_rejected(self):
        ok, reason = validate_same_tenant(1, 2)
        self.assertFalse(ok)
        self.assertIn("cross-tenant", reason)
        self.assertIn("1", reason)
        self.assertIn("2", reason)

    def test_survivor_tenant_none_rejected(self):
        ok, reason = validate_same_tenant(None, 1)
        self.assertFalse(ok)
        self.assertIn("resolvable TenantID", reason)

    def test_loser_tenant_none_rejected(self):
        ok, reason = validate_same_tenant(1, None)
        self.assertFalse(ok)
        self.assertIn("resolvable TenantID", reason)

    def test_both_tenants_none_rejected_not_silently_matched(self):
        """None == None must NOT be treated as a passing match -- two
        unresolvable papers 'matching' would be a meaningless pass."""
        ok, reason = validate_same_tenant(None, None)
        self.assertFalse(ok)


class IsCaseOnlyDifferenceTests(unittest.TestCase):
    """Phase 4Y -- narrow case-only AuthorNameRaw classifier. Pure, no DB
    access. Deliberately far narrower than disambiguation/pipeline.py's
    normalize_name() (Phase 4X's investigated tool): the ONLY
    transformation is str.lower() -- no punctuation stripping, no
    whitespace collapsing, no diacritic removal, no Unicode normalization,
    no fuzzy/substring matching, no token reordering."""

    # 1. identical names
    def test_identical_names_not_a_difference(self):
        ok, reason = is_case_only_difference("A Chakrabarty", "A Chakrabarty")
        self.assertFalse(ok)
        self.assertIn("no difference", reason)

    # 2. exact case-only difference
    def test_exact_case_only_difference_detected(self):
        ok, reason = is_case_only_difference("MH Al-adaileh", "MH Al-Adaileh")
        self.assertTrue(ok)
        self.assertIn("letter case", reason)

    # 3. punctuation difference -> NOT case-only
    def test_punctuation_difference_not_case_only(self):
        ok, _ = is_case_only_difference("Al-Amin", "Al Amin")
        self.assertFalse(ok)

    # 4. whitespace difference -> NOT case-only
    def test_whitespace_difference_not_case_only(self):
        ok, _ = is_case_only_difference("A  Chakrabarty", "A Chakrabarty")
        self.assertFalse(ok)

    # 5. tokenization difference -> NOT case-only
    def test_tokenization_difference_not_case_only(self):
        ok, _ = is_case_only_difference("NB Aoun", "N Ben Aoun")
        self.assertFalse(ok)

    # 6. token-count difference -> NOT case-only
    def test_token_count_difference_not_case_only(self):
        ok, _ = is_case_only_difference("AM Alomari", "A Alomari, F Comeau")
        self.assertFalse(ok)

    # 7. diacritic difference -> NOT case-only
    def test_diacritic_difference_not_case_only(self):
        ok, _ = is_case_only_difference("Salem", "Salém")
        self.assertFalse(ok)

    # 8. reordered tokens -> NOT case-only
    def test_reordered_tokens_not_case_only(self):
        ok, _ = is_case_only_difference("Ahmed Salem", "Salem Ahmed")
        self.assertFalse(ok)

    # 9. missing value vs non-missing -> NOT case-only
    def test_missing_vs_present_not_case_only(self):
        ok, reason = is_case_only_difference(None, "A Chakrabarty")
        self.assertFalse(ok)
        self.assertIn("missing", reason)
        ok2, reason2 = is_case_only_difference("A Chakrabarty", None)
        self.assertFalse(ok2)
        self.assertIn("missing", reason2)
        ok3, reason3 = is_case_only_difference("", "A Chakrabarty")
        self.assertFalse(ok3)

    # 10. both missing -> NOT equivalent
    def test_both_none_not_treated_as_equivalent(self):
        ok, _ = is_case_only_difference(None, None)
        self.assertFalse(ok)

    def test_both_empty_string_not_treated_as_equivalent(self):
        ok, _ = is_case_only_difference("", "")
        self.assertFalse(ok)

    # 11. Al-Amin vs Al Amin -> NOT case-only (Phase 4X's proven collision pair)
    def test_al_amin_collision_pair_not_case_only(self):
        ok, _ = is_case_only_difference("Al-Amin", "Al Amin")
        self.assertFalse(ok)
        ok2, _ = is_case_only_difference("al-amin", "al amin")
        self.assertFalse(ok2)

    # 12. D'Angelo vs D Angelo -> NOT case-only (Phase 4X's proven collision pair)
    def test_dangelo_collision_pair_not_case_only(self):
        ok, _ = is_case_only_difference("D'Angelo", "D Angelo")
        self.assertFalse(ok)


class FetchPaperTenantIdsTests(unittest.TestCase):
    """Thin DB-facing wrapper -- mocked cursor, no live DB."""

    class _TenantFakeCursor:
        def __init__(self, tenants):
            self.tenants = tenants  # PaperID -> TenantID
            self._result = None

        def execute(self, sql, params=None):
            (ids,) = params
            self._result = [(pid, self.tenants[pid]) for pid in ids if pid in self.tenants]

        def fetchall(self):
            return list(self._result or [])

    def test_both_exist_same_tenant(self):
        cur = self._TenantFakeCursor({5232: 1, 5482: 1})
        self.assertEqual(fetch_paper_tenant_ids(cur, 5232, 5482), (1, 1))

    def test_both_exist_different_tenants(self):
        cur = self._TenantFakeCursor({5232: 1, 5482: 2})
        self.assertEqual(fetch_paper_tenant_ids(cur, 5232, 5482), (1, 2))

    def test_missing_survivor_returns_none_for_it(self):
        cur = self._TenantFakeCursor({5482: 1})
        self.assertEqual(fetch_paper_tenant_ids(cur, 5232, 5482), (None, 1))

    def test_missing_loser_returns_none_for_it(self):
        cur = self._TenantFakeCursor({5232: 1})
        self.assertEqual(fetch_paper_tenant_ids(cur, 5232, 5482), (1, None))

    def test_both_missing_returns_none_none(self):
        cur = self._TenantFakeCursor({})
        self.assertEqual(fetch_paper_tenant_ids(cur, 5232, 5482), (None, None))


class ValidateAgainstPlanTests(unittest.TestCase):
    def _plan(self, **overrides):
        base = {"survivor": 5232, "loser": 5482, "plan_fingerprint": "abc123"}
        base.update(overrides)
        return base

    def test_ok_when_everything_matches(self):
        fp = compute_plan_fingerprint(5232, 5482, BASE_WINNER, BASE_LOSER,
                                       BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5)
        result = validate_against_plan(self._plan(plan_fingerprint=fp), BASE_WINNER, BASE_LOSER,
                                        BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5,
                                        "high", None, False)
        self.assertEqual(result.status, PREFLIGHT_OK)
        self.assertTrue(result.passed)

    def test_missing_row(self):
        result = validate_against_plan(self._plan(), None, BASE_LOSER, [], [], 0, 0, "high", None, False)
        self.assertEqual(result.status, PREFLIGHT_MISSING_ROW)

    def test_reversed_winner_loser(self):
        result = validate_against_plan(self._plan(survivor=5482, loser=5232), BASE_WINNER, BASE_LOSER,
                                        BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5, "high", None, False)
        self.assertEqual(result.status, PREFLIGHT_REVERSED)

    def test_stale_fingerprint(self):
        result = validate_against_plan(self._plan(plan_fingerprint="stale-value"), BASE_WINNER, BASE_LOSER,
                                        BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5, "high", None, False)
        self.assertEqual(result.status, PREFLIGHT_STALE_FINGERPRINT)

    def test_duplicate_safety_failed_low_confidence(self):
        fp = compute_plan_fingerprint(5232, 5482, BASE_WINNER, BASE_LOSER,
                                       BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5)
        result = validate_against_plan(self._plan(plan_fingerprint=fp), BASE_WINNER, BASE_LOSER,
                                        BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5, "review", None, False)
        self.assertEqual(result.status, PREFLIGHT_DUPLICATE_SAFETY_FAILED)

    def test_duplicate_safety_failed_hard_exclusion(self):
        fp = compute_plan_fingerprint(5232, 5482, BASE_WINNER, BASE_LOSER,
                                       BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5)
        result = validate_against_plan(self._plan(plan_fingerprint=fp), BASE_WINNER, BASE_LOSER,
                                        BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5,
                                        "high", "different_doi_and_year_gap_gt_2", False)
        self.assertEqual(result.status, PREFLIGHT_DUPLICATE_SAFETY_FAILED)

    def test_doi_safety_failed(self):
        fp = compute_plan_fingerprint(5232, 5482, BASE_WINNER, BASE_LOSER,
                                       BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5)
        result = validate_against_plan(self._plan(plan_fingerprint=fp), BASE_WINNER, BASE_LOSER,
                                        BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 10, 5, "high", None, True)
        self.assertEqual(result.status, PREFLIGHT_DOI_SAFETY_FAILED)


class DoiClaimedElsewhereTests(unittest.TestCase):
    def test_no_doi_is_never_a_conflict(self):
        cur = FakeCursor(research_papers={})
        self.assertFalse(is_doi_claimed_elsewhere(cur, None, [1, 2]))

    def test_doi_claimed_by_another_row(self):
        cur = FakeCursor(research_papers={99: _paper_dict(PaperID=99, DOI="10.1/x")})
        self.assertTrue(is_doi_claimed_elsewhere(cur, "10.1/x", [1, 2]))

    def test_doi_not_claimed_elsewhere(self):
        cur = FakeCursor(research_papers={})
        self.assertFalse(is_doi_claimed_elsewhere(cur, "10.1/x", [1, 2]))


# ===========================================================================
# TASK C -- idempotency preflight tests
# ===========================================================================

class IdempotencyPreflightTests(unittest.TestCase):
    def test_no_prior_execution_is_eligible(self):
        result = check_idempotency([], 5232, 5482)
        self.assertTrue(result.eligible)
        self.assertEqual(result.status, IDEMPOTENCY_ELIGIBLE)

    def test_exact_prior_execution_blocks(self):
        rows = [{"LogID": 1, "TargetID": 5482, "Metadata": {"kept_paper_id": 5232}, "CreatedAt": "x"}]
        result = check_idempotency(rows, 5232, 5482)
        self.assertEqual(result.status, IDEMPOTENCY_BLOCKED_EXACT_PRIOR_EXECUTION)
        self.assertFalse(result.eligible)

    def test_reversed_prior_pair_blocks(self):
        rows = [{"LogID": 2, "TargetID": 5232, "Metadata": {"kept_paper_id": 5482}, "CreatedAt": "x"}]
        result = check_idempotency(rows, 5232, 5482)
        self.assertEqual(result.status, IDEMPOTENCY_BLOCKED_REVERSED_PRIOR_PAIR)

    def test_ambiguous_malformed_metadata_blocks(self):
        rows = [{"LogID": 3, "TargetID": 5482, "Metadata": {"note": "no kept_paper_id here"}, "CreatedAt": "x"}]
        result = check_idempotency(rows, 5232, 5482)
        self.assertEqual(result.status, IDEMPOTENCY_UNKNOWN_HISTORY_BLOCKED)

    def test_ambiguous_null_metadata_blocks(self):
        rows = [{"LogID": 4, "TargetID": 5482, "Metadata": None, "CreatedAt": "x"}]
        result = check_idempotency(rows, 5232, 5482)
        self.assertEqual(result.status, IDEMPOTENCY_UNKNOWN_HISTORY_BLOCKED)

    def test_contradictory_history_third_paper_blocks(self):
        rows = [{"LogID": 5, "TargetID": 5482, "Metadata": {"kept_paper_id": 9999}, "CreatedAt": "x"}]
        result = check_idempotency(rows, 5232, 5482)
        self.assertEqual(result.status, IDEMPOTENCY_BLOCKED_CONTRADICTORY_HISTORY)

    def test_unrelated_audit_history_does_not_falsely_block(self):
        """Rows for entirely different PaperIDs must never even reach
        check_idempotency (fetch_merge_audit_rows scopes by TargetID = ANY
        ([winner,loser])) -- simulated here by simply not including them,
        proving an empty/unrelated-only result set is still ELIGIBLE."""
        result = check_idempotency([], 5232, 5482)
        self.assertTrue(result.eligible)

    def test_fetch_merge_audit_rows_scopes_correctly_via_fake_cursor(self):
        from merge_execution_safety import fetch_merge_audit_rows
        cur = FakeCursor(audit_rows=[
            (1, 5482, {"kept_paper_id": 5232}, "t1"),
            (2, 99999, {"kept_paper_id": 88888}, "t2"),  # unrelated pair
        ])
        rows = fetch_merge_audit_rows(cur, [5232, 5482])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["TargetID"], 5482)


# ===========================================================================
# TASK D -- approval artifact tests
# ===========================================================================

class IdempotencyVerdictThreeWayTests(unittest.TestCase):
    """This phase's requested coarse contract: NOT_PREVIOUSLY_EXECUTED /
    ALREADY_EXECUTED / HISTORICAL_STATE_AMBIGUOUS, built on top of the
    already-tested check_idempotency() logic."""

    def test_no_history_is_not_previously_executed(self):
        verdict, detailed = idempotency_verdict([], 5232, 5482)
        self.assertEqual(verdict, IDEMPOTENCY_NOT_PREVIOUSLY_EXECUTED)

    def test_exact_prior_execution_is_already_executed(self):
        rows = [{"LogID": 1, "TargetID": 5482, "Metadata": {"kept_paper_id": 5232}, "CreatedAt": "x"}]
        verdict, detailed = idempotency_verdict(rows, 5232, 5482)
        self.assertEqual(verdict, IDEMPOTENCY_ALREADY_EXECUTED)

    def test_reversed_prior_pair_is_already_executed(self):
        rows = [{"LogID": 2, "TargetID": 5232, "Metadata": {"kept_paper_id": 5482}, "CreatedAt": "x"}]
        verdict, detailed = idempotency_verdict(rows, 5232, 5482)
        self.assertEqual(verdict, IDEMPOTENCY_ALREADY_EXECUTED)

    def test_contradictory_history_is_already_executed(self):
        rows = [{"LogID": 3, "TargetID": 5482, "Metadata": {"kept_paper_id": 9999}, "CreatedAt": "x"}]
        verdict, detailed = idempotency_verdict(rows, 5232, 5482)
        self.assertEqual(verdict, IDEMPOTENCY_ALREADY_EXECUTED)

    def test_malformed_metadata_is_historical_state_ambiguous(self):
        rows = [{"LogID": 4, "TargetID": 5482, "Metadata": None, "CreatedAt": "x"}]
        verdict, detailed = idempotency_verdict(rows, 5232, 5482)
        self.assertEqual(verdict, IDEMPOTENCY_HISTORICAL_STATE_AMBIGUOUS)

    def test_detailed_result_is_preserved_not_discarded(self):
        rows = [{"LogID": 1, "TargetID": 5482, "Metadata": {"kept_paper_id": 5232}, "CreatedAt": "x"}]
        verdict, detailed = idempotency_verdict(rows, 5232, 5482)
        self.assertEqual(detailed.evidence, rows)


class ApprovalStorageVerdictTests(unittest.TestCase):
    def test_verdict_is_new_storage_required(self):
        verdict, evidence = approval_storage_verdict()
        self.assertEqual(verdict, APPROVAL_VERDICT_NEW_STORAGE_REQUIRED)

    def test_evidence_covers_all_three_investigated_tables(self):
        _, evidence = approval_storage_verdict()
        for table in ("AuditLog", "ReportPaperDecision", "AuthorReviewQueue"):
            self.assertIn(table, evidence)
            self.assertGreater(len(evidence[table]), 20, f"evidence for {table} should not be a stub")


class ApprovalArtifactTests(unittest.TestCase):
    def _artifact(self, **overrides):
        base = dict(plan_id="plan-1", winner_paper_id=5232, loser_paper_id=5482,
                    plan_fingerprint="fp-abc", approval_status=APPROVAL_APPROVED,
                    approver_identity="ra20awn@gmail.com", approval_timestamp="2026-08-21T00:00:00Z",
                    approval_version=1, reason_notes=None)
        base.update(overrides)
        return ApprovalArtifact(**base)

    def test_no_artifact_is_invalid(self):
        valid, reason = validate_approval_artifact(None, "fp-abc")
        self.assertFalse(valid)

    def test_pending_status_is_invalid(self):
        valid, reason = validate_approval_artifact(self._artifact(approval_status=APPROVAL_PENDING), "fp-abc")
        self.assertFalse(valid)

    def test_rejected_status_is_invalid(self):
        valid, reason = validate_approval_artifact(self._artifact(approval_status=APPROVAL_REJECTED), "fp-abc")
        self.assertFalse(valid)

    def test_fingerprint_mismatch_is_invalid(self):
        """An approval for an older plan must not approve a changed plan."""
        valid, reason = validate_approval_artifact(self._artifact(plan_fingerprint="old-fp"), "new-fp")
        self.assertFalse(valid)

    def test_approved_and_matching_fingerprint_is_valid(self):
        valid, reason = validate_approval_artifact(self._artifact(), "fp-abc")
        self.assertTrue(valid)
        self.assertIsNone(reason)

    def test_artifact_carries_all_required_fields(self):
        a = self._artifact()
        d = a.to_dict()
        for required in ("plan_id", "winner_paper_id", "loser_paper_id", "plan_fingerprint",
                          "approval_status", "approver_identity", "approval_timestamp",
                          "approval_version", "reason_notes"):
            self.assertIn(required, d)


# ===========================================================================
# TASK E -- canary simulation integration test (mocked cursor)
# ===========================================================================

class CanarySimulationTests(unittest.TestCase):
    def _live_like_cursor(self):
        winner = _paper_dict(PaperID=5232, DOI="10.1155/2022/8531213", JournalID=1803, _citations=61)
        loser = _paper_dict(PaperID=5482, DOI=None, JournalID=None, _citations=0)
        return FakeCursor(
            research_papers={5232: winner, 5482: loser},
            authors={5232: BASE_WINNER_AUTHORS, 5482: BASE_LOSER_AUTHORS},
            audit_rows=[],
        )

    def _reference_plan(self):
        winner = _paper_dict(PaperID=5232, DOI="10.1155/2022/8531213", JournalID=1803)
        loser = _paper_dict(PaperID=5482, DOI=None, JournalID=None)
        fp = compute_plan_fingerprint(5232, 5482, winner, loser, BASE_WINNER_AUTHORS, BASE_LOSER_AUTHORS, 61, 0)
        return {"survivor": 5232, "loser": 5482, "plan_fingerprint": fp}

    def test_no_approval_artifact_blocks_on_approval(self):
        cur = self._live_like_cursor()
        result = run_canary_simulation(cur, 5232, 5482, self._reference_plan(), approval_artifact=None)
        self.assertEqual(result["final_verdict"], SIM_BLOCKED_APPROVAL)

    def test_stale_reference_plan_blocks_on_fingerprint(self):
        cur = self._live_like_cursor()
        stale_plan = dict(self._reference_plan(), plan_fingerprint="deliberately-wrong")
        result = run_canary_simulation(cur, 5232, 5482, stale_plan, approval_artifact=None)
        self.assertEqual(result["final_verdict"], SIM_BLOCKED_STALE_PLAN)

    def test_prior_execution_blocks_on_idempotency_even_with_fresh_fingerprint(self):
        cur = self._live_like_cursor()
        cur.audit_rows = [(1, 5482, {"kept_paper_id": 5232}, "t1")]
        result = run_canary_simulation(cur, 5232, 5482, self._reference_plan(), approval_artifact=None)
        self.assertEqual(result["final_verdict"], SIM_BLOCKED_IDEMPOTENCY)

    def test_valid_approval_with_matching_fingerprint_reaches_simulation_ready(self):
        cur = self._live_like_cursor()
        plan = self._reference_plan()
        artifact = ApprovalArtifact(
            plan_id="canary-1", winner_paper_id=5232, loser_paper_id=5482,
            plan_fingerprint=plan["plan_fingerprint"], approval_status=APPROVAL_APPROVED,
            approver_identity="test@example.com", approval_timestamp="2026-08-21T00:00:00Z",
            approval_version=1, reason_notes="test fixture only, not a real approval",
        )
        result = run_canary_simulation(cur, 5232, 5482, plan, approval_artifact=artifact)
        self.assertEqual(result["final_verdict"], SIM_READY)

    def test_simulation_ready_never_writes_anything(self):
        """SIMULATION_READY must not authorize or perform a merge -- proven
        here by construction: run_canary_simulation only ever calls
        FakeCursor.execute(), which itself raises on any write verb."""
        cur = self._live_like_cursor()
        plan = self._reference_plan()
        artifact = ApprovalArtifact(
            plan_id="canary-1", winner_paper_id=5232, loser_paper_id=5482,
            plan_fingerprint=plan["plan_fingerprint"], approval_status=APPROVAL_APPROVED,
            approver_identity="test@example.com", approval_timestamp="2026-08-21T00:00:00Z",
            approval_version=1,
        )
        run_canary_simulation(cur, 5232, 5482, plan, approval_artifact=artifact)
        for sql in cur.executed_sql:
            for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                self.assertNotIn(verb, sql.upper())


# ===========================================================================
# TASK F -- explicit non-goals static source scan
# ===========================================================================

class NonGoalsStaticSourceScan(unittest.TestCase):
    def _source(self):
        import merge_execution_safety as mod
        with open(mod.__file__, encoding="utf-8") as f:
            return f.read()

    def test_no_merge_group_import_or_call(self):
        """merge_group must never be imported, and must never appear as an
        actual function CALL. Prose mentions in docstrings/comments
        explaining what dedup_papers.py's real merge_group() does (e.g.
        'reuse merge_group()'s existing logic') are expected and fine --
        only real imports/calls matter, same precise-scan approach used by
        merge_plan_generator.py's own equivalent guard test (Phase 4C)."""
        import re
        src = self._source()
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertNotIn("merge_group", stripped)
        # Every prose mention in this file writes 'merge_group()' with
        # EMPTY parens (referring to the function conceptually). A real
        # call always has real arguments, e.g.
        # 'merge_group(cur, keep, losers, ...)'. So any 'merge_group(' NOT
        # immediately followed by ')' is a real call and must not exist.
        for m in re.finditer(r'merge_group\(', src):
            after = src[m.end():m.end() + 1]
            self.assertEqual(after, ")", f"found merge_group( with real arguments at offset {m.start()}")

    def test_no_apply_flag_or_path(self):
        """'--apply' appears in this file only inside prose/docstrings
        describing dedup_papers.py's real CLI flag (e.g. 'dedup_papers.py
        --apply is never invoked'). What must never exist is an actual
        argparse wiring of that flag or a subprocess/CLI invocation
        containing it."""
        src = self._source()
        self.assertNotIn('add_argument("--apply"', src)
        self.assertNotIn("add_argument('--apply'", src)
        self.assertNotIn("subprocess", src)
        self.assertNotIn("os.system", src)

    def test_no_network_client(self):
        src = self._source()
        for forbidden in ("requests.", "urllib.request", "httpx.", "socket.", "http.client"):
            self.assertNotIn(forbidden, src)

    def test_no_write_sql_in_any_actual_execute_call(self):
        import re
        src = self._source()
        sql_calls = re.findall(r'cur\.execute\(\s*(?:f?)([\'"]{1,3})(.*?)\1', src, re.DOTALL)
        self.assertGreater(len(sql_calls), 0)
        write_verbs = ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "ALTER ", "DROP ", "CREATE ")
        for _quote, sql in sql_calls:
            sql_upper = sql.upper()
            for verb in write_verbs:
                self.assertNotIn(verb, sql_upper, f"write verb {verb!r} found in {sql!r}")

    def test_no_insert_anywhere_since_task_d_concluded_classification_b(self):
        """Task D concluded 'no legitimate existing approval storage' --
        therefore this file must contain zero INSERT statements anywhere,
        not just outside cur.execute() calls."""
        src = self._source()
        self.assertNotIn("INSERT INTO", src.upper())

    def test_no_executor_loop_marker(self):
        """No function in this module iterates a list of plans and executes
        them -- there is no 'for plan in plans: execute(plan)'-shaped loop.
        Checked by absence of any function whose body both loops over a
        plans/queue collection AND calls a write-capable helper; approximated
        here by confirming no function named anything executor-shaped exists."""
        import merge_execution_safety as mod
        names = [n for n in dir(mod) if callable(getattr(mod, n))]
        for n in names:
            self.assertNotIn("execute_plan", n.lower())
            self.assertNotIn("run_executor", n.lower())
            self.assertNotIn("apply_merge", n.lower())


if __name__ == "__main__":
    unittest.main()
