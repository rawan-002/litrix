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
from datetime import date

from django.db import connection

from .stats import _cites_expr, _default_focus_years, verified_affil_clause

AAV = verified_affil_clause(True, 'rp')  # AffiliationVerified = TRUE, fixed on for chat

# Q1 (and Q2-4) only mean something for journals - a conference/book/chapter/
# preprint can't carry a journal quartile, so it must never count toward one.
# Shared by get_overview_stats and compare_departments so "Q1 papers" means
# the exact same set of papers regardless of which tool answered the question.
_JELIG = (' AND (rp."VenueType" IS NULL OR (rp."VenueType" NOT ILIKE '
          '\'Conference%%\' AND rp."VenueType" NOT IN (\'Book\', \'BookChapter\', \'Preprint\')))')

_TITLE_PREFIX = re.compile(r'^(dr\.?|prof\.?|professor|mr\.?|mrs\.?|ms\.?)\s+', re.I)


def _scope_meta(department_id, source):
    """Explicit scope metadata every tool result carries, so the model (and
    a human reading the raw tool output) can never mix up a department-level
    number with a university-level one just because both look like a plain
    int. Added after live testing showed the model both (a) summing
    get_department_stats' per-department rows into a fake "university"
    total, and (b) echoing a HoD's department-only number back using the
    question's own "across the university" phrasing - neither mistake is
    possible if every number is stamped with what it actually covers.

    `source` (the tool function name) travels with every result too - once a
    conversation holds results from two DIFFERENT tool calls (e.g. an
    overview total plus a department comparison), scope alone doesn't say
    which number came from which call; naming the source does. This is what
    a model needs to enforce, for itself, the rule stated in the system
    prompt: a metric may only be used to make a claim at the scope it was
    actually measured at - it can't silently borrow a number pulled from a
    different tool/scope to answer a question about this one."""
    dept_name = None
    if department_id:
        with connection.cursor() as cur:
            cur.execute('SELECT "DepartmentName" FROM "Department" WHERE "DepartmentID" = %s', [department_id])
            row = cur.fetchone()
            dept_name = row[0] if row else None
    return {
        'data_scope': 'department' if department_id else 'university',
        'department': dept_name,
        'department_id': department_id,
        'source': source,
        'as_of': date.today().isoformat(),
    }


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
    with connection.cursor() as cur:
        params1 = [years]
        dept_clause1 = _dept_author_exists_clause('rp', department_id, params1)
        cur.execute(f'''
            SELECT
                COUNT(DISTINCT rp."PaperID") AS papers,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q1'{_JELIG}) AS q1,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q2'{_JELIG}) AS q2,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q3'{_JELIG}) AS q3,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q4'{_JELIG}) AS q4,
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
        **_scope_meta(department_id, source='get_overview_stats'),
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
        **_scope_meta(department_id, source='get_top_researchers'),
    }


def _match_researchers_sql(name):
    """The one, non-negotiable name-matching policy shared by every tool
    that looks someone up by name (find_researcher, get_researcher_topics).
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
    so a caller gets EMPTY rather than a loose word-scatter guess. A
    genuinely misspelled surname (edit distance, not substring) will not
    match either - there's no fuzzy/phonetic matching here.

    Returns (where_sql, params) - caller supplies the rest of the query
    (which columns, which table alias 'u').
    """
    cleaned = _TITLE_PREFIX.sub('', (name or '').strip()).strip()
    if not cleaned:
        return None, None
    phrase = f'%{cleaned}%'
    where = (
        '(u."ScholarDisplayName" ILIKE %s '
        'OR TRIM(CONCAT_WS(\' \', u."FirstName", u."LastName")) ILIKE %s '
        'OR u."FullName_Ar" ILIKE %s)'
    )
    return where, [phrase, phrase, phrase]


def find_researcher(name):
    """Look up a specific researcher by (partial) name - department, papers,
    citations, litrix_id. See _match_researchers_sql for the matching policy
    (exact contiguous phrase, no fuzzy matching - a wrong person is worse
    than no answer).
    """
    where, params = _match_researchers_sql(name)
    if where is None:
        return {'error': 'name is required', 'researchers': []}
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
                # real JSON array, not a double-encoded string. Named
                # listed_research_interests (not "research topics" or
                # "frequent topics") because it's SELF-REPORTED - it is not
                # derived from this person's actual publications. Live
                # testing showed the model describing these as someone's
                # "most frequent research topics" with zero paper-level
                # evidence for that claim - a category error this field
                # name and the tool description below both guard against.
                # For a claim about what someone ACTUALLY publishes on, use
                # get_researcher_topics' publication_derived_topics instead.
                'listed_research_interests': json.loads(r[6]) if isinstance(r[6], str) else (r[6] or []),
            }
            for r in rows
        ],
        'note': (
            'Empty result means the query did not match as a phrase in any '
            'researcher\'s name - NOT proof the person does not exist. Try '
            'asking the user for the correctly-spelled full name rather '
            'than guessing who they might mean from a partial/misspelled '
            'match; this tool never returns a weak or ambiguous match.'
        ) if not rows else (
            'listed_research_interests is SELF-REPORTED (typed in by the '
            'researcher or an admin) - it is NOT a frequency ranking of '
            'their actual publications. Never call these "most frequent '
            'topics" or imply they were derived from paper analysis; say '
            '"listed research interests" instead. For a claim about what '
            'topics recur across someone\'s actual papers, call '
            'get_researcher_topics instead - that is a different, '
            'publication-derived claim.'
        ),
        'scope': 'Al-Baha-affiliated papers only for the "papers" count; citations are lifetime totals from each researcher\'s own Scholar profile',
        # A 4th data_scope value distinct from department/university/
        # all_departments: this is ONE named individual, never an aggregate.
        # A researcher's own numbers must never be quoted as representative
        # of their department or the university.
        'data_scope': 'researcher',
        'source': 'find_researcher',
        'as_of': date.today().isoformat(),
    }


# Generic connector/method words stripped before counting title word
# frequency in get_researcher_topics - deliberately NOT a stemmer/lemmatizer
# and NOT a domain-specific stopword list beyond this. Keeping it small and
# auditable matters more than completeness: every phrase that survives this
# filter is a literal, exact substring a human can go verify in the titles
# themselves, not a "cleaned up" or normalized version of one.
_TOPIC_STOPWORDS = {
    'a', 'an', 'the', 'of', 'for', 'and', 'or', 'in', 'on', 'using', 'based',
    'approach', 'towards', 'review', 'comparison', 'comparative', 'case',
    'from', 'to', 'as', 'with', 'into', 'via', 'study', 'method', 'methods',
    'framework', 'new', 'novel', 'system', 'systems', 'application',
    'applications', 'vs', 'versus', 'over', 'by', 'is', 'are', 'their',
    'this', 'these', 'it', 'its', 'we', 'how', 'what', 'oriented', 'an',
    'evaluation', 'validated', 'automatic',
}
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]+")


def _title_topics(titles):
    """Bigram (falling back to unigram) frequency across a set of paper
    titles - literal word-adjacency counting after stopword removal, NOT
    semantic/topic-model classification. Every phrase returned traces to an
    exact, checkable substring appearing in 2+ of the given titles; nothing
    here is inferred beyond "this exact phrase recurs". Only a phrase
    reaching 2+ distinct papers is kept - a single occurrence is a title,
    not a pattern.

    `titles` is [(paper_id, title_text), ...]. Returns {phrase: {paper_ids}}.
    """
    from collections import defaultdict
    bigram_papers = defaultdict(set)
    unigram_papers = defaultdict(set)
    for paper_id, title in titles:
        words = _WORD_RE.findall((title or '').lower())
        kept = [w for w in words if w not in _TOPIC_STOPWORDS and len(w) > 2]
        for w in kept:
            unigram_papers[w].add(paper_id)
        for a, b in zip(kept, kept[1:]):
            bigram_papers[f'{a} {b}'].add(paper_id)

    topics = {phrase: papers for phrase, papers in bigram_papers.items() if len(papers) >= 2}
    # A unigram is only worth surfacing on its own if it ISN'T already
    # covered by a qualifying bigram containing it - otherwise "data" and
    # "warehouse" would both clutter the list right next to "data warehouse".
    bigram_words = {w for phrase in topics for w in phrase.split(' ')}
    for word, papers in unigram_papers.items():
        if len(papers) >= 2 and word not in bigram_words:
            topics[word] = papers
    return topics


def get_researcher_topics(name):
    """Distinguishes SELF-REPORTED listed_research_interests
    (Researcher.ResearchInterests - free text the researcher or an admin
    typed in, same field find_researcher exposes) from
    publication_derived_topics (word-adjacency frequency counted from this
    person's own paper TITLES, Al-Baha-affiliated only - see _title_topics).
    These are two DIFFERENT claims about a person and must never be
    presented as if they were the same thing - added after live testing
    described someone's listed interests as her "most frequent research
    topics" with no paper-level evidence backing that specific framing.

    Uses the exact same phrase-match policy as find_researcher (see
    _match_researchers_sql) - ambiguous or no match never guesses; it
    reports how many candidates matched and lets the caller ask which one.
    """
    where, params = _match_researchers_sql(name)
    if where is None:
        return {'error': 'name is required'}
    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT u."UserID",
                COALESCE(NULLIF(u."ScholarDisplayName", ''),
                         TRIM(CONCAT_WS(' ', u."FirstName", u."LastName")),
                         u."FullName_Ar") AS name,
                r."ResearchInterests"
            FROM "Users" u
            JOIN "Researcher" r ON r."UserID" = u."UserID"
            WHERE u."UserType" = 'Researcher' AND ({where})
        ''', params)
        rows = cur.fetchall()

    if not rows:
        return {
            'error': 'no researcher matched that name in Litrix',
            'researchers_found': 0,
        }
    if len(rows) > 1:
        return {
            'error': 'that name matches more than one researcher - ask the user which one',
            'researchers_found': len(rows),
            'candidates': [r[1] for r in rows],
        }

    user_id, matched_name, interests_raw = rows[0]
    listed = json.loads(interests_raw) if isinstance(interests_raw, str) else (interests_raw or [])

    with connection.cursor() as cur:
        cur.execute('''
            SELECT rp."PaperID", COALESCE(NULLIF(rp."Title", ''), rp."Title_En"), rp."PubYear"
            FROM "ResearchPaper" rp
            JOIN "Authors" a ON a."PaperID" = rp."PaperID"
            WHERE a."UserID" = %s AND rp."AffiliationVerified" = TRUE
        ''', [user_id])
        papers = cur.fetchall()

    meta = {pid: (title, year) for pid, title, year in papers}
    publication_derived_topics = None
    if papers:
        topic_map = _title_topics([(pid, title) for pid, title, _ in papers])
        ranked = sorted(topic_map.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:8]
        publication_derived_topics = [
            {
                'topic': phrase,
                'paper_count': len(pids),
                'papers': [
                    {'paper_id': pid, 'title': meta[pid][0], 'year': meta[pid][1]}
                    for pid in sorted(pids, key=lambda p: meta[p][1] or 0, reverse=True)
                ],
            }
            for phrase, pids in ranked
        ]

    return {
        'researcher': matched_name,
        'listed_research_interests': listed,
        'publication_derived_topics': publication_derived_topics,
        'topic_analysis_available': bool(papers),
        'topic_analysis_method': (
            'Word-adjacency (bigram, falling back to unigram) frequency '
            "counted across this researcher's own Al-Baha-affiliated paper "
            'titles - a phrase is only listed if it occurs verbatim in 2 or '
            'more of their titles, and every entry lists the exact papers '
            'behind it. This is a literal, auditable word-frequency count, '
            'NOT semantic/topic-model classification - a real topic can be '
            "missed if titles phrase it inconsistently (plural/singular, "
            'synonyms). It is also a DIFFERENT claim than '
            'listed_research_interests (self-reported, not derived from '
            'publications at all): never present one as the other, and if '
            'topic_analysis_available is false there are no verified papers '
            'to analyze - say so rather than falling back to the listed '
            'interests as if they were the same kind of evidence.'
        ),
        'scope': 'Al-Baha-affiliated papers only',
        'data_scope': 'researcher',
        'source': 'get_researcher_topics',
        'as_of': date.today().isoformat(),
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
        **_scope_meta(department_id, source='get_department_stats'),
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
        **_scope_meta(department_id, source='get_publication_trend'),
    }


