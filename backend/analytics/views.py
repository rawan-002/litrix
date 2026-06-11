"""Read-only DRF endpoints for the dashboard and reports.

Everything here is ReadOnlyModelViewSet — the frontend never writes through
these; the scraper and bootstrap scripts own all writes. Filtering goes
through django-filter (e.g. ?department_id=2). The overview/export endpoints
are scoped to FOCUS_YEARS, so widen or narrow that list to move the window.
"""

from rest_framework import viewsets, filters, decorators, response
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Avg


from .models import (
    ResearcherStats, DepartmentStats, TopPaper, PublicationTrend,
)
from .serializers import (
    ResearcherStatsSerializer, DepartmentStatsSerializer,
    TopPaperSerializer, PublicationTrendSerializer,
)

from .stats import (
    CHART_YEAR_FLOOR, FOCUS_YEARS,
    _resolve_years, _hod_scope_department_id, _albaha_only,
    _cites_expr, _affil_clause,
    _dept_cards_windowed, _researcher_rows_windowed,
)
from .exports import export_excel  # re-exported for urls.py
from .researcher_views import ResearcherViewSet  # re-exported for urls.py


@decorators.api_view(['GET'])
def paper_detail(request, paper_id):
    """GET /api/papers/<paper_id>/detail/

    Full paper details for the modal popup, pulled from ResearchPaper plus the
    original scraped RawData_Log: metadata, raw author string, citation graph,
    journal info, and the Al-Baha researchers attributed to the paper.
    """
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute('''
            SELECT
                rp."PaperID",
                rp."Title",
                rp."Abstract",
                rp."DOI",
                rp."PubYear",
                rp."Source",
                rp."Indexing",
                rp."CitationsByYear",
                rp."RawData_Log"->>'authors'      AS raw_authors,
                rp."RawData_Log"->>'publication'  AS publication,
                rp."RawData_Log"->>'link'         AS link,
                rp."RawData_Log"->>'citation_id'  AS cites_id,
                COALESCE(
                    ("RawData_Log"->'cited_by'->>'value')::int,
                    ("RawData_Log"->>'cited_by_count')::int,
                    0
                ) AS total_citations,
                COALESCE(j."JournalName", rp."RawData_Log"->>'publication') AS journal_name,
                j."ISSN_Print",
                j."VenueType",
                jr."Quartile",
                jr."ImpactFactor",
                rp."RawData_Log"->'authorships'   AS authorships_jsonb
            FROM "ResearchPaper" rp
            LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
            LEFT JOIN LATERAL (    SELECT "Quartile", "ImpactFactor"    FROM "JournalRankings"    WHERE "JournalID" = j."JournalID"    ORDER BY "RankingYear" DESC NULLS LAST, "Source"    LIMIT 1) jr ON TRUE
            WHERE rp."PaperID" = %s
        ''', [paper_id])
        row = cur.fetchone()
        if not row:
            return response.Response(
                {'error': 'Paper not found'}, status=404
            )

        cby_raw = row[7]
        if isinstance(cby_raw, str):
            try:
                import json
                cby = json.loads(cby_raw)
            except Exception:
                cby = None
        else:
            cby = cby_raw

        # RawData_Log->'authorships' is the OpenAlex-shape array we store on
        # every scrape. For each entry grab the display name and every
        # institution string we can reach, then flag whether any of them is
        # Al-Baha University.
        import re as _re
        ALBAHA = _re.compile(r'(al[\s\-]?baha|albaha|الباحة)', _re.IGNORECASE)
        authorships_payload = []
        raw_authorships = row[18]
        if isinstance(raw_authorships, str):
            try:
                import json as _json
                raw_authorships = _json.loads(raw_authorships)
            except Exception:
                raw_authorships = None
        if isinstance(raw_authorships, list):
            for ship in raw_authorships:
                if not isinstance(ship, dict):
                    continue
                name = ((ship.get('author') or {}).get('display_name') or '').strip()
                insts = []
                for inst in (ship.get('institutions') or []):
                    n = (inst or {}).get('display_name')
                    if n:
                        insts.append(n)
                for raw in (ship.get('raw_affiliation_strings') or []):
                    if raw and raw not in insts:
                        insts.append(raw)
                single = ship.get('raw_affiliation_string')
                if single and single not in insts:
                    insts.append(single)
                at_albaha = any(ALBAHA.search(n) for n in insts)
                authorships_payload.append({
                    'name':         name,
                    'institutions': insts,
                    'at_albaha':    at_albaha,
                })

        paper = {
            'paper_id':        row[0],
            'title':           row[1],
            'abstract':        row[2],
            'doi':             row[3],
            'pub_year':        row[4],
            'source':          row[5],
            'indexing':        row[6],
            'citations_by_year': cby,
            'raw_authors':     row[8],
            'publication':     row[9],
            'link':            row[10],
            'cites_id':        row[11],
            'total_citations': row[12],
            'journal_name':    row[13],
            'issn_print':      row[14],
            'venue_type':      row[15],
            'quartile':        row[16],
            'impact_factor':   row[17],
            # Empty until backfill_authorships runs or a new scrape fills it;
            # the frontend falls back to raw_authors when this list is empty.
            'authorships':     authorships_payload,
        }

        # Al-Baha researchers attributed to this paper
        cur.execute('''
            SELECT u."UserID", u."FullName_Ar", d."DepartmentName"
            FROM "Authors" a
            JOIN "Users" u ON u."UserID" = a."UserID"
            LEFT JOIN "Works_In" w ON w."UserID" = u."UserID" AND w."IsCurrentPosition" = TRUE
            LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
            WHERE a."PaperID" = %s
            ORDER BY a."AuthorOrder" NULLS LAST
        ''', [paper_id])
        paper['albaha_authors'] = [
            {'user_id': r[0], 'full_name_ar': r[1], 'department_name': r[2]}
            for r in cur.fetchall()
        ]

    return response.Response(paper)


class DepartmentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/departments/         → list with aggregated stats
    GET /api/departments/{id}/    → single department detail

    HoDs are scoped to their own department; Admins/Deans see all.

    Paper + citation figures are recomputed this-period (per-paper) before
    returning, so this page agrees with the overview dashboard rather than
    showing the all-time/lifetime numbers stored in v_department_stats.
    """
    serializer_class = DepartmentStatsSerializer
    filter_backends = [filters.OrderingFilter]
    ordering_fields = [
        'total_papers', 'total_citations', 'total_q1_papers',
        'avg_h_index', 'total_researchers',
    ]
    ordering = ['-total_papers']

    def get_queryset(self):
        qs = DepartmentStats.objects.all()
        scope = _hod_scope_department_id(self.request)
        if scope is not None:
            qs = qs.filter(department_id=scope)
        return qs

    def list(self, request, *args, **kwargs):
        resp = super().list(request, *args, **kwargs)
        data = resp.data
        rows = data.get('results') if isinstance(data, dict) else data
        if rows:
            albaha = _albaha_only(request)
            win = _dept_cards_windowed(list(FOCUS_YEARS), albaha)
            for r in rows:
                w = win.get(r.get('department_id'))
                if w:
                    r.update(w)
        return resp

    @decorators.action(detail=True, methods=['get'])
    def researchers(self, request, pk=None):
        """GET /api/departments/{id}/researchers/ — list of researchers."""
        # A HoD may only inspect their own department's researchers.
        scope = _hod_scope_department_id(request)
        if scope is not None and str(scope) != str(pk):
            return response.Response(
                {'error': 'You can only view your own department.'},
                status=403,
            )
        qs = ResearcherStats.objects.filter(department_id=pk).order_by(
            '-h_index', '-total_papers'
        )
        page = self.paginate_queryset(qs)
        rows = [dict(r) for r in ResearcherStatsSerializer(page or qs, many=True).data]
        win = _researcher_rows_windowed(
            list(FOCUS_YEARS), [r['user_id'] for r in rows], _albaha_only(request))
        for r in rows:
            w = win.get(r['user_id'])
            r.update(w if w else {'total_papers': 0, 'q1_papers': 0, 'total_citations': 0})
        return self.get_paginated_response(rows) if page else response.Response(rows)


class TopPaperViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/papers/top/?limit=10   → most-cited papers
    GET /api/papers/top/?quartile=Q1
    GET /api/papers/top/?pub_year=2024
    """
    queryset = TopPaper.objects.all()
    serializer_class = TopPaperSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['quartile', 'pub_year', 'source']
    ordering_fields = ['citations', 'pub_year', 'impact_factor']
    ordering = ['-citations']


class PublicationTrendViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/trends/                       → all departments × all years
    GET /api/trends/?department_id=2       → single department's trend
    """
    queryset = PublicationTrend.objects.all()
    serializer_class = PublicationTrendSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['department_id', 'year']
    ordering = ['department_name', 'year']

    def get_queryset(self):
        # A HoD only sees their own department's trend; Admin/Dean see all
        # (consistent with the overview + departments scoping).
        qs = PublicationTrend.objects.all()
        scope = _hod_scope_department_id(self.request)
        if scope is not None:
            qs = qs.filter(department_id=scope)
        return qs


@decorators.api_view(['GET'])
def yearly_breakdown(request):
    """GET /api/yearly-breakdown/?year=2025

    Department-level breakdown for one year: per-department journal/conference
    counts and citations, plus a flat paper list the frontend splits by
    venue_type into expandable lists under each department card.
    """
    year = request.query_params.get('year')
    if not year:
        return response.Response(
            {'error': 'year parameter required'}, status=400
        )
    try:
        year_int = int(year)
    except (TypeError, ValueError):
        return response.Response(
            {'error': 'year must be an integer'}, status=400
        )

    # This drill-down belongs to the institutional overview, so it gets the same
    # gate + HoD scoping: a plain Researcher can't pull it and a HoD sees only
    # their own department (it was unscoped before — every dept leaked).
    _u = getattr(request, 'user', None)
    if not (_u and _u.is_authenticated and (
            _u.has_litrix_perm('view_all_researchers') or
            _u.has_litrix_perm('view_dept_researchers'))):
        return response.Response({'error': 'Forbidden'}, status=403)
    hod_dept_id = _hod_scope_department_id(request)
    dept_clause = ' AND department_id = %s' if hod_dept_id else ''
    dept_param = [hod_dept_id] if hod_dept_id else []

    from django.db import connection
    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT
                department_id,
                department_name,
                COUNT(*) FILTER (WHERE venue_type = 'Journal')    AS journal_papers,
                COUNT(*) FILTER (WHERE venue_type = 'Conference') AS conference_papers,
                COUNT(*)                                          AS total_papers,
                COALESCE(SUM(citations), 0)                       AS total_citations
            FROM v_paper_details
            WHERE pub_year = %s
              AND department_id IS NOT NULL{dept_clause}
            GROUP BY department_id, department_name
            ORDER BY total_papers DESC
        ''', [year_int] + dept_param)
        dept_rows = cur.fetchall()
        departments = [
            {
                'department_id':     r[0],
                'department_name':   r[1],
                'journal_papers':    r[2],
                'conference_papers': r[3],
                'total_papers':      r[4],
                'total_citations':   r[5],
            }
            for r in dept_rows
        ]

        cur.execute(f'''
            SELECT
                paper_id, title, doi, citations, journal_name,
                venue_type, quartile, impact_factor, indexing,
                department_id, department_name, authors_ar
            FROM v_paper_details
            WHERE pub_year = %s
              AND department_id IS NOT NULL{dept_clause}
            ORDER BY citations DESC NULLS LAST, paper_id
        ''', [year_int] + dept_param)
        cols = [c[0] for c in cur.description]
        papers = [dict(zip(cols, row)) for row in cur.fetchall()]

    return response.Response({
        'year': year_int,
        'departments': departments,
        'papers': papers,
    })


@decorators.api_view(['GET'])
def overview(request):
    """GET /api/stats/overview/ (optionally ?year=YYYY)

    One-shot payload for the Admin/Dean/HoD landing page; HoDs are auto-scoped
    to their own department.

    Pass ?affiliation=albaha to restrict every paper-derived metric to papers
    not confirmed authored elsewhere (AffiliationVerified IS DISTINCT FROM FALSE
    keeps confirmed-Al-Baha TRUE and not-yet-verified NULL, drops only
    confirmed-elsewhere FALSE). The default leaves the historical numbers
    untouched. Citation totals come from Scholar's author-level CitationsByYear
    graph and can't be paper-filtered, so the toggle only affects paper counts.
    """
    years = _resolve_years(request)

    albaha_only = _albaha_only(request)
    AFFIL_CLAUSE = _affil_clause(albaha_only, 'rp')
    # Same predicate for the citation CTEs that alias the paper table as rp_all.
    AFFIL_CLAUSE_RPALL = _affil_clause(albaha_only, 'rp_all')

    # Institutional dashboard data is for roles that may view researchers; a
    # plain Researcher would otherwise fall into the unscoped branch and get the
    # full institution view (the UI already routes them to their own dashboard).
    _u = getattr(request, 'user', None)
    if not (_u and _u.is_authenticated and (
            _u.has_litrix_perm('view_all_researchers') or
            _u.has_litrix_perm('view_dept_researchers'))):
        return response.Response({'error': 'Forbidden'}, status=403)

    # Resolve via the shared helper so HoD detection matches the rest of the
    # app. Returns None for Admin/Dean (full institution), a dept id for a HoD,
    # or -1 when a HoD has no department — the -1 sentinel scopes every query
    # below to nothing instead of leaking the whole institution.
    hod_dept_id = _hod_scope_department_id(request)

    # avg_h keeps the historical definition (average of dept averages).
    _dept_qs = DepartmentStats.objects.all()
    if hod_dept_id:
        _dept_qs = _dept_qs.filter(department_id=hod_dept_id)
    dept_agg = _dept_qs.aggregate(avg_h=Avg('avg_h_index'))

    # COUNT(DISTINCT UserID) over current positions. The old
    # Sum('total_researchers') across DepartmentStats rows double-counted anyone
    # holding positions in two departments (one row each).
    from django.db import connection as _hc_conn
    with _hc_conn.cursor() as _hc_cur:
        _hc_cur.execute('''
            SELECT COUNT(DISTINCT u."UserID") AS researchers,
                   COUNT(DISTINCT u."UserID")
                       FILTER (WHERE r."LastSyncedAt" IS NOT NULL) AS active
            FROM "Users" u
            JOIN "Works_In" w ON w."UserID" = u."UserID"
                             AND w."IsCurrentPosition" = TRUE
            LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
            WHERE u."UserType" = 'Researcher'
              AND (%s::int IS NULL OR w."DepartmentID" = %s::int)
        ''', [hod_dept_id, hod_dept_id])
        _hc_row = _hc_cur.fetchone()
    dept_agg['researchers'] = _hc_row[0]
    dept_agg['active'] = _hc_row[1]

    from django.db import connection
    with connection.cursor() as cur:
        # Papers KPI plus Q1/Scopus/ISI counts — papers published in window.
        # The author-in-dept clause is appended for HoDs only.
        kpi_sql = (
            'SELECT COUNT(DISTINCT rp."PaperID") AS papers, '
            '       COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = \'Q1\') AS q1, '
            '       COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = \'Scopus\' OR jr."Quartile" IS NOT NULL) AS scopus, '
            '       COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = \'ISI\') AS isi '
            'FROM "ResearchPaper" rp '
            'LEFT JOIN LATERAL (SELECT "Quartile","ImpactFactor" FROM "JournalRankings" '
            '  WHERE "JournalID" = rp."JournalID" ORDER BY "RankingYear" DESC NULLS LAST, "Source" LIMIT 1) jr ON TRUE '
            'WHERE rp."PubYear" = ANY(%s) '
            '  AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"'
        )
        kpi_params = [years]
        if hod_dept_id:
            kpi_sql += (
                ' AND EXISTS (SELECT 1 FROM "Works_In" w2 '
                '              WHERE w2."UserID" = a."UserID" '
                '                AND w2."DepartmentID" = %s '
                '                AND w2."IsCurrentPosition" = TRUE)'
            )
            kpi_params.append(hod_dept_id)
        kpi_sql += ')'
        kpi_sql += AFFIL_CLAUSE
        cur.execute(kpi_sql, kpi_params)
        paper_count_row = cur.fetchone()

        # Citations received in the window, summed per-paper over the selected
        # years across ALL the scope's papers (any publication year) — not
        # citations of papers published in the window. This keeps the metric
        # meaningful for a single recent year: older highly-cited papers still
        # contribute what they earned that year, whereas papers published in
        # e.g. 2026 have barely been cited yet. EXISTS, not JOIN, counts each
        # paper once even when several scope authors share it.
        cites_year_expr = _cites_expr('rp', years)
        cit_sql = (
            f'SELECT COALESCE(SUM({cites_year_expr}), 0) AS citations '
            'FROM "ResearchPaper" rp '
            'WHERE EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"'
        )
        cit_params = [str(y) for y in years]
        if hod_dept_id:
            cit_sql += (
                ' AND EXISTS (SELECT 1 FROM "Works_In" w2 '
                '              WHERE w2."UserID" = a."UserID" '
                '                AND w2."DepartmentID" = %s '
                '                AND w2."IsCurrentPosition" = TRUE)'
            )
            cit_params.append(hod_dept_id)
        cit_sql += ')'
        cit_sql += AFFIL_CLAUSE
        cur.execute(cit_sql, cit_params)
        citations_row = cur.fetchone()

        # paper_count_row is (papers, q1, scopus, isi); splice the citation total
        # in to get the (papers, citations, q1, scopus, isi) shape.
        paper_totals = (
            paper_count_row[0],
            citations_row[0],
            paper_count_row[1],
            paper_count_row[2],
            paper_count_row[3],
        )

        # Top researchers: papers PUBLISHED in window + citations
        # RECEIVED in window (per-year sum across ALL their papers).
        year_keys_expr_alias = _cites_expr('rp_all', years)
        cur.execute(f'''
            WITH papers_in_window AS (
                SELECT a."UserID",
                       COUNT(DISTINCT rp."PaperID") AS focus_papers
                FROM "Authors" a
                JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
                WHERE rp."PubYear" = ANY(%s){AFFIL_CLAUSE}
                GROUP BY a."UserID"
            ),
            citations_in_window AS (
                SELECT a."UserID",
                       COALESCE(SUM({year_keys_expr_alias}), 0) AS focus_citations
                FROM "Authors" a
                JOIN "ResearchPaper" rp_all ON rp_all."PaperID" = a."PaperID"
                WHERE 1=1{AFFIL_CLAUSE_RPALL}
                GROUP BY a."UserID"
            )
            SELECT
                u."UserID",
                u."FullName_Ar",
                d."DepartmentName",
                COALESCE(p.focus_papers, 0) AS focus_papers,
                COALESCE(c.focus_citations, 0) AS focus_citations
            FROM "Users" u
            LEFT JOIN papers_in_window    p ON p."UserID" = u."UserID"
            LEFT JOIN citations_in_window c ON c."UserID" = u."UserID"
            LEFT JOIN "Works_In"   w ON w."UserID" = u."UserID" AND w."IsCurrentPosition" = TRUE
            LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
            WHERE u."UserType" = 'Researcher'
              AND COALESCE(p.focus_papers, 0) > 0
              AND (%s::int IS NULL OR w."DepartmentID" = %s::int)
            ORDER BY focus_papers DESC, focus_citations DESC
            LIMIT 5
        ''', [years] + [str(y) for y in years] + [hod_dept_id, hod_dept_id])
        top_researchers_rows = cur.fetchall()

        # For a HoD, restrict to papers authored by someone currently in their
        # department, else this leaks the institution's most-cited papers onto a
        # department dashboard.
        top_papers_sql = 'SELECT * FROM v_top_papers WHERE pub_year = ANY(%s)'
        top_papers_params = [years]
        if hod_dept_id is not None:
            top_papers_sql += (
                ' AND EXISTS (SELECT 1 FROM "Authors" a '
                '             JOIN "Works_In" w_tp ON w_tp."UserID" = a."UserID" '
                '                                 AND w_tp."IsCurrentPosition" = TRUE '
                '             WHERE a."PaperID" = v_top_papers.paper_id '
                '               AND w_tp."DepartmentID" = %s)'
            )
            top_papers_params.append(hod_dept_id)
        if albaha_only:
            top_papers_sql += (
                ' AND (SELECT rp_af."AffiliationVerified" FROM "ResearchPaper" rp_af '
                'WHERE rp_af."PaperID" = v_top_papers.paper_id) IS DISTINCT FROM FALSE'
            )
        top_papers_sql += ' ORDER BY citations DESC NULLS LAST LIMIT 5'
        cur.execute(top_papers_sql, top_papers_params)
        top_paper_cols = [c[0] for c in cur.description]
        top_papers = [dict(zip(top_paper_cols, row)) for row in cur.fetchall()]

        # Papers published in window + citations received in window. The
        # citation aggregation lives in its own CTE to avoid the cartesian blowup
        # of joining authors→papers→citations; each dept-researcher contributes
        # once.
        year_keys_dept = _cites_expr('rp_all', years)
        cur.execute(f'''
            WITH dept_citations AS (
                SELECT dept AS "DepartmentID", SUM(cites) AS total_citations
                FROM (
                    SELECT DISTINCT
                        w."DepartmentID"     AS dept,
                        rp_all."PaperID"     AS pid,
                        ({year_keys_dept})   AS cites
                    FROM "Works_In" w
                    JOIN "Authors" a ON a."UserID" = w."UserID"
                    JOIN "ResearchPaper" rp_all ON rp_all."PaperID" = a."PaperID"
                    WHERE w."IsCurrentPosition" = TRUE{AFFIL_CLAUSE_RPALL}
                ) ded
                GROUP BY dept
            )
            SELECT
                d."DepartmentID"   AS department_id,
                d."DepartmentName" AS department_name,
                d."CollegeID"      AS college_id,
                COUNT(DISTINCT u."UserID") AS total_researchers,
                COUNT(DISTINCT u."UserID") FILTER (WHERE r."LastSyncedAt" IS NOT NULL) AS active_researchers,
                COUNT(DISTINCT rp."PaperID") AS total_papers,
                COALESCE(MAX(dc.total_citations), 0) AS total_citations,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q1') AS total_q1_papers,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q2') AS total_q2_papers,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q3') AS total_q3_papers,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q4') AS total_q4_papers,
                COUNT(DISTINCT rp."PaperID")
                    FILTER (WHERE rp."Indexing" = 'Scopus' OR jr."Quartile" IS NOT NULL) AS total_scopus_papers,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = 'ISI') AS total_isi_papers,
                -- Venue split: 'Conference Proceedings' folds into Conference;
                -- Book/Book Series/NULL fold into Journal (same rule as
                -- v_department_stats after 20260607_dept_stats_split).
                COUNT(DISTINCT rp."PaperID")
                    FILTER (WHERE j."VenueType" ILIKE 'Conference%%') AS conference_papers,
                COUNT(DISTINCT rp."PaperID")
                    FILTER (WHERE rp."PaperID" IS NOT NULL
                            AND (j."VenueType" IS NULL
                                 OR j."VenueType" NOT ILIKE 'Conference%%')) AS journal_papers
            FROM "Department" d
            LEFT JOIN "Works_In" w ON w."DepartmentID" = d."DepartmentID" AND w."IsCurrentPosition" = TRUE
            LEFT JOIN "Users" u ON u."UserID" = w."UserID" AND u."UserType" = 'Researcher'
            LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
            LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
            LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID" AND rp."PubYear" = ANY(%s){AFFIL_CLAUSE}
            LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
            LEFT JOIN LATERAL (    SELECT "Quartile", "ImpactFactor"    FROM "JournalRankings"    WHERE "JournalID" = rp."JournalID"    ORDER BY "RankingYear" DESC NULLS LAST, "Source"    LIMIT 1) jr ON TRUE
            LEFT JOIN dept_citations dc ON dc."DepartmentID" = d."DepartmentID"
            GROUP BY d."DepartmentID", d."DepartmentName", d."CollegeID"
            HAVING (%s::int IS NULL OR d."DepartmentID" = %s::int)
            ORDER BY total_papers DESC NULLS LAST
        ''', [str(y) for y in years] + [years] + [hod_dept_id, hod_dept_id])
        dept_cols = [c[0] for c in cur.description]
        departments = [dict(zip(dept_cols, row)) for row in cur.fetchall()]

        # The KPI strip and per-department table use the full `years` floor, but
        # the trend chart clips to CHART_YEAR_FLOOR — 16 sparse points crush the
        # recent-growth signal. A narrower explicit ?year= filter is honored via
        # the intersection; an empty intersection falls back to `years` so we
        # never return an empty chart.
        chart_years = [y for y in years if y >= CHART_YEAR_FLOOR] or years

        # Papers published per (dept, year) — DISTINCT so a paper shared by
        # several co-authored faculty isn't double-counted.
        cur.execute(f'''
            SELECT w."DepartmentID", rp."PubYear",
                   COUNT(DISTINCT rp."PaperID") AS papers
            FROM "Works_In" w
            JOIN "Authors" a ON a."UserID" = w."UserID"
            JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
            WHERE w."IsCurrentPosition" = TRUE
              AND rp."PubYear" = ANY(%s)
              AND (%s::int IS NULL OR w."DepartmentID" = %s::int){AFFIL_CLAUSE}
            GROUP BY w."DepartmentID", rp."PubYear"
        ''', [chart_years, hod_dept_id, hod_dept_id])
        papers_by_dept_year = {}
        for did, yr, n in cur.fetchall():
            papers_by_dept_year.setdefault(did, {})[int(yr)] = int(n)

        # Per-year citations per department from the per-paper graph (same source
        # as the KPI, so the affiliation filter applies). DISTINCT on (dept,
        # paper, year) keeps a paper co-authored within one department from being
        # double-counted; a paper spanning two departments still counts once per
        # department, which is intended.
        cur.execute(f'''
            SELECT dept, yr, SUM(cites) AS citations
            FROM (
                SELECT DISTINCT
                    w."DepartmentID"      AS dept,
                    rp."PaperID"          AS pid,
                    year_kv.key::int      AS yr,
                    (year_kv.value)::int  AS cites
                FROM "Works_In" w
                JOIN "Authors" a ON a."UserID" = w."UserID"
                JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
                CROSS JOIN LATERAL jsonb_each_text(
                    COALESCE(rp."CitationsByYear", '{{}}'::jsonb)
                ) AS year_kv
                WHERE w."IsCurrentPosition" = TRUE
                  AND year_kv.value ~ '^[0-9]+$'
                  AND year_kv.key::int = ANY(%s)
                  AND (%s::int IS NULL OR w."DepartmentID" = %s::int){AFFIL_CLAUSE}
            ) ded
            GROUP BY dept, yr
        ''', [chart_years, hod_dept_id, hod_dept_id])
        cites_by_dept_year = {}
        for did, yr, n in cur.fetchall():
            cites_by_dept_year.setdefault(did, {})[int(yr)] = int(n)

        # Attach by_year to each department row over chart_years, so the chart
        # axis stays reasonable even though the KPIs span 2011+.
        for d in departments:
            did = d['department_id']
            d['by_year'] = [
                {
                    'year': y,
                    'papers':    papers_by_dept_year.get(did, {}).get(y, 0),
                    'citations': cites_by_dept_year.get(did, {}).get(y, 0),
                }
                for y in sorted(chart_years)
            ]

    return response.Response({
        'focus_years': years,
        # Echo the active filter back so the UI can reflect the mode.
        'affiliation_filter': 'albaha' if albaha_only else 'all',
        # Axis-year list so a chart doesn't have to re-derive the window.
        'chart_years': sorted(chart_years),
        'totals': {
            'researchers':         dept_agg['researchers'] or 0,
            'active_researchers':  dept_agg['active'] or 0,
            'papers':              paper_totals[0] or 0,
            'citations':           paper_totals[1] or 0,
            'q1_papers':           paper_totals[2] or 0,
            'scopus_papers':       paper_totals[3] or 0,
            'isi_papers':          paper_totals[4] or 0,
            'avg_h_index':         float(dept_agg['avg_h'] or 0),
        },
        'top_researchers': [
            {
                'user_id':         r[0],
                'full_name_ar':    r[1],
                'department_name': r[2],
                'total_papers':    r[3],
                'total_citations': r[4],
                'h_index':         0,
            }
            for r in top_researchers_rows
        ],
        'top_papers':      top_papers,
        'departments':     departments,
    })


# Spotlight-style global search returning { profiles: [...], papers: [...] }.
# The permission gate is the key rule: Admin/Dean/HoD see the full corpus
# (including papers by external, non-registered authors) for institutional
# oversight, while a plain Researcher only sees papers with at least one
# registered system author. Both sides cap results to keep the modal snappy.
@decorators.api_view(['GET'])
def universal_search(request):
    q = (request.query_params.get('q') or '').strip()
    if len(q) < 2:
        return response.Response({'profiles': [], 'papers': []})

    # has_litrix_perm lives on accounts.User; getattr keeps this safe when
    # SimpleJWT hands us an AnonymousUser-like object — we default to False.
    user = getattr(request, 'user', None)
    has_full_access = bool(
        user
        and user.is_authenticated
        and (
            user.has_litrix_perm('view_all_researchers')
            or user.has_litrix_perm('view_dept_researchers')
        )
    )

    from django.db import connection
    like = f'%{q}%'

    PROFILE_LIMIT = 8
    PAPER_LIMIT   = 10

    # All roles now see every UserType — the search is intentionally
    # unrestricted (it used to limit Researchers to Researcher-type profiles).
    user_type_filter = ''

    with connection.cursor() as cur:
        # Cross-script aware: we match Arabic name, English first/last, the
        # email/Litrix_ID identifiers, and Authors.AuthorNameRaw. That last one
        # is the bridge — scrapers populate it with the English-script author
        # string per paper, so an English query finds an Arabic-only profile (and
        # vice versa) without any transliteration heuristics.
        cur.execute(f'''
            SELECT
                u."UserID",
                u."Litrix_ID",
                u."FullName_Ar",
                TRIM(CONCAT_WS(' ', u."FirstName", u."LastName")) AS full_name_en,
                u."UserType",
                d."DepartmentName",
                (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = u."UserID") AS papers
            FROM "Users" u
            -- Pick ONE current Works_In per user. If the user is HoD
            -- of a department, that row is deprioritized so the
            -- researcher-side department wins. Stable tiebreak on
            -- StartDate ASC (oldest position first).
            LEFT JOIN LATERAL (
                SELECT wx."DepartmentID", dx."DepartmentName"
                FROM "Works_In" wx
                LEFT JOIN "Department" dx ON dx."DepartmentID" = wx."DepartmentID"
                WHERE wx."UserID" = u."UserID"
                  AND wx."IsCurrentPosition" = TRUE
                ORDER BY (dx."HeadID" = u."UserID") ASC NULLS FIRST,
                         wx."StartDate" ASC NULLS LAST
                LIMIT 1
            ) w ON TRUE
            LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
            WHERE 1=1
              {user_type_filter}
              AND (
                   u."FullName_Ar" ILIKE %s
                OR u."FirstName"   ILIKE %s
                OR u."LastName"    ILIKE %s
                OR u."Email"       ILIKE %s
                OR u."Litrix_ID"   ILIKE %s
                OR EXISTS (
                    SELECT 1 FROM "Authors" a
                    WHERE a."UserID" = u."UserID"
                      AND a."AuthorNameRaw" ILIKE %s
                )
              )
            ORDER BY papers DESC NULLS LAST, u."FullName_Ar"
            LIMIT %s
        ''', [like, like, like, like, like, like, PROFILE_LIMIT])
        profiles = [
            {
                'user_id':         r[0],
                'litrix_id':       r[1],
                'full_name_ar':    r[2],
                'full_name_en':    r[3],
                'user_type':       r[4],
                'department_name': r[5],
                'papers':          r[6],
            }
            for r in cur.fetchall()
        ]

        # Title match with the permission gate baked in: the EXISTS subquery
        # enforces "at least one system author" for restricted users, while the
        # full-access path skips it.
        if has_full_access:
            paper_filter = ''
        else:
            paper_filter = '''
                AND EXISTS (
                    SELECT 1 FROM "Authors" a
                    WHERE a."PaperID" = rp."PaperID"
                      AND a."UserID" IS NOT NULL
                )
            '''


        cur.execute(f'''
            SELECT
                rp."PaperID",
                rp."Title",
                rp."Title_En",
                rp."PubYear",
                rp."DOI",
                rp."Indexing",
                COALESCE(j."JournalName", rp."RawData_Log"->>'publication') AS journal_name,
                jr."Quartile",
                COALESCE(
                    (rp."RawData_Log"->'cited_by'->>'value')::int,
                    (rp."RawData_Log"->>'cited_by_count')::int,
                    0
                ) AS citations,
                -- compact summary of the first 3 system authors, for the card
                (
                    SELECT STRING_AGG(u2."FullName_Ar", '  ·  ')
                    FROM (
                        SELECT u3."FullName_Ar" FROM "Authors" a2
                        JOIN "Users" u3 ON u3."UserID" = a2."UserID"
                        WHERE a2."PaperID" = rp."PaperID"
                          AND u3."FullName_Ar" IS NOT NULL
                        ORDER BY a2."AuthorOrder" NULLS LAST
                        LIMIT 3
                    ) u2
                ) AS authors_summary
            FROM "ResearchPaper" rp
            LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
            LEFT JOIN LATERAL (    SELECT "Quartile", "ImpactFactor"    FROM "JournalRankings"    WHERE "JournalID" = rp."JournalID"    ORDER BY "RankingYear" DESC NULLS LAST, "Source"    LIMIT 1) jr ON TRUE
            WHERE (
                   rp."Title"    ILIKE %s
                OR rp."Title_En" ILIKE %s
                OR rp."DOI"      ILIKE %s
            )
              {paper_filter}
            ORDER BY rp."PubYear" DESC NULLS LAST, citations DESC
            LIMIT %s
        ''', [like, like, like, PAPER_LIMIT])
        papers = [
            {
                'paper_id':        r[0],
                'title':           r[1],
                'title_en':        r[2],
                'pub_year':        r[3],
                'doi':
           r[4],
                'indexing':        r[5],
                'journal_name':    r[6],
                'quartile':        r[7],
                'citations':       r[8],
                'authors_summary': r[9],
            }
            for r in cur.fetchall()
        ]

    return response.Response({
        'profiles':         profiles,
        'papers':           papers,
        'has_full_access':  has_full_access,
    })
