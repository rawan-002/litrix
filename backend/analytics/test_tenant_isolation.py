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

class AiToolsTenantFenceTests(unittest.TestCase):
    def _run(self, fn, scripted_results, **kwargs):
        cur = _FakeCursor(scripted_results)
        with patch.object(ai_tools, 'connection', _FakeConnection(cur)):
            fn(**kwargs)
        return cur.calls

    def test_get_overview_stats_binds_caller_tenant_in_both_queries(self):
        for tenant in (TENANT_A, TENANT_B):
            calls = self._run(
                ai_tools.get_overview_stats,
                scripted_results=[tuple(0 for _ in range(12)), (0,)],
                tenant_id=tenant,
            )
            self.assertEqual(len(calls), 2)
            sql1, params1 = calls[0]
            self.assertIn('rp."TenantID" = %s', sql1)
            self.assertEqual(_nth_placeholder_param(sql1, params1, 'rp."TenantID" = %s'), tenant)
            sql2, params2 = calls[1]
            self.assertIn('rp."TenantID" = %s', sql2)
            self.assertEqual(_nth_placeholder_param(sql2, params2, 'rp."TenantID" = %s'), tenant)

    def test_get_top_researchers_binds_caller_tenant(self):
        for tenant in (TENANT_A, TENANT_B):
            calls = self._run(ai_tools.get_top_researchers, scripted_results=[[]], tenant_id=tenant)
            sql, params = calls[0]
            self.assertIn('u."TenantID" = %s', sql)
            self.assertEqual(_nth_placeholder_param(sql, params, 'u."TenantID" = %s'), tenant)

    def test_find_researcher_binds_caller_tenant(self):
        for tenant in (TENANT_A, TENANT_B):
            calls = self._run(ai_tools.find_researcher, scripted_results=[[]], name='Nizar', tenant_id=tenant)
            sql, params = calls[0]
            self.assertIn('u."TenantID" = %s', sql)
            self.assertEqual(_nth_placeholder_param(sql, params, 'u."TenantID" = %s'), tenant)

    def test_get_researcher_topics_binds_caller_tenant(self):
        for tenant in (TENANT_A, TENANT_B):
            calls = self._run(ai_tools.get_researcher_topics, scripted_results=[[]], name='Nizar', tenant_id=tenant)
            sql, params = calls[0]
            self.assertIn('u."TenantID" = %s', sql)
            self.assertEqual(_nth_placeholder_param(sql, params, 'u."TenantID" = %s'), tenant)

    def test_get_department_stats_binds_caller_tenant_with_and_without_dept_filter(self):
        for tenant in (TENANT_A, TENANT_B):
            calls = self._run(ai_tools.get_department_stats, scripted_results=[[]], tenant_id=tenant)
            sql, params = calls[0]
            self.assertIn('d."TenantID" = %s', sql)
            self.assertEqual(_nth_placeholder_param(sql, params, 'd."TenantID" = %s'), tenant)

            calls = self._run(ai_tools.get_department_stats, scripted_results=[[]],
                               department_id=7, tenant_id=tenant)
            sql, params = calls[0]
            self.assertIn('d."TenantID" = %s', sql)
            self.assertEqual(_nth_placeholder_param(sql, params, 'd."TenantID" = %s'), tenant)

    def test_get_publication_trend_binds_caller_tenant(self):
        for tenant in (TENANT_A, TENANT_B):
            calls = self._run(ai_tools.get_publication_trend, scripted_results=[[]], tenant_id=tenant)
            sql, params = calls[0]
            self.assertIn('rp."TenantID" = %s', sql)
            self.assertEqual(_nth_placeholder_param(sql, params, 'rp."TenantID" = %s'), tenant)

    def test_compare_departments_binds_caller_tenant(self):
        for tenant in (TENANT_A, TENANT_B):
            calls = self._run(ai_tools.compare_departments, scripted_results=[[]], tenant_id=tenant)
            sql, params = calls[0]
            self.assertIn('d."TenantID" = %s', sql)
            self.assertEqual(_nth_placeholder_param(sql, params, 'd."TenantID" = %s'), tenant)

    def test_every_tool_requires_tenant_id_explicitly(self):
        """No default value anywhere -- calling without it must raise
        TypeError (caught by ai_views._run_tool_call as a safe error, never
        silently querying with no tenant filter bound at all)."""
        for name, spec in ai_tools.TOOLS.items():
            with self.assertRaises(TypeError, msg=f'{name} must require tenant_id'):
                # Every tool's required positional args are also missing here,
                # but tenant_id being keyword-only-with-no-default must be
                # what's enforced regardless of what else is omitted.
                spec['fn']()

    def test_tenant_id_never_in_any_tool_schema_sent_to_the_model(self):
        for name, spec in ai_tools.TOOLS.items():
            props = spec['parameters'].get('properties', {})
            self.assertNotIn('tenant_id', props, f'{name} leaks tenant_id to the LLM-facing schema')


