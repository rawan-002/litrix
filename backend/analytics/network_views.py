"""
Litrix Research Network
=======================
Endpoints powering the /network page: given a researcher, return the
shape of their collaboration graph as nodes + edges. The frontend
renders this with a D3 force simulation.

Modes
-----
?mode=coauthors  (default)  -> only people who actually wrote a paper
                                with the centre.
?mode=interests             -> only "thematic" neighbours — researchers
                                whose Top-N OpenAlex `concepts` overlap
                                with the centre, even if they never
                                co-authored. Edge weight = #shared
                                concepts.
?mode=both                  -> union of the two graphs.

Hidden gems
-----------
Registered Users with HIGH interest overlap but ZERO co-authored papers
with the centre. Surfaces "people you should probably talk to."

Interests source (hybrid, in this order)
----------------------------------------
1. **Researcher.ResearchInterests** — Google-Scholar-style profile
   labels the researcher curated themselves (e.g. "Artificial
   Intelligence", "Computational Intelligence"). Populated by the
   `fetch_scholar_interests` management command, or edited manually
   from the profile page. This is the **preferred** signal because
   it reflects the researcher's stated intent.
2. **RawData_Log->'concepts' + 'topics'** — OpenAlex auto-tags derived
   from paper content. Used as a **fallback** for any researcher who
   hasn't refreshed their Scholar interests yet, so the network
   keeps working on day one.

The hybrid logic lives in INTERESTS_SQL and INTEREST_NEIGHBOURS_SQL.
"""
import logging
import re

from rest_framework import decorators, response
from django.db import connection

logger = logging.getLogger(__name__)


LITRIX_ID_RE = re.compile(r'^lit-(\d+)$', re.IGNORECASE)

CONCEPT_MIN_SCORE = 0.25
INTERESTS_PER_NODE = 5
# Min number of shared topics required to draw an "interest" edge.
# We start at 1 because most researchers only have 3-5 Scholar labels —
# requiring 2 overlaps is too strict at this data volume. Bump back to
# 2 once most researchers have populated interests.
INTEREST_OVERLAP_MIN = 1
MAX_INTERNAL = 80
MAX_EXTERNAL = 40
MAX_INTEREST_NEIGHBOURS = 30
MAX_HIDDEN_GEMS = 6


def _resolve_user_id(pk):
    """Accept either a numeric UserID or a Litrix-ID and return UserID."""
    if pk is None:
        return None
    m = LITRIX_ID_RE.match(str(pk).strip())
    if m:
        seq = int(m.group(1))
        with connection.cursor() as cur:
            cur.execute(
                'SELECT "UserID" FROM "Users" '
                'WHERE "Litrix_ID" IS NOT NULL '
                '  AND "Litrix_ID" ~* \'^lit-[0-9]+$\' '
                '  AND CAST(SUBSTRING("Litrix_ID" FROM 5) AS INTEGER) = %s '
                'LIMIT 1',
                [seq],
            )
            row = cur.fetchone()
            return row[0] if row else None
    try:
        return int(pk)
    except (TypeError, ValueError):
        return None


