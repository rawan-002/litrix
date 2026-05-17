"""
Fix orphan papers - v2 (multi-pass).

Passes:
    1. ORCID match              (confidence 1.00)
    2. OpenAlex Author ID match (confidence 0.95)
    3. Scholar citation_id      (confidence 1.00)
    4. Name + Al-Baha affil. uniqueness gate (confidence 0.85)

Usage:
    python fix_orphan_papers_v2.py --dry-run
    python fix_orphan_papers_v2.py
"""
import os, sys, argparse
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction


ALBAHA_AR = "الباحة"  # uses unicode escapes only


def orphan_count(cur):
    cur.execute(
        'SELECT COUNT(*) FROM "ResearchPaper" rp WHERE NOT EXISTS ('
        '  SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")'
    )
    return cur.fetchone()[0]


# --- PASS 1: ORCID match (via Researcher.ORCID_ID) -------------------------
# NOTE: The Litrix ORCIDs live in "Researcher"."ORCID_ID" (39 active values),
# NOT in "Users"."Orcid_ID" (which is mostly empty). The previous version of
# this pass joined on Users.Orcid_ID and missed all 39 valid mappings.
# This version joins on Researcher.ORCID_ID and links co-authored papers
# wherever a paper's authorships list a researcher's verified ORCID.
PASS1_SQL = r"""
INSERT INTO "Authors" (
    "UserID","PaperID","AuthorOrder","IsCorrespondingAuthor",
    "MappingConfidence","MappingCriteria","AuthorNameRaw","Is_Verified"
)
SELECT r."UserID", rp."PaperID", NULL, FALSE, 1.0,
       'orcid_backfill',
       LEFT(ship->'author'->>'display_name', 255), TRUE
FROM "ResearchPaper" rp
CROSS JOIN LATERAL jsonb_array_elements(rp."RawData_Log"->'authorships') AS ship
JOIN "Researcher" r
  ON r."ORCID_ID" IS NOT NULL
 AND r."ORCID_ID" = REPLACE(
       REPLACE(ship->'author'->>'orcid', 'https://orcid.org/', ''),
       'http://orcid.org/', ''
     )
WHERE ship->'author'->>'orcid' IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM "Authors" a
      WHERE a."PaperID" = rp."PaperID" AND a."UserID" = r."UserID"
  )
ON CONFLICT ("UserID","PaperID") DO NOTHING
"""


# --- PASS 2: OpenAlex Author ID match --------------------------------------
PASS2_SQL = r"""
INSERT INTO "Authors" (
    "UserID","PaperID","AuthorOrder","IsCorrespondingAuthor",
    "MappingConfidence","MappingCriteria","AuthorNameRaw","Is_Verified"
)
SELECT r."UserID", rp."PaperID", NULL, FALSE, 0.95,
       'openalex_id_backfill',
       LEFT(ship->'author'->>'display_name', 255), TRUE
FROM "ResearchPaper" rp
CROSS JOIN LATERAL jsonb_array_elements(rp."RawData_Log"->'authorships') AS ship
JOIN "Researcher" r
  ON r."OpenAlex_AuthorID" IS NOT NULL
 AND r."OpenAlex_AuthorID" = REPLACE(ship->'author'->>'id', 'https://openalex.org/', '')
WHERE ship->'author'->>'id' IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM "Authors" a
      WHERE a."PaperID" = rp."PaperID" AND a."UserID" = r."UserID"
  )
ON CONFLICT ("UserID","PaperID") DO NOTHING
"""


# --- PASS 3: Scholar citation_id match -------------------------------------
PASS3_SQL = r"""
INSERT INTO "Authors" (
    "UserID","PaperID","AuthorOrder","IsCorrespondingAuthor",
    "MappingConfidence","MappingCriteria","AuthorNameRaw","Is_Verified"
)
SELECT u."UserID", rp."PaperID", NULL, FALSE, 1.0,
       'scholar_id_backfill',
       LEFT(rp."RawData_Log"->>'authors', 255), TRUE
FROM "ResearchPaper" rp
JOIN "Users" u
  ON u."Scholar_ID" IS NOT NULL
 AND u."Scholar_ID" = rp."RawData_Log"->>'citation_id'
WHERE NOT EXISTS (
    SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID"
)
ON CONFLICT ("UserID","PaperID") DO NOTHING
"""


