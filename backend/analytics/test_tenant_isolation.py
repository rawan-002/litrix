"""Multi-tenancy isolation regression suite.

NOT a live-DB test suite -- there is no local Postgres in this environment
and the production Neon DB must never be touched for test data (see
CLAUDE.md: DATABASE_URL in .env is the single environment switch, and this
project has no local/test DB set up). Every test here mocks
django.db.connection at the module level and asserts on the ACTUAL SQL text
and bound params a function passed to cursor.execute(sql, params) -- not on
query results. That proves the tenant fence is really in the WHERE clause
(not a post-fetch Python filter) and pins its exact param position, so
deleting the "TenantID" = %s clause later breaks these tests loudly instead
of silently leaking cross-tenant data.

Mirrors the existing convention in this codebase (see
tools/test_merge_approval.py): plain unittest, SimpleNamespace fake
request.user objects, no network, no real DB connection ever opened.

Run: cd backend && python analytics/test_tenant_isolation.py
"""
import contextlib
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'litrix_backend.settings')
import django  # noqa: E402
django.setup()

from django.test import RequestFactory  # noqa: E402

from analytics import ai_tools, ai_views, network_views, public_views, reconciliation_views  # noqa: E402
from analytics.researcher_views import ResearcherViewSet  # noqa: E402

TENANT_A = 101
TENANT_B = 202


def make_user(user_id=1, tenant_id=TENANT_A, user_type='Admin', perms=(), authenticated=True):
    perm_set = set(perms)
    return SimpleNamespace(
        user_id=user_id,
        tenant_id=tenant_id,
        user_type=user_type,
        is_authenticated=authenticated,
        is_active=authenticated,
        has_litrix_perm=lambda code: code in perm_set,
    )


# ---------------------------------------------------------------------------
# Fake DB plumbing -- captures every cursor.execute(sql, params) call;
# scripted_results feeds fetchone()/fetchall() in call order. No real
# connection, no network, no production data.
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, scripted_results=None):
        self.calls = []  # [(sql, params), ...] in call order
        self._results = list(scripted_results or [])

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def fetchall(self):
        return self._results.pop(0) if self._results else []

    @property
    def description(self):
        return []


class _CursorCM:
    """What connection.cursor() must return: a context manager yielding the
    same _FakeCursor every time, like the real psycopg2/Django cursor()."""
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self._cur

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return _CursorCM(self._cur)


def _nth_placeholder_param(sql, params, needle, occurrence=0):
    """Returns params[i] where i = how many '%s' placeholders appear in sql
    strictly before `needle`'s occurrence-th occurrence. Lets a test assert
    'this specific clause is bound to this specific param' without hardcoding
    positions by hand for every query -- still a literal text/position check,
    not a semantic SQL parser (this codebase never emits '%%s' adjacency, so
    a plain substring count of '%s' is exact here)."""
    idx = -1
    for _ in range(occurrence + 1):
        idx = sql.index(needle, idx + 1)
    return params[sql.count('%s', 0, idx)]


# ---------------------------------------------------------------------------
# ai_tools.py -- every function's TenantID must come from the explicit
# tenant_id kwarg (never a model/LLM-supplied value -- see ai_views.py's
# _run_tool_call, which strips any 'tenant_id' the model tries to pass
# before injecting the real one) and must be a literal SQL predicate, not a
# post-fetch filter.
# ---------------------------------------------------------------------------