INTERESTS_SQL = '''
-- Hybrid interest source:
--   1) Researcher.ResearchInterests  (Scholar profile labels — preferred)
--   2) RawData_Log->'concepts' + 'topics'  (OpenAlex auto-tags — fallback)
--
-- For each user we first try the manually-curated labels. If that's
-- empty/NULL we fall back to the OpenAlex concepts derived from their
-- papers so the network still works on day one (before anyone has
-- refreshed their Scholar interests).
WITH from_scholar AS (
    SELECT r."UserID",
           TRIM(elem #>> '{{}}')::text          AS name,
           NULL::float                         AS score,
           1.0::float                          AS weight   -- highest priority
    FROM "Researcher" r
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(r."ResearchInterests") = 'array'
             THEN r."ResearchInterests"
             ELSE '[]'::jsonb END
    ) elem
    WHERE r."UserID" = ANY(%s)
      AND jsonb_typeof(r."ResearchInterests") = 'array'
      AND jsonb_array_length(r."ResearchInterests") > 0
),
from_openalex AS (
    SELECT a."UserID",
           TRIM(elem->>'display_name')         AS name,
           (elem->>'score')::float             AS score,
           0.5::float                          AS weight   -- fallback only
    FROM "Authors" a
    JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(rp."RawData_Log"->'concepts') = 'array'
             THEN rp."RawData_Log"->'concepts'
             ELSE '[]'::jsonb END
        ||
        CASE WHEN jsonb_typeof(rp."RawData_Log"->'topics') = 'array'
             THEN rp."RawData_Log"->'topics'
             ELSE '[]'::jsonb END
    ) elem
    WHERE a."UserID" = ANY(%s)
      {year_sql}
      AND jsonb_typeof(elem) = 'object'
      AND a."UserID" NOT IN (SELECT "UserID" FROM from_scholar)
),
per_concept AS (
    SELECT * FROM from_scholar
    UNION ALL
    SELECT * FROM from_openalex
)
SELECT "UserID",
       canonicalize_interest(name)  AS lname,
       MAX(name)                AS pretty,
       SUM(weight)::float       AS freq
FROM per_concept
WHERE name IS NOT NULL
  AND LENGTH(name) BETWEEN 3 AND 80
  AND (score IS NULL OR score >= %s)
GROUP BY "UserID", canonicalize_interest(name)
'''


def _fetch_interests(cur, user_ids, year_sql, year_params):
    """
    Returns {UserID: {'top': [...], 'set': {...}}} or {} on failure.

    SAFETY NET: any SQL/runtime failure here is swallowed and logged —
    we don't want a problem in the (optional, decorative) interests
    chips to bring down the whole network endpoint.
    """
    if not user_ids:
        return {}
    uid_list = list(user_ids)
    try:
        cur.execute(
            INTERESTS_SQL.format(year_sql=year_sql),
            [uid_list, uid_list] + year_params + [CONCEPT_MIN_SCORE],
        )
        rows_raw = cur.fetchall()
    except Exception as e:
        logger.exception("interests fetch failed: %s", e)
        # CRITICAL: roll back so the aborted transaction state doesn't
        # poison the *next* cursor.execute() inside the same with-block.
        # Without this, every subsequent query in the endpoint dies with
        # 'current transaction is aborted'.
        try:
            connection.rollback()
        except Exception:
            pass
        return {}

    bucket = {}
    for uid, lname, pretty, freq in rows_raw:
        b = bucket.setdefault(uid, [])
        b.append((lname, pretty, freq))
    out = {}
    for uid, rows in bucket.items():
        rows.sort(key=lambda r: -r[2])
        out[uid] = {
            'top': [r[1] for r in rows[:INTERESTS_PER_NODE]],
            'set': {r[0] for r in rows[:25]},
        }
    return out