# --- PASS 4: Name + Al-Baha affiliation + uniqueness gate ------------------
# We build the SQL as a template and inject the Arabic word from a
# python variable to avoid any encoding issues at file-write time.
PASS4_SQL_TEMPLATE = r"""
WITH norm AS (
    SELECT
        rp."PaperID" AS paper_id,
        t.ship_idx   AS ship_idx,
        t.ship       AS ship,
        LEFT(t.ship->'author'->>'display_name', 255) AS display_name,
        LOWER(REGEXP_REPLACE(
            COALESCE(t.ship->'author'->>'display_name',''),
            '[._,-]', ' ', 'g'
        )) AS norm_scraped
    FROM "ResearchPaper" rp
    CROSS JOIN LATERAL jsonb_array_elements(rp."RawData_Log"->'authorships')
        WITH ORDINALITY AS t(ship, ship_idx)
),
norm_with_affil AS (
    SELECT n.*,
        (
            COALESCE((
                SELECT string_agg(x, ' | ')
                FROM jsonb_array_elements_text(
                    COALESCE(n.ship->'raw_affiliation_strings','[]'::jsonb)
                ) AS x
            ),'')
            || ' | ' ||
            COALESCE((
                SELECT string_agg(inst->>'display_name', ' | ')
                FROM jsonb_array_elements(
                    COALESCE(n.ship->'institutions','[]'::jsonb)
                ) AS inst
            ),'')
            || ' | ' ||
            COALESCE(n.ship->>'raw_affiliation_string','')
        ) AS affil_text
    FROM norm n
),
candidates AS (
    SELECT
        nw.paper_id, nw.ship_idx, nw.display_name,
        u."UserID" AS candidate_uid
    FROM norm_with_affil nw
    JOIN "Users" u
      ON u."UserType" = 'Researcher'
     AND u."AccountStatus" = 'Active'
     AND (
            LOWER(REGEXP_REPLACE(
                CONCAT(u."FirstName",' ',u."LastName"),
                '[._,-]',' ','g'
            )) = nw.norm_scraped
         OR LOWER(REGEXP_REPLACE(
                COALESCE(u."FullName_Ar",''),
                '[._,-]',' ','g'
            )) = nw.norm_scraped
        )
    WHERE nw.affil_text ~* '(al[ -]?baha|albaha|__ARABIC_BAHA__)'
      AND NOT EXISTS (
          SELECT 1 FROM "Authors" a
          WHERE a."PaperID" = nw.paper_id AND a."UserID" = u."UserID"
      )
),
unique_matches AS (
    SELECT paper_id, ship_idx, display_name, candidate_uid
    FROM (
        SELECT *, COUNT(*) OVER (PARTITION BY paper_id, ship_idx) AS n_cands
        FROM candidates
    ) c
    WHERE n_cands = 1
)
INSERT INTO "Authors" (
    "UserID","PaperID","AuthorOrder","IsCorrespondingAuthor",
    "MappingConfidence","MappingCriteria","AuthorNameRaw","Is_Verified"
)
SELECT um.candidate_uid, um.paper_id, NULL, FALSE, 0.85,
       'name_albaha_backfill', um.display_name, TRUE
FROM unique_matches um
ON CONFLICT ("UserID","PaperID") DO NOTHING
"""

PASS4_SQL = PASS4_SQL_TEMPLATE.replace("__ARABIC_BAHA__", ALBAHA_AR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with transaction.atomic():
        with connection.cursor() as cur:
            before = orphan_count(cur)
            print("Orphans before:", before)

            cur.execute(PASS1_SQL)
            print("  PASS 1 (ORCID match)              -> linked", cur.rowcount)
            cur.execute(PASS2_SQL)
            print("  PASS 2 (OpenAlex Author ID)       -> linked", cur.rowcount)
            cur.execute(PASS3_SQL)
            print("  PASS 3 (Scholar citation_id)      -> linked", cur.rowcount)
            cur.execute(PASS4_SQL)
            print("  PASS 4 (Al-Baha + unique name)    -> linked", cur.rowcount)

            after = orphan_count(cur)
            print("Orphans after :", after, " (fixed", before - after, ")")

            cur.execute(
                'SELECT COALESCE(rp."Source",\'(null)\'), COUNT(*) '
                'FROM "ResearchPaper" rp '
                'WHERE NOT EXISTS ('
                '  SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID") '
                'GROUP BY rp."Source" ORDER BY 2 DESC'
            )
            print("\nRemaining orphans by Source:")
            for src, n in cur.fetchall():
                print(" ", src, n)

            if args.dry_run:
                transaction.set_rollback(True)
                print("\n--dry-run: rolled back. Nothing written.")
            else:
                print("\nDone. Changes committed.")


if __name__ == "__main__":
    main()
