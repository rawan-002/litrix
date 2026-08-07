"""Read-only data functions the Litrix AI chat can call as "tools" (function
calling). Each one wraps a real, direct SQL query against the same tables and
policy the rest of the dashboard uses - the chat never invents numbers, it
only ever reports what one of these functions actually returned.

Scoped to the same default as every OFFICIAL KPI in the app (see stats.py's
policy table): AffiliationVerified = TRUE only (verified_affil_clause), not
the wider active/all set. A chat answer should be exactly as defensible as
the dashboard number it's describing.

Every function returns a plain JSON-serializable dict - that's what gets
serialized back to the model as the tool result.
"""
import json
import re

from django.db import connection

from .stats import _cites_expr, _default_focus_years, verified_affil_clause

AAV = verified_affil_clause(True, 'rp')  # AffiliationVerified = TRUE, fixed on for chat

_TITLE_PREFIX = re.compile(r'^(dr\.?|prof\.?|professor|mr\.?|mrs\.?|ms\.?)\s+', re.I)


def _dept_author_exists_clause(alias, department_id, params):
    """EXISTS clause restricting `alias`'s paper to one department's current
    authors. Appends its own param(s) to `params` in place and returns the
    SQL fragment - caller inlines it right after the existing Authors-exists
    check. `department_id` is NEVER model-supplied - see ai_views.py's
    scope resolution; this is the backend-enforced HoD department fence."""
    if not department_id:
        return ''
    params.append(department_id)
    return (
        f' AND EXISTS (SELECT 1 FROM "Authors" a3 '
        f'JOIN "Works_In" w3 ON w3."UserID" = a3."UserID" AND w3."IsCurrentPosition" = TRUE '
        f'WHERE a3."PaperID" = {alias}."PaperID" AND w3."DepartmentID" = %s)'
    )


def get_overview_stats(years=None, department_id=None):
    """Institution-wide totals: papers, citations, Q1-4, Scopus/ISI, venue
    split. `years` defaults to the full institutional window (2011-current).

    Two separate queries by design, matching views.py::overview() exactly:
    paper counts are papers PUBLISHED in `years`, but citations are citations
    RECEIVED in `years` summed across ALL papers regardless of when they were
    published (an old paper cited this year still counts this year). Merging
    these into one PubYear-filtered query would quietly under-count citations.

    `department_id` is a backend-only scope fence (never in the tool's
    model-facing schema) - see ai_views.py's per-role scope resolution.
    """
    years = years or _default_focus_years()
    jelig = (' AND (rp."VenueType" IS NULL OR (rp."VenueType" NOT ILIKE '
             '\'Conference%%\' AND rp."VenueType" NOT IN (\'Book\', \'BookChapter\', \'Preprint\')))')
    with connection.cursor() as cur:
        params1 = [years]
        dept_clause1 = _dept_author_exists_clause('rp', department_id, params1)
        cur.execute(f'''
            SELECT
                COUNT(DISTINCT rp."PaperID") AS papers,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q1'{jelig}) AS q1,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q2'{jelig}) AS q2,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q3'{jelig}) AS q3,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q4'{jelig}) AS q4,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = 'Scopus' OR jr."Quartile" IS NOT NULL) AS scopus,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."Indexing" = 'ISI') AS isi,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."VenueType" = 'Journal') AS journal,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."VenueType" ILIKE 'Conference%%') AS conference,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."VenueType" = 'Book') AS book,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."VenueType" = 'BookChapter') AS book_chapter,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE rp."VenueType" = 'Preprint') AS preprint
            FROM "ResearchPaper" rp
            LEFT JOIN LATERAL (SELECT "Quartile" FROM "JournalRankings"
                WHERE "JournalID" = rp."JournalID"
                ORDER BY "RankingYear" DESC NULLS LAST, "Source" LIMIT 1) jr ON TRUE
            WHERE rp."PubYear" = ANY(%s)
              AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"){AAV}{dept_clause1}
        ''', params1)
        r = cur.fetchone()

        cites_expr = _cites_expr('rp', years)
        params2 = [str(y) for y in years]
        dept_clause2 = _dept_author_exists_clause('rp', department_id, params2)
        cur.execute(f'''
            SELECT COALESCE(SUM({cites_expr}), 0)
            FROM "ResearchPaper" rp
            WHERE EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"){AAV}{dept_clause2}
        ''', params2)
        citations = cur.fetchone()[0]

    return {
        'years': f'{min(years)}-{max(years)}', 'papers': r[0], 'citations': citations,
        'q1_papers': r[1], 'q2_papers': r[2], 'q3_papers': r[3], 'q4_papers': r[4],
        'scopus_papers': r[5], 'isi_papers': r[6],
        'journal_papers': r[7], 'conference_papers': r[8],
        'book_papers': r[9], 'book_chapter_papers': r[10], 'preprint_papers': r[11],
        'scope': 'Al-Baha affiliation confirmed (AffiliationVerified = TRUE) only',
    }