class ReconciliationTenantFenceTests(unittest.TestCase):
    """review_queue_decide is a DRF @api_view, so it needs a real
    django.http.HttpRequest (DRF asserts the type) -- RequestFactory builds
    one with no DB/network involved; request.user is set directly on it
    (no Authorization header means every DRF authenticator declines, so DRF
    falls back to the plain Django request.user we set here)."""

    def _make_request(self, tenant_id, decision='SKIPPED'):
        req = RequestFactory().post(
            '/api/admin/author-review-queue/1/decide/',
            data=json.dumps({'decision': decision}),
            content_type='application/json',
        )
        req.user = make_user(tenant_id=tenant_id, user_type='Admin')
        # SessionAuthentication (DRF's fallback here, since there's no
        # Authorization header) treats a truthy request.user as a session
        # login and enforces CSRF -- irrelevant to what this test checks, so
        # disable it the same way Django's own test client does for
        # CSRF-exempt test requests.
        req._dont_enforce_csrf_checks = True
        return req

    def test_decide_on_another_tenants_review_id_returns_not_found(self):
        """The SELECT...FOR UPDATE is scoped by rp.\"TenantID\" = %s, so a
        review_id that exists but belongs to a different tenant must come
        back as no row (simulating the real scoped query's behavior) --
        never leak the row and never let the decision proceed."""
        request = self._make_request(TENANT_A)
        cur = _FakeCursor(scripted_results=[None])  # scoped SELECT finds nothing
        with patch.object(reconciliation_views, 'connection', _FakeConnection(cur)), \
             patch.object(reconciliation_views.transaction, 'atomic',
                           return_value=contextlib.nullcontext()):
            resp = reconciliation_views.review_queue_decide(request, review_id=999)

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data, {'error': 'not_found'})
        sql, params = cur.calls[0]
        self.assertIn('rp."TenantID" = %s', sql)
        self.assertEqual(_nth_placeholder_param(sql, params, 'rp."TenantID" = %s'), TENANT_A)

    def test_decide_is_scoped_by_the_deciding_admins_own_tenant_not_a_default(self):
        for tenant in (TENANT_A, TENANT_B):
            request = self._make_request(tenant)
            cur = _FakeCursor(scripted_results=[None])
            with patch.object(reconciliation_views, 'connection', _FakeConnection(cur)), \
                 patch.object(reconciliation_views.transaction, 'atomic',
                               return_value=contextlib.nullcontext()):
                reconciliation_views.review_queue_decide(request, review_id=1)
            sql, params = cur.calls[0]
            self.assertEqual(
                _nth_placeholder_param(sql, params, 'rp."TenantID" = %s'), tenant,
                'the bound tenant must track the ACTING admin, never a fixed/default tenant',
            )


# ---------------------------------------------------------------------------
# Cross-tenant UserID-guessing fence -- the exact bug class found and fixed
# in researcher_views.ResearcherViewSet._resolve_user_id and
# network_views._resolve_user_id: a plain numeric UserID must 404 instead of
# resolving when it belongs to a different tenant.
# ---------------------------------------------------------------------------

class ResolveUserIdTenantFenceTests(unittest.TestCase):
    def test_network_views_numeric_pk_rejects_cross_tenant_id(self):
        cur = _FakeCursor(scripted_results=[None])  # tenant-scoped lookup: no match
        with patch.object(network_views, 'connection', _FakeConnection(cur)):
            result = network_views._resolve_user_id(42, TENANT_B)
        self.assertIsNone(result, 'a numeric UserID from a different tenant must not resolve')
        sql, params = cur.calls[0]
        self.assertIn('"TenantID" = %s', sql)
        self.assertEqual(params, [42, TENANT_B])

    def test_network_views_numeric_pk_accepts_same_tenant_id(self):
        cur = _FakeCursor(scripted_results=[(1,)])  # tenant-scoped lookup: match
        with patch.object(network_views, 'connection', _FakeConnection(cur)):
            result = network_views._resolve_user_id(42, TENANT_A)
        self.assertEqual(result, 42)

    def test_researcher_viewset_numeric_pk_rejects_cross_tenant_id(self):
        # _resolve_user_id does `from django.db import connection` INSIDE the
        # method body, so the patch target is the real django.db module
        # attribute, not an analytics.researcher_views module-level name.
        cur = _FakeCursor(scripted_results=[None])
        with patch('django.db.connection', _FakeConnection(cur)):
            user_id, err = ResearcherViewSet._resolve_user_id('42', TENANT_B)
        self.assertIsNone(user_id)
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 404)
        sql, params = cur.calls[0]
        self.assertIn('"TenantID" = %s', sql)
        self.assertEqual(params, [42, TENANT_B])


# ---------------------------------------------------------------------------
# network_views.py -- the CTEs flagged by the earlier pass (coauthor_papers,
# external_authorships, INTEREST_NEIGHBOURS_SQL's centre/candidate/
# coauthor_ids CTEs, and the shared _fetch_interests helper) must all bind
# the acting user's own tenant_id explicitly, never trust that a paper/user
# reachable from an already-verified centre is automatically same-tenant.
# ---------------------------------------------------------------------------

