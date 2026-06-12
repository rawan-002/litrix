"""
Scopus-based Author Attribution Fix (One-shot Data Repair).

=============================================================================
WHY THIS EXISTS
=============================================================================
For 7 specific researchers, the Scholar-based attribution pipeline produced
"contaminated" results — papers from OTHER institutions (Pakistani Bahawalpur,
Indonesian Sriwijaya, etc.) got mis-attributed to Al-Baha researchers because
Scholar's name matching is fuzzy and doesn't enforce affiliation.

Concrete evidence: Rahmat Budiarto has 264 papers in his Scopus profile,
but only 159 with Al-Baha affiliation. The other 105 are from his earlier
career at Sriwijaya/UTM and should NOT be counted in Al-Baha's metrics.

NOTE: This is intentionally separate from `scrapers/scopus.py`. That file
uses OpenAlex as an indirect Scopus query; this one uses curated Excel
exports as the canonical ground truth for a one-shot repair pass.

=============================================================================
ARCHITECTURE (LOGIC-FIRST)
=============================================================================
1. Scopus Author ID is a *deterministic* identifier issued by Scopus —
   far more reliable than name-based matching. We use it as the canonical
   linking key between Researcher and Paper.

2. Al-Baha University's Scopus Affiliation ID is verified as 60104698.
   Papers without this ID in their `Scopus Affiliation IDs` field are
   from the researcher's OTHER institutions and must be excluded.

3. Process is IDEMPOTENT — safe to re-run. Existing DOIs are upserted,
   not duplicated.

4. Every state change is logged to a timestamped JSON audit file.

5. Transactional — uses Postgres BEGIN/COMMIT for all-or-nothing safety.
   Any error rolls back the entire run.

=============================================================================
USAGE
=============================================================================
    # Read-only pre-flight report (SAFE, no DB writes) — RUN THIS FIRST
    python backend/scopus_attribution_fix.py --dry-run

    # Apply changes (transactional) — only after dry-run looks correct
    python backend/scopus_attribution_fix.py --apply

    # Single researcher only (useful for testing one at a time)
    python backend/scopus_attribution_fix.py --user-id 89 --dry-run

=============================================================================
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# UTF-8 stdout for Arabic — matches convention used in manual.py
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

# Silence openpyxl's "no default style" noise — irrelevant for reading
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# Load .env from project root (one directory up from backend/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# =============================================================================
# CONFIGURATION — the canonical mappings for this one-shot fix
# =============================================================================

# Al-Baha University's Scopus Affiliation ID. VERIFIED from BU-only files
# (appeared 185× in the filtered exports vs 3× for Bahawalpur and 1× for
# Albaha Private College — those are false positives Scopus's name search
# accidentally included). All Scopus-imported papers MUST contain this ID
# in their `Scopus Affiliation IDs` column.
ALBAHA_SCOPUS_AFFILIATION_ID = "60104698"

# Mapping: Scopus filename → (Litrix UserID, Scopus Author ID, Arabic name).
# UserIDs were discovered by joining Users + Researcher tables and matching
# on FullName_Ar. Scopus Author IDs were extracted as the most-frequent
# author ID across each researcher's papers (90-99% coverage = high confidence).
RESEARCHERS: dict[str, dict[str, Any]] = {
    "Publications_by_Alzahrani,_Nouf_Matar_1996_-_2026.xlsx": {
        "user_id": 106,
        "scopus_author_id": "57191341075",
        "arabic_name": "نوف مطر مسفر الزهراني",
    },
    "Publications_by_Saleem,_Muhammad_Qaiser_1996_2026_BU_only.xlsx": {
        "user_id": 89,
        "scopus_author_id": "57209136547",
        "arabic_name": "محمد قيصر سليم",
    },
    "Publications_by_Alshehri,_Abdullah_1996_-_2026.xlsx": {
        "user_id": 8,
        "scopus_author_id": "57210352986",
        "arabic_name": "عبدالله احمد عبدالله الشهري",
    },
    "Publications_by_Budiarto,_Rahmat_1996_-_2026 (BU only).xlsx": {
        "user_id": 6,
        "scopus_author_id": "58131692700",
        "arabic_name": "رحمات بوديارتو",
    },
    "Publications_by_Alzahrani,_Mohammed_Yahya_1996_-_2026.xlsx": {
        "user_id": 93,
        "scopus_author_id": "56125509600",
        "arabic_name": "محمد يحيى مرضي الزهراني",
    },
    "Publications_by_Alghamdi,_Mohammed_Yahya_1996_-_2026.xlsx": {
        "user_id": 92,
        "scopus_author_id": "57220804430",
        "arabic_name": "محمد يحيى عبدالخالق آل بنه الغامدي",
    },
    "Publications_by_Alghamdi,_Mohammed_I._1996_-_2026.xlsx": {
        "user_id": 81,
        "scopus_author_id": "57761024200",
        "arabic_name": "محمد ابراهيم يعن الله آل مشلح",
    },
}

# Default location of the Scopus exports.
# Override via SCOPUS_UPLOADS_DIR env var when running elsewhere.
DEFAULT_UPLOADS_DIR = PROJECT_ROOT / "data" / "scopus"

# Where to write the audit log. Timestamped so reruns don't overwrite.
AUDIT_LOG_DIR = PROJECT_ROOT / "data" / "scopus_audit"


# =============================================================================
# SCOPUS EXCEL PARSING
# =============================================================================

def find_header_row(filepath: Path) -> int:
    """Scopus prepends 18-19 metadata rows before the actual table header.

    We can't hardcode the offset because Scopus occasionally changes the
    preamble length between export tool versions. Instead we scan the first
    30 rows for the row that contains both 'Title' and 'Authors'.
    """
    raw = pd.read_excel(filepath, header=None, nrows=30)
    for i in range(len(raw)):
        row_values = raw.iloc[i].astype(str).tolist()
        if 'Title' in row_values and 'Authors' in row_values:
            return i
    raise ValueError(
        f"Could not locate header row in {filepath.name}. "
        f"Expected a row containing both 'Title' and 'Authors' in the first 30 rows."
    )


def load_scopus_file(filepath: Path) -> pd.DataFrame:
    """Load one Scopus export and return clean DataFrame of publications.

    Filters out:
        • Pre-table metadata rows (handled by find_header_row offset).
        • Post-table footer rows like Scopus's Elsevier copyright notice
          (which has a Title-shaped string but no numeric Year). We
          require Year to be numeric to weed those out.
    """
    header_row = find_header_row(filepath)
    df = pd.read_excel(filepath, header=header_row)
    # Drop rows lacking a Title (early/late blanks)
    df = df.dropna(subset=['Title'])
    # Require numeric Year — kills the Elsevier copyright footer row
    # which has a string "© 2026 Elsevier..." in Title but Year=NaN.
    if 'Year' in df.columns:
        df = df[pd.to_numeric(df['Year'], errors='coerce').notna()]
    return df.reset_index(drop=True)


def filter_by_albaha_affiliation(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split DataFrame into (kept, dropped) based on AlBaha Scopus Affiliation ID.

    A paper is KEPT if its `Scopus Affiliation IDs` cell contains the
    AlBaha Affiliation ID (60104698). Otherwise it's DROPPED — meaning
    the researcher published this with a different institution and it
    shouldn't count toward Al-Baha's analytics.
    """
    if 'Scopus Affiliation IDs' not in df.columns:
        # No affiliation column — keep everything but log this oddity
        return df, df.iloc[0:0]

    def has_albaha(cell: Any) -> bool:
        if pd.isna(cell):
            return False
        ids = [x.strip() for x in str(cell).split('|')]
        return ALBAHA_SCOPUS_AFFILIATION_ID in ids

    mask = df['Scopus Affiliation IDs'].apply(has_albaha)
    return (
        df[mask].reset_index(drop=True),
        df[~mask].reset_index(drop=True),
    )