def get_top_researchers(department=None, limit=10, department_id=None):
    """Top researchers by lifetime citations (Researcher.CitationsByYear sum),
    optionally filtered to one department (case-insensitive substring).

    `department_id` is a backend-only scope fence (never in the tool's
    model-facing schema) - when set it OVERRIDES `department` entirely, so a
    HoD-scoped caller can't be redirected to another department just because
    the model was asked/tricked into passing a different `department` string.
    """
    limit = max(1, min(int(limit or 10), 25))
    dept_clause = ''
    params = []
    if department_id:
        dept_clause = ' AND w."DepartmentID" = %s'
        params.append(department_id)
    elif department:
        dept_clause = ' AND d."DepartmentName" ILIKE %s'
        params.append(f'%{department}%')
    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT
                COALESCE(NULLIF(u."ScholarDisplayName", ''),
                         TRIM(CONCAT_WS(' ', u."FirstName", u."LastName")),
                         u."FullName_Ar") AS name,
                d."DepartmentName",
                COALESCE((
                    SELECT SUM(v::int) FROM jsonb_each_text(
                        COALESCE(r."CitationsByYear", '{{}}'::jsonb)) AS kv(k, v)
                    WHERE v ~ '^[0-9]+$'
                ), 0) AS citations,
                (SELECT COUNT(*) FROM "Authors" a
                  JOIN "ResearchPaper" rp2 ON rp2."PaperID" = a."PaperID"
                  WHERE a."UserID" = u."UserID"
                    AND rp2."AffiliationVerified" = TRUE) AS papers
            FROM "Users" u
            JOIN "Researcher" r ON r."UserID" = u."UserID"
            LEFT JOIN LATERAL (
                SELECT w."DepartmentID" FROM "Works_In" w
                WHERE w."UserID" = u."UserID" AND w."IsCurrentPosition" = TRUE
                ORDER BY w."StartDate" ASC NULLS LAST LIMIT 1
            ) w ON TRUE
            LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
            WHERE u."UserType" = 'Researcher'{dept_clause}
            ORDER BY citations DESC NULLS LAST
            LIMIT %s
        ''', params + [limit])
        rows = cur.fetchall()
    return {
        'department_filter': department,
        'researchers': [
            {'name': r[0], 'department': r[1], 'citations': int(r[2]), 'papers': r[3]}
            for r in rows
        ],
        'scope': 'Al-Baha-affiliated papers only for the "papers" count; citations are lifetime totals from each researcher\'s own Scholar profile',
    }


def find_researcher(name):
    """Look up a specific researcher by (partial) name - department, papers,
    citations, litrix_id.

    Matches the (title-stripped) query as a CONTIGUOUS PHRASE against one
    combined name field (Scholar display name, "First Last", or the Arabic
    name) - not "every word matches somewhere across the name independently".

    That distinction is load-bearing, not stylistic: an earlier per-word
    version matched 'Abdulkarim Alzahrani' to a COMPLETELY DIFFERENT real
    person ("Hanaa Abdulkarim Mohammed Alzahrani") purely because her middle
    name happens to be Abdulkarim and Alzahrani is a common family name (14+
    researchers share it) - a real misidentification, not a near-miss,
    surfaced by live testing. Per this project's non-negotiable attribution
    rule (see CLAUDE.md: no name-based fuzzy matching, after the 602-paper
    cross-contamination incident), a wrong person is worse than no answer -
    so this deliberately returns EMPTY rather than a loose word-scatter
    guess. A genuinely misspelled surname (edit distance, not substring)
    will not match either - there's no fuzzy/phonetic matching here.
    """
    cleaned = _TITLE_PREFIX.sub('', (name or '').strip()).strip()
    if not cleaned:
        return {'error': 'name is required', 'researchers': []}
    phrase = f'%{cleaned}%'
    where = (
        '(u."ScholarDisplayName" ILIKE %s '
        'OR TRIM(CONCAT_WS(\' \', u."FirstName", u."LastName")) ILIKE %s '
        'OR u."FullName_Ar" ILIKE %s)'
    )
    params = [phrase, phrase, phrase]
    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT
                u."Litrix_ID",
                COALESCE(NULLIF(u."ScholarDisplayName", ''),
                         TRIM(CONCAT_WS(' ', u."FirstName", u."LastName")),
                         u."FullName_Ar") AS name,
                u."FullName_Ar",
                d."DepartmentName",
                COALESCE((
                    SELECT SUM(v::int) FROM jsonb_each_text(
                        COALESCE(r."CitationsByYear", '{{}}'::jsonb)) AS kv(k, v)
                    WHERE v ~ '^[0-9]+$'
                ), 0) AS citations,
                (SELECT COUNT(*) FROM "Authors" a
                  JOIN "ResearchPaper" rp2 ON rp2."PaperID" = a."PaperID"
                  WHERE a."UserID" = u."UserID"
                    AND rp2."AffiliationVerified" = TRUE) AS papers,
                r."ResearchInterests"
            FROM "Users" u
            JOIN "Researcher" r ON r."UserID" = u."UserID"
            LEFT JOIN LATERAL (
                SELECT w."DepartmentID" FROM "Works_In" w
                WHERE w."UserID" = u."UserID" AND w."IsCurrentPosition" = TRUE
                ORDER BY w."StartDate" ASC NULLS LAST LIMIT 1
            ) w ON TRUE
            LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
            WHERE u."UserType" = 'Researcher' AND ({where})
            ORDER BY citations DESC NULLS LAST
            LIMIT 5
        ''', params)
        rows = cur.fetchall()
    return {
        'query': name,
        'researchers': [
            {
                'litrix_id': r[0], 'name': r[1], 'name_ar': r[2],
                'department': r[3], 'citations': int(r[4]), 'papers': r[5],
                # jsonb comes back from this raw cursor as text, not an
                # already-parsed list - decode it so the tool result is a
                # real JSON array, not a double-encoded string.
                'research_interests': json.loads(r[6]) if isinstance(r[6], str) else (r[6] or []),
            }
            for r in rows
        ],
        'note': (
            'Empty result means the query did not match as a phrase in any '
            'researcher\'s name - NOT proof the person does not exist. Try '
            'asking the user for the correctly-spelled full name rather '
            'than guessing who they might mean from a partial/misspelled '
            'match; this tool never returns a weak or ambiguous match.'
        ) if not rows else None,
        'scope': 'Al-Baha-affiliated papers only for the "papers" count; citations are lifetime totals from each researcher\'s own Scholar profile',
    }


