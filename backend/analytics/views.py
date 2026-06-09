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
import re

from rest_framework import viewsets, filters, decorators, response
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum, Count, Avg
from django.http import HttpResponse


# Canonical Litrix-ID = "Lit-" + 6 zero-padded digits (e.g. Lit-000042).
# We accept user input liberally (LIT-42, lit-0042, Lit-000042) and
# normalize to canonical form before any DB lookup. This keeps URLs
# robust to manual typing and case-insensitive path parameters.
LITRIX_ID_PATTERN = re.compile(r'^lit-(\d+)$', re.IGNORECASE)


def normalize_litrix_id(raw):
    """Return canonical Lit-NNNNNN if `raw` looks like a Litrix-ID, else None."""
    if not isinstance(raw, str):
        return None
    m = LITRIX_ID_PATTERN.match(raw.strip())
    if not m:
        return None
    return f'Lit-{int(m.group(1)):06d}'

from .models import (
    ResearcherStats, DepartmentStats, TopPaper, PublicationTrend,
    ResearchPaper,
)
from .serializers import (
    ResearcherStatsSerializer, DepartmentStatsSerializer,
    TopPaperSerializer, PublicationTrendSerializer,
    ResearchPaperSerializer,
)

# Two windows, two different purposes:
#
#   YEAR_FLOOR (2011) — the institutional "all-time" floor.
#       College of Computing was founded in 2011, so any meaningful
#       cumulative KPI (total papers, total citations, h-index avg)
#       starts here. Used for the totals strip and the per-dept tables.
#
#   CHART_YEAR_FLOOR (2019) — the time-series chart floor.
#       The trend/area charts on the dashboard render one data point
#       per year. Stretching them back to 2011 makes a sparse, crowded
#       X-axis and the recent growth signal gets lost. 2019 gives a
#       readable 7–8 point window that still captures the post-sync
#       era.
#
# Both upper bounds slide forward with the current year automatically.
YEAR_FLOOR        = 2011
CHART_YEAR_FLOOR  = 2019


def _default_focus_years():
    from datetime import datetime
    current = datetime.now().year
    return list(range(YEAR_FLOOR, current + 1))


def _default_chart_years():
    """Used by the time-series chart payload — see CHART_YEAR_FLOOR."""
    from datetime import datetime
    current = datetime.now().year
    return list(range(CHART_YEAR_FLOOR, current + 1))


FOCUS_YEARS = _default_focus_years()


