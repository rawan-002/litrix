"""Researcher list + profile endpoints (the public researcher-facing API)."""
import re

from rest_framework import viewsets, filters, decorators, response
from django_filters.rest_framework import DjangoFilterBackend

from .models import ResearcherStats
from .serializers import ResearcherStatsSerializer
from .stats import CHART_YEAR_FLOOR


# Canonical Litrix-ID is "Lit-" + 6 zero-padded digits. We accept anything
# loose (LIT-42, lit-0042, Lit-000042) and normalize before a DB lookup so
# manually-typed and differently-cased URLs still resolve.
LITRIX_ID_PATTERN = re.compile(r'^lit-(\d+)$', re.IGNORECASE)


def normalize_litrix_id(raw):
    """Return canonical Lit-NNNNNN if `raw` looks like a Litrix-ID, else None."""
    if not isinstance(raw, str):
        return None
    m = LITRIX_ID_PATTERN.match(raw.strip())
    if not m:
        return None
    return f'Lit-{int(m.group(1)):06d}'


def _normalize_interests(raw):
    """Normalize Researcher.ResearchInterests (Scholar's areas of interest) into
    a clean list of strings. The column is jsonb but historically holds either a
    JSON array (['AI', 'NLP']) or a single comma-separated string (sometimes with
    the Arabic comma '،'). Returns [] when empty."""
    if not raw:
        return []
    if isinstance(raw, str):
        import json as _json
        try:
            raw = _json.loads(raw)
        except Exception:
            raw = [raw]
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for item in (raw or []):
        if not isinstance(item, str):
            continue
        # Split the legacy single-string-with-commas rows into separate tags.
        for part in re.split(r'[،,]', item):
            tag = part.strip()
            if tag and tag not in out:
                out.append(tag)
    return out


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
                    COALESCE(NULLIF(u."ScholarDisplayName", ''),
                             TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))) AS full_name_en,
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
                    COALESCE(NULLIF(u."ScholarDisplayName", ''),
                             TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))) AS full_name_en,
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
        """GET /api/researchers/{id}/profile/

        `id` accepts either a numeric UserID or the public Lit-NNNNNN; we
        resolve to the integer PK at the boundary so the SQL keeps using the
        indexed key. Both are accepted for backward compat with tooling that
        stored numeric IDs (the frontend now uses Litrix_ID everywhere).

        One shot for the whole profile page — identity, aggregated stats,
        chart-ready per-year citations, and the papers list. The page renders
        all of these from the same data, so batching avoids three round-trips
        and a flash of empty-then-filled UI.
        """
        from django.db import connection

        # Litrix-ID gets normalized then looked up; a pure number is treated as
        # the UserID directly. Malformed input returns 400 rather than leaking a
        # 500 from a Postgres type error.
        canonical = normalize_litrix_id(pk)
        if canonical is not None:
            # Match on the numeric core so "LIT-0001", "Lit-000001" and "lit-1"
            # all resolve to the same user regardless of stored zero-padding.
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
                    u."Litrix_ID", u."PhotoURL", r."ResearchInterests",
                    u."ScholarDisplayName"
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
                'photo_url':         row[12],
                'research_interests': _normalize_interests(row[13]),
                'scholar_display_name': row[14],
            }

            # For citations, sum the per-paper Scholar cited_by.value (the most
            # accurate per-paper signal), falling back to OpenAlex's count.
            cur.execute('''
                SELECT
                    COUNT(DISTINCT rp."PaperID")                         AS total_papers,
                    COALESCE(SUM(COALESCE(
                        ("RawData_Log"->'cited_by'->>'value')::int,
                        ("RawData_Log"->>'cited_by_count')::int,
                        0)), 0)                                          AS total_citations,
                    COUNT(DISTINCT rp."PaperID")
                        FILTER (WHERE jr."Quartile" = 'Q1'
                                   AND (rp."VenueType" IS NULL
                                        OR (rp."VenueType" NOT ILIKE 'Conference%%'
                                            AND rp."VenueType" NOT IN ('Book', 'Preprint'))))  AS q1_papers,
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

            # When Scholar gives us an author-level per-year graph, prefer its
            # total over the per-paper aggregation above.
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

            # Prefer Researcher.CitationsByYear — the author-level graph straight
            # from Scholar's cited_by.graph, which is what Scholar shows on the
            # profile. Fall back to summing the per-paper CitationsByYear.
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

                # Clip to CHART_YEAR_FLOOR to match the admin chart window —
                # earlier years just stretch the X-axis and bury recent growth.
                citations_by_year = [
                    pt for pt in citations_by_year
                    if pt['year'] >= CHART_YEAR_FLOOR
                ]
            except Exception:
                citations_by_year = []

            # Papers with full metadata. When there's no linked JournalID, the
            # journal name falls back to Scholar's free-text "publication" string
            # (e.g. "Sustainability 14 (2), 829, 2022").
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
                    COALESCE(rp."VenueType", j."VenueType") AS venue_type,
                    jr."Quartile",
                    jr."ImpactFactor",
                    rp."AffiliationVerified"
                FROM "ResearchPaper" rp
                JOIN "Authors" a ON a."PaperID" = rp."PaperID"
                LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
                LEFT JOIN LATERAL (    SELECT "Quartile", "ImpactFactor"    FROM "JournalRankings"    WHERE "JournalID" = rp."JournalID"    ORDER BY "RankingYear" DESC NULLS LAST, "Source"    LIMIT 1) jr ON TRUE
                WHERE a."UserID" = %s
                ORDER BY rp."PubYear" DESC NULLS LAST, rp."PaperID" DESC
            ''', [resolved_user_id])
            papers = [dict(zip([
                'paper_id', 'title', 'doi', 'pub_year', 'source',
                'indexing', 'citations_by_year', 'citations',
                'journal_name', 'issn_print', 'venue_type',
                'quartile', 'impact_factor', 'affiliation_verified',
            ], r)) for r in cur.fetchall()]

            # Co-authors: everyone who shares a paper with this researcher.
            # Platform members (a non-null UserID -> they're on the Litrix
            # roster, i.e. Al-Baha) carry a Litrix_ID so the card can link to
            # their profile; external names come through with is_albaha = false.
            # Sorted Al-Baha-first, then by how many papers they share.
            cur.execute('''
                SELECT user_id, litrix_id, name, photo_url,
                       COUNT(DISTINCT paper_id) AS shared_papers
                FROM (
                    SELECT
                        co."UserID"    AS user_id,
                        cu."Litrix_ID" AS litrix_id,
                        cu."PhotoURL"  AS photo_url,
                        COALESCE(
                            NULLIF(cu."ScholarDisplayName", ''),
                            NULLIF(TRIM(CONCAT_WS(' ', cu."FirstName", cu."LastName")), ''),
                            co."AuthorNameRaw"
                        )              AS name,
                        co."PaperID"   AS paper_id
                    FROM "Authors" me
                    JOIN "Authors" co ON co."PaperID" = me."PaperID"
                                      AND co."UserID" IS DISTINCT FROM me."UserID"
                    LEFT JOIN "Users" cu ON cu."UserID" = co."UserID"
                    WHERE me."UserID" = %s
                      AND COALESCE(NULLIF(cu."ScholarDisplayName", ''),
                                   NULLIF(TRIM(CONCAT_WS(' ', cu."FirstName", cu."LastName")), ''),
                                   co."AuthorNameRaw") IS NOT NULL
                ) sub
                GROUP BY user_id, litrix_id, name, photo_url
                ORDER BY (user_id IS NOT NULL) DESC, shared_papers DESC, name
                LIMIT 200
            ''', [resolved_user_id])
            coauthors = [
                {
                    'user_id':       r[0],
                    'litrix_id':     r[1],
                    'name':          r[2],
                    'photo_url':     r[3],
                    'shared_papers': r[4],
                    'is_albaha':     r[0] is not None,
                }
                for r in cur.fetchall()
            ]

        return response.Response({
            'identity':          identity,
            'stats':             stats,
            'citations_by_year': citations_by_year,
            'papers':            papers,
            'coauthors':         coauthors,
        })

    @staticmethod
    def _resolve_user_id(pk):
        """Litrix-ID or numeric UserID -> integer UserID. Returns
        (user_id, None) or (None, error_response)."""
        from django.db import connection

        canonical = normalize_litrix_id(pk)
        if canonical is not None:
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
                    return None, response.Response(
                        {'error': f'Researcher {canonical} not found'}, status=404)
                return row[0], None
        try:
            return int(pk), None
        except (TypeError, ValueError):
            return None, response.Response({'error': 'Invalid researcher ID'}, status=400)

    @decorators.action(detail=True, methods=['get'], url_path='openalex-live-affiliation-check')
    def openalex_live_affiliation_check(self, request, pk=None):
        """
        GET /api/researchers/{id}/openalex-live-affiliation-check/

        TEST ENDPOINT (Moez Krichen's profile only, for now). Runs the exact
        same per-paper Al-Baha detection affiliation_verifier.py's OpenAlex
        tier already does (verify_via_openalex) — live, on demand, for every
        DOI'd paper of this researcher — so the profile's existing Al-Baha
        filter (same list/KPIs the top-bar toggle drives) can be tried out
        against fresh OpenAlex data instead of the stored AffiliationVerified
        column. Nothing is written to the DB.

        Returns {"results": {"<paper_id>": true|false|null, ...}} - null
        means OpenAlex was inconclusive (no DOI, not found, no institution
        data), same tri-state as AffiliationVerified.
        """
        from django.db import connection
        import sys, os, time
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/
        from affiliation_verifier import verify_via_openalex, OPENALEX_DELAY

        resolved_user_id, err = self._resolve_user_id(pk)
        if err is not None:
            return err

        with connection.cursor() as cur:
            cur.execute(
                '''
                SELECT rp."PaperID", rp."DOI"
                FROM "ResearchPaper" rp
                JOIN "Authors" a ON a."PaperID" = rp."PaperID"
                WHERE a."UserID" = %s
                ''',
                [resolved_user_id],
            )
            rows = cur.fetchall()

        results = {}
        for paper_id, doi in rows:
            if not doi:
                results[paper_id] = None
                continue
            verified, _evidence = verify_via_openalex(doi)
            results[paper_id] = verified
            time.sleep(OPENALEX_DELAY)

        return response.Response({'results': results})

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
