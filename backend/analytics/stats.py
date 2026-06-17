"""Shared stats logic for the dashboard and reports.

Holds the year window, HoD scoping, the affiliation-filter parse, the canonical
citation/affiliation SQL fragments, and the per-department / per-researcher
windowed aggregates. The views and the Excel export both import from here so
their numbers can't drift apart.
"""

# YEAR_FLOOR is the institutional "all-time" floor: the College of Computing
# was founded in 2011, so cumulative KPIs (totals strip, per-dept tables) start
# there. CHART_YEAR_FLOOR (2019) is the trend-chart floor — stretching the
# yearly charts back to 2011 just gives a sparse X-axis and buries recent
# growth. Both upper bounds slide forward with the current year.
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


def _resolve_years(request) -> list:
    """Read the year filter from ?year= / ?years= — one year, a comma list, or
    nothing (defaults to FOCUS_YEARS). Non-numeric entries in the list are
    dropped so a stray comma or trailing space won't 500 the dashboard.
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


def _hod_scope_department_id(request):
    """The one department a HoD-scoped user is limited to, or None for
    institution-wide roles (Admin/Dean).

    A HoD has `view_dept_researchers` but not `view_all_researchers`. We resolve
    their department from `Department.HeadID` first, then their current
    `Works_In` position — invite-provisioned HoDs are linked via Works_In, not
    HeadID. Returns the sentinel -1 when a HoD has no department at all, so
    callers scope to nothing rather than leaking every department.
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


def _albaha_only(request):
    """True when the caller asked to exclude papers confirmed authored under a
    non-Al-Baha affiliation (?affiliation=albaha). Mirrors the overview parse."""
    qp = getattr(request, 'query_params', None) or getattr(request, 'GET', {})
    v = (qp.get('affiliation') or '').strip().lower()
    return v in ('albaha', 'al-baha', 'verified', 'true', '1')


# Single source of truth for two SQL fragments every dashboard/report surface
# needs: the per-paper "citations received in a set of years" sum and the
# Al-Baha affiliation filter. These were copy-pasted across overview(), the
# /departments helpers, and export_excel(), which is exactly how those surfaces
# drifted apart before — change the rule here and it propagates everywhere.
# `alias` is the ResearchPaper alias in the target query (rp, rp_all, rp2,
# rp_w, ...). `_cites_expr` emits one %s per year; the caller appends
# [str(y) for y in years] to its params where the expr appears.

def _cites_expr(alias, years):
    """Per-paper citations RECEIVED in `years` (sum of CitationsByYear keys)."""
    return ' + '.join(
        f'COALESCE(({alias}."CitationsByYear"->>%s)::int, 0)' for _ in years
    ) or '0'


def _affil_clause(albaha_only, alias):
    """SQL fragment dropping papers confirmed authored elsewhere, or '' when
    the filter is off. AffiliationVerified IS DISTINCT FROM FALSE keeps TRUE +
    not-yet-verified NULL, excludes only confirmed-elsewhere FALSE."""
    if not albaha_only:
        return ''
    return f' AND ({alias}."AffiliationVerified" IS DISTINCT FROM FALSE)'