def extract_dois(df: pd.DataFrame) -> set[str]:
    """Normalized DOI set for clean set arithmetic vs. the DB."""
    if 'DOI' not in df.columns:
        return set()
    dois = df['DOI'].dropna().astype(str).str.lower().str.strip()
    return {d for d in dois if d and d != 'nan'}


# =============================================================================
# SCOPUS ROW → DB FIELD MAPPING
# =============================================================================

def quartile_from_percentile(percentile: Any) -> str | None:
    """Derive Q1-Q4 from a 0-100 percentile.

    Scopus exports the journal's SJR percentile in its primary field.
    Top 25% → Q1, next 25% → Q2, etc. Returns None for missing/invalid data.
    """
    try:
        p = float(percentile)
    except (TypeError, ValueError):
        return None
    if pd.isna(p):
        return None
    if p >= 75: return 'Q1'
    if p >= 50: return 'Q2'
    if p >= 25: return 'Q3'
    return 'Q4'


def safe_str(val: Any, max_len: int | None = None) -> str | None:
    """Normalize a cell value to a clean string (None for NaN/empty)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() == 'nan':
        return None
    return s[:max_len] if max_len else s


def safe_int(val: Any) -> int | None:
    """Normalize to int (None for NaN/empty)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def safe_float(val: Any) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def parse_authors_field(authors_str: str) -> list[str]:
    """Scopus joins author names with ' | ' (pipe + spaces)."""
    if not authors_str:
        return []
    return [a.strip() for a in authors_str.split('|') if a.strip()]


def parse_scopus_author_ids(ids_str: str) -> list[str]:
    """Scopus joins author IDs with ' | ' — same separator as names."""
    if not ids_str:
        return []
    return [i.strip() for i in ids_str.split('|') if i.strip()]


# =============================================================================
# DATABASE INSPECTION (READ-ONLY)
# =============================================================================

# Shared DB helper (single source — litrix_db.py lives in backend/).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db as db_connect


