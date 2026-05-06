"""
DRF ViewSets — REST endpoint logic.

Each ViewSet is read-only (ReadOnlyModelViewSet) because the Angular
frontend never WRITES to the DB through these — writes go through the
scraper and bootstrap scripts. This is enforced at the framework level
(no PUT/POST/DELETE handlers exist), giving us defense-in-depth.

Filtering: we expose django-filter's DjangoFilterBackend so the frontend
can do GET /api/researchers/?department_id=2 etc.

Dashboard scope: the overview/export endpoints focus on the years in
FOCUS_YEARS. Edit this list to broaden or narrow the dashboard window.
"""
from rest_framework import viewsets, filters, decorators, response
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum, Count, Avg
from django.http import HttpResponse

from .models import (
    ResearcherStats, DepartmentStats, TopPaper, PublicationTrend,
    ResearchPaper,
)
from .serializers import (
    ResearcherStatsSerializer, DepartmentStatsSerializer,
    TopPaperSerializer, PublicationTrendSerializer,
    ResearchPaperSerializer,
)

FOCUS_YEARS = [2025, 2026]


def _excel_response(filename: str):
    """Helper: build an HttpResponse with the right xlsx headers."""
    resp = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@decorators.api_view(['GET'])
def export_excel(request):
    """
    GET /api/export/excel/?years=2025,2026&sheets=summary,departments,researchers,journals,conferences

    Build a comprehensive xlsx workbook based on user-selected years +
    sheets. The Dashboard opens an options modal that posts to this URL
    with the chosen filters. Defaults to all years + all sheets if no
    params are provided.

    Sheet layout (when all selected):
        • Summary YYYY            — one per year picked
        • Departments YYYY        — one per year picked (journal/conf split)
        • Researchers             — single sheet, contributions in window
        • Journals YYYY           — one per year, full paper details
        • Conferences YYYY        — one per year, full paper details
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from django.db import connection

    years_param = request.query_params.get('years', '').strip()
    if years_param:
        try:
            years = [int(y) for y in years_param.split(',') if y.strip().isdigit()]
        except ValueError:
            years = list(FOCUS_YEARS)
    else:
        years = list(FOCUS_YEARS)
    if not years:
        years = list(FOCUS_YEARS)

    sheets_param = request.query_params.get('sheets', '').strip()
    if sheets_param:
        sheets = {s.strip().lower() for s in sheets_param.split(',') if s.strip()}
    else:
        sheets = {'summary', 'departments', 'researchers', 'journals', 'conferences'}

    wb = Workbook()
    wb.remove(wb.active)
    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='1D1D1F')
    header_align = Alignment(horizontal='center', vertical='center', wrap_text=True)

    # Apple-style KPI overview sheet — first sheet so it opens by default.
    # Mirrors the dashboard cards: Researchers / Publications / Citations / h-index.
    if 'summary' in sheets or 'departments' in sheets or 'researchers' in sheets:
        ws_overview = wb.create_sheet('نظرة عامة', 0)
        big_value_font = Font(bold=True, size=28, color='1D1D1F')
        label_font = Font(bold=True, size=11, color='86868B')
        sublabel_font = Font(size=10, color='86868B')
        title_font = Font(bold=True, size=18, color='1D1D1F')
        bg_fill = PatternFill('solid', fgColor='FAFAFA')

        # Title row
        ws_overview['A1'] = 'Litrix — نظرة عامة'
        ws_overview['A1'].font = title_font
        ws_overview.row_dimensions[1].height = 36
        years_label = '، '.join(str(y) for y in sorted(years))
        ws_overview['A2'] = f'خلال {("السنتين" if len(years) == 2 else "السنوات")}: {years_label}'
        ws_overview['A2'].font = sublabel_font

        # KPI computation — same per-year semantics as the dashboard.
        with connection.cursor() as cur:
            year_keys_expr = ' + '.join([
                f"COALESCE((rp.\"CitationsByYear\"->>%s)::int, 0)"
                for _ in years
            ])
            cur.execute(f'''
                WITH window_papers AS (
                    SELECT DISTINCT rp."PaperID", jr."Quartile", rp."Indexing"
                    FROM "ResearchPaper" rp
                    LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
                    WHERE rp."PubYear" = ANY(%s)
                      AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")
                ),
                year_citations AS (
                    SELECT COALESCE(SUM({year_keys_expr}), 0) AS total
                    FROM "ResearchPaper" rp
                    WHERE EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")
                )
                SELECT
                    (SELECT COUNT(*) FROM "Users" WHERE "UserType" = 'Researcher')                  AS researchers,
                    (SELECT COUNT(*) FROM "Users" u
                       JOIN "Researcher" r ON r."UserID" = u."UserID"
                      WHERE r."LastSyncedAt" IS NOT NULL)                                            AS active,
                    (SELECT COUNT(*) FROM window_papers)                                            AS papers,
                    (SELECT COUNT(*) FROM window_papers WHERE "Quartile" = 'Q1')                    AS q1,
                    (SELECT total FROM year_citations)                                              AS citations,
                    (SELECT ROUND(AVG(h_index)::numeric, 1) FROM v_researcher_h_index)              AS avg_h
            ''', [years] + [str(y) for y in years])
            r_total, r_active, p_total, p_q1, c_total, avg_h = cur.fetchone()

        # KPI cards laid out across columns (4 cards in a row)
        # Each card spans 2 columns: A-B, C-D, E-F, G-H
        cards = [
            ('Researchers',  str(r_total),                f'{r_active} active'),
            ('Publications', f'{p_total:,}',              f'{p_q1} in Q1 journals'),
            ('Citations',    f'{c_total:,}',              f'خلال {years_label}'),
            ('h-index',      str(avg_h or 0),             'avg h-index'),
        ]
        for idx, (label, value, sub) in enumerate(cards):
            col_label = chr(ord('A') + idx * 2)
            col_value = chr(ord('A') + idx * 2)  # same col, multiple rows

            # Row 4: label
            cell_label = ws_overview.cell(row=4, column=idx * 2 + 1, value=label.upper())
            cell_label.font = label_font
            cell_label.fill = bg_fill

            # Row 5: big value
            cell_val = ws_overview.cell(row=5, column=idx * 2 + 1, value=value)
            cell_val.font = big_value_font
            cell_val.fill = bg_fill
            ws_overview.row_dimensions[5].height = 44

            # Row 6: sublabel
            cell_sub = ws_overview.cell(row=6, column=idx * 2 + 1, value=sub)
            cell_sub.font = sublabel_font
            cell_sub.fill = bg_fill

        # Widen the KPI columns
        for col in ['A', 'C', 'E', 'G']:
            ws_overview.column_dimensions[col].width = 22
        for col in ['B', 'D', 'F', 'H']:
            ws_overview.column_dimensions[col].width = 4  # spacer

    def style_header(ws, ncols):
        for col in range(1, ncols + 1):
            c = ws.cell(row=1, column=col)
            c.font = header_font
            c.fill = header_fill
            c.alignment = header_align
        ws.row_dimensions[1].height = 26

    def set_widths(ws, widths):
        for col_idx, w in enumerate(widths, start=1):
            ws.column_dimensions[chr(64 + col_idx)].width = w

    if 'summary' in sheets:
        for year in sorted(years):
            ws = wb.create_sheet(f'Summary {year}')
            ws.append(['Metric', 'Value'])
            style_header(ws, 2)
            with connection.cursor() as cur:
                cur.execute('''
                    SELECT
                        COUNT(DISTINCT rp."PaperID"),
                        COALESCE(SUM(COALESCE(("RawData_Log"->'cited_by'->>'value')::int, 0)), 0),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q1'),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q2'),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q3'),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q4'),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE j."VenueType" = 'Journal'),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE j."VenueType" = 'Conference')
                    FROM "ResearchPaper" rp
                    LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
                    LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
                    WHERE rp."PubYear" = %s
                      AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")
                ''', [year])
                p, c, q1, q2, q3, q4, jp, cp = cur.fetchone()
            for label, val in [
                ('Year', year),
                ('Total Papers', p),
                ('Total Citations', c),
                ('Journal Papers', jp),
                ('Conference Papers', cp),
                ('Q1 Papers', q1),
                ('Q2 Papers', q2),
                ('Q3 Papers', q3),
                ('Q4 Papers', q4),
            ]:
                ws.append([label, val])
            set_widths(ws, [25, 20])

    if 'departments' in sheets:
        for year in sorted(years):
            ws = wb.create_sheet(f'Departments {year}')
            ws.append([
                'Department', 'Researchers',
                'Journal Papers', 'Conference Papers', 'Total Papers',
                'Citations', 'Q1', 'Q2', 'Q3', 'Q4'
            ])
            style_header(ws, 10)
            with connection.cursor() as cur:
                cur.execute('''
                    SELECT
                        d."DepartmentName",
                        COUNT(DISTINCT u."UserID"),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE j."VenueType" = 'Journal'),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE j."VenueType" = 'Conference'),
                        COUNT(DISTINCT rp."PaperID"),
                        COALESCE(SUM(COALESCE((rp."RawData_Log"->'cited_by'->>'value')::int, 0)), 0),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q1'),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q2'),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q3'),
                        COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q4')
                    FROM "Department" d
                    LEFT JOIN "Works_In" w ON w."DepartmentID" = d."DepartmentID"
                                          AND w."IsCurrentPosition" = TRUE
                    LEFT JOIN "Users" u ON u."UserID" = w."UserID" AND u."UserType" = 'Researcher'
                    LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
                    LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
                                               AND rp."PubYear" = %s
                    LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
                    LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
                    GROUP BY d."DepartmentName"
                    ORDER BY 5 DESC
                ''', [year])
                for row in cur.fetchall():
                    ws.append(list(row))
            set_widths(ws, [32, 12, 14, 16, 12, 12, 8, 8, 8, 8])

    if 'researchers' in sheets:
        ws = wb.create_sheet('Researchers')
        ws.append([
            'Researcher (AR)', 'Department', 'Rank',
            'Papers (window)', 'Citations (window)',
            'Papers (all-time)', 'Citations (all-time)',
            'h-index (all-time)', 'Status',
            'Scholar ID', 'ORCID'
        ])
        style_header(ws, 11)
        with connection.cursor() as cur:
            cur.execute('''
                SELECT
                    u."FullName_Ar",
                    d."DepartmentName",
                    r."AcademicRank",
                    -- Window stats (focus years only)
                    COUNT(DISTINCT a_w."PaperID") FILTER (WHERE rp_w."PubYear" = ANY(%(years)s)) AS papers_window,
                    COALESCE(SUM(COALESCE((rp_w."RawData_Log"->'cited_by'->>'value')::int, 0))
                        FILTER (WHERE rp_w."PubYear" = ANY(%(years)s)), 0) AS citations_window,
                    -- All-time stats
                    COUNT(DISTINCT a_w."PaperID") AS papers_all,
                    COALESCE(SUM(COALESCE((rp_w."RawData_Log"->'cited_by'->>'value')::int, 0)), 0) AS citations_all,
                    COALESCE(hi.h_index, 0) AS h_index,
                    -- Status with clear priority:
                    --   1. Has papers in the focus window  → Active
                    --   2. Has all-time papers (manual OR scraped) → Historical
                    --   3. Has a public profile but no papers yet → Pending Sync
                    --   4. Nothing at all → No Profile (manual outreach needed)
                    CASE
                        WHEN COUNT(DISTINCT a_w."PaperID")
                                FILTER (WHERE rp_w."PubYear" = ANY(%(years)s)) > 0
                            THEN 'Active'
                        WHEN COUNT(DISTINCT a_w."PaperID") > 0
                            THEN 'Historical'
                        WHEN u."Scholar_ID" IS NOT NULL
                          OR r."ORCID_ID" IS NOT NULL
                          OR r."OpenAlex_AuthorID" IS NOT NULL
                          OR r."Scopus_ID" IS NOT NULL
                            THEN 'Pending Sync'
                        ELSE 'No Profile'
                    END AS sync_status,
                    u."Scholar_ID",
                    r."ORCID_ID"
                FROM "Users" u
                JOIN "Researcher" r ON r."UserID" = u."UserID"
                LEFT JOIN "Works_In" w ON w."UserID" = u."UserID" AND w."IsCurrentPosition" = TRUE
                LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
                LEFT JOIN "Authors" a_w ON a_w."UserID" = u."UserID"
                LEFT JOIN "ResearchPaper" rp_w ON rp_w."PaperID" = a_w."PaperID"
                LEFT JOIN v_researcher_h_index hi ON hi."UserID" = u."UserID"
                WHERE u."UserType" = 'Researcher'
                GROUP BY u."UserID", u."FullName_Ar", d."DepartmentName",
                         r."AcademicRank", hi.h_index, u."Scholar_ID",
                         r."ORCID_ID", r."OpenAlex_AuthorID", r."Scopus_ID",
                         r."LastSyncedAt"
                ORDER BY papers_window DESC, h_index DESC
            ''', {'years': years})
            for row in cur.fetchall():
                ws.append(list(row))
        set_widths(ws, [32, 22, 18, 12, 14, 14, 16, 14, 14, 18, 22])

    if 'journals' in sheets:
        for year in sorted(years):
            ws = wb.create_sheet(f'Journals {year}')
            # Two author columns:
            #   1. Al-Baha researchers only (Arabic names, NO "(جامعة الباحة)" suffix)
            #   2. All authors combined (Arabic + foreign, no affiliations)
            ws.append([
                'Department', 'Title',
                'باحثو جامعة الباحة', 'كل المؤلفين',
                'Journal', 'Quartile', 'IF', 'Indexing',
                'Citations', 'DOI'
            ])
            style_header(ws, 10)
            with connection.cursor() as cur:
                cur.execute('''
                    SELECT
                        department_name, title,
                        authors_ar,        -- Al-Baha researchers (Arabic)
                        all_authors_en,    -- ALL authors (English)
                        journal_name, quartile, impact_factor, indexing,
                        citations, doi
                    FROM v_paper_details
                    WHERE pub_year = %s AND venue_type = 'Journal'
                    ORDER BY department_name, citations DESC NULLS LAST
                ''', [year])
                for row in cur.fetchall():
                    ws.append(list(row))
            set_widths(ws, [22, 60, 50, 50, 30, 10, 8, 12, 10, 30])

    if 'conferences' in sheets:
        for year in sorted(years):
            ws = wb.create_sheet(f'Conferences {year}')
            ws.append([
                'Department', 'Title',
                'باحثو جامعة الباحة', 'كل المؤلفين',
                'Conference', 'Indexing', 'Citations', 'DOI'
            ])
            style_header(ws, 8)
            with connection.cursor() as cur:
                cur.execute('''
                    SELECT
                        department_name, title,
                        authors_ar,        -- Al-Baha researchers (Arabic)
                        all_authors_en,    -- ALL authors (English)
                        journal_name, indexing, citations, doi
                    FROM v_paper_details
                    WHERE pub_year = %s AND venue_type = 'Conference'
                    ORDER BY department_name, citations DESC NULLS LAST
                ''', [year])
                for row in cur.fetchall():
                    ws.append(list(row))
            set_widths(ws, [22, 60, 50, 50, 30, 12, 10, 30])

    if not wb.sheetnames:
        ws = wb.create_sheet('Empty')
        ws.append(['No sheets selected'])

    fname = f"litrix_export_{'-'.join(map(str, sorted(years)))}.xlsx"
    resp = _excel_response(fname)
    wb.save(resp)
    return resp


class ResearcherViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/researchers/             → list of all researchers
    GET /api/researchers/?department_id=2  → filtered
    GET /api/researchers/?search=محمد  → fuzzy search
    GET /api/researchers/?ordering=-h_index  → sort
    GET /api/researchers/{user_id}/   → single researcher detail
    GET /api/researchers/{user_id}/papers/ → researcher's papers
    """
    queryset = ResearcherStats.objects.all()
    serializer_class = ResearcherStatsSerializer
    filter_backends = [
        DjangoFilterBackend,
        filters.OrderingFilter,
        filters.SearchFilter,
    ]
    filterset_fields = ['department_id', 'academic_rank']
    search_fields = ['full_name_ar', 'full_name_en', 'scholar_id', 'orcid_id']
    ordering_fields = [
        'total_papers', 'total_citations', 'h_index',
        'q1_papers', 'last_pub_year',
    ]
    ordering = ['-h_index', '-total_papers']

    @decorators.action(detail=True, methods=['get'])
    def profile(self, request, pk=None):
        """
        GET /api/researchers/{user_id}/profile/

        One-shot payload powering the researcher profile page:
          - identity         (name, dept, scholar/orcid/openalex IDs)
          - aggregated stats (papers, citations, h-index)
          - per-year citations (merged across all papers — chart-ready)
          - papers list      (full metadata: journal, quartile, citations,
                              citations_by_year, source, indexing)

        Why one endpoint: the profile page renders 3 sections that all
        depend on the same data; batching avoids 3 round-trips and a
        flash of empty-then-filled UI.
        """
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute('''
                SELECT
                    u."UserID", u."FullName_Ar",
                    u."FirstName", u."LastName", u."Email",
                    u."Scholar_ID", u."ORCID",
                    r."OpenAlex_AuthorID", r."LastSyncedAt",
                    d."DepartmentID", d."DepartmentName"
                FROM "Users" u
                LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
                LEFT JOIN "Works_In" w ON w."UserID" = u."UserID"
                                       AND w."IsCurrentPosition" = TRUE
                LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
                WHERE u."UserID" = %s
            ''', [pk])
            row = cur.fetchone()
            if not row:
                return response.Response(
                    {'error': 'Researcher not found'}, status=404
                )
            identity = {
                'user_id':           row[0],
                'full_name_ar':      row[1],
                'first_name':        row[2],
                'last_name':         row[3],
                'email':             row[4],
                'scholar_id':        row[5],
                'orcid':             row[6],
                'openalex_author_id': row[7],
                'last_synced_at':    row[8],
                'department_id':     row[9],
                'department_name':   row[10],
            }

            # Aggregated stats + per-year citations
            cur.execute('''
                SELECT
                    COUNT(DISTINCT rp."PaperID")                         AS total_papers,
                    COALESCE(SUM(COALESCE(
                        ("RawData_Log"->'cited_by'->>'value')::int,
                        ("RawData_Log"->>'cited_by_count')::int,
                        0)), 0)                                          AS total_citations,
                    COUNT(DISTINCT rp."PaperID")
                        FILTER (WHERE jr."Quartile" = 'Q1')              AS q1_papers,
                    COUNT(DISTINCT rp."PaperID")
                        FILTER (WHERE rp."Indexing" = 'Scopus'
                                   OR jr."Quartile" IS NOT NULL)         AS scopus_papers,
                    COUNT(DISTINCT rp."PaperID")
                        FILTER (WHERE rp."Indexing" = 'ISI')             AS isi_papers
                FROM "ResearchPaper" rp
                JOIN "Authors" a ON a."PaperID" = rp."PaperID"
                LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
                WHERE a."UserID" = %s
            ''', [pk])
            stats_row = cur.fetchone()
            stats = {
                'total_papers':    stats_row[0],
                'total_citations': stats_row[1],
                'q1_papers':       stats_row[2],
                'scopus_papers':   stats_row[3],
                'isi_papers':      stats_row[4],
            }

            # Per-year citations: merge JSONB across all his papers.
            # We unfold each CitationsByYear into rows then sum by year.
            cur.execute('''
                SELECT
                    yr.year::int            AS year,
                    SUM(yr.cnt::int)        AS citations
                FROM "ResearchPaper" rp
                JOIN "Authors" a ON a."PaperID" = rp."PaperID"
                CROSS JOIN LATERAL jsonb_each_text(
                    COALESCE(rp."CitationsByYear", '{}'::jsonb)
                ) AS yr(year, cnt)
                WHERE a."UserID" = %s
                  AND yr.year ~ '^[0-9]+$'
                GROUP BY yr.year
                ORDER BY yr.year
            ''', [pk])
            citations_by_year = [
                {'year': r[0], 'citations': r[1]}
                for r in cur.fetchall()
            ]

            # Papers list with full metadata
            cur.execute('''
                SELECT
                    rp."PaperID", rp."Title", rp."DOI", rp."PubYear",
                    rp."Source", rp."Indexing", rp."CitationsByYear",
                    COALESCE(
                        ("RawData_Log"->'cited_by'->>'value')::int,
                        ("RawData_Log"->>'cited_by_count')::int,
                        0
                    )                       AS citations,
                    j."JournalName",
                    j."ISSN_Print",
                    j."VenueType",
                    jr."Quartile",
                    jr."ImpactFactor"
                FROM "ResearchPaper" rp
                JOIN "Authors" a ON a."PaperID" = rp."PaperID"
                LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
                LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
                WHERE a."UserID" = %s
                ORDER BY rp."PubYear" DESC NULLS LAST, rp."PaperID" DESC
            ''', [pk])
            paper_cols = [c[0].lower() for c in cur.description]
            papers = [dict(zip([
                'paper_id', 'title', 'doi', 'pub_year', 'source',
                'indexing', 'citations_by_year', 'citations',
                'journal_name', 'issn_print', 'venue_type',
                'quartile', 'impact_factor',
            ], r)) for r in cur.fetchall()]

        return response.Response({
            'identity':          identity,
            'stats':             stats,
            'citations_by_year': citations_by_year,
            'papers':            papers,
        })

    @decorators.action(detail=True, methods=['get'])
    def papers(self, request, pk=None):
        """
        GET /api/researchers/{user_id}/papers/
        Returns ALL papers authored by this researcher (newest first).
        """
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute('''
                SELECT
                    rp."PaperID", rp."Title", rp."Title_En", rp."Abstract",
                    rp."Language", rp."DOI", rp."PubYear",
                    rp."Volume", rp."Issue", rp."Pages",
                    rp."Source", rp."IsVerified", rp."ScrapedAt"
                FROM "ResearchPaper" rp
                JOIN "Authors" a ON a."PaperID" = rp."PaperID"
                WHERE a."UserID" = %s
                ORDER BY rp."PubYear" DESC NULLS LAST, rp."PaperID" DESC
            ''', [pk])
            rows = cur.fetchall()
            cols = [c[0].lower() for c in cur.description]

        field_map = {
            'paperid': 'paper_id', 'title': 'title', 'title_en': 'title_en',
            'abstract': 'abstract', 'language': 'language', 'doi': 'doi',
            'pubyear': 'pub_year', 'volume': 'volume', 'issue': 'issue',
            'pages': 'pages', 'source': 'source',
            'isverified': 'is_verified', 'scrapedat': 'scraped_at',
        }
        data = [
            {field_map.get(c, c): row[i] for i, c in enumerate(cols)}
            for row in rows
        ]
        return response.Response(data)


class DepartmentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/departments/         → list with aggregated stats
    GET /api/departments/{id}/    → single department detail
    """
    queryset = DepartmentStats.objects.all()
    serializer_class = DepartmentStatsSerializer
    filter_backends = [filters.OrderingFilter]
    ordering_fields = [
        'total_papers', 'total_citations', 'total_q1_papers',
        'avg_h_index', 'total_researchers',
    ]
    ordering = ['-total_papers']

    @decorators.action(detail=True, methods=['get'])
    def researchers(self, request, pk=None):
        """GET /api/departments/{id}/researchers/ — list of researchers."""
        qs = ResearcherStats.objects.filter(department_id=pk).order_by(
            '-h_index', '-total_papers'
        )
        page = self.paginate_queryset(qs)
        ser = ResearcherStatsSerializer(page or qs, many=True)
        return self.get_paginated_response(ser.data) if page else response.Response(ser.data)


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


@decorators.api_view(['GET'])
def yearly_breakdown(request):
    """
    GET /api/yearly-breakdown/?year=2025

    Returns the department-level breakdown for a given year:
        • For each department: journal_papers + conference_papers + citations
        • A flat list of all papers (split by venue_type on the frontend)

    The frontend renders this as: Year tabs → Dept summary cards →
    expandable Journal/Conference paper lists.
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

    from django.db import connection
    with connection.cursor() as cur:
        cur.execute('''
            SELECT
                department_id,
                department_name,
                COUNT(*) FILTER (WHERE venue_type = 'Journal')    AS journal_papers,
                COUNT(*) FILTER (WHERE venue_type = 'Conference') AS conference_papers,
                COUNT(*)                                          AS total_papers,
                COALESCE(SUM(citations), 0)                       AS total_citations
            FROM v_paper_details
            WHERE pub_year = %s
              AND department_id IS NOT NULL
            GROUP BY department_id, department_name
            ORDER BY total_papers DESC
        ''', [year_int])
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

        cur.execute('''
            SELECT
                paper_id, title, doi, citations, journal_name,
                venue_type, quartile, impact_factor, indexing,
                department_id, department_name, authors_ar
            FROM v_paper_details
            WHERE pub_year = %s
              AND department_id IS NOT NULL
            ORDER BY citations DESC NULLS LAST, paper_id
        ''', [year_int])
        cols = [c[0] for c in cur.description]
        papers = [dict(zip(cols, row)) for row in cur.fetchall()]

    return response.Response({
        'year': year_int,
        'departments': departments,
        'papers': papers,
    })