# ---------------------------------------------------------------------------
# ai_views.py dispatch layer -- the model must never be able to supply or
# override tenant_id, and an unauthenticated caller must never resolve to
# a real tenant (NULL binds to nothing in SQL, never silently to tenant 1).
# ---------------------------------------------------------------------------

class AiViewsDispatchFenceTests(unittest.TestCase):
    def test_run_tool_call_ignores_model_supplied_tenant_id_and_injects_real_one(self):
        call = {
            'function': {
                'name': 'get_top_researchers',
                # The model tries to smuggle a foreign tenant_id as an arg.
                'arguments': '{"tenant_id": %d, "limit": 3}' % TENANT_B,
            }
        }
        cur = _FakeCursor(scripted_results=[[]])
        with patch.object(ai_tools, 'connection', _FakeConnection(cur)):
            ai_views._run_tool_call(call, department_id=None, tenant_id=TENANT_A)
        sql, params = cur.calls[0]
        self.assertEqual(
            _nth_placeholder_param(sql, params, 'u."TenantID" = %s'),
            TENANT_A,
            'model-supplied tenant_id must never win over the authenticated caller\'s own tenant',
        )

    def test_resolve_scope_unauthenticated_never_yields_a_tenant(self):
        anon_request = SimpleNamespace(user=make_user(authenticated=False))
        scope_level, department_id, tenant_id = ai_views._resolve_scope(anon_request)
        self.assertEqual(scope_level, 'none')
        self.assertIsNone(tenant_id)

    def test_resolve_scope_uses_the_real_authenticated_users_own_tenant(self):
        request = SimpleNamespace(user=make_user(
            tenant_id=TENANT_B, perms=('view_all_researchers',)))
        scope_level, department_id, tenant_id = ai_views._resolve_scope(request)
        self.assertEqual(scope_level, 'all')
        self.assertEqual(tenant_id, TENANT_B)


# ---------------------------------------------------------------------------
# reconciliation_views.review_queue_decide -- write path. An admin from
# tenant A must never be able to decide a review item belonging to tenant B,
# even by guessing/incrementing review_id.
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