INTEREST_NEIGHBOURS_SQL = '''
-- Hybrid: prefer Scholar interests, fall back to OpenAlex concepts.
WITH centre_concepts AS (
    -- Centre: try Scholar first, fall back to OpenAlex
    SELECT DISTINCT canonicalize_interest(TRIM(elem #>> '{{}}')) AS lname
    FROM "Researcher" r
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(r."ResearchInterests") = 'array'
             THEN r."ResearchInterests"
             ELSE '[]'::jsonb END
    ) elem
    WHERE r."UserID" = %s
      AND jsonb_typeof(r."ResearchInterests") = 'array'
      AND jsonb_array_length(r."ResearchInterests") > 0
      AND TRIM(elem #>> '{{}}') IS NOT NULL
      AND LENGTH(TRIM(elem #>> '{{}}')) BETWEEN 3 AND 80

    UNION

    SELECT DISTINCT canonicalize_interest(TRIM(elem->>'display_name')) AS lname
    FROM "Authors" a
    JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(rp."RawData_Log"->'concepts') = 'array'
             THEN rp."RawData_Log"->'concepts'
             ELSE '[]'::jsonb END
        ||
        CASE WHEN jsonb_typeof(rp."RawData_Log"->'topics') = 'array'
             THEN rp."RawData_Log"->'topics'
             ELSE '[]'::jsonb END
    ) elem
    WHERE a."UserID" = %s
      {year_sql_centre}
      AND TRIM(elem->>'display_name') IS NOT NULL
      AND LENGTH(TRIM(elem->>'display_name')) BETWEEN 3 AND 80
      AND ((elem->>'score')::float IS NULL OR (elem->>'score')::float >= %s)
      AND NOT EXISTS (
          SELECT 1 FROM "Researcher" rr
          WHERE rr."UserID" = a."UserID"
            AND jsonb_typeof(rr."ResearchInterests") = 'array'
            AND jsonb_array_length(rr."ResearchInterests") > 0
      )
),
coauthor_ids AS (
    SELECT DISTINCT a2."UserID"
    FROM "Authors" a1
    JOIN "Authors" a2 ON a2."PaperID" = a1."PaperID"
                      AND a2."UserID" <> a1."UserID"
    WHERE a1."UserID" = %s
),
candidate_concepts AS (
    -- Candidates: same hybrid logic for every other researcher
    SELECT r."UserID",
           canonicalize_interest(TRIM(elem #>> '{{}}')) AS lname
    FROM "Researcher" r
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(r."ResearchInterests") = 'array'
             THEN r."ResearchInterests"
             ELSE '[]'::jsonb END
    ) elem
    WHERE r."UserID" <> %s
      AND jsonb_typeof(r."ResearchInterests") = 'array'
      AND jsonb_array_length(r."ResearchInterests") > 0
      AND TRIM(elem #>> '{{}}') IS NOT NULL
      AND LENGTH(TRIM(elem #>> '{{}}')) BETWEEN 3 AND 80

    UNION

    SELECT a."UserID",
           canonicalize_interest(TRIM(elem->>'display_name')) AS lname
    FROM "Authors" a
    JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(rp."RawData_Log"->'concepts') = 'array'
             THEN rp."RawData_Log"->'concepts'
             ELSE '[]'::jsonb END
        ||
        CASE WHEN jsonb_typeof(rp."RawData_Log"->'topics') = 'array'
             THEN rp."RawData_Log"->'topics'
             ELSE '[]'::jsonb END
    ) elem
    WHERE a."UserID" <> %s
      {year_sql_cand}
      AND TRIM(elem->>'display_name') IS NOT NULL
      AND LENGTH(TRIM(elem->>'display_name')) BETWEEN 3 AND 80
      AND ((elem->>'score')::float IS NULL OR (elem->>'score')::float >= %s)
      AND NOT EXISTS (
          SELECT 1 FROM "Researcher" rr
          WHERE rr."UserID" = a."UserID"
            AND jsonb_typeof(rr."ResearchInterests") = 'array'
            AND jsonb_array_length(rr."ResearchInterests") > 0
      )
)
SELECT cc."UserID",
       u."Litrix_ID",
       COALESCE(u."FullName_Ar",
                TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))) AS label,
       COALESCE(d."DepartmentName", 'Unaffiliated')                AS dept,
       COUNT(DISTINCT cc.lname)                                     AS overlap,
       (cc."UserID" IN (SELECT "UserID" FROM coauthor_ids))         AS already_collab,
       (SELECT COUNT(*) FROM "Authors" ax WHERE ax."UserID" = cc."UserID") AS papers
FROM candidate_concepts cc
JOIN centre_concepts c ON c.lname = cc.lname
JOIN "Users" u ON u."UserID" = cc."UserID"
LEFT JOIN LATERAL (
    SELECT wx."DepartmentID"
    FROM "Works_In" wx
    LEFT JOIN "Department" dx ON dx."DepartmentID" = wx."DepartmentID"
    WHERE wx."UserID" = u."UserID" AND wx."IsCurrentPosition" = TRUE
    ORDER BY (dx."HeadID" = u."UserID") ASC NULLS FIRST,
             wx."StartDate" ASC NULLS LAST
    LIMIT 1
) w ON TRUE
LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
GROUP BY cc."UserID", u."Litrix_ID", u."FullName_Ar", u."FirstName",
         u."LastName", d."DepartmentName"
HAVING COUNT(DISTINCT cc.lname) >= %s
ORDER BY overlap DESC
LIMIT %s
'''