def _resolve_years(request) -> list:
    """
    Read the optional ?year= query param. Returns:
        - [year] if a single year is requested (e.g. ?year=2025)
        - FOCUS_YEARS otherwise (default = both 2025 and 2026)
    """
    year_param = request.query_params.get('year')
    if year_param and year_param.isdigit():
        return [int(year_param)]
    return list(FOCUS_YEARS)


@decorators.api_view(['GET'])
def overview(request):
    """
    GET /api/stats/overview/         → both focus years
    GET /api/stats/overview/?year=2025  → just 2025
    GET /api/stats/overview/?year=2026  → just 2026

    A one-shot payload that powers the Admin/Dean landing page. Combines
    multiple views into a single response so the frontend doesn't have
    to make 5 round-trips on first load.
    """
    years = _resolve_years(request)
    dept_agg = DepartmentStats.objects.aggregate(
        researchers=Sum('total_researchers'),
        active=Sum('active_researchers'),
        avg_h=Avg('avg_h_index'),
    )

    from django.db import connection
    with connection.cursor() as cur:
        # Papers count: papers PUBLISHED in window (sets the denominator
        # for "publications" KPI).
        cur.execute('''
            SELECT
                COUNT(DISTINCT rp."PaperID") AS papers,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q1') AS q1,
                COUNT(DISTINCT rp."PaperID")
                    FILTER (WHERE rp."Indexing" = 'Scopus' OR jr."Quartile" IS NOT NULL) AS scopus,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = 'ISI') AS isi
            FROM "ResearchPaper" rp
            LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
            WHERE rp."PubYear" = ANY(%s)
              AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")
        ''', [years])
        paper_count_row = cur.fetchone()

        # Citations: per-year semantics — a citation is bound to the
        # year it was received, not the year the paper was published.
        # SUM(CitationsByYear[year]) across ALL papers attributed to
        # our researchers (any pub_year). This matches academic norms
        # for "research impact in YYYY".
        year_keys_expr = ' + '.join([
            f"COALESCE((rp.\"CitationsByYear\"->>%s)::int, 0)"
            for _ in years
        ])
        cur.execute(f'''
            SELECT COALESCE(SUM({year_keys_expr}), 0) AS citations
            FROM "ResearchPaper" rp
            WHERE EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")
        ''', [str(y) for y in years])
        citations_row = cur.fetchone()

        # Combine into the (papers, citations, q1, scopus, isi) shape
        paper_totals = (
            paper_count_row[0],
            citations_row[0],
            paper_count_row[1],
            paper_count_row[2],
            paper_count_row[3],
        )

        # Top researchers: papers PUBLISHED in window + citations
        # RECEIVED in window (per-year sum across ALL their papers).
        year_keys_expr_alias = ' + '.join([
            f"COALESCE((rp_all.\"CitationsByYear\"->>%s)::int, 0)"
            for _ in years
        ])
        cur.execute(f'''
            WITH papers_in_window AS (
                SELECT a."UserID",
                       COUNT(DISTINCT rp."PaperID") AS focus_papers
                FROM "Authors" a
                JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
                WHERE rp."PubYear" = ANY(%s)
                GROUP BY a."UserID"
            ),
            citations_in_window AS (
                SELECT a."UserID",
                       COALESCE(SUM({year_keys_expr_alias}), 0) AS focus_citations
                FROM "Authors" a
                JOIN "ResearchPaper" rp_all ON rp_all."PaperID" = a."PaperID"
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
            ORDER BY focus_papers DESC, focus_citations DESC
            LIMIT 5
        ''', [years] + [str(y) for y in years])
        top_researchers_rows = cur.fetchall()

        cur.execute('''
            SELECT * FROM v_top_papers
            WHERE pub_year = ANY(%s)
            ORDER BY citations DESC NULLS LAST
            LIMIT 5
        ''', [years])
        top_paper_cols = [c[0] for c in cur.description]
        top_papers = [dict(zip(top_paper_cols, row)) for row in cur.fetchall()]

        # Departments: papers PUBLISHED in window + citations RECEIVED in
        # window (per-year). Citation aggregation lives in a separate CTE
        # to avoid the cartesian explosion of joining authors→papers→
        # citations (each Department-Researcher contributes once).
        year_keys_dept = ' + '.join([
            f"COALESCE((rp_all.\"CitationsByYear\"->>%s)::int, 0)"
            for _ in years
        ])
        cur.execute(f'''
            WITH dept_citations AS (
                SELECT
                    w."DepartmentID",
                    COALESCE(SUM({year_keys_dept}), 0) AS total_citations
                FROM "Works_In" w
                JOIN "Authors" a ON a."UserID" = w."UserID"
                JOIN "ResearchPaper" rp_all ON rp_all."PaperID" = a."PaperID"
                WHERE w."IsCurrentPosition" = TRUE
                GROUP BY w."DepartmentID"
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
                COUNT(DISTINCT rp."PaperID")
                    FILTER (WHERE rp."Indexing" = 'Scopus' OR jr."Quartile" IS NOT NULL) AS total_scopus_papers,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = 'ISI') AS total_isi_papers
            FROM "Department" d
            LEFT JOIN "Works_In" w ON w."DepartmentID" = d."DepartmentID" AND w."IsCurrentPosition" = TRUE
            LEFT JOIN "Users" u ON u."UserID" = w."UserID" AND u."UserType" = 'Researcher'
            LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
            LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
            LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID" AND rp."PubYear" = ANY(%s)
            LEFT JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
            LEFT JOIN dept_citations dc ON dc."DepartmentID" = d."DepartmentID"
            GROUP BY d."DepartmentID", d."DepartmentName", d."CollegeID"
            ORDER BY total_papers DESC NULLS LAST
        ''', [str(y) for y in years] + [years])
        dept_cols = [c[0] for c in cur.description]
        departments = [dict(zip(dept_cols, row)) for row in cur.fetchall()]

    return response.Response({
        'focus_years': years,
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