class PublicViewsTenantFenceTests(unittest.TestCase):
    def test_missing_slug_404s_without_even_querying_the_db(self):
        cur = _FakeCursor(scripted_results=[])
        with patch.object(public_views, 'connection', _FakeConnection(cur)):
            tenant_id, err = public_views._resolve_tenant_or_404(None)
        self.assertIsNone(tenant_id)
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 404)
        self.assertEqual(cur.calls, [], 'a missing slug must never even reach the DB')

        cur2 = _FakeCursor(scripted_results=[])
        with patch.object(public_views, 'connection', _FakeConnection(cur2)):
            tenant_id, err = public_views._resolve_tenant_or_404('')
        self.assertIsNone(tenant_id)
        self.assertEqual(err.status_code, 404)
        self.assertEqual(cur2.calls, [])

    def test_unknown_slug_404s_never_defaults_to_any_tenant(self):
        cur = _FakeCursor(scripted_results=[None])  # no matching row
        with patch.object(public_views, 'connection', _FakeConnection(cur)):
            tenant_id, err = public_views._resolve_tenant_or_404('no-such-university')
        self.assertIsNone(tenant_id)
        self.assertEqual(err.status_code, 404)
        sql, params = cur.calls[0]
        self.assertEqual(params, ['no-such-university'])

    def test_resolver_query_requires_active_status_literally_in_the_sql(self):
        """Pins the actual query text, not just behavior -- an inactive
        tenant row existing in the DB must be excluded by the SQL itself
        (Status = 'active'), not by incidental non-matching test data."""
        cur = _FakeCursor(scripted_results=[None])
        with patch.object(public_views, 'connection', _FakeConnection(cur)):
            public_views._resolve_tenant_or_404('some-inactive-or-unknown-slug')
        sql, _ = cur.calls[0]
        self.assertIn('"Slug" = %s', sql)
        self.assertIn('"Status" = \'active\'', sql)

    def test_valid_active_slug_resolves_to_its_own_tenant_id(self):
        for tenant in (TENANT_A, TENANT_B):
            cur = _FakeCursor(scripted_results=[(tenant,)])
            with patch.object(public_views, 'connection', _FakeConnection(cur)):
                tenant_id, err = public_views._resolve_tenant_or_404('some-slug')
            self.assertIsNone(err)
            self.assertEqual(tenant_id, tenant)

    def test_endpoints_are_allowany_by_design_resolver_is_the_whole_fence(self):
        """public_views endpoints are AllowAny by design -- there is no
        request.user to misuse, so _resolve_tenant_or_404 is the WHOLE
        fence (unlike the authenticated views, which also check
        request.user.tenant_id). Pins that design choice structurally
        rather than asserting on a request.user these views never read."""
        from rest_framework.permissions import AllowAny
        self.assertIn(AllowAny, public_views.overview.cls.permission_classes)
        self.assertIn(AllowAny, public_views.trends.cls.permission_classes)

    def test_swapping_only_the_slug_binds_a_different_tenant_in_the_real_query(self):
        """End-to-end through one real endpoint (trends): resolving tenant A
        vs tenant B from two different slugs must bind that SAME, correctly
        different tenant_id into the endpoint's own data query -- changing
        the slug alone must be the only thing that can change which
        tenant's rows come back, and it must actually take effect downstream
        of the resolver, not get dropped."""
        for tenant, slug in ((TENANT_A, 'uni-a'), (TENANT_B, 'uni-b')):
            cur = _FakeCursor(scripted_results=[
                (tenant,),  # _resolve_tenant_or_404's lookup
                [],         # trends' own attributed_papers query
            ])
            req = RequestFactory().get(f'/api/public/{slug}/trends/')
            with patch.object(public_views, 'connection', _FakeConnection(cur)):
                resp = public_views.trends(req, tenant_slug=slug)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(cur.calls), 2)
            resolve_sql, resolve_params = cur.calls[0]
            self.assertEqual(resolve_params, [slug])
            data_sql, data_params = cur.calls[1]
            self.assertIn('rp."TenantID" = %s', data_sql)
            self.assertEqual(
                _nth_placeholder_param(data_sql, data_params, 'rp."TenantID" = %s'),
                tenant,
                'the tenant resolved from THIS slug must be exactly what the data query binds',
            )

    def test_unknown_slug_never_reaches_the_data_query_at_all(self):
        """The inverse of the swap test: when the slug doesn't resolve,
        trends() must return 404 immediately and never run its data query
        with no tenant filter (or a stale one)."""
        cur = _FakeCursor(scripted_results=[None])  # resolver finds nothing
        req = RequestFactory().get('/api/public/does-not-exist/trends/')
        with patch.object(public_views, 'connection', _FakeConnection(cur)):
            resp = public_views.trends(req, tenant_slug='does-not-exist')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(cur.calls), 1, 'must stop at the resolver, never run the data query')


if __name__ == '__main__':
    unittest.main()