# ----------------------------------------------------------------------
# Researcher-editable interests
# ----------------------------------------------------------------------
#
# GET  /api/researchers/<id>/interests/  → current list
# PUT  /api/researchers/<id>/interests/  → replace the whole list
#                                           body: {"interests": [...]}
#
# Permission model
#   • A researcher can edit their OWN interests (UserID match).
#   • Admins can edit anyone's (relies on the existing role middleware
#     attaching `request.user`).
#
# Why PUT (not PATCH): the list is intentionally replace-all. The
# editor in the UI is a chip-list — the user sees the final state and
# saves it as a whole. PATCH would invite race conditions in a chip
# UI with multiple browser tabs open.
# ----------------------------------------------------------------------

@decorators.api_view(['GET', 'PUT'])
def researcher_interests(request, pk):
    import json
    from datetime import datetime, timezone as _tz

    user_id = _resolve_user_id(pk)
    if not user_id:
        return response.Response({'error': 'Invalid researcher id'}, status=404)

    # ---- READ ----
    if request.method == 'GET':
        with connection.cursor() as cur:
            cur.execute(
                'SELECT "ResearchInterests", "ResearchInterestsUpdatedAt" '
                'FROM "Researcher" WHERE "UserID" = %s',
                [user_id],
            )
            row = cur.fetchone()
            if not row:
                return response.Response(
                    {'error': 'Researcher not found'}, status=404)
            interests = row[0] or []
            if isinstance(interests, str):
                try:
                    interests = json.loads(interests)
                except Exception:
                    interests = []
            return response.Response({
                'interests':  interests,
                'updated_at': row[1].isoformat() if row[1] else None,
            })

    # ---- WRITE (PUT) ----
    auth_user = getattr(request, 'user', None)
    auth_user_id = getattr(auth_user, 'user_id', None) or getattr(auth_user, 'pk', None)
    is_admin = bool(getattr(auth_user, 'is_staff', False)) or \
               getattr(auth_user, 'user_type', None) == 'Admin'
    if auth_user_id != user_id and not is_admin:
        return response.Response(
            {'error': 'You can only edit your own interests.'}, status=403)

    raw = request.data.get('interests') or []
    if not isinstance(raw, list):
        return response.Response(
            {'error': '`interests` must be a JSON array of strings.'}, status=400)

    # Sanitise: trim, drop empties, dedupe (case-insensitive), cap to 20.
    seen, cleaned = set(), []
    for x in raw:
        if not isinstance(x, str):
            continue
        t = x.strip()
        if not t or len(t) > 80:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        cleaned.append(t)
        if len(cleaned) >= 20:
            break

    with connection.cursor() as cur:
        payload = json.dumps(cleaned) if cleaned else None
        cur.execute(
            '''
            UPDATE "Researcher"
               SET "ResearchInterests"          = %s::jsonb,
                   "ResearchInterestsUpdatedAt" = %s
             WHERE "UserID" = %s
            ''',
            [payload, datetime.now(_tz.utc), user_id],
        )
    return response.Response({'interests': cleaned, 'saved': True})