# Every metric compare_departments ranks, tagged with whether it's a raw
# count straight from SQL ('direct') or a ratio computed from two other
# counts here in Python ('derived'). A model citing a 'derived' metric as
# fact about a department is on weaker ground than citing a 'direct' one -
# e.g. "30.29 papers per researcher" depends on the researcher headcount
# being current/complete, "212 papers" does not. Neither is invented, but
# they're not the same kind of evidence, so both the metric list AND that
# distinction travel with the result instead of living only in a docstring.
_COMPARISON_METRICS = {
    'papers':                 'direct',
    'researchers':            'direct',
    'scopus_papers':          'direct',
    'q1_papers':              'direct',
    'papers_per_researcher':  'derived',
    'scopus_rate':            'derived',
}


def compare_departments():
    """Ranks every department across several metrics - ALL computed here in
    Python, never by the model combining numbers from separate tool calls
    or from get_department_stats' raw rows itself. Live testing showed the
    model doing its own cross-department arithmetic (summing rows into a
    fake university total) and asserting a department was "the best" from
    one raw number with no comparison at all - this tool exists so ranking
    and comparison are never something the model has to compute by hand.

    Also the only tool that breaks Q1 papers out per department (using the
    same journal-only eligibility as get_overview_stats' university-wide
    q1_papers, see _JELIG) - added after live testing showed the model
    answering a department-level "highest quality" question with
    get_overview_stats' UNIVERSITY-WIDE Q1 count, because no tool exposed a
    per-department Q1 breakdown for it to reach for instead. A missing
    metric at the right scope doesn't make the model say "I don't have
    that" - it reaches for the nearest wrong-scope number that looks like
    it fits, so the fix is to make sure the right-scope metric actually
    exists somewhere to be called.

    University-scope only (see ai_views.py's _ALL_ONLY_TOOLS) - ranking
    departments against each other inherently exposes every department's
    numbers, which a HoD-scoped caller must never receive.
    """
    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT
                d."DepartmentID", d."DepartmentName",
                COUNT(DISTINCT u."UserID") AS researchers,
                COUNT(DISTINCT rp."PaperID") AS papers,
                COUNT(DISTINCT rp."PaperID") FILTER (
                    WHERE rp."Indexing" = 'Scopus' OR jr."Quartile" IS NOT NULL) AS scopus_papers,
                COUNT(DISTINCT rp."PaperID") FILTER (WHERE jr."Quartile" = 'Q1'{_JELIG}) AS q1_papers
            FROM "Department" d
            LEFT JOIN "Works_In" w ON w."DepartmentID" = d."DepartmentID" AND w."IsCurrentPosition" = TRUE
            LEFT JOIN "Users" u ON u."UserID" = w."UserID" AND u."UserType" = 'Researcher'
            LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
            LEFT JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"{AAV}
            LEFT JOIN LATERAL (SELECT "Quartile" FROM "JournalRankings"
                WHERE "JournalID" = rp."JournalID"
                ORDER BY "RankingYear" DESC NULLS LAST, "Source" LIMIT 1) jr ON TRUE
            GROUP BY d."DepartmentID", d."DepartmentName"
        ''')
        rows = cur.fetchall()

    depts = []
    for did, name, researchers, papers, scopus, q1 in rows:
        depts.append({
            'department_id': did, 'department': name,
            'researchers': researchers, 'papers': papers,
            'scopus_papers': scopus, 'q1_papers': q1,
            'papers_per_researcher': round(papers / researchers, 2) if researchers else None,
            'scopus_rate': round(scopus / papers, 3) if papers else None,
        })

    # Rank each metric independently (1 = highest) - the ONLY ranking the
    # model is ever allowed to cite as "top"/"highest"/"best" comes from
    # here, never from eyeballing raw numbers across tool calls. `rankings`
    # gives the same information pre-sorted per metric (so "who's #1 by X"
    # is a direct lookup, not something to scan `departments` for) - both
    # views are kept because `departments` answers "how does dept D do
    # across every metric" and `rankings` answers "who's #1/#2/... on metric
    # M", and a model reaching for the wrong shape has to compute by hand.
    rankings = {}
    for metric, evidence_level in _COMPARISON_METRICS.items():
        ranked = sorted((d for d in depts if d[metric] is not None), key=lambda d: d[metric], reverse=True)
        for i, d in enumerate(ranked, start=1):
            d[f'{metric}_rank'] = i
        rankings[metric] = {
            'evidence_level': evidence_level,
            'ranked': True,
            'scope': 'all_departments',
            'entries': [
                {'rank': i, 'department': d['department'], 'value': d[metric]}
                for i, d in enumerate(ranked, start=1)
            ],
        }

    depts.sort(key=lambda d: d.get('papers_rank') or 999)

    return {
        'departments': depts,
        'rankings': rankings,
        'total_departments': len(depts),
        'source': 'compare_departments',
        # Deliberately NOT 'university' - that value (see _scope_meta) means
        # "one aggregate number for the whole institution" elsewhere in this
        # tool set (e.g. get_overview_stats' total paper count). This result
        # is the opposite shape: a per-department comparison table that
        # happens to cover every department. Reusing 'university' here once
        # led the model to describe a single department's row (e.g. "536
        # papers") as if it were a university-wide total. 'all_departments'
        # can only be read as "compared across departments".
        'data_scope': 'all_departments',
        'as_of': date.today().isoformat(),
        'note': (
            'data_scope "all_departments" means every department is listed '
            'and ranked side-by-side here - it is NOT a single '
            'university-wide total, and no individual department\'s number '
            '(e.g. papers) should be reported as a university total. Use '
            '"rankings" for "who ranks #1/#2/... by <metric>" questions - '
            'it is already sorted, do not re-sort "departments" yourself. '
            'Every "_rank" field (e.g. papers_rank) is computed here, not '
            'by you - rank 1 means highest for that specific metric. '
            'Always state the scope, metric, and value together when '
            'citing a rank - e.g. "ranks #1 among all {n} departments by '
            'papers_per_researcher, at 30.29 papers per researcher" - never '
            'just "rank 1" or "the best" on its own with no metric named. '
            'A metric with evidence_level "derived" (a ratio computed from '
            'two other counts, e.g. papers_per_researcher, scopus_rate) is '
            'still real data, not a guess - but say what it is (e.g. "papers '
            'per researcher", not just "productivity") rather than '
            'presenting it as an official raw count. A department can rank '
            '#1 on one metric and low on another - never imply one metric '
            'proves overall superiority (e.g. "highest quality", "best '
            'department" with no metric attached) without naming which '
            'metric backs that claim. If asked to rank departments by '
            'something none of these metrics measure (e.g. "teaching '
            'quality", "student satisfaction"), say plainly this tool '
            'does not have that metric rather than substituting one of '
            'these instead.'
        ).format(n=len(depts)),
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
            "does Dr. Nizar Alsharif work in', 'who is Abdulkareem Alzahrani') "
            "- returns their department, paper count, citations, and "
            "listed_research_interests (self-reported tags, empty list if "
            "none recorded - say so plainly rather than 'not specified' if "
            "it's empty). listed_research_interests is NOT derived from "
            "their publications and must never be called their 'most "
            "frequent research topics' or similar - for a question about "
            "what topics actually recur across someone's papers, call "
            "get_researcher_topics instead. Use this for any question "
            "naming a specific person, NOT get_top_researchers (that's only "
            "for ranked lists). If the result list is empty, tell the user "
            "you couldn't find that name in Litrix rather than guessing - "
            "do not answer from general knowledge about people, only from "
            "this tool's result."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': "The researcher's name, as much of it as given."},
            },
            'required': ['name'],
        },
    },
    'get_researcher_topics': {
        'fn': get_researcher_topics,
        'description': (
            "Use this for ANY question about what topics/areas a specific "
            "named researcher actually publishes on, works on, or has "
            "written about most (e.g. 'what does she research', 'what are "
            "his main research topics', 'what has she published on most') - "
            "NEVER answer that kind of question from find_researcher's "
            "listed_research_interests alone, which is a self-reported tag "
            "list, not a publication analysis. Returns BOTH "
            "listed_research_interests (self-reported) and "
            "publication_derived_topics (word-frequency counted from their "
            "actual paper titles, each backed by real paper IDs/titles/"
            "years) side by side - state which one you're citing, never "
            "blend them into one claim. If publication_derived_topics is "
            "null (topic_analysis_available: false), say there are no "
            "verified papers to analyze rather than substituting the listed "
            "interests as if they were the same kind of evidence."
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
    'compare_departments': {
        'fn': compare_departments,
        'description': (
            "Ranks EVERY department across multiple metrics (papers, "
            "researchers, scopus_papers, q1_papers, papers_per_researcher, "
            "scopus_rate), each with its own precomputed rank in both "
            "'departments' (one row per dept, every metric) and 'rankings' "
            "(pre-sorted per metric - use this for 'who's #1/#2 by X'). "
            "This is the ONLY tool with a per-department Q1 breakdown - "
            "get_overview_stats' q1_papers is university-wide only and "
            "must never be used to answer a department-level quality "
            "question. Use this tool for ANY comparative or superlative "
            "question ('best', 'top', 'highest', 'which department...', "
            "'how does X compare to Y', 'highest quality') - never call "
            "get_department_stats and compare the raw numbers yourself, "
            "and never call this to answer a single-department or "
            "university-total question (use get_department_stats or "
            "get_overview_stats for those). If the question asks about a "
            "notion of quality/performance not covered by these metrics, "
            "say so rather than substituting one of these metrics silently."
        ),
        'parameters': {'type': 'object', 'properties': {}},
    },
}