def get_department_stats(department_id=None):
    """Per-department totals: researchers, papers, citations, Scopus papers.

    `department_id` is a backend-only scope fence (never in the tool's
    model-facing schema) - restricts the result to that one department, for
    a HoD-scoped caller."""
    where = ' WHERE d."DepartmentID" = %s' if department_id else ''
    params = [department_id] if department_id else []
    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT
                d."DepartmentName",
                COUNT(DISTINCT u."UserID") AS researchers,
                COUNT(DISTINCT rp."PaperID") AS papers,
                COUNT(DISTINCT rp."PaperID") FILTER (
                    WHERE rp."Indexing" = 'Scopus' OR jr."Quartile" IS NOT NULL) AS scopus_papers
            FROM "Department" d
            LEFT JOIN "Works_In" w ON w."DepartmentID" = d."DepartmentID" AND w."IsCurrentPosition" = TRUE
            LEFT JOIN "Users" u ON u."UserID" = w."UserID" AND u."UserType" = 'Researcher'
            LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
            LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"{AAV}
            LEFT JOIN LATERAL (SELECT "Quartile" FROM "JournalRankings"
                WHERE "JournalID" = rp."JournalID"
                ORDER BY "RankingYear" DESC NULLS LAST, "Source" LIMIT 1) jr ON TRUE
            {where}
            GROUP BY d."DepartmentName"
            ORDER BY papers DESC NULLS LAST
        ''', params)
        rows = cur.fetchall()
    return {
        'departments': [
            {'department': r[0], 'researchers': r[1], 'papers': r[2], 'scopus_papers': r[3]}
            for r in rows
        ],
        'scope': 'Al-Baha affiliation confirmed only',
    }


def get_publication_trend(num_years=5, department_id=None):
    """Papers published per year for the last `num_years` years (incl. current).

    `department_id` is a backend-only scope fence (never in the tool's
    model-facing schema) - see ai_views.py's per-role scope resolution."""
    from datetime import datetime
    num_years = max(1, min(int(num_years or 5), 15))
    current = datetime.now().year
    years = list(range(current - num_years + 1, current + 1))
    with connection.cursor() as cur:
        params = [years]
        dept_clause = _dept_author_exists_clause('rp', department_id, params)
        cur.execute(f'''
            SELECT rp."PubYear", COUNT(DISTINCT rp."PaperID")
            FROM "ResearchPaper" rp
            WHERE rp."PubYear" = ANY(%s)
              AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"){AAV}{dept_clause}
            GROUP BY rp."PubYear"
            ORDER BY rp."PubYear"
        ''', params)
        rows = dict(cur.fetchall())
    return {
        'by_year': [{'year': y, 'papers': rows.get(y, 0)} for y in years],
        'scope': 'Al-Baha affiliation confirmed only',
    }


# Registry the chat loop walks to build the tool-call schema + dispatch a
# model-requested call. Add a new tool by adding one entry here - never as
# more inline branches in ai_views.py.
TOOLS = {
    'get_overview_stats': {
        'fn': get_overview_stats,
        'description': (
            "Institution-wide totals: paper count, citations, Q1-Q4 counts, "
            "Scopus/ISI counts, and the Journal/Conference/Book/BookChapter/"
            "Preprint venue split. For 'all years' / 'all Q1 papers' / any "
            "unscoped question, call this with NO arguments at all - do not "
            "pass years as the string \"all\" or similar, only ever as an "
            "array of integers, e.g. [2024, 2025]."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'years': {
                    'type': 'array', 'items': {'type': 'integer'},
                    'description': (
                        'Specific publication years to scope to, e.g. '
                        '[2024, 2025]. Omit this parameter entirely for all '
                        'years (2011-present) - never pass a string here.'
                    ),
                },
            },
        },
    },
    'find_researcher': {
        'fn': find_researcher,
        'description': (
            "Look up ONE SPECIFIC named researcher (e.g. 'which department "
            "does Dr. Nizar Alsharif work in', 'who is Abdulkareem Alzahrani', "
            "'what are his research interests') - returns their department, "
            "paper count, citations, and research_interests (a list of "
            "topics, empty list if none recorded - say so plainly rather "
            "than 'not specified' if it's empty). Use this for any question "
            "naming a specific person, NOT "
            "get_top_researchers (that's only for ranked lists). If the "
            "result list is empty, tell the user you couldn't find that name "
            "in Litrix rather than guessing - do not answer from general "
            "knowledge about people, only from this tool's result."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': "The researcher's name, as much of it as given."},
            },
            'required': ['name'],
        },
    },
    'get_top_researchers': {
        'fn': get_top_researchers,
        'description': (
            "Top researchers ranked by lifetime citations. Optionally filter "
            "to one department by name (partial match, e.g. 'Computer Science')."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'department': {'type': 'string', 'description': 'Department name filter (optional).'},
                'limit': {'type': 'integer', 'description': 'How many to return (default 10, max 25).'},
            },
        },
    },
    'get_department_stats': {
        'fn': get_department_stats,
        'description': (
            'Per-department breakdown: researcher count, paper count, '
            'Scopus-indexed paper count, ONE ROW PER DEPARTMENT - for '
            'comparing departments against each other, not for an '
            'institution-wide total. Do NOT sum these rows yourself to '
            'answer a "total across the university" question - a '
            'researcher or paper linked to more than one department is '
            'counted once per department here, so summing double-counts. '
            'For any institution-wide total, call get_overview_stats '
            'instead (with no years argument for all-time).'
        ),
        'parameters': {'type': 'object', 'properties': {}},
    },
    'get_publication_trend': {
        'fn': get_publication_trend,
        'description': 'Papers published per year for the last N years (default 5).',
        'parameters': {
            'type': 'object',
            'properties': {
                'num_years': {'type': 'integer', 'description': 'How many recent years (default 5, max 15).'},
            },
        },
    },
}