def fetch_researcher_current_state(conn, user_id: int) -> dict[str, Any]:
    """Snapshot of a researcher's current attribution state in Litrix.

    Returns:
        {
            'meta':   { UserID, FullName_Ar, Scholar_ID, Scopus_ID, ... },
            'papers': [ {PaperID, DOI, Title, MappingCriteria, ...}, ... ],
        }
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            '''
            SELECT u."UserID", u."FullName_Ar", u."Email",
                   u."Scholar_ID",
                   r."Scopus_ID", r."ORCID_ID", r."AcademicRank"
            FROM "Users" u
            LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
            WHERE u."UserID" = %s
            ''',
            (user_id,),
        )
        meta = cur.fetchone() or {}

        cur.execute(
            '''
            SELECT a."PaperID", a."MappingCriteria", a."MappingConfidence",
                   a."Is_Verified", a."AuthorNameRaw",
                   rp."DOI", rp."Title", rp."PubYear", rp."Source"
            FROM "Authors" a
            JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
            WHERE a."UserID" = %s
            ORDER BY rp."PubYear" DESC NULLS LAST, rp."PaperID"
            ''',
            (user_id,),
        )
        papers = cur.fetchall() or []

    return {"meta": dict(meta), "papers": [dict(p) for p in papers]}


# =============================================================================
# UPSERT HELPERS — used by --apply mode
# =============================================================================

def upsert_journal(cur, row: pd.Series) -> int | None:
    """Find or create the Journal row for this paper. Returns JournalID or None.

    Lookup priority:
        1. ISSN (Print or Online) — deterministic, prefer this.
        2. JournalName (case-insensitive exact) — fallback because the
           Journals table has a UNIQUE constraint on JournalName that the
           information_schema dump didn't show. Without this fallback,
           every paper whose journal already exists without an ISSN row
           triggers a UniqueViolation on INSERT.
        3. INSERT a new row.

    Also: if we find an existing row by name and Scopus gives us an ISSN
    the existing row lacks, we patch it in. Same for Publisher.
    """
    issn       = safe_str(row.get('ISSN'), 30)
    journal_nm = safe_str(row.get('Scopus Source title'), 500)
    publisher  = safe_str(row.get('Publisher'), 300)
    # VenueType is varchar(20) in the schema. Scopus values like
    # "Conference Proceeding" (21 chars) overflow, so truncate to 20.
    venue_type = safe_str(row.get('Source type'), 20)

    if not journal_nm and not issn:
        return None  # Nothing useful to store

    # 1. ISSN match (deterministic, preferred)
    if issn:
        cur.execute(
            '''
            SELECT "JournalID" FROM "Journals"
            WHERE "ISSN_Print" = %s OR "ISSN_Online" = %s
            LIMIT 1
            ''',
            (issn, issn),
        )
        hit = cur.fetchone()
        if hit:
            return hit[0] if not isinstance(hit, dict) else hit["JournalID"]

    # 2. JournalName fallback (case-insensitive) — required to honor the
    #    UNIQUE constraint on JournalName.
    if journal_nm:
        cur.execute(
            '''
            SELECT "JournalID", "ISSN_Print", "ISSN_Online", "Publisher"
            FROM "Journals"
            WHERE LOWER("JournalName") = LOWER(%s)
            LIMIT 1
            ''',
            (journal_nm,),
        )
        hit = cur.fetchone()
        if hit:
            jid = hit[0] if not isinstance(hit, dict) else hit["JournalID"]
            existing_issn_print = (hit.get("ISSN_Print") if isinstance(hit, dict)
                                   else hit[1])
            existing_issn_online = (hit.get("ISSN_Online") if isinstance(hit, dict)
                                    else hit[2])
            existing_publisher = (hit.get("Publisher") if isinstance(hit, dict)
                                  else hit[3])
            # Backfill missing fields opportunistically — no destructive update.
            if issn and not existing_issn_print and not existing_issn_online:
                cur.execute(
                    'UPDATE "Journals" SET "ISSN_Print" = %s WHERE "JournalID" = %s',
                    (issn, jid),
                )
            if publisher and not existing_publisher:
                cur.execute(
                    'UPDATE "Journals" SET "Publisher" = %s WHERE "JournalID" = %s',
                    (publisher, jid),
                )
            return jid

    # 3. Truly new — INSERT.
    cur.execute(
        '''
        INSERT INTO "Journals" ("JournalName", "ISSN_Print", "Publisher", "VenueType")
        VALUES (%s, %s, %s, %s)
        RETURNING "JournalID"
        ''',
        (journal_nm or '(unknown)', issn, publisher, venue_type),
    )
    return cur.fetchone()["JournalID"]


def upsert_journal_ranking(cur, journal_id: int | None, row: pd.Series) -> int | None:
    """Refresh (or create) the ranking row for this paper's journal.

    The JournalRankings table has a UNIQUE constraint on `Issn` (single
    column, NOT on the composite key we'd expect). So our lookup strategy:

      1. If Scopus gave us an ISSN, find any existing row with that ISSN
         and UPDATE it. This satisfies the UNIQUE constraint while keeping
         the ranking data fresh.
      2. Else, find by (JournalID, RankingYear, Source='Scopus') and UPDATE.
      3. Else, INSERT a new row.

    Note: Scopus ISSN values come pre-formatted with the "ISSN-" prefix
    (e.g. "ISSN-15462218"), matching the existing convention in Litrix DB.
    """
    if not journal_id:
        return None

    pub_year = safe_int(row.get('Year'))
    if not pub_year:
        return None

    quartile = quartile_from_percentile(row.get('SJR percentile (publication year) *'))
    if not quartile:
        quartile = quartile_from_percentile(
            row.get('CiteScore percentile (publication year) *')
        )
    impact_factor = safe_float(row.get('CiteScore (publication year)'))
    issn = safe_str(row.get('ISSN'), 30)
    category = safe_str(
        row.get('All Science Journal Classification (ASJC) field name'),
        500,
    )

    # Policy: Scimago is the project's canonical source for Quartile rankings
    # (see classification/scimago_import.py). Scopus should NEVER overwrite
    # Scimago rows. We only fill gaps OR refresh our own previous Scopus rows.

    # 1. (JournalID, RankingYear) lookup — this is the strict UNIQUE constraint.
    cur.execute(
        '''
        SELECT "RankingID", "Source"
        FROM "JournalRankings"
        WHERE "JournalID" = %s AND "RankingYear" = %s
        LIMIT 1
        ''',
        (journal_id, pub_year),
    )
    hit = cur.fetchone()
    if hit:
        rid = hit["RankingID"] if isinstance(hit, dict) else hit[0]
        existing_source = hit["Source"] if isinstance(hit, dict) else hit[1]
        if existing_source == 'Scopus':
            # Our own row — refresh, but use COALESCE so we don't blank fields.
            cur.execute(
                '''
                UPDATE "JournalRankings" SET
                    "Quartile"     = COALESCE(%s, "Quartile"),
                    "ImpactFactor" = COALESCE(%s, "ImpactFactor"),
                    "Issn"         = COALESCE(%s, "Issn"),
                    "Category"     = COALESCE(%s, "Category")
                WHERE "RankingID" = %s
                ''',
                (quartile, impact_factor, issn, category, rid),
            )
        # else: Scimago (or other) wins for this journal-year. Leave it alone.
        return rid

    # 2. No (JournalID, RankingYear) match. Before INSERT, check ISSN to honor
    #    the second UNIQUE constraint (unique_rank_issn). If an ISSN match
    #    exists but for a different year/journal, we can't INSERT — skip
    #    silently so the paper still imports.
    if issn:
        cur.execute(
            'SELECT "RankingID", "Source" FROM "JournalRankings" '
            'WHERE "Issn" = %s LIMIT 1',
            (issn,),
        )
        hit = cur.fetchone()
        if hit:
            # Existing ISSN row but different (JournalID, RankingYear). Can't
            # INSERT and shouldn't UPDATE (it belongs to a different journal/year
            # combo). Just return the row — paper still gets linked to its
            # Journal correctly via journal_id, ranking data just isn't refreshed.
            return hit["RankingID"] if isinstance(hit, dict) else hit[0]

    # 3. Truly new — INSERT.
    cur.execute(
        '''
        INSERT INTO "JournalRankings"
            ("JournalID", "RankingYear", "Source", "Quartile",
             "ImpactFactor", "Issn", "Category")
        VALUES (%s, %s, 'Scopus', %s, %s, %s, %s)
        RETURNING "RankingID"
        ''',
        (journal_id, pub_year, quartile, impact_factor, issn, category),
    )
    return cur.fetchone()["RankingID"]


def upsert_research_paper(cur, row: pd.Series, journal_id: int | None) -> tuple[int, str]:
    """Find-or-create the ResearchPaper row by DOI.

    Returns (PaperID, action) where action ∈ {'inserted', 'updated', 'found'}.
    We always REFRESH the metadata for found papers — Scopus is more
    authoritative than Scholar for journal/year/abstract details.
    """
    doi = safe_str(row.get('DOI'), 100)
    if not doi:
        raise ValueError("upsert_research_paper called without DOI")

    title    = safe_str(row.get('Title')) or '(no title)'
    abstract = safe_str(row.get('Abstract'))
    pub_year = safe_int(row.get('Year'))
    volume   = safe_str(row.get('Volume'), 50)
    issue    = safe_str(row.get('Issue'), 50)
    pages    = safe_str(row.get('Pages'), 50)
    language = safe_str(row.get('Language'), 10)

    # Serialize the full Scopus row as a JSON dict for audit/trace
    raw_log = {
        k: (None if (isinstance(v, float) and pd.isna(v)) else v)
        for k, v in row.to_dict().items()
    }

    # Lookup priority for upsert:
    #   1. DOI (case-insensitive exact) — preferred deterministic key
    #   2. Title (case-insensitive exact) — required because the schema has
    #      a UNIQUE constraint on Title. A paper might already be in the
    #      DB under a different DOI (e.g. preprint vs final, or DOI missing)
    #      but the same title. If we INSERT we'd violate the constraint;
    #      if we UPDATE the existing row we patch in the canonical DOI.
    #   3. INSERT fresh
    #
    # We keep Title verbatim from Scopus on UPDATE — IT'S the canonical version.
    # If the DB had a slightly different title (case, punctuation), we overwrite
    # to the Scopus form so the next dedup lookup is stable.

    # 1. DOI match
    cur.execute(
        'SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("DOI") = LOWER(%s) LIMIT 1',
        (doi,),
    )
    hit = cur.fetchone()
    if not hit:
        # 2. Title fallback (case-insensitive exact). Honors the UNIQUE
        #    constraint on Title and avoids creating duplicate papers
        #    when the existing row lacks a DOI or has a different one.
        cur.execute(
            'SELECT "PaperID", "DOI" FROM "ResearchPaper" '
            'WHERE LOWER("Title") = LOWER(%s) LIMIT 1',
            (title,),
        )
        title_hit = cur.fetchone()
        if title_hit:
            paper_id = (title_hit["PaperID"] if isinstance(title_hit, dict)
                        else title_hit[0])
            existing_doi = (title_hit.get("DOI") if isinstance(title_hit, dict)
                            else title_hit[1])
            # If the existing row has no DOI, backfill ours; otherwise leave it
            # alone (the existing DOI is a different valid identifier we
            # shouldn't overwrite).
            # NOTE: we deliberately do NOT update "Title" here. The Title is
            # the identifier we matched on; rewriting it to the Scopus form
            # can collide with another existing row that has the canonical
            # title (researchpaper_title_unique would fire). Keeping the
            # existing Title is safe — same paper, same identity.
            cur.execute(
                '''
                UPDATE "ResearchPaper" SET
                    "Abstract"     = COALESCE(%s, "Abstract"),
                    "JournalID"    = COALESCE(%s, "JournalID"),
                    "PubYear"      = COALESCE(%s, "PubYear"),
                    "Volume"       = COALESCE(%s, "Volume"),
                    "Issue"        = COALESCE(%s, "Issue"),
                    "Pages"        = COALESCE(%s, "Pages"),
                    "Language"     = COALESCE(%s, "Language"),
                    "DOI"          = COALESCE("DOI", %s),
                    "RawData_Log"  = %s,
                    "ScrapedAt"    = NOW(),
                    "Source"       = 'Scopus'
                WHERE "PaperID" = %s
                ''',
                (
                    abstract, journal_id, pub_year,
                    volume, issue, pages, language, doi,
                    json.dumps(raw_log, default=str, ensure_ascii=False),
                    paper_id,
                ),
            )
            return paper_id, 'updated'

    if hit:
        paper_id = hit[0] if not isinstance(hit, dict) else hit["PaperID"]
        # Same reason as above: don't touch Title on UPDATE — it can collide
        # with another row's Title and break the UNIQUE constraint.
        cur.execute(
            '''
            UPDATE "ResearchPaper" SET
                "Abstract"     = COALESCE(%s, "Abstract"),
                "JournalID"    = COALESCE(%s, "JournalID"),
                "PubYear"      = COALESCE(%s, "PubYear"),
                "Volume"       = COALESCE(%s, "Volume"),
                "Issue"        = COALESCE(%s, "Issue"),
                "Pages"        = COALESCE(%s, "Pages"),
                "Language"     = COALESCE(%s, "Language"),
                "RawData_Log"  = %s,
                "ScrapedAt"    = NOW(),
                "Source"       = 'Scopus'
            WHERE "PaperID" = %s
            ''',
            (
                abstract, journal_id, pub_year,
                volume, issue, pages, language,
                json.dumps(raw_log, default=str, ensure_ascii=False),
                paper_id,
            ),
        )
        return paper_id, 'updated'

    # INSERT with SAVEPOINT fallback — bulletproof against the
    # `researchpaper_title_unique` constraint, which may be defined as an
    # expression index (e.g. LOWER("Title")) that ON CONFLICT ("Title")
    # doesn't match. The SAVEPOINT lets us recover from the UniqueViolation
    # inside the same transaction and find the existing row by title.
    # We catch the broad psycopg2.IntegrityError (parent of UniqueViolation)
    # and inspect SQLSTATE to be sure it's a unique violation (23505).
    # Catching the specific psycopg2.errors.UniqueViolation has proven flaky
    # across psycopg2-binary builds / Neon's pooler — IntegrityError is the
    # universal parent class and always available.
    savepoint = "sp_insert_paper"
    cur.execute(f'SAVEPOINT {savepoint}')
    try:
        cur.execute(
            '''
            INSERT INTO "ResearchPaper"
                ("Title", "Abstract", "JournalID", "DOI", "PubYear",
                 "Volume", "Issue", "Pages", "Language",
                 "RawData_Log", "ScrapedAt", "Source")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'Scopus')
            RETURNING "PaperID"
            ''',
            (
                title, abstract, journal_id, doi, pub_year,
                volume, issue, pages, language,
                json.dumps(raw_log, default=str, ensure_ascii=False),
            ),
        )
        # Critical ordering: fetch BEFORE releasing the savepoint.
        # RELEASE SAVEPOINT clears the cursor's pending result rows,
        # so calling fetchone() after it raises ProgrammingError.
        inserted = cur.fetchone()
        cur.execute(f'RELEASE SAVEPOINT {savepoint}')
        return inserted["PaperID"], 'inserted'
    except Exception as e:
        # DEBUG: print what we actually caught — temporary diagnostic.
        pgcode = getattr(e, 'pgcode', None)
        diag_sqlstate = getattr(getattr(e, 'diag', None), 'sqlstate', None)
        print(f"  [upsert_research_paper] caught {type(e).__module__}.{type(e).__name__} "
              f"pgcode={pgcode!r} diag.sqlstate={diag_sqlstate!r}")
        # Always attempt recovery — the SELECT will tell us if the row
        # actually exists. If it doesn't (truly different kind of error),
        # we re-raise from the lookup-failed branch below.
        # Title conflict — roll back the INSERT attempt, then find the
        # existing row and UPDATE it. Use case-insensitive LOWER match
        # AND fall back to plain Title= match in case the index normalizes
        # whitespace differently.
        cur.execute(f'ROLLBACK TO SAVEPOINT {savepoint}')
        cur.execute(
            '''
            SELECT "PaperID", "DOI" FROM "ResearchPaper"
            WHERE LOWER(TRIM("Title")) = LOWER(TRIM(%s))
               OR "Title" = %s
            LIMIT 1
            ''',
            (title, title),
        )
        title_hit = cur.fetchone()
        if not title_hit:
            # The constraint matched but our lookup didn't — this should
            # be impossible unless the index uses a weirder normalization.
            # Bail loudly rather than silently lose the paper.
            raise RuntimeError(
                f"Title constraint hit but lookup failed. Title='{title[:60]}…'. "
                f"Check pg_indexes for researchpaper_title_unique's actual expression."
            )
        paper_id = (title_hit["PaperID"] if isinstance(title_hit, dict)
                    else title_hit[0])
        cur.execute(
            '''
            UPDATE "ResearchPaper" SET
                "Abstract"    = COALESCE(%s, "Abstract"),
                "JournalID"   = COALESCE(%s, "JournalID"),
                "PubYear"     = COALESCE(%s, "PubYear"),
                "Volume"      = COALESCE(%s, "Volume"),
                "Issue"       = COALESCE(%s, "Issue"),
                "Pages"       = COALESCE(%s, "Pages"),
                "Language"    = COALESCE(%s, "Language"),
                "DOI"         = COALESCE("DOI", %s),
                "RawData_Log" = %s,
                "ScrapedAt"   = NOW(),
                "Source"      = 'Scopus'
            WHERE "PaperID" = %s
            ''',
            (
                abstract, journal_id, pub_year,
                volume, issue, pages, language, doi,
                json.dumps(raw_log, default=str, ensure_ascii=False),
                paper_id,
            ),
        )
        return paper_id, 'updated'


def upsert_author_link(
    cur,
    user_id: int,
    paper_id: int,
    author_name_raw: str | None,
    author_order: int | None,
    is_corresponding: bool,
) -> str:
    """Find-or-create the Authors row for (UserID, PaperID).

    Always stamps MappingCriteria='scopus_author_id', MappingConfidence=1.0,
    Is_Verified=true — this is the canonical link from Scopus's deterministic
    Author ID, which is the highest-trust signal the schema supports.
    """
    cur.execute(
        '''
        SELECT "AuthorLinkID" FROM "Authors"
        WHERE "UserID" = %s AND "PaperID" = %s
        LIMIT 1
        ''',
        (user_id, paper_id),
    )
    hit = cur.fetchone()
    if hit:
        cur.execute(
            '''
            UPDATE "Authors" SET
                "AuthorOrder"           = COALESCE(%s, "AuthorOrder"),
                "IsCorrespondingAuthor" = %s,
                "MappingCriteria"       = 'scopus_author_id',
                "MappingConfidence"     = 1.0,
                "Is_Verified"           = TRUE,
                "AuthorNameRaw"         = COALESCE(%s, "AuthorNameRaw")
            WHERE "UserID" = %s AND "PaperID" = %s
            ''',
            (author_order, is_corresponding, author_name_raw, user_id, paper_id),
        )
        return 'updated'

    cur.execute(
        '''
        INSERT INTO "Authors"
            ("UserID", "PaperID", "AuthorOrder", "IsCorrespondingAuthor",
             "MappingCriteria", "MappingConfidence", "Is_Verified", "AuthorNameRaw")
        VALUES (%s, %s, %s, %s, 'scopus_author_id', 1.0, TRUE, %s)
        ''',
        (user_id, paper_id, author_order, is_corresponding, author_name_raw),
    )
    return 'inserted'


def replace_external_authors(
    cur,
    paper_id: int,
    row: pd.Series,
    skip_scopus_id: str,
) -> int:
    """Refresh ExternalAuthors for this paper: drop+rewrite the co-author list.

    We replace rather than upsert because co-author ordering matters and
    Scopus is canonical for it. Skip the row for the researcher we just
    linked via Authors (skip_scopus_id) — that one's an internal author,
    not external.
    """
    authors_str = safe_str(row.get('Authors')) or ''
    ids_str     = safe_str(row.get('Scopus Author Ids')) or ''
    affs_str    = safe_str(row.get('Institutions')) or ''

    names = parse_authors_field(authors_str)
    ids   = parse_scopus_author_ids(ids_str)

    # Wipe existing externals for this paper (idempotency)
    cur.execute('DELETE FROM "ExternalAuthors" WHERE "PaperID" = %s', (paper_id,))

    if not names or not ids or len(names) != len(ids):
        # Lists don't align — skip rather than guess
        return 0

    inserted = 0
    seen_names: set[str] = set()
    for name, sid in zip(names, ids):
        if sid == skip_scopus_id:
            continue  # internal author, already in Authors table
        clean_name = name[:255]
        # Two-level dedup: in-loop (same name appearing twice in this paper's
        # author list) + DB-level via ON CONFLICT DO NOTHING (catches anything
        # the loop dedup misses). The (FullName, PaperID) constraint —
        # uq_external_author_paper — is on raw FullName, so identical name
        # strings collide even if they represent different real people.
        if clean_name in seen_names:
            continue
        seen_names.add(clean_name)
        cur.execute(
            '''
            INSERT INTO "ExternalAuthors" ("FullName", "Affiliation", "PaperID")
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            ''',
            (clean_name, affs_str[:255] if affs_str else None, paper_id),
        )
        if cur.rowcount > 0:
            inserted += 1
    return inserted


def cleanup_old_attributions(
    cur,
    user_id: int,
    keep_paper_ids: set[int],
) -> list[dict[str, Any]]:
    """Delete Authors rows for this user where PaperID NOT IN keep_paper_ids.

    Returns the list of deleted rows for the audit log.
    """
    # Snapshot first for audit, then delete
    cur.execute(
        '''
        SELECT a."AuthorLinkID", a."PaperID", a."MappingCriteria", a."AuthorNameRaw",
               rp."DOI", rp."Title", rp."PubYear", rp."Source"
        FROM "Authors" a
        JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
        WHERE a."UserID" = %s
        ''',
        (user_id,),
    )
    all_rows = cur.fetchall() or []
    rows = [dict(r) if not isinstance(r, dict) else r for r in all_rows]
    to_delete = [r for r in rows if r["PaperID"] not in keep_paper_ids]
    if to_delete:
        ids = [r["PaperID"] for r in to_delete]
        cur.execute(
            'DELETE FROM "Authors" WHERE "UserID" = %s AND "PaperID" = ANY(%s)',
            (user_id, ids),
        )
    return to_delete


# =============================================================================
# APPLY MODE — one researcher per transaction
# =============================================================================

def apply_for_researcher(
    conn,
    filename: str,
    config: dict[str, Any],
    uploads_dir: Path,
) -> dict[str, Any]:
    """Transactionally apply Scopus attribution fix for one researcher."""
    user_id = config["user_id"]
    scopus_author_id = config["scopus_author_id"]
    filepath = uploads_dir / filename
    if not filepath.exists():
        return {"user_id": user_id, "success": False,
                "error": f"File not found: {filepath}"}

    df_raw = load_scopus_file(filepath)
    df_albaha, _ = filter_by_albaha_affiliation(df_raw)
    df_importable = df_albaha[
        df_albaha['DOI'].notna() & (df_albaha['DOI'].astype(str).str.strip() != '')
    ]

    audit: dict[str, Any] = {
        "user_id": user_id,
        "arabic_name": config["arabic_name"],
        "scopus_author_id_set": scopus_author_id,
        "papers_processed": [],
        "deletions": [],
        "stats": {
            "scopus_albaha_with_doi": len(df_importable),
            "papers_inserted": 0,
            "papers_updated": 0,
            "authors_inserted": 0,
            "authors_updated": 0,
            "external_authors_inserted": 0,
            "old_attributions_deleted": 0,
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                'UPDATE "Researcher" SET "Scopus_ID" = %s WHERE "UserID" = %s',
                (scopus_author_id, user_id),
            )
            if cur.rowcount == 0:
                raise RuntimeError(f"No Researcher row for UserID={user_id}")

            kept_paper_ids: set[int] = set()

            for _, row in df_importable.iterrows():
                jid = upsert_journal(cur, row)
                upsert_journal_ranking(cur, jid, row)
                paper_id, paper_action = upsert_research_paper(cur, row, jid)
                kept_paper_ids.add(paper_id)

                ids_list = parse_scopus_author_ids(
                    safe_str(row.get('Scopus Author Ids')) or '')
                names_list = parse_authors_field(
                    safe_str(row.get('Authors')) or '')
                author_order = None
                author_name_raw = None
                if scopus_author_id in ids_list:
                    idx = ids_list.index(scopus_author_id)
                    author_order = idx + 1
                    if idx < len(names_list):
                        author_name_raw = names_list[idx]

                is_corresponding = (
                    safe_str(row.get('Scopus Author ID Corresponding Author'))
                    == scopus_author_id
                )

                auth_action = upsert_author_link(
                    cur, user_id, paper_id,
                    author_name_raw, author_order, is_corresponding,
                )
                ext_count = replace_external_authors(
                    cur, paper_id, row, skip_scopus_id=scopus_author_id,
                )

                if paper_action == 'inserted':
                    audit["stats"]["papers_inserted"] += 1
                else:
                    audit["stats"]["papers_updated"] += 1
                if auth_action == 'inserted':
                    audit["stats"]["authors_inserted"] += 1
                else:
                    audit["stats"]["authors_updated"] += 1
                audit["stats"]["external_authors_inserted"] += ext_count

                audit["papers_processed"].append({
                    "doi": safe_str(row.get('DOI')),
                    "paper_id": paper_id,
                    "paper_action": paper_action,
                    "author_link_action": auth_action,
                    "external_authors_added": ext_count,
                })

            deletions = cleanup_old_attributions(cur, user_id, kept_paper_ids)
            audit["deletions"] = [
                {
                    "paper_id": d["PaperID"],
                    "doi": d.get("DOI"),
                    "title": (d.get("Title") or "")[:120],
                    "pub_year": d.get("PubYear"),
                    "source": d.get("Source"),
                    "criteria": d.get("MappingCriteria"),
                    "reason": "no_doi" if not d.get("DOI") else "doi_mismatch",
                }
                for d in deletions
            ]
            audit["stats"]["old_attributions_deleted"] = len(deletions)

            conn.commit()
            audit["success"] = True
            audit["completed_at"] = datetime.now(timezone.utc).isoformat()
            return audit

    except Exception as e:
        conn.rollback()
        audit["success"] = False
        audit["error"] = f"{type(e).__name__}: {e}"
        audit["completed_at"] = datetime.now(timezone.utc).isoformat()
        return audit


def save_apply_audit(audits: list[dict[str, Any]]) -> Path:
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = AUDIT_LOG_DIR / f"apply_log_{timestamp}.json"
    out_path.write_text(
        json.dumps(audits, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return out_path


def print_apply_summary(audits: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 120)
    print(" SCOPUS ATTRIBUTION APPLY — RESULTS".center(120))
    print("=" * 120)
    print(
        f"\n{'Researcher (AR)':<40} {'UID':>4} {'OK':>4} "
        f"{'p_INS':>6} {'p_UPD':>6} {'a_INS':>6} {'a_UPD':>6} "
        f"{'ext':>5} {'DEL':>5}"
    )
    print("-" * 120)
    for a in audits:
        ok = "OK" if a.get("success") else "FAIL"
        st = a.get("stats", {})
        print(
            f"{(a.get('arabic_name') or '?')[:40]:<40} "
            f"{a['user_id']:>4} {ok:>4} "
            f"{st.get('papers_inserted', 0):>6} "
            f"{st.get('papers_updated', 0):>6} "
            f"{st.get('authors_inserted', 0):>6} "
            f"{st.get('authors_updated', 0):>6} "
            f"{st.get('external_authors_inserted', 0):>5} "
            f"{st.get('old_attributions_deleted', 0):>5}"
        )
        if not a.get("success"):
            print(f"     ERROR: {a.get('error')}")

    print("\nLegend:")
    print("  p_INS = ResearchPaper rows INSERTED (new DOIs)")
    print("  p_UPD = ResearchPaper rows UPDATED (existing DOIs refreshed)")
    print("  a_INS = Authors rows INSERTED (new researcher-paper links)")
    print("  a_UPD = Authors rows UPDATED (re-stamped with scopus_author_id)")
    print("  ext   = ExternalAuthors rows inserted (co-authors not in Users)")
    print("  DEL   = old Authors rows deleted (DOI mismatch + no-DOI strict)")


# =============================================================================
# REPORT BUILDING
# =============================================================================

def build_researcher_report(
    conn,
    filename: str,
    config: dict[str, Any],
    uploads_dir: Path,
) -> dict[str, Any]:
    """Compose a delta report for one researcher: Scopus vs DB."""
    user_id = config["user_id"]
    filepath = uploads_dir / filename
    if not filepath.exists():
        return {"filename": filename,
                "error": f"File not found at {filepath}",
                "config": config}

    df_raw = load_scopus_file(filepath)
    df_albaha, df_dropped = filter_by_albaha_affiliation(df_raw)
    df_albaha_with_doi = df_albaha[
        df_albaha['DOI'].notna() & (df_albaha['DOI'].astype(str).str.strip() != '')
    ]
    df_albaha_no_doi = df_albaha[
        df_albaha['DOI'].isna() | (df_albaha['DOI'].astype(str).str.strip() == '')
    ]
    scopus_dois_albaha = extract_dois(df_albaha_with_doi)

    db_state = fetch_researcher_current_state(conn, user_id)
    current_with_doi = [p for p in db_state["papers"] if p.get("DOI")]
    current_no_doi = [p for p in db_state["papers"] if not p.get("DOI")]
    current_dois = {p["DOI"].lower().strip() for p in current_with_doi}

    will_insert = scopus_dois_albaha - current_dois
    will_keep = scopus_dois_albaha & current_dois
    will_delete_doi = current_dois - scopus_dois_albaha
    will_delete_nodoi_count = len(current_no_doi)

    criteria_counts = Counter(p.get("MappingCriteria") for p in db_state["papers"])
    source_counts = Counter(p.get("Source") for p in db_state["papers"])

    return {
        "filename": filename,
        "config": config,
        "db_state": {
            "user_id": user_id,
            "arabic_name": db_state["meta"].get("FullName_Ar"),
            "current_scopus_id": db_state["meta"].get("Scopus_ID"),
            "current_scholar_id": db_state["meta"].get("Scholar_ID"),
            "current_orcid": db_state["meta"].get("ORCID_ID"),
            "academic_rank": db_state["meta"].get("AcademicRank"),
            "current_paper_count": len(db_state["papers"]),
            "current_dois_count": len(current_dois),
            "current_papers_without_doi": len(current_no_doi),
            "criteria_distribution": dict(criteria_counts),
            "source_distribution": dict(source_counts),
        },
        "scopus_state": {
            "total_in_file": len(df_raw),
            "albaha_affiliated": len(df_albaha),
            "albaha_with_doi": len(df_albaha_with_doi),
            "albaha_skipped_no_doi": len(df_albaha_no_doi),
            "dropped_other_affiliations": len(df_dropped),
        },
        "delta": {
            "will_insert_new_papers": len(will_insert),
            "will_keep_and_relink": len(will_keep),
            "will_delete_by_doi_mismatch": len(will_delete_doi),
            "will_delete_no_doi_strict_policy": will_delete_nodoi_count,
            "will_delete_total": len(will_delete_doi) + will_delete_nodoi_count,
        },
        "samples": {
            "to_insert_first_3": list(will_insert)[:3],
            "to_delete_doi_mismatch_first_5": [
                {"doi": p["DOI"], "title": (p["Title"] or "")[:80],
                 "year": p["PubYear"], "source": p.get("Source")}
                for p in current_with_doi
                if p["DOI"].lower().strip() in will_delete_doi
            ][:5],
            "to_delete_no_doi_first_5": [
                {"paper_id": p["PaperID"], "title": (p["Title"] or "")[:80],
                 "year": p["PubYear"], "source": p.get("Source"),
                 "criteria": p.get("MappingCriteria")}
                for p in current_no_doi
            ][:5],
            "scopus_albaha_skipped_no_doi_first_5": [
                {"title": str(row.get("Title", ""))[:80], "year": row.get("Year")}
                for _, row in df_albaha_no_doi.head(5).iterrows()
            ],
        },
    }


def print_report(reports: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 120)
    print(" SCOPUS ATTRIBUTION PRE-FLIGHT REPORT".center(120))
    print(f" Generated: {datetime.now(timezone.utc).isoformat()}".center(120))
    print("=" * 120)

    print(
        f"\n{'#':>2}  {'Researcher (AR)':<38} {'UID':>4} {'Cur':>4} "
        f"{'AlBh':>5} {'+DOI':>5} {'INS':>4} {'KEEP':>5} "
        f"{'DEL_d':>6} {'DEL_n':>6} {'Skip':>5}"
    )
    print("-" * 120)

    totals: dict[str, int] = defaultdict(int)
    for i, r in enumerate(reports, 1):
        if "error" in r:
            print(f"{i:>2}  {r['filename'][:38]:<38}  ERROR: {r['error']}")
            continue
        d = r["db_state"]; s = r["scopus_state"]; dx = r["delta"]
        name = (d["arabic_name"] or r["config"]["arabic_name"] or "?")[:38]
        print(
            f"{i:>2}  {name:<38} "
            f"{d['user_id']:>4} {d['current_paper_count']:>4} "
            f"{s['albaha_affiliated']:>5} {s['albaha_with_doi']:>5} "
            f"{dx['will_insert_new_papers']:>4} "
            f"{dx['will_keep_and_relink']:>5} "
            f"{dx['will_delete_by_doi_mismatch']:>6} "
            f"{dx['will_delete_no_doi_strict_policy']:>6} "
            f"{s['albaha_skipped_no_doi']:>5}"
        )
        totals["current"] += d["current_paper_count"]
        totals["scopus_albaha"] += s["albaha_affiliated"]
        totals["scopus_doi"] += s["albaha_with_doi"]
        totals["insert"] += dx["will_insert_new_papers"]
        totals["keep"] += dx["will_keep_and_relink"]
        totals["delete_doi"] += dx["will_delete_by_doi_mismatch"]
        totals["delete_nodoi"] += dx["will_delete_no_doi_strict_policy"]
        totals["skipped_no_doi"] += s["albaha_skipped_no_doi"]

    print("-" * 120)
    print(
        f"{'TOTAL':<43} "
        f"{'':>4} {totals['current']:>4} "
        f"{totals['scopus_albaha']:>5} {totals['scopus_doi']:>5} "
        f"{totals['insert']:>4} {totals['keep']:>5} "
        f"{totals['delete_doi']:>6} {totals['delete_nodoi']:>6} "
        f"{totals['skipped_no_doi']:>5}"
    )

    print("\nLegend (Strict-DOI policy active):")
    print("  Cur    = current Authors->Paper links in Litrix")
    print("  AlBh   = Scopus papers in AlBaha affiliation (60104698)")
    print("  +DOI   = AlBh subset that has a DOI (the importable set)")
    print("  INS    = new papers to INSERT (in Scopus+DOI, not in DB)")
    print("  KEEP   = papers in both (relinked with scopus_author_id criteria)")
    print("  DEL_d  = current papers WITH DOI but not in Scopus AlBaha -> delete")
    print("  DEL_n  = current papers WITHOUT DOI -> delete (strict policy)")
    print("  Skip   = Scopus AlBh papers without DOI -- flagged for manual review")

    print("\n" + "=" * 120)
    print(" PER-RESEARCHER DETAILS".center(120))
    print("=" * 120)

    for r in reports:
        if "error" in r:
            continue
        d = r["db_state"]; s = r["scopus_state"]; dx = r["delta"]; sa = r["samples"]
        print(f"\n+-- {d['arabic_name']} (UserID {d['user_id']}) --")
        print(f"|  Rank: {d['academic_rank']}")
        print(f"|  Scholar_ID: {d['current_scholar_id'] or '(empty)'}")
        print(f"|  Scopus_ID:  {d['current_scopus_id'] or '(empty)'}  ->  "
              f"will set: {r['config']['scopus_author_id']}")
        print(f"|  ORCID:      {d['current_orcid'] or '(empty)'}")
        print(f"|")
        print(f"|  Current attribution criteria: {d['criteria_distribution']}")
        print(f"|  Current source distribution:  {d['source_distribution']}")
        print(f"|")
        print(f"|  Scopus file: {s['total_in_file']} total -> "
              f"{s['albaha_affiliated']} AlBaha -> "
              f"{s['albaha_with_doi']} with DOI (importable), "
              f"{s['albaha_skipped_no_doi']} skipped (no DOI)")
        print(f"|")
        print(f"|  CHANGES PREVIEW (Strict-DOI policy):")
        print(f"|    * INSERT new papers:        {dx['will_insert_new_papers']}")
        print(f"|    * KEEP + relink:            {dx['will_keep_and_relink']}")
        print(f"|    * DELETE (DOI mismatch):    {dx['will_delete_by_doi_mismatch']}")
        print(f"|    * DELETE (no DOI, strict):  {dx['will_delete_no_doi_strict_policy']}")
        print(f"|    * DELETE TOTAL:             {dx['will_delete_total']}")

        if dx["will_keep_and_relink"] == 0 and d["current_paper_count"] > 0:
            print(f"|")
            print(f"|  WARNING: KEEP=0 -- current attributions don't overlap with")
            print(f"|      Scopus AlBaha papers by DOI. After --apply, this")
            print(f"|      researcher will have ONLY Scopus AlBaha-verified papers.")

        if sa["to_delete_doi_mismatch_first_5"]:
            print(f"|")
            print(f"|  Sample DEL by DOI-mismatch (first 5):")
            for p in sa["to_delete_doi_mismatch_first_5"]:
                print(f"|    [{p['year']}] {p['title']}...")
                print(f"|           DOI: {p['doi']}  | Source: {p['source']}")

        if sa["to_delete_no_doi_first_5"]:
            print(f"|")
            print(f"|  Sample DEL by no-DOI strict policy (first 5):")
            for p in sa["to_delete_no_doi_first_5"]:
                print(f"|    [{p['year']}] {p['title']}...")
                print(f"|           PaperID: {p['paper_id']} | Source: {p['source']} | Crit: {p['criteria']}")

        if sa["scopus_albaha_skipped_no_doi_first_5"]:
            print(f"|")
            print(f"|  Sample Scopus AlBaha papers SKIPPED (no DOI, manual review):")
            for p in sa["scopus_albaha_skipped_no_doi_first_5"]:
                print(f"|    [{p['year']}] {p['title']}...")
        print("+" + "-" * 100)


def save_audit_json(reports: list[dict[str, Any]]) -> Path:
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = AUDIT_LOG_DIR / f"preflight_report_{timestamp}.json"
    out_path.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return out_path


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Scopus-based Author Attribution Fix",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Read-only pre-flight report (no DB writes). RECOMMENDED first run.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually apply the changes (transactional). Run --dry-run FIRST.")
    ap.add_argument("--user-id", type=int, default=None,
                    help="Limit to a single researcher by UserID -- useful for testing.")
    ap.add_argument("--uploads-dir", type=Path,
                    default=Path(os.getenv("SCOPUS_UPLOADS_DIR", str(DEFAULT_UPLOADS_DIR))),
                    help=f"Directory containing the Scopus xlsx exports. Default: {DEFAULT_UPLOADS_DIR}")
    args = ap.parse_args()

    if not args.dry_run and not args.apply:
        ap.error("Specify either --dry-run (safe) or --apply (writes to DB).")

    if not args.uploads_dir.exists():
        print(f"ERROR: Uploads directory does not exist:\n       {args.uploads_dir}")
        print(f"\nExpected files (8 total):")
        for fn in RESEARCHERS:
            print(f"  - {fn}")
        return 1

    print(f"Uploads dir: {args.uploads_dir}")
    print(f"Connecting to database...")
    try:
        conn = db_connect()
    except Exception as e:
        print(f"ERROR connecting to database: {e}")
        return 1

    researchers_to_process = list(RESEARCHERS.items())
    if args.user_id is not None:
        researchers_to_process = [
            (fn, cfg) for fn, cfg in RESEARCHERS.items()
            if cfg["user_id"] == args.user_id
        ]
        if not researchers_to_process:
            print(f"ERROR: No researcher configured with UserID={args.user_id}")
            print(f"       Configured UserIDs: "
                  f"{sorted({c['user_id'] for c in RESEARCHERS.values()})}")
            conn.close()
            return 1

    try:
        if args.dry_run:
            reports = []
            for filename, config in researchers_to_process:
                print(f"  -> {config['arabic_name']} (UserID {config['user_id']})")
                report = build_researcher_report(conn, filename, config, args.uploads_dir)
                reports.append(report)
            print_report(reports)
            out_path = save_audit_json(reports)
            print(f"\nFull JSON report saved to:\n  {out_path}")
            print(f"\nNext step: review the report. If numbers look right, run with --apply.")
            return 0

        if args.user_id is None:
            print("\n" + "!" * 72)
            print("! WARNING: --apply without --user-id will modify ALL 7 researchers !")
            print("! Make sure you have a fresh pg_dump backup before continuing.    !")
            print("!" * 72)
            answer = input("\nType 'YES' (capital) to proceed, anything else to cancel: ")
            if answer.strip() != "YES":
                print("Cancelled.")
                conn.close()
                return 1
        else:
            print(f"\n-> Applying to single researcher: UserID {args.user_id}")
            print(f"   (transactional -- auto-rollback on any error)")

        audits = []
        for filename, config in researchers_to_process:
            print(f"\n  -> Processing {config['arabic_name']} (UserID {config['user_id']})...")
            audit = apply_for_researcher(conn, filename, config, args.uploads_dir)
            audits.append(audit)
            if audit.get("success"):
                st = audit["stats"]
                print(f"     OK Committed: "
                      f"p_INS={st['papers_inserted']} "
                      f"p_UPD={st['papers_updated']} "
                      f"a_INS={st['authors_inserted']} "
                      f"a_UPD={st['authors_updated']} "
                      f"ext={st['external_authors_inserted']} "
                      f"DEL={st['old_attributions_deleted']}")
            else:
                print(f"     FAIL ROLLBACK: {audit.get('error')}")
                break

        print_apply_summary(audits)
        out_path = save_apply_audit(audits)
        print(f"\nFull JSON audit log saved to:\n  {out_path}")
        all_ok = all(a.get("success") for a in audits)
        return 0 if all_ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