class NetworkViewsTenantFenceTests(unittest.TestCase):
    def _make_request(self, tenant_id, query='', user_id=1):
        req = RequestFactory().get(f'/api/researchers/{user_id}/network/{query}')
        req.user = make_user(user_id=user_id, tenant_id=tenant_id)
        req._dont_enforce_csrf_checks = True
        return req

    # Centre node (UserID, FullName_Ar, name_en, Litrix_ID, DeptName, papers)
    _CENTRE_ROW = (1, 'مركز', 'Centre', 'Lit-000001', 'CS', 3)

    def test_coauthors_mode_binds_caller_tenant_in_every_query(self):
        for tenant in (TENANT_A, TENANT_B):
            cur = _FakeCursor(scripted_results=[
                (1,),               # _resolve_user_id's own tenant-scoped lookup
                self._CENTRE_ROW,  # centre node
                [],                 # internal co-authors (coauthor_papers)
                [],                 # external co-authors
                [],                 # _fetch_interests
            ])
            with patch.object(network_views, 'connection', _FakeConnection(cur)):
                resp = network_views.researcher_network(self._make_request(tenant), pk=1)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(cur.calls), 5)

            resolve_sql, resolve_params = cur.calls[0]
            self.assertEqual(resolve_params, [1, tenant])

            centre_sql, centre_params = cur.calls[1]
            self.assertIn('u."TenantID" = %s', centre_sql)
            self.assertEqual(centre_params, [1, tenant])

            internal_sql, internal_params = cur.calls[2]
            self.assertIn('rp."TenantID" = %s', internal_sql)
            self.assertIn('u."TenantID" = %s', internal_sql)
            self.assertEqual(
                _nth_placeholder_param(internal_sql, internal_params, 'rp."TenantID" = %s'),
                tenant)
            self.assertEqual(
                _nth_placeholder_param(internal_sql, internal_params, 'u."TenantID" = %s'),
                tenant)

            ext_sql, ext_params = cur.calls[3]
            self.assertIn('rp."TenantID" = %s', ext_sql)
            self.assertEqual(
                _nth_placeholder_param(ext_sql, ext_params, 'rp."TenantID" = %s'), tenant)

            interests_sql, interests_params = cur.calls[4]
            self.assertIn('rp."TenantID" = %s', interests_sql)
            self.assertEqual(
                _nth_placeholder_param(interests_sql, interests_params, 'rp."TenantID" = %s'),
                tenant)

    def test_both_mode_binds_caller_tenant_in_interest_neighbours_cte(self):
        """mode=both additionally runs INTEREST_NEIGHBOURS_SQL, which has
        FOUR separate tenant-bound slots: centre_concepts' openalex branch,
        coauthor_ids' ResearchPaper join, candidate_concepts' openalex
        branch, and the final candidate->Users join. All four must bind the
        SAME acting tenant -- swapping tenant must move all four together,
        never leave one on a stale/default value."""
        for tenant in (TENANT_A, TENANT_B):
            cur = _FakeCursor(scripted_results=[
                (1,),               # _resolve_user_id's own tenant-scoped lookup
                self._CENTRE_ROW,  # centre node
                [],                 # internal co-authors
                [],                 # external co-authors
                [],                 # _fetch_interests (centre + internal ids)
                [],                 # INTEREST_NEIGHBOURS_SQL
            ])
            with patch.object(network_views, 'connection', _FakeConnection(cur)):
                resp = network_views.researcher_network(
                    self._make_request(tenant, query='?mode=both'), pk=1)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(cur.calls), 6)

            sql, params = cur.calls[5]
            # centre_concepts' openalex branch (1st rp."TenantID" occurrence)
            self.assertEqual(_nth_placeholder_param(sql, params, 'rp."TenantID" = %s', 0), tenant)
            # coauthor_ids' ResearchPaper join
            self.assertEqual(_nth_placeholder_param(sql, params, 'rp1."TenantID" = %s'), tenant)
            # candidate_concepts' openalex branch (2nd rp."TenantID" occurrence)
            self.assertEqual(_nth_placeholder_param(sql, params, 'rp."TenantID" = %s', 1), tenant)
            # final candidate -> Users join
            self.assertEqual(_nth_placeholder_param(sql, params, 'u."TenantID" = %s'), tenant)


# ---------------------------------------------------------------------------
# public_views.py -- AllowAny, no request.user at all, so the ENTIRE fence
# is _resolve_tenant_or_404(tenant_slug). Missing/unknown/inactive slugs
# must always 404 and must NEVER fall back to any tenant (not TenantID=1,
# not "whatever's in the DB").
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    unittest.main()