@decorators.api_view(['GET'])
def paper_detail(request, paper_id):
    """
    GET /api/papers/<paper_id>/detail/

    Full paper details for the modal popup. Pulls everything from
    ResearchPaper + RawData_Log (the original scraped JSON), including:
      - title, abstract, doi, year, publisher
      - authors string (raw from Scholar)
      - citation_id, link, total citations
      - per-year citations breakdown
      - journal info (name, issn, venue type, quartile, IF)
      - department + Al-Baha researchers attribution
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

        # Normalise the authorships blob (RawData_Log->'authorships' is
        # the OpenAlex-shape array we now store on every scrape). For
        # each authorship pull the display name + every institution
        # name we can reach (display_name + raw_affiliation_strings),
        # and flag whether any affiliation matches Al-Baha University.
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
            # Structured authorships: empty until backfill_authorships
            # runs (or until new scrapes populate it). Frontend should
            # fall back to `raw_authors` when this list is empty.
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

    # ------------------------------------------------------------------
    # HoD scoping. A HoD (view_dept_researchers without view_all_researchers)
    # may only export their OWN department, so every sheet below is filtered
    # to `hod_dept_id`. Admin/Dean get the full institution (hod_dept_id is
    # None → all the `scoped`-guarded clauses are skipped). The shared helper
    # resolves the department via Department.HeadID then current Works_In and
    # returns the sentinel -1 for a HoD with no department at all.
    # ------------------------------------------------------------------
    hod_dept_id = _hod_scope_department_id(request)
    if hod_dept_id == -1:
        return response.Response(
            {'error': 'You are not assigned to any department. '
                      'Contact an admin to set your department.'},
            status=403,
        )
    scoped = hod_dept_id is not None

    # Department label for the Overview sheet, so a HoD's file makes its
    # scope obvious.
    hod_dept_name = None
    if scoped:
        with connection.cursor() as _c:
            _c.execute(
                'SELECT "DepartmentName" FROM "Department" WHERE "DepartmentID" = %s',
                [hod_dept_id],
            )
            _row = _c.fetchone()
            hod_dept_name = _row[0] if _row else None

    # EXISTS predicate: the paper has at least one author whose CURRENT
    # position is in the HoD's department. Appended to paper queries when
    # scoped; `rp_alias` is the ResearchPaper alias in the target query.
    def _dept_paper_clause(rp_alias: str) -> str:
        return (
            f' AND EXISTS (SELECT 1 FROM "Authors" a_dept '
            f'             JOIN "Works_In" w_dept ON w_dept."UserID" = a_dept."UserID" '
            f'                                   AND w_dept."IsCurrentPosition" = TRUE '
            f'             WHERE a_dept."PaperID" = {rp_alias}."PaperID" '
            f'               AND w_dept."DepartmentID" = %s)'
        )

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
        ws_overview = wb.create_sheet('Overview', 0)
        big_value_font = Font(bold=True, size=28, color='1D1D1F')
        label_font = Font(bold=True, size=11, color='86868B')
        sublabel_font = Font(size=10, color='86868B')
        title_font = Font(bold=True, size=18, color='1D1D1F')
        bg_fill = PatternFill('solid', fgColor='FAFAFA')

        ws_overview['A1'] = 'Litrix — Research Analytics Overview'
        ws_overview['A1'].font = title_font
        ws_overview.row_dimensions[1].height = 36
        years_label = ', '.join(str(y) for y in sorted(years))
        scope_label = f' · Department: {hod_dept_name}' if hod_dept_name else ''
        ws_overview['A2'] = f'Window: {years_label}{scope_label}'
        ws_overview['A2'].font = sublabel_font

        # KPI computation — same per-year semantics as the dashboard. Each
        # KPI is its own small query so the HoD department filter can be
        # added cleanly (param order stays trivial).
        year_strs = [str(y) for y in years]
        with connection.cursor() as cur:
            # Researchers + active (scoped to current dept positions).
            if scoped:
                cur.execute('''
                    SELECT COUNT(DISTINCT u."UserID"),
                           COUNT(DISTINCT u."UserID") FILTER (WHERE r."LastSyncedAt" IS NOT NULL)
                    FROM "Users" u
                    JOIN "Works_In" w ON w."UserID" = u."UserID" AND w."IsCurrentPosition" = TRUE
                    LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
                    WHERE u."UserType" = 'Researcher' AND w."DepartmentID" = %s
                ''', [hod_dept_id])
            else:
                cur.execute('''
                    SELECT COUNT(DISTINCT u."UserID"),
                           COUNT(DISTINCT u."UserID") FILTER (WHERE r."LastSyncedAt" IS NOT NULL)
                    FROM "Users" u
                    LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
                    WHERE u."UserType" = 'Researcher'
                ''')
            r_total, r_active = cur.fetchone()

            # Papers in window + Q1 (author-in-dept when scoped).
            papers_sql = (
                'SELECT COUNT(*), COUNT(*) FILTER (WHERE q = \'Q1\') FROM ('
                '  SELECT DISTINCT rp."PaperID", jr."Quartile" AS q'
                '  FROM "ResearchPaper" rp'
                '  LEFT JOIN LATERAL (SELECT "Quartile" FROM "JournalRankings"'
                '      WHERE "JournalID" = rp."JournalID"'
                '      ORDER BY "RankingYear" DESC NULLS LAST, "Source" LIMIT 1) jr ON TRUE'
                '  WHERE rp."PubYear" = ANY(%s)'
                '    AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")'
            )
            papers_params = [years]
            if scoped:
                papers_sql += _dept_paper_clause('rp')
                papers_params.append(hod_dept_id)
            papers_sql += ') sub'
            cur.execute(papers_sql, papers_params)
            p_total, p_q1 = cur.fetchone()

            # Citations (Researcher.CitationsByYear) summed, scoped to dept.
            year_keys_expr = ' + '.join([
                "COALESCE((r.\"CitationsByYear\"->>%s)::int, 0)" for _ in years
            ])
            cit_sql = (
                f'SELECT COALESCE(SUM({year_keys_expr}), 0) FROM "Researcher" r '
                'WHERE r."CitationsByYear" IS NOT NULL'
            )
            cit_params = list(year_strs)
            if scoped:
                cit_sql += (
                    ' AND EXISTS (SELECT 1 FROM "Works_In" w '
                    '             WHERE w."UserID" = r."UserID" '
                    '               AND w."IsCurrentPosition" = TRUE '
                    '               AND w."DepartmentID" = %s)'
                )
                cit_params.append(hod_dept_id)
            cur.execute(cit_sql, cit_params)
            c_total = cur.fetchone()[0]

            # Average h-index, scoped to dept researchers.
            if scoped:
                cur.execute('''
                    SELECT ROUND(AVG(hi.h_index)::numeric, 1)
                    FROM v_researcher_h_index hi
                    JOIN "Works_In" w ON w."UserID" = hi."UserID"
                                     AND w."IsCurrentPosition" = TRUE
                    WHERE w."DepartmentID" = %s
                ''', [hod_dept_id])
            else:
                cur.execute('SELECT ROUND(AVG(h_index)::numeric, 1) FROM v_researcher_h_index')
            avg_h = cur.fetchone()[0]

        # KPI cards laid out across columns (4 cards in a row)
        # Each card spans 2 columns: A-B, C-D, E-F, G-H
        cards = [
            ('Researchers',  str(r_total),                f'{r_active} active'),
            ('Publications', f'{p_total:,}',              f'{p_q1} in Q1 journals'),
            ('Citations',    f'{c_total:,}',              f'received in {years_label}'),
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

    def wrap_column(ws, col_idx: int):
        """
        Apply wrap_text alignment to every body cell in the given
        column. Used for the Abstract column so long paragraphs render
        readably in Excel instead of overflowing into the next cell.
        """
        from openpyxl.styles import Alignment
        for row in range(2, ws.max_row + 1):
            ws.cell(row=row, column=col_idx).alignment = Alignment(
                wrap_text=True, vertical='top',
            )

    if 'summary' in sheets:
        for year in sorted(years):
            ws = wb.create_sheet(f'Summary {year}')
            ws.append(['Metric', 'Value'])
            style_header(ws, 2)
            with connection.cursor() as cur:
                # Paper counts per year (published-in-year semantics)
                summary_sql = (
                    'SELECT '
                    '    COUNT(DISTINCT rp."PaperID"), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = \'Q1\'), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = \'Q2\'), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = \'Q3\'), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = \'Q4\'), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE j."VenueType" = \'Journal\'), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE j."VenueType" = \'Conference\') '
                    'FROM "ResearchPaper" rp '
                    'LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID" '
                    'LEFT JOIN LATERAL (SELECT "Quartile", "ImpactFactor" FROM "JournalRankings" '
                    '  WHERE "JournalID" = rp."JournalID" ORDER BY "RankingYear" DESC NULLS LAST, "Source" LIMIT 1) jr ON TRUE '
                    'WHERE rp."PubYear" = %s '
                    '  AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")'
                )
                summary_params = [year]
                if scoped:
                    summary_sql += _dept_paper_clause('rp')
                    summary_params.append(hod_dept_id)
                cur.execute(summary_sql, summary_params)
                p, q1, q2, q3, q4, jp, cp = cur.fetchone()

                # Citations: SUM Researcher.CitationsByYear[year] across
                # researchers (scoped to the dept's current members for HoDs).
                # Year-of-receipt semantics: citations RECEIVED this year.
                cit_sql = (
                    'SELECT COALESCE(SUM(COALESCE((r."CitationsByYear"->>%s)::int, 0)), 0) '
                    'FROM "Researcher" r WHERE r."CitationsByYear" IS NOT NULL'
                )
                cit_params = [str(year)]
                if scoped:
                    cit_sql += (
                        ' AND EXISTS (SELECT 1 FROM "Works_In" w '
                        '             WHERE w."UserID" = r."UserID" '
                        '               AND w."IsCurrentPosition" = TRUE '
                        '               AND w."DepartmentID" = %s)'
                    )
                    cit_params.append(hod_dept_id)
                cur.execute(cit_sql, cit_params)
                c = cur.fetchone()[0]
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
                # Citations come from Researcher.CitationsByYear[year] —
                # same source as the dashboard. Computed in a separate
                # subquery so it's not multiplied by the papers JOIN.
                dept_sql = (
                    'WITH dept_cites AS ('
                    '    SELECT w."DepartmentID", '
                    '           SUM(COALESCE((r."CitationsByYear"->>%s)::int, 0)) AS cites '
                    '    FROM "Works_In" w '
                    '    JOIN "Researcher" r ON r."UserID" = w."UserID" '
                    '    WHERE w."IsCurrentPosition" = TRUE AND r."CitationsByYear" IS NOT NULL '
                    '    GROUP BY w."DepartmentID"'
                    ') '
                    'SELECT '
                    '    d."DepartmentName", '
                    '    COUNT(DISTINCT u."UserID"), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE j."VenueType" = \'Journal\'), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE j."VenueType" = \'Conference\'), '
                    '    COUNT(DISTINCT rp."PaperID"), '
                    '    COALESCE(MAX(dc.cites), 0) AS citations, '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = \'Q1\'), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = \'Q2\'), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = \'Q3\'), '
                    '    COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = \'Q4\') '
                    'FROM "Department" d '
                    'LEFT JOIN "Works_In" w ON w."DepartmentID" = d."DepartmentID" AND w."IsCurrentPosition" = TRUE '
                    'LEFT JOIN "Users" u ON u."UserID" = w."UserID" AND u."UserType" = \'Researcher\' '
                    'LEFT JOIN "Authors" a ON a."UserID" = u."UserID" '
                    'LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID" AND rp."PubYear" = %s '
                    'LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID" '
                    'LEFT JOIN LATERAL (SELECT "Quartile", "ImpactFactor" FROM "JournalRankings" '
                    '  WHERE "JournalID" = rp."JournalID" ORDER BY "RankingYear" DESC NULLS LAST, "Source" LIMIT 1) jr ON TRUE '
                    'LEFT JOIN dept_cites dc ON dc."DepartmentID" = d."DepartmentID" '
                )
                dept_params = [str(year), year]
                if scoped:
                    dept_sql += 'WHERE d."DepartmentID" = %s '
                    dept_params.append(hod_dept_id)
                dept_sql += 'GROUP BY d."DepartmentName" ORDER BY 5 DESC'
                cur.execute(dept_sql, dept_params)
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
            researchers_sql = '''
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
                {dept_filter}
                GROUP BY u."UserID", u."FullName_Ar", d."DepartmentName",
                         r."AcademicRank", hi.h_index, u."Scholar_ID",
                         r."ORCID_ID", r."OpenAlex_AuthorID", r."Scopus_ID",
                         r."LastSyncedAt"
                ORDER BY papers_window DESC, h_index DESC
            '''
            researchers_params = {'years': years}
            if scoped:
                researchers_sql = researchers_sql.format(
                    dept_filter='AND w."DepartmentID" = %(dept)s')
                researchers_params['dept'] = hod_dept_id
            else:
                researchers_sql = researchers_sql.format(dept_filter='')
            cur.execute(researchers_sql, researchers_params)
            for row in cur.fetchall():
                ws.append(list(row))
        set_widths(ws, [32, 22, 18, 12, 14, 14, 16, 14, 14, 18, 22])

    if 'journals' in sheets:
        for year in sorted(years):
            ws = wb.create_sheet(f'Journals {year}')
            # Columns:
            #   • Department, Title, Abstract
            #   • Two author columns:
            #       1. Al-Baha researchers only (Arabic names)
            #       2. All authors combined (Arabic + foreign, no affiliations)
            #   • Journal, Quartile, IF, Indexing, Citations, DOI
            ws.append([
                'Department', 'Title', 'Abstract',
                'Al-Baha Researchers', 'All Authors (raw)',
                'Journal', 'Quartile', 'IF', 'Indexing',
                'Citations', 'DOI'
            ])
            style_header(ws, 11)
            with connection.cursor() as cur:
                # LEFT JOIN ResearchPaper to attach Abstract.
                # v_paper_details doesn't expose paper_id, so we match
                # by DOI when available (canonical identifier) and fall
                # back to a case-insensitive title match for papers
                # without a DOI on either side.
                journals_sql = (
                    'SELECT '
                    '    v.department_name, v.title, '
                    '    rp."Abstract" AS abstract, '
                    '    v.authors_ar, v.all_authors_en, '
                    '    v.journal_name, v.quartile, v.impact_factor, v.indexing, '
                    '    v.citations, v.doi '
                    'FROM v_paper_details v '
                    'LEFT JOIN "ResearchPaper" rp ON '
                    '    (v.doi IS NOT NULL AND LOWER(rp."DOI") = LOWER(v.doi)) '
                    '    OR ((v.doi IS NULL OR rp."DOI" IS NULL) AND LOWER(rp."Title") = LOWER(v.title)) '
                    'WHERE v.pub_year = %s AND v.venue_type = \'Journal\''
                )
                journals_params = [year]
                if scoped:
                    journals_sql += ' AND v.department_id = %s'
                    journals_params.append(hod_dept_id)
                journals_sql += ' ORDER BY v.department_name, v.citations DESC NULLS LAST'
                cur.execute(journals_sql, journals_params)
                for row in cur.fetchall():
                    ws.append(list(row))
            set_widths(ws, [22, 60, 80, 50, 50, 30, 10, 8, 12, 10, 30])
            wrap_column(ws, 3)  # Abstract column

    if 'conferences' in sheets:
        for year in sorted(years):
            ws = wb.create_sheet(f'Conferences {year}')
            ws.append([
                'Department', 'Title', 'Abstract',
                'Al-Baha Researchers', 'All Authors (raw)',
                'Conference', 'Indexing', 'Citations', 'DOI'
            ])
            style_header(ws, 9)
            with connection.cursor() as cur:
                conf_sql = (
                    'SELECT '
                    '    v.department_name, v.title, '
                    '    rp."Abstract" AS abstract, '
                    '    v.authors_ar, v.all_authors_en, '
                    '    v.journal_name, v.indexing, v.citations, v.doi '
                    'FROM v_paper_details v '
                    'LEFT JOIN "ResearchPaper" rp ON '
                    '    (v.doi IS NOT NULL AND LOWER(rp."DOI") = LOWER(v.doi)) '
                    '    OR ((v.doi IS NULL OR rp."DOI" IS NULL) AND LOWER(rp."Title") = LOWER(v.title)) '
                    'WHERE v.pub_year = %s AND v.venue_type = \'Conference\''
                )
                conf_params = [year]
                if scoped:
                    conf_sql += ' AND v.department_id = %s'
                    conf_params.append(hod_dept_id)
                conf_sql += ' ORDER BY v.department_name, v.citations DESC NULLS LAST'
                cur.execute(conf_sql, conf_params)
                for row in cur.fetchall():
                    ws.append(list(row))
            set_widths(ws, [22, 60, 80, 50, 50, 30, 12, 10, 30])
            wrap_column(ws, 3)  # Abstract column

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

    @decorators.action(detail=False, methods=['get'])
    def all(self, request):
        """
        GET /api/researchers/all/
        Returns ALL researchers as a flat list — for the sidebar listing.
        Lightweight: name + department + paper count only.
        """
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute('''
                SELECT
                    u."UserID",
                    u."Litrix_ID",
                    u."FullName_Ar",
                    TRIM(CONCAT_WS(' ', u."FirstName", u."LastName")) AS full_name_en,
                    d."DepartmentName",
                    (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = u."UserID") AS papers
                FROM "Users" u
                LEFT JOIN "Works_In" w ON w."UserID" = u."UserID" AND w."IsCurrentPosition" = TRUE
                LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
                WHERE u."UserType" = 'Researcher'
                ORDER BY papers DESC NULLS LAST, u."FullName_Ar"
            ''')
            results = [
                {
                    'user_id': r[0],
                    'litrix_id': r[1],
                    'full_name_ar': r[2],
                    'full_name_en': r[3],
                    'department_name': r[4],
                    'papers': r[5],
                }
                for r in cur.fetchall()
            ]
        return response.Response({'results': results})

    @decorators.action(detail=False, methods=['get'])
    def search(self, request):
        """
        GET /api/researchers/search/?q=محمد
        Lightweight search — hits Users table directly. No aggregation.
        Returns up to 15 results in <100ms.
        """
        q = (request.query_params.get('q') or '').strip()
        if not q or len(q) < 2:
            return response.Response({'results': []})

        from django.db import connection
        with connection.cursor() as cur:
            like = f'%{q}%'
            cur.execute('''
                SELECT
                    u."UserID",
                    u."Litrix_ID",
                    u."FullName_Ar",
                    TRIM(CONCAT_WS(' ', u."FirstName", u."LastName")) AS full_name_en,
                    d."DepartmentName",
                    u."Scholar_ID"
                FROM "Users" u
                LEFT JOIN "Works_In" w ON w."UserID" = u."UserID" AND w."IsCurrentPosition" = TRUE
                LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
                WHERE u."UserType" = 'Researcher'
                  AND (
                       u."FullName_Ar" ILIKE %s
                    OR u."FirstName" ILIKE %s
                    OR u."LastName" ILIKE %s
                  )
                ORDER BY u."FullName_Ar"
                LIMIT 15
            ''', [like, like, like])
            results = [
                {
                    'user_id': r[0],
                    'litrix_id': r[1],
                    'full_name_ar': r[2],
                    'full_name_en': r[3],
                    'department_name': r[4],
                    'scholar_id': r[5],
                }
                for r in cur.fetchall()
            ]
        return response.Response({'results': results})

    @decorators.action(detail=True, methods=['get'])
    def profile(self, request, pk=None):
        """
        GET /api/researchers/{id}/profile/

        `id` accepts either an internal numeric UserID or the public
        Lit-NNNNNN identifier. We resolve Litrix_ID → UserID at the
        boundary so the rest of the SQL can keep using the integer PK
        (cheaper joins, established indices). Why allow both?
          • Backward compat with any tooling that stored numeric IDs.
          • The frontend now drives all profile URLs with Litrix_ID.

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

        # Resolve the public identifier → internal UserID at the boundary.
        # Two paths:
        #   1. Litrix-ID (case-insensitive, any digit length) → normalize
        #      to canonical Lit-NNNNNN, then look up.
        #   2. Pure numeric → treat as UserID directly.
        # Any malformed input returns 400 instead of leaking a 500 from
        # a Postgres type error.
        canonical = normalize_litrix_id(pk)
        if canonical is not None:
            # Match the numeric core regardless of case or zero-padding
            # in the stored value. This way "LIT-0001", "Lit-000001",
            # and "lit-1" all resolve to the same user.
            seq = int(LITRIX_ID_PATTERN.match(pk.strip()).group(1))
            with connection.cursor() as cur:
                cur.execute(
                    '''
                    SELECT "UserID" FROM "Users"
                    WHERE "Litrix_ID" IS NOT NULL
                      AND "Litrix_ID" ~* '^lit-[0-9]+$'
                      AND CAST(SUBSTRING("Litrix_ID" FROM 5) AS INTEGER) = %s
                    LIMIT 1
                    ''',
                    [seq],
                )
                row = cur.fetchone()
                if not row:
                    return response.Response(
                        {'error': f'Researcher {canonical} not found'},
                        status=404,
                    )
                resolved_user_id = row[0]
        else:
            try:
                resolved_user_id = int(pk)
            except (TypeError, ValueError):
                return response.Response(
                    {'error': 'Invalid researcher ID'}, status=400
                )

        with connection.cursor() as cur:
            cur.execute('''
                SELECT
                    u."UserID", u."FullName_Ar",
                    u."FirstName", u."LastName", u."Email",
                    u."Scholar_ID", u."ORCID",
                    r."OpenAlex_AuthorID", r."LastSyncedAt",
                    d."DepartmentID", d."DepartmentName",
                    u."Litrix_ID"
                FROM "Users" u
                LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
                LEFT JOIN "Works_In" w ON w."UserID" = u."UserID"
                                       AND w."IsCurrentPosition" = TRUE
                LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
                WHERE u."UserID" = %s
            ''', [resolved_user_id])
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
                'litrix_id':         row[11],
            }

            # Aggregated stats. For citations, sum the per-paper Scholar
            # cited_by.value (most accurate per-paper signal), falling
            # back to OpenAlex's cumulative count.
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
                LEFT JOIN LATERAL (    SELECT "Quartile", "ImpactFactor"    FROM "JournalRankings"    WHERE "JournalID" = rp."JournalID"    ORDER BY "RankingYear" DESC NULLS LAST, "Source"    LIMIT 1) jr ON TRUE
                WHERE a."UserID" = %s
            ''', [resolved_user_id])
            stats_row = cur.fetchone()

            # If we have author-level per-year data from Scholar, prefer
            # its TOTAL (Scholar's own count) over per-paper aggregation.
            try:
                cur.execute(
                    'SELECT "CitationsByYear" FROM "Researcher" WHERE "UserID" = %s',
                    [resolved_user_id],
                )
                rcby = cur.fetchone()
                if rcby and rcby[0]:
                    raw = rcby[0]
                    if isinstance(raw, str):
                        import json as _json
                        try:
                            raw = _json.loads(raw)
                        except Exception:
                            raw = {}
                    scholar_total = sum(
                        int(v) for v in (raw or {}).values()
                        if str(v).isdigit() or isinstance(v, int)
                    )
                    if scholar_total > 0:
                        # Override with Scholar's authoritative total
                        stats_row = (
                            stats_row[0],
                            scholar_total,
                            stats_row[2],
                            stats_row[3],
                            stats_row[4],
                        )
            except Exception:
                pass
            stats = {
                'total_papers':    stats_row[0],
                'total_citations': stats_row[1],
                'q1_papers':       stats_row[2],
                'scopus_papers':   stats_row[3],
                'isi_papers':      stats_row[4],
            }

            # Per-year citations: PREFER Researcher.CitationsByYear (the
            # author-level data straight from Scholar's cited_by.graph,
            # which is what Scholar itself displays in the profile).
            # Fall back to summing per-paper CitationsByYear if not set.
            citations_by_year = []
            try:
                cur.execute('''
                    SELECT "CitationsByYear" FROM "Researcher"
                    WHERE "UserID" = %s
                ''', [resolved_user_id])
                rcby = cur.fetchone()
                if rcby and rcby[0]:
                    raw = rcby[0]
                    if isinstance(raw, str):
                        import json as _json
                        try:
                            raw = _json.loads(raw)
                        except Exception:
                            raw = {}
                    citations_by_year = sorted(
                        ({'year': int(y), 'citations': int(v)}
                         for y, v in (raw or {}).items()
                         if str(y).isdigit()),
                        key=lambda x: x['year'],
                    )
                else:
                    cur.execute('''
                        SELECT
                            yr.year::int AS year,
                            SUM(CASE WHEN yr.cnt ~ '^[0-9]+$'
                                     THEN yr.cnt::int
                                     ELSE 0 END) AS citations
                        FROM "ResearchPaper" rp
                        JOIN "Authors" a ON a."PaperID" = rp."PaperID"
                        CROSS JOIN LATERAL jsonb_each_text(
                            COALESCE(rp."CitationsByYear", '{}'::jsonb)
                        ) AS yr(year, cnt)
                        WHERE a."UserID" = %s
                          AND yr.year ~ '^[0-9]+$'
                        GROUP BY yr.year
                        ORDER BY yr.year
                    ''', [resolved_user_id])
                    citations_by_year = [
                        {'year': r[0], 'citations': r[1]}
                        for r in cur.fetchall()
                    ]

                # Clip to CHART_YEAR_FLOOR (2019) to match the admin chart
                # window. Years before 2019 stretch the X-axis and the
                # recent-growth signal gets lost. Same logic as the
                # admin yearly_breakdown view (see line ~1353).
                citations_by_year = [
                    pt for pt in citations_by_year
                    if pt['year'] >= CHART_YEAR_FLOOR
                ]
            except Exception:
                citations_by_year = []

            # Papers list with full metadata.
            # Journal name falls back to Scholar's free-text "publication"
            # string (like "Sustainability 14 (2), 829, 2022") when we
            # don't have a JournalID linked.
            cur.execute('''
                SELECT
                    rp."PaperID", rp."Title", rp."DOI", rp."PubYear",
                    rp."Source", rp."Indexing", rp."CitationsByYear",
                    COALESCE(
                        ("RawData_Log"->'cited_by'->>'value')::int,
                        ("RawData_Log"->>'cited_by_count')::int,
                        0
                    )                       AS citations,
                    COALESCE(j."JournalName", rp."RawData_Log"->>'publication')
                                            AS journal_name,
                    j."ISSN_Print",
                    j."VenueType",
                    jr."Quartile",
                    jr."ImpactFactor"
                FROM "ResearchPaper" rp
                JOIN "Authors" a ON a."PaperID" = rp."PaperID"
                LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
                LEFT JOIN LATERAL (    SELECT "Quartile", "ImpactFactor"    FROM "JournalRankings"    WHERE "JournalID" = rp."JournalID"    ORDER BY "RankingYear" DESC NULLS LAST, "Source"    LIMIT 1) jr ON TRUE
                WHERE a."UserID" = %s
                ORDER BY rp."PubYear" DESC NULLS LAST, rp."PaperID" DESC
            ''', [resolved_user_id])
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


def _hod_scope_department_id(request):
    """
    The single department a HoD-scoped user is limited to, or None for
    institution-wide roles (Admin/Dean see every department).

    A HoD is anyone with `view_dept_researchers` but NOT
    `view_all_researchers`. Their department is resolved two ways, in
    order: the canonical `Department.HeadID`, then their current
    `Works_In` position — invitation-provisioned HoDs are linked via
    `Works_In`, not `HeadID`. Returns the sentinel -1 when the user IS
    HoD-scoped but has no department at all, so callers scope to nothing
    instead of accidentally leaking every department.
    """
    u = getattr(request, 'user', None)
    if not (u and u.is_authenticated):
        return None
    if not u.has_litrix_perm('view_dept_researchers'):
        return None
    if u.has_litrix_perm('view_all_researchers'):
        return None

    from django.db import connection
    with connection.cursor() as cur:
        cur.execute(
            'SELECT "DepartmentID" FROM "Department" WHERE "HeadID" = %s LIMIT 1',
            [u.user_id],
        )
        r = cur.fetchone()
        if not r:
            cur.execute(
                'SELECT "DepartmentID" FROM "Works_In" '
                'WHERE "UserID" = %s AND "IsCurrentPosition" = TRUE '
                'ORDER BY "StartDate" DESC LIMIT 1',
                [u.user_id],
            )
            r = cur.fetchone()
    return r[0] if r else -1


class DepartmentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/departments/         → list with aggregated stats
    GET /api/departments/{id}/    → single department detail

    HoDs are scoped to their own department; Admins/Deans see all.
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
    Read the year filter from the request, supporting:
      • single year   — ?year=2025
      • multiple      — ?year=2024,2025,2026  (or ?years=...)
      • all (default) — no param given → uses FOCUS_YEARS
    Anything non-numeric inside the comma list is silently ignored so a
    stray comma or trailing space won't 500 the dashboard.
    """
    raw = (
        request.query_params.get('year')
        or request.query_params.get('years')
        or ''
    )
    if raw:
        years = []
        for part in raw.split(','):
            part = part.strip()
            if part.isdigit():
                years.append(int(part))
        if years:
            return years
    return list(FOCUS_YEARS)


@decorators.api_view(['GET'])
def overview(request):
    """
    GET /api/stats/overview/         → both focus years
    GET /api/stats/overview/?year=2025  → just 2025
    GET /api/stats/overview/?year=2026  → just 2026

    A one-shot payload that powers the Admin/Dean/HoD landing page.
    HoDs are auto-scoped to their own department (detected via
    Department.HeadID = current user).
    """
    years = _resolve_years(request)

    # --- HoD scoping ---
    # Admin/Dean keep the full institution view (perm: view_all_researchers).
    # HoD has perm view_dept_researchers but NOT view_all_researchers.
    # Resolve via the shared helper so detection matches the rest of the
    # app (Department.HeadID, then current Works_In — invite-provisioned
    # HoDs are linked via Works_In). Returns None for institution-wide
    # roles, a dept id for a HoD, or -1 when a HoD has no department — the
    # -1 sentinel scopes every query below to nothing instead of leaking
    # the whole institution.
    hod_dept_id = _hod_scope_department_id(request)

    # avg_h keeps the historical definition (average of dept averages).
    _dept_qs = DepartmentStats.objects.all()
    if hod_dept_id:
        _dept_qs = _dept_qs.filter(department_id=hod_dept_id)
    dept_agg = _dept_qs.aggregate(avg_h=Avg('avg_h_index'))

    # Researcher head-count: COUNT(DISTINCT UserID) over current positions.
    # The previous Sum('total_researchers') across DepartmentStats rows
    # double-counted any researcher holding positions in TWO departments
    # (they appear once in each department's row).
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
        # Papers count: papers PUBLISHED in window (sets the denominator
        # for "publications" KPI).
        # KPI papers + Q1/Scopus/ISI counts. Author-in-dept clause added
        # at the end for HoDs only.
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
        cur.execute(kpi_sql, kpi_params)
        paper_count_row = cur.fetchone()

        # Citations: SUM Researcher.CitationsByYear for the requested years.
        # This is Scholar's authoritative per-year graph at the AUTHOR level
        # (no per-paper backfill needed). Slight over-count risk on co-authored
        # papers between our own researchers, but acceptable given Scholar's
        # native granularity and the cost of per-paper SerpAPI fetches.
        year_keys_expr = ' + '.join([
            f"COALESCE((r.\"CitationsByYear\"->>%s)::int, 0)"
            for _ in years
        ])
        # HoD-scoped: restrict the citations sum to researchers whose
        # current Works_In matches the HoD's department. For Admin/Dean,
        # the filter is bypassed (%s IS NULL).
        cur.execute(f'''
            SELECT COALESCE(SUM({year_keys_expr}), 0) AS citations
            FROM "Researcher" r
            WHERE r."CitationsByYear" IS NOT NULL
              AND (%s::int IS NULL OR EXISTS (
                    SELECT 1 FROM "Works_In" w_cit
                    WHERE w_cit."UserID" = r."UserID"
                      AND w_cit."DepartmentID" = %s::int
                      AND w_cit."IsCurrentPosition" = TRUE
              ))
        ''', [str(y) for y in years] + [hod_dept_id, hod_dept_id])
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
              AND (%s::int IS NULL OR w."DepartmentID" = %s::int)
            ORDER BY focus_papers DESC, focus_citations DESC
            LIMIT 5
        ''', [years] + [str(y) for y in years] + [hod_dept_id, hod_dept_id])
        top_researchers_rows = cur.fetchall()

        # Top papers — for a HoD, restrict to papers authored by someone
        # currently in their department (otherwise this list leaks the
        # institution's most-cited papers onto a department dashboard).
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
        top_papers_sql += ' ORDER BY citations DESC NULLS LAST LIMIT 5'
        cur.execute(top_papers_sql, top_papers_params)
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
            LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID" AND rp."PubYear" = ANY(%s)
            LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
            LEFT JOIN LATERAL (    SELECT "Quartile", "ImpactFactor"    FROM "JournalRankings"    WHERE "JournalID" = rp."JournalID"    ORDER BY "RankingYear" DESC NULLS LAST, "Source"    LIMIT 1) jr ON TRUE
            LEFT JOIN dept_citations dc ON dc."DepartmentID" = d."DepartmentID"
            GROUP BY d."DepartmentID", d."DepartmentName", d."CollegeID"
            HAVING (%s::int IS NULL OR d."DepartmentID" = %s::int)
            ORDER BY total_papers DESC NULLS LAST
        ''', [str(y) for y in years] + [years] + [hod_dept_id, hod_dept_id])
        dept_cols = [c[0] for c in cur.description]
        departments = [dict(zip(dept_cols, row)) for row in cur.fetchall()]

        # ----------------------------------------------------------------
        # Time-series chart window.
        #
        # The KPI strip and per-department table use `years` (the full
        # 2011-present floor — every paper that's ever been written by
        # an Al-Baha researcher counts). The trend chart is different:
        # rendering 16 sparse data points crushes the recent-growth
        # signal. We clip to CHART_YEAR_FLOOR (2019) for that surface.
        #
        # If the caller explicitly filtered to a narrower window via
        # ?year=, honor it — the intersection is just whatever overlap
        # exists. Empty intersection falls back to `years` so we never
        # send back an empty chart.
        # ----------------------------------------------------------------
        chart_years = [y for y in years if y >= CHART_YEAR_FLOOR] or years

        # Per-year breakdown per department.
        # Papers: published count per (dept, year) — DISTINCT to avoid
        # double-counting papers shared across multiple co-authored faculty.
        # Citations: sum of Researcher.CitationsByYear[year] for each
        # researcher in the dept (Scholar's authoritative author-level graph).
        cur.execute('''
            SELECT w."DepartmentID", rp."PubYear",
                   COUNT(DISTINCT rp."PaperID") AS papers
            FROM "Works_In" w
            JOIN "Authors" a ON a."UserID" = w."UserID"
            JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
            WHERE w."IsCurrentPosition" = TRUE
              AND rp."PubYear" = ANY(%s)
              AND (%s::int IS NULL OR w."DepartmentID" = %s::int)
            GROUP BY w."DepartmentID", rp."PubYear"
        ''', [chart_years, hod_dept_id, hod_dept_id])
        papers_by_dept_year = {}
        for did, yr, n in cur.fetchall():
            papers_by_dept_year.setdefault(did, {})[int(yr)] = int(n)

        cur.execute('''
            SELECT w."DepartmentID",
                   year_kv.key::int AS year,
                   SUM((year_kv.value)::int) AS citations
            FROM "Works_In" w
            JOIN "Researcher" r ON r."UserID" = w."UserID"
            CROSS JOIN LATERAL jsonb_each_text(
                COALESCE(r."CitationsByYear", '{}'::jsonb)
            ) AS year_kv
            WHERE w."IsCurrentPosition" = TRUE
              AND year_kv.value ~ '^[0-9]+$'
              AND year_kv.key::int = ANY(%s)
              AND (%s::int IS NULL OR w."DepartmentID" = %s::int)
            GROUP BY w."DepartmentID", year_kv.key::int
        ''', [chart_years, hod_dept_id, hod_dept_id])
        cites_by_dept_year = {}
        for did, yr, n in cur.fetchall():
            cites_by_dept_year.setdefault(did, {})[int(yr)] = int(n)

        # Inject by_year into each department row — uses chart_years
        # so the chart axis is reasonable, even when the KPIs span 2011+.
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
        # Separate axis-year list for any frontend chart so it doesn't
        # have to re-derive the window from the by_year length.
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


# ============================================================================
# Universal Search — Spotlight-style global search.
# ============================================================================
# Returns a unified payload: { profiles: [...], papers: [...] }.
#
# Permission gate (the key business rule):
#   • view_all_researchers / view_dept_researchers (Admin/Dean/HoD) →
#     full corpus, including papers whose authors are external (not
#     registered Users). They need this for institutional oversight.
#   • Otherwise (Researcher) → restrict to papers that have AT LEAST ONE
#     author who is a registered system User. Researchers shouldn't be
#     surfacing papers from authors outside the institution they don't
#     have a relationship with.
#
# Both sides cap result count to keep the modal snappy.
# ============================================================================
@decorators.api_view(['GET'])
def universal_search(request):
    q = (request.query_params.get('q') or '').strip()
    if len(q) < 2:
        return response.Response({'profiles': [], 'papers': []})

    # has_litrix_perm is on accounts.User; safe even when SimpleJWT
    # hands us an AnonymousUser-like object — we just default to False.
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

    # --------------------------------------------------------------------
    # Profile search — registered system Users matching the query.
    # Researchers see only Researcher-type profiles; full-access roles
    # see every UserType (so an Admin can find a Dean by name, etc).
    # --------------------------------------------------------------------
    # All roles see all UserTypes - the search is intentionally
    # unrestricted now (was: Researchers saw only Researcher-type).
    user_type_filter = ''

    with connection.cursor() as cur:
        # ----------------------------------------------------------------
        # Profile search — cross-script aware.
        #
        # We match against four signals so an English query can still
        # find an Arabic-only profile (and vice versa):
        #
        #   1. Users.FullName_Ar       — Arabic side
        #   2. Users.FirstName / LastName — English side, if registered
        #   3. Users.Email / Litrix_ID — exact-ish identifiers
        #   4. Authors.AuthorNameRaw   — bridge: scrapers (Scholar /
        #      OpenAlex) populate this with the English-script author
        #      string for every paper, so even if the user only has
        #      FullName_Ar locally, their English transliteration lives
        #      here and we surface it through this EXISTS subquery.
        #
        # No transliteration heuristics — we lean on real data captured
        # from the academic sources of truth.
        # ----------------------------------------------------------------
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

        # ----------------------------------------------------------------
        # Paper search — title match, with the permission gate baked in.
        # The EXISTS subquery enforces "at least one system author" for
        # restricted users; the full-access path skips it entirely.
        # ----------------------------------------------------------------
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