@decorators.api_view(['GET'])
def researcher_network(request, pk):
    user_id = _resolve_user_id(pk)
    if not user_id:
        return response.Response({'error': 'Invalid researcher id'}, status=404)

    mode = (request.query_params.get('mode') or 'coauthors').lower()
    if mode not in ('coauthors', 'interests', 'both'):
        mode = 'coauthors'

    internal_only = request.query_params.get('internal_only', '').lower() == 'true'
    try:
        min_papers = max(1, int(request.query_params.get('min_papers') or 1))
    except ValueError:
        min_papers = 1
    try:
        year_from = int(request.query_params.get('year_from') or 0) or None
    except ValueError:
        year_from = None
    try:
        year_to = int(request.query_params.get('year_to') or 0) or None
    except ValueError:
        year_to = None

    year_clauses, year_params = [], []
    if year_from:
        year_clauses.append('rp."PubYear" >= %s')
        year_params.append(year_from)
    if year_to:
        year_clauses.append('rp."PubYear" <= %s')
        year_params.append(year_to)
    year_sql = ('AND ' + ' AND '.join(year_clauses)) if year_clauses else ''

    with connection.cursor() as cur:

        # ---- Center node ----
        cur.execute(
            'SELECT u."UserID", u."FullName_Ar", '
            '       TRIM(CONCAT_WS(\' \', u."FirstName", u."LastName")), '
            '       u."Litrix_ID", '
            '       d."DepartmentName", '
            '       (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = u."UserID") '
            'FROM "Users" u '
            'LEFT JOIN LATERAL ('
            '    SELECT wx."DepartmentID", dx."DepartmentName" '
            '    FROM "Works_In" wx '
            '    LEFT JOIN "Department" dx ON dx."DepartmentID" = wx."DepartmentID" '
            '    WHERE wx."UserID" = u."UserID" AND wx."IsCurrentPosition" = TRUE '
            '    ORDER BY (dx."HeadID" = u."UserID") ASC NULLS FIRST, '
            '             wx."StartDate" ASC NULLS LAST '
            '    LIMIT 1'
            ') w ON TRUE '
            'LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID" '
            'WHERE u."UserID" = %s',
            [user_id],
        )
        crow = cur.fetchone()
        if not crow:
            return response.Response({'error': 'Researcher not found'}, status=404)

        center = {
            'user_id':   crow[0],
            'name_ar':   crow[1],
            'name_en':   crow[2],
            'litrix_id': crow[3],
            'dept':      crow[4] or 'Unaffiliated',
            'papers':    crow[5] or 0,
        }

        # ---- Internal co-authors ----
        cur.execute(
            f'''
            WITH coauthor_papers AS (
                SELECT a2."UserID" AS coauthor_id,
                       COUNT(DISTINCT rp."PaperID") AS shared
                FROM "Authors" a1
                JOIN "Authors" a2  ON a2."PaperID" = a1."PaperID"
                                   AND a2."UserID" <> a1."UserID"
                JOIN "ResearchPaper" rp ON rp."PaperID" = a1."PaperID"
                WHERE a1."UserID" = %s
                  {year_sql}
                GROUP BY a2."UserID"
                HAVING COUNT(DISTINCT rp."PaperID") >= %s
            )
            SELECT u."UserID", u."Litrix_ID",
                   COALESCE(u."FullName_Ar",
                            TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))) AS label,
                   COALESCE(d."DepartmentName", 'Unaffiliated') AS dept,
                   cp.shared,
                   (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = u."UserID") AS total_papers
            FROM coauthor_papers cp
            JOIN "Users" u ON u."UserID" = cp.coauthor_id
            LEFT JOIN LATERAL (
                SELECT wx."DepartmentID"
                FROM "Works_In" wx
                LEFT JOIN "Department" dx ON dx."DepartmentID" = wx."DepartmentID"
                WHERE wx."UserID" = u."UserID" AND wx."IsCurrentPosition" = TRUE
                ORDER BY (dx."HeadID" = u."UserID") ASC NULLS FIRST,
                         wx."StartDate" ASC NULLS LAST
                LIMIT 1
            ) w ON TRUE
            LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
            ORDER BY cp.shared DESC
            LIMIT %s
            ''',
            [user_id] + year_params + [min_papers, MAX_INTERNAL],
        )
        internal_rows = cur.fetchall()

        # ---- External co-authors ----
        # ID-FIRST DISAMBIGUATION
        # -----------------------
        # We deliberately DROP name-based matching. Name comparison
        # across languages ("سعد القثامي" vs "Saad Alqithami") is
        # unreliable, produces duplicates, and creates ghost externals.
        #
        # The new rule: an authorship is "external" iff
        #   - its author.id (OpenAlex)  is NOT a registered Researcher, AND
        #   - its author.orcid          is NOT a registered Researcher.
        #
        # Trade-off: any authorship in the JSON that lacks BOTH ids is
        # also kept as external (we have no signal to dedupe). That's
        # correct — without an id, we can't claim it's anyone in particular.
        #
        # We also drop the from_ext_table path: the ExternalAuthors
        # table stores only names/affiliations (no ids), so it
        # inherently can't be disambiguated. We rely solely on the JSON
        # authorships which are the OpenAlex source of truth.
        ext_rows = []
        if mode in ('coauthors', 'both') and not internal_only:
            cur.execute(
                f'''
                WITH internal_oa_ids AS (
                    -- Every OpenAlex Author ID held by a registered
                    -- Researcher. We strip any URL prefix so we can
                    -- compare against the raw last segment.
                    SELECT DISTINCT
                        REGEXP_REPLACE(TRIM(r."OpenAlex_AuthorID"),
                                       '^.*/', '') AS oa_id
                    FROM "Researcher" r
                    WHERE r."OpenAlex_AuthorID" IS NOT NULL
                      AND TRIM(r."OpenAlex_AuthorID") <> ''
                ),
                internal_orcids AS (
                    -- Every ORCID held by a registered Researcher.
                    SELECT DISTINCT
                        REGEXP_REPLACE(TRIM(r."ORCID_ID"),
                                       '^.*/', '') AS orcid
                    FROM "Researcher" r
                    WHERE r."ORCID_ID" IS NOT NULL
                      AND TRIM(r."ORCID_ID") <> ''
                ),
                -- One row per (paper, external_author).
                -- We bucket by OpenAlex ID when present (most reliable
                -- key), else ORCID, else fall back to the raw name as
                -- a last-resort bucket — but with a TRUE id, the name
                -- is just for display.
                external_authorships AS (
                    SELECT
                        rp."PaperID",
                        REGEXP_REPLACE(
                            COALESCE(ship->'author'->>'id', ''),
                            '^.*/', ''
                        ) AS oa_id,
                        REGEXP_REPLACE(
                            COALESCE(ship->'author'->>'orcid', ''),
                            '^.*/', ''
                        ) AS orcid,
                        TRIM(ship->'author'->>'display_name') AS name,
                        COALESCE(
                            (SELECT string_agg(x, ', ')
                             FROM jsonb_array_elements_text(
                                 COALESCE(ship->'raw_affiliation_strings',
                                          '[]'::jsonb)
                             ) x),
                            ''
                        ) AS affil
                    FROM "Authors" a
                    JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
                    CROSS JOIN LATERAL jsonb_array_elements(
                        COALESCE(rp."RawData_Log"->'authorships',
                                 '[]'::jsonb)
                    ) AS ship
                    WHERE a."UserID" = %s
                      {year_sql}
                      AND ship->'author'->>'display_name' IS NOT NULL
                      AND TRIM(ship->'author'->>'display_name') <> ''
                ),
                -- Strict ID-based exclusion. The author is external
                -- only if NEITHER identifier matches a registered
                -- Researcher.
                non_internal AS (
                    SELECT *
                    FROM external_authorships ea
                    WHERE (ea.oa_id = ''
                           OR ea.oa_id NOT IN (SELECT oa_id FROM internal_oa_ids))
                      AND (ea.orcid = ''
                           OR ea.orcid NOT IN (SELECT orcid FROM internal_orcids))
                ),
                -- Stable bucket key: prefer OpenAlex ID, then ORCID,
                -- then a normalised lower-cased name. This gives us
                -- one row per distinct external author, even when the
                -- display_name varies slightly across papers.
                bucketed AS (
                    SELECT
                        CASE
                            WHEN oa_id <> ''  THEN 'oa:'    || oa_id
                            WHEN orcid <> ''  THEN 'orcid:' || orcid
                            ELSE 'name:' || LOWER(name)
                        END AS bucket_key,
                        name,
                        affil,
                        "PaperID"
                    FROM non_internal
                ),
                grouped AS (
                    SELECT
                        bucket_key,
                        MAX(name)                    AS name,
                        MAX(affil)                   AS affil,
                        COUNT(DISTINCT "PaperID")    AS shared
                    FROM bucketed
                    GROUP BY bucket_key
                )
                SELECT name, affil, shared
                FROM grouped
                WHERE shared >= %s
                ORDER BY shared DESC
                LIMIT %s
                ''',
                [user_id] + year_params + [min_papers, MAX_EXTERNAL],
            )
            ext_rows = cur.fetchall()

        # ---- Interests for centre + internal coauthors ----
        interests_ids = [user_id] + [r[0] for r in internal_rows]
        interests_map = _fetch_interests(cur, interests_ids, year_sql, year_params)

        # ---- Interest neighbours ----
        # Wrapped in try/except so a SQL hiccup here doesn't kill the
        # whole endpoint — we degrade gracefully to an empty interest
        # neighbours list and still return the co-authors graph.
        interest_rows = []
        if mode in ('interests', 'both'):
            # Param order matches CTEs in INTEREST_NEIGHBOURS_SQL:
            # centre_concepts.scholar (user_id)
            # centre_concepts.openalex (user_id, year_params, CONCEPT_MIN_SCORE)
            # coauthor_ids (user_id)
            # candidate_concepts.scholar (user_id)
            # candidate_concepts.openalex (user_id, year_params, CONCEPT_MIN_SCORE)
            # HAVING / LIMIT (INTEREST_OVERLAP_MIN, MAX_INTEREST_NEIGHBOURS)
            params = (
                [user_id, user_id]
                + year_params
                + [CONCEPT_MIN_SCORE, user_id, user_id, user_id]
                + year_params
                + [CONCEPT_MIN_SCORE, INTEREST_OVERLAP_MIN, MAX_INTEREST_NEIGHBOURS]
            )
            try:
                cur.execute(
                    INTEREST_NEIGHBOURS_SQL.format(
                        year_sql_centre=year_sql,
                        year_sql_cand=year_sql,
                    ),
                    params,
                )
                interest_rows = cur.fetchall()
            except Exception as e:
                logger.exception("interest neighbours fetch failed: %s", e)
                # Roll back so the aborted transaction doesn't poison
                # the next cursor.execute() below.
                try:
                    connection.rollback()
                except Exception:
                    pass
                interest_rows = []

            if interest_rows:
                new_ids = [r[0] for r in interest_rows if r[0] not in interests_map]
                if new_ids:
                    extra = _fetch_interests(cur, new_ids, year_sql, year_params)
                    interests_map.update(extra)

    # ---- Build payload ----
    centre_interests = interests_map.get(center['user_id'], {'top': [], 'set': set()})
    centre_set = centre_interests['set']

    nodes = [{
        'id':        f'u{center["user_id"]}',
        'label':     center['name_ar'] or center['name_en'] or '—',
        'dept':      center['dept'],
        'papers':    center['papers'],
        'shared':    center['papers'],
        'kind':      'self',
        'litrix_id': center['litrix_id'],
        'interests': centre_interests['top'],
    }]
    edges = []
    dept_collab_counts = {}
    coauthor_id_set = set()

    if mode in ('coauthors', 'both'):
        for uid, lit_id, label, dept, shared, total_papers in internal_rows:
            coauthor_id_set.add(uid)
            ints = interests_map.get(uid, {'top': [], 'set': set()})
            shared_int = sorted(centre_set & ints['set'])
            nodes.append({
                'id':         f'u{uid}',
                'label':      label,
                'dept':       dept,
                'papers':     total_papers,
                'shared':     shared,
                'kind':       'same' if dept == center['dept'] else 'cross',
                'litrix_id':  lit_id,
                'interests':  ints['top'],
                'shared_interests_count': len(shared_int),
                'shared_interests': shared_int[:8],
            })
            edges.append({
                'source': f'u{center["user_id"]}',
                'target': f'u{uid}',
                'weight': shared,
                'kind':   'coauthor',
            })
            dept_collab_counts[dept] = dept_collab_counts.get(dept, 0) + shared

        for i, (name, affil, shared) in enumerate(ext_rows):
            nodes.append({
                'id':     f'ext{i}',
                'label':  name,
                'dept':   (affil.split(',')[0].strip() if affil else 'External'),
                'papers': shared,
                'shared': shared,
                'kind':   'external',
                'affil':  affil or '',
                'interests': [],
            })
            edges.append({
                'source': f'u{center["user_id"]}',
                'target': f'ext{i}',
                'weight': shared,
                'kind':   'coauthor',
            })

    hidden_gems = []
    if mode in ('interests', 'both'):
        for uid, lit_id, label, dept, overlap, already_collab, papers in interest_rows:
            if uid == center['user_id']:
                continue
            if mode == 'both' and uid in coauthor_id_set:
                continue
            ints = interests_map.get(uid, {'top': [], 'set': set()})
            shared_int = sorted(centre_set & ints['set'])
            nodes.append({
                'id':         f'u{uid}',
                'label':      label,
                'dept':       dept,
                'papers':     papers,
                'shared':     overlap,
                'kind':       'interest',
                'litrix_id':  lit_id,
                'interests':  ints['top'],
                'shared_interests_count': len(shared_int),
                'shared_interests': shared_int[:8],
            })
            edges.append({
                'source': f'u{center["user_id"]}',
                'target': f'u{uid}',
                'weight': overlap,
                'kind':   'interest',
            })
            if not already_collab and overlap >= INTEREST_OVERLAP_MIN + 1:
                hidden_gems.append({
                    'litrix_id':        lit_id,
                    'name':             label,
                    'dept':             dept,
                    'overlap':          overlap,
                    'papers':           papers,
                    'shared_interests': shared_int[:5],
                })
        hidden_gems = sorted(hidden_gems, key=lambda g: -g['overlap'])[:MAX_HIDDEN_GEMS]

    institution_counts = {}
    for _, affil, _ in ext_rows:
        if not affil:
            continue
        inst = affil.split(',')[0].strip()
        if inst:
            institution_counts[inst] = institution_counts.get(inst, 0) + 1

    top_internal = internal_rows[0] if internal_rows else None
    top_external = ext_rows[0] if ext_rows else None
    most_connected_dept = None
    if dept_collab_counts:
        most_connected_dept = max(dept_collab_counts.items(), key=lambda kv: kv[1])[0]

    insights = {
        'total_collaborators': len(internal_rows) + len(ext_rows),
        'internal':            len(internal_rows),
        'external':            len(ext_rows),
        'top_coauthor':        (
            {'name': top_internal[2], 'dept': top_internal[3], 'shared': top_internal[4]}
            if top_internal else None
        ),
        'top_external': (
            {'name': top_external[0], 'affil': top_external[1], 'shared': top_external[2]}
            if top_external else None
        ),
        'most_connected_dept': most_connected_dept,
        'top_institutions':    sorted(institution_counts.items(), key=lambda kv: -kv[1])[:5],
        'top_interests':       centre_interests['top'],
        'interest_neighbours': len([n for n in nodes if n.get('kind') == 'interest']),
        'hidden_gems':         hidden_gems,
    }

    return response.Response({
        'center':   center,
        'nodes':    nodes,
        'edges':    edges,
        'insights': insights,
        'filters':  {
            'mode':          mode,
            'internal_only': internal_only,
            'min_papers':    min_papers,
            'year_from':     year_from,
            'year_to':       year_to,
        },
    })