# This-period (FOCUS_YEARS) per-paper stats for the /departments page.
# v_department_stats / v_researcher_stats are all-time + lifetime citations,
# which disagreed with the overview dashboard. Rather than rewrite those shared
# views (a researcher's lifetime total is legitimately different elsewhere), we
# recompute the period-scoped numbers here with the overview's definition and
# override them onto the serialized rows.
def _dept_cards_windowed(years, albaha_only=False):
    """Returns {department_id: {windowed paper/citation fields}} matching the
    overview dashboard's departments block. Citations are deduped per
    (department, paper) so a paper co-authored within a department counts once.
    When albaha_only, papers confirmed authored elsewhere (AffiliationVerified
    = FALSE) are dropped — same predicate the overview uses."""
    from django.db import connection
    year_strs = [str(y) for y in years]
    yk = _cites_expr('rp_all', years)
    affil_rp    = _affil_clause(albaha_only, 'rp')
    affil_rpall = _affil_clause(albaha_only, 'rp_all')
    sql = f'''
        WITH dept_citations AS (
            SELECT dept AS did, SUM(cites) AS total_citations FROM (
                SELECT DISTINCT w."DepartmentID" AS dept, rp_all."PaperID" AS pid,
                       ({yk}) AS cites
                FROM "Works_In" w
                JOIN "Authors" a ON a."UserID" = w."UserID"
                JOIN "ResearchPaper" rp_all ON rp_all."PaperID" = a."PaperID"
                WHERE w."IsCurrentPosition" = TRUE{affil_rpall}
            ) ded GROUP BY dept
        )
        SELECT d."DepartmentID" AS department_id,
            COUNT(DISTINCT rp."PaperID") AS total_papers,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q1') AS total_q1_papers,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q2') AS total_q2_papers,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q3') AS total_q3_papers,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q4') AS total_q4_papers,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = 'Scopus' OR jr."Quartile" IS NOT NULL) AS total_scopus_papers,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = 'ISI') AS total_isi_papers,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE COALESCE(rp."VenueType", j."VenueType") ILIKE 'Conference%%') AS conference_papers,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."PaperID" IS NOT NULL
                AND (COALESCE(rp."VenueType", j."VenueType") IS NULL OR COALESCE(rp."VenueType", j."VenueType") NOT ILIKE 'Conference%%')) AS journal_papers,
            COALESCE(MAX(dc.total_citations), 0) AS total_citations
        FROM "Department" d
        LEFT JOIN "Works_In" w ON w."DepartmentID" = d."DepartmentID" AND w."IsCurrentPosition" = TRUE
        LEFT JOIN "Users" u ON u."UserID" = w."UserID" AND u."UserType" = 'Researcher'
        LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
        LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID" AND rp."PubYear" = ANY(%s){affil_rp}
        LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        LEFT JOIN LATERAL (SELECT "Quartile" FROM "JournalRankings"
            WHERE "JournalID" = rp."JournalID"
            ORDER BY "RankingYear" DESC NULLS LAST, "Source" LIMIT 1) jr ON TRUE
        LEFT JOIN dept_citations dc ON dc.did = d."DepartmentID"
        GROUP BY d."DepartmentID"
    '''
    out = {}
    with connection.cursor() as cur:
        cur.execute(sql, year_strs + [years])
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            rec = dict(zip(cols, row))
            out[rec.pop('department_id')] = {k: int(v or 0) for k, v in rec.items()}
    return out


def _researcher_rows_windowed(years, user_ids, albaha_only=False):
    """Returns {user_id: {total_papers, q1_papers, total_citations}} for the
    given researchers, this-period per-paper (papers PUBLISHED in window;
    citations RECEIVED in window across all their papers — matching overview).
    When albaha_only, papers confirmed authored elsewhere are dropped."""
    from django.db import connection
    if not user_ids:
        return {}
    year_strs = [str(y) for y in years]
    yk = _cites_expr('rp', years)
    affil = _affil_clause(albaha_only, 'rp')
    sql = f'''
        SELECT a."UserID" AS uid,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."PubYear" = ANY(%s)) AS total_papers,
            COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."PubYear" = ANY(%s) AND jr."Quartile" = 'Q1') AS q1_papers,
            COALESCE(SUM({yk}), 0) AS total_citations
        FROM "Authors" a
        JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
        LEFT JOIN LATERAL (SELECT "Quartile" FROM "JournalRankings"
            WHERE "JournalID" = rp."JournalID"
            ORDER BY "RankingYear" DESC NULLS LAST, "Source" LIMIT 1) jr ON TRUE
        WHERE a."UserID" = ANY(%s){affil}
        GROUP BY a."UserID"
    '''
    out = {}
    with connection.cursor() as cur:
        cur.execute(sql, [years, years] + year_strs + [list(user_ids)])
        for uid, papers, q1, cites in cur.fetchall():
            out[uid] = {
                'total_papers':    int(papers or 0),
                'q1_papers':       int(q1 or 0),
                'total_citations': int(cites or 0),
            }
    return out
