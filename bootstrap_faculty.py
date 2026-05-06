"""
Litrix Bootstrap Faculty Loader (v1)
=====================================
One-shot CSV → DB loader for the faculty roster. Populates:
    - College    (single row, configurable via env)
    - Department (one per unique departmentName found in CSV)
    - Users      (FullName_Ar + Scholar/Scopus IDs + placeholder English name)
    - Researcher (academic rank + scholar/scopus/researchgate URLs)
    - Works_In   (department assignment, IsCurrentPosition = TRUE)

Design Notes (the "why"):
    1. The CSV is the system-of-record for *who is a faculty member*. Their
       English names will come later from Google Scholar via the scraper, so
       FirstName/LastName are placeholders here (split from the Arabic name).
    2. FullName_Ar is the natural key for re-runs — uniqueness enforced via
       migration 002. Re-running the script UPDATES existing rows safely.
    3. Email is required (NN) but we don't have real ones. We generate
       deterministic placeholders so re-runs don't create duplicates.
    4. Scholar URL data quality is bad — typos like trailing 'J' or '&'.
       The cleaner is defensive: extract user=…, validate to 12 alnum chars.
    5. Researchers without ANY public profile still get registered. The
       scraper will skip them gracefully (no Scholar_ID = no SerpAPI call).

Layer separation:
    Layer 1: CONFIG
    Layer 2: PARSERS    (pure logic — testable in isolation)
    Layer 3: REPOSITORY (DB writes — one function per entity)
    Layer 4: PIPELINE   (orchestration + reporting)

Run modes:
    python bootstrap_faculty.py --dry-run   # parse + report, NO DB writes
    python bootstrap_faculty.py             # full load (default CSV path)
    python bootstrap_faculty.py path/to.csv # explicit CSV path
"""

import os
import re
import sys
import csv
import hashlib
import logging
from contextlib import contextmanager
from typing import Optional, Tuple, Dict, List

import psycopg2
from dotenv import load_dotenv


load_dotenv()

DB_CONFIG = {
    "dbname":   os.getenv("DB_NAME", "LitrixDB"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     os.getenv("DB_PORT", "5432"),
}

DEFAULT_COLLEGE_NAME = os.getenv(
    "BOOTSTRAP_COLLEGE_NAME",
    "كلية الحاسبات وتقنية المعلومات"
)

DEFAULT_CSV_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "faculty_roster.csv"
)

PLACEHOLDER_EMAIL_DOMAIN = "litrix.placeholder"

LOG_FILE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "bootstrap.log"
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH, mode='w', encoding='utf-8'),
        logging.StreamHandler(),
    ]
)


SCHOLAR_ID_PATTERN = re.compile(r'user=([A-Za-z0-9_\-]+)')
SCHOLAR_ID_VALID = re.compile(r'^[A-Za-z0-9_\-]{12}$')

ORCID_PATTERN_STRICT = re.compile(r'^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$')


def clean_str(value: Optional[str]) -> Optional[str]:
    """Strip whitespace, BOM, and return None for empty strings."""
    if value is None:
        return None
    cleaned = value.replace('﻿', '').strip()
    return cleaned if cleaned else None


def extract_scholar_id(url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract a clean Scholar_ID from a Scholar URL.
    Returns (scholar_id, normalized_url) or (None, None).

    Handles known data quality issues from the source CSV:
        - Trailing '&'           : ...AAAAJ&     → AAAAJ
        - Extra trailing letter  : ...AAAAJJ     → AAAAJ
        - Trailing whitespace    : '...AAAAJ '   → AAAAJ

    Strategy: greedy match of [A-Za-z0-9_-] after 'user=', then trim down
    to the first 12 chars (the canonical Scholar_ID length). If the resulting
    string passes the pattern check, we accept it.
    """
    url = clean_str(url)
    if not url:
        return None, None

    match = SCHOLAR_ID_PATTERN.search(url)
    if not match:
        return None, None

    raw = match.group(1)
    candidate = raw[:12]

    if not SCHOLAR_ID_VALID.match(candidate):
        return None, None

    normalized_url = f"https://scholar.google.com/citations?user={candidate}"
    return candidate, normalized_url


def split_arabic_name(full_name: str) -> Tuple[str, Optional[str], str]:
    """
    Split a full Arabic name into (first, middle, last) placeholders.
    These are temporary — the scraper overwrites them with the English name
    pulled from the researcher's Scholar profile.
    """
    parts = (full_name or "").strip().split()
    if not parts:
        return "Unknown", None, "Researcher"
    if len(parts) == 1:
        return parts[0], None, parts[0]
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return parts[0], " ".join(parts[1:-1])[:100], parts[-1]


def make_placeholder_email(scholar_id: Optional[str], full_name_ar: str) -> str:
    """
    Deterministic placeholder email. Same input → same output, so re-running
    the bootstrap doesn't create duplicates.
    """
    if scholar_id:
        return f"scholar.{scholar_id.lower()}@{PLACEHOLDER_EMAIL_DOMAIN}"
    digest = hashlib.md5(full_name_ar.encode('utf-8')).hexdigest()[:12]
    return f"pending.{digest}@{PLACEHOLDER_EMAIL_DOMAIN}"


def normalize_department_name(name: Optional[str]) -> Optional[str]:
    """Trim trailing spaces — the source CSV has them on every department."""
    n = clean_str(name)
    if not n:
        return None
    return re.sub(r'\s+', ' ', n)


def normalize_rank(rank: Optional[str]) -> Optional[str]:
    """Keep Arabic rank as-is (it's domain terminology). Just clean whitespace."""
    return clean_str(rank)


def extract_orcid_from_url(value: Optional[str]) -> Optional[str]:
    """
    The CSV's ORCID column may contain either:
        - A bare ORCID:        '0000-0001-2345-6789'
        - An ORCID URL:        'https://orcid.org/0000-0001-2345-6789'
        - An ORCID URL w/ trailing slash, scheme variations, etc.
    We extract the last path segment and validate against the canonical
    format. Anything that doesn't match is rejected (returns None).
    """
    raw = clean_str(value)
    if not raw:
        return None
    candidate = raw.rstrip('/').rsplit('/', 1)[-1].strip()
    if ORCID_PATTERN_STRICT.match(candidate):
        return candidate
    return None


def parse_csv_row(row: Dict[str, str]) -> Optional[Dict]:
    """
    Convert one raw CSV row into a clean record dict.
    Returns None if the row lacks even a name (effectively empty).

    The CSV header has odd whitespace (e.g., 'الاسم, المرتبة') so we look
    up keys defensively by stripping each column name.
    """
    clean = {(k or '').strip(): clean_str(v) for k, v in row.items()}

    full_name_ar = clean.get('الاسم')
    if not full_name_ar:
        return None

    rank = normalize_rank(clean.get('المرتبة'))
    department = normalize_department_name(clean.get('القسم'))
    scholar_url_raw = clean.get('Google Scholar Link')
    scopus_id = clean.get('scopus')
    researchgate_url = clean.get('ResearchGate')
    dblp_url = clean.get('dblp')
    orcid = extract_orcid_from_url(clean.get('ORCID'))

    scholar_id, scholar_url = extract_scholar_id(scholar_url_raw)
    first, middle, last = split_arabic_name(full_name_ar)

    return {
        "full_name_ar":     full_name_ar,
        "first_name":       first,
        "middle_name":      middle,
        "last_name":        last,
        "academic_rank":    rank,
        "department_name":  department,
        "scholar_id":       scholar_id,
        "scholar_url":      scholar_url,
        "scholar_url_raw":  scholar_url_raw,
        "scopus_id":        scopus_id,
        "researchgate_url": researchgate_url,
        "dblp_url":         dblp_url,
        "orcid":            orcid,
        "email":            make_placeholder_email(scholar_id, full_name_ar),
    }


@contextmanager
def db_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


def get_or_create_college(cur, name: str) -> int:
    """Idempotent — relies on uq_college_name from migration 002."""
    cur.execute('''
        INSERT INTO "College" ("CollegeName")
        VALUES (%s)
        ON CONFLICT ("CollegeName") DO UPDATE
            SET "CollegeName" = EXCLUDED."CollegeName"
        RETURNING "CollegeID"
    ''', (name,))
    return cur.fetchone()[0]


def get_or_create_department(cur, name: str, college_id: int) -> int:
    """Idempotent — relies on uq_department_name_college from migration 002."""
    cur.execute('''
        INSERT INTO "Department" ("DepartmentName", "CollegeID")
        VALUES (%s, %s)
        ON CONFLICT ("DepartmentName", "CollegeID") DO UPDATE
            SET "DepartmentName" = EXCLUDED."DepartmentName"
        RETURNING "DepartmentID"
    ''', (name, college_id))
    return cur.fetchone()[0]


def _find_existing_user_id(cur, record: Dict) -> Optional[int]:
    """
    Multi-key lookup. Tries natural identifiers in order of strength:
        1. Scholar_ID  (strongest — unique academic identity)
        2. FullName_Ar (the bootstrap's canonical key)
        3. Email       (placeholder, but still a unique constraint)
    Returns the first matching UserID, or None if no match.
    """
    if record.get("scholar_id"):
        cur.execute(
            'SELECT "UserID" FROM "Users" WHERE "Scholar_ID" = %s LIMIT 1',
            (record["scholar_id"],)
        )
        res = cur.fetchone()
        if res:
            return res[0]

    cur.execute(
        'SELECT "UserID" FROM "Users" WHERE "FullName_Ar" = %s LIMIT 1',
        (record["full_name_ar"],)
    )
    res = cur.fetchone()
    if res:
        return res[0]

    cur.execute(
        'SELECT "UserID" FROM "Users" WHERE "Email" = %s LIMIT 1',
        (record["email"],)
    )
    res = cur.fetchone()
    if res:
        return res[0]

    return None


def upsert_user(cur, record: Dict) -> Tuple[int, bool]:
    """
    Find-or-create a User using multi-key idempotent merge.
    Returns (user_id, was_inserted).

    Why not a single ON CONFLICT? Because Users has THREE unique constraints
    (Scholar_ID, FullName_Ar, Email) and any of them can collide with rows
    created by the legacy scraper (which only filled Scholar_ID + Email,
    leaving FullName_Ar NULL). A single ON CONFLICT only catches one
    constraint — we need to detect collisions across all three.

    Behavior:
        - Match found → UPDATE only the NULL/empty fields (never clobber data)
        - No match    → INSERT new row + generate Litrix_ID
    """
    existing_id = _find_existing_user_id(cur, record)

    if existing_id is not None:
        cur.execute('''
            UPDATE "Users" SET
                "FullName_Ar" = COALESCE("FullName_Ar", %s),
                "FirstName"   = COALESCE("FirstName",   %s),
                "MiddleName"  = COALESCE("MiddleName",  %s),
                "LastName"    = COALESCE("LastName",    %s),
                "Scholar_ID"  = COALESCE("Scholar_ID",  %s),
                "Email"       = COALESCE(NULLIF("Email", ''), %s)
            WHERE "UserID" = %s
        ''', (
            record["full_name_ar"],
            record["first_name"],
            record["middle_name"],
            record["last_name"],
            record["scholar_id"],
            record["email"],
            existing_id,
        ))
        return existing_id, False

    cur.execute('''
        INSERT INTO "Users" (
            "FirstName", "MiddleName", "LastName",
            "FullName_Ar", "Email", "UserType", "AccountStatus",
            "Scholar_ID", "CreatedAt"
        )
        VALUES (%s, %s, %s, %s, %s, 'Researcher', 'Pending', %s, NOW())
        RETURNING "UserID"
    ''', (
        record["first_name"],
        record["middle_name"],
        record["last_name"],
        record["full_name_ar"],
        record["email"],
        record["scholar_id"],
    ))
    user_id = cur.fetchone()[0]

    cur.execute(
        'UPDATE "Users" SET "Litrix_ID" = %s WHERE "UserID" = %s '
        'AND "Litrix_ID" IS NULL',
        (f"LIT-{user_id:06d}", user_id)
    )

    return user_id, True


def upsert_researcher(cur, user_id: int, record: Dict) -> None:
    """
    Upsert the Researcher profile. ON CONFLICT we only fill empty fields —
    we never overwrite data the researcher (or admin) has already curated.

    ORCID handling: when the CSV provides an explicit ORCID we trust it
    fully (this is data the admin curated). It still won't overwrite a
    pre-existing ORCID — that one was already validated.
    """
    cur.execute('''
        INSERT INTO "Researcher" (
            "UserID", "AcademicRank",
            "GoogleScholar_URL", "Scopus_ID", "ResearchGate_URL",
            "ORCID_ID", "DBLP_URL", "IsSurveyCompleted"
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
        ON CONFLICT ("UserID") DO UPDATE
            SET "AcademicRank"      = COALESCE("Researcher"."AcademicRank",      EXCLUDED."AcademicRank"),
                "GoogleScholar_URL" = COALESCE("Researcher"."GoogleScholar_URL", EXCLUDED."GoogleScholar_URL"),
                "Scopus_ID"         = COALESCE("Researcher"."Scopus_ID",         EXCLUDED."Scopus_ID"),
                "ResearchGate_URL"  = COALESCE("Researcher"."ResearchGate_URL",  EXCLUDED."ResearchGate_URL"),
                "ORCID_ID"          = COALESCE("Researcher"."ORCID_ID",          EXCLUDED."ORCID_ID"),
                "DBLP_URL"          = COALESCE("Researcher"."DBLP_URL",          EXCLUDED."DBLP_URL")
    ''', (
        user_id,
        record["academic_rank"],
        record["scholar_url"],
        record["scopus_id"],
        record["researchgate_url"],
        record["orcid"],
        record["dblp_url"],
    ))


def assign_to_department(cur, user_id: int, department_id: int) -> None:
    """Idempotent — relies on uq_works_in_user_dept from migration 002."""
    cur.execute('''
        INSERT INTO "Works_In" (
            "UserID", "DepartmentID", "Role_Position",
            "StartDate", "IsCurrentPosition"
        )
        VALUES (%s, %s, 'Faculty', CURRENT_DATE, TRUE)
        ON CONFLICT ("UserID", "DepartmentID") DO UPDATE
            SET "IsCurrentPosition" = TRUE
    ''', (user_id, department_id))


def read_csv_records(csv_path: str) -> List[Dict]:
    """Read + parse the entire CSV up-front so we can fail fast."""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    records: List[Dict] = []
    with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for raw in reader:
            parsed = parse_csv_row(raw)
            if parsed:
                records.append(parsed)
    return records


def report_dry_run(records: List[Dict]) -> None:
    """Print a quality summary so the operator can sanity-check before writes."""
    total = len(records)
    with_scholar    = sum(1 for r in records if r["scholar_id"])
    with_scopus     = sum(1 for r in records if r["scopus_id"])
    with_rg         = sum(1 for r in records if r["researchgate_url"])
    with_dblp       = sum(1 for r in records if r["dblp_url"])
    with_orcid      = sum(1 for r in records if r.get("orcid"))
    no_links        = sum(1 for r in records if not any([
        r["scholar_id"], r["scopus_id"], r["researchgate_url"], r["dblp_url"]
    ]))

    url_fixes = [
        (r["full_name_ar"], r["scholar_url_raw"], r["scholar_url"])
        for r in records
        if r["scholar_url_raw"] and r["scholar_url"]
        and r["scholar_url_raw"].strip() != r["scholar_url"].strip()
    ]

    dept_count: Dict[str, int] = {}
    for r in records:
        d = r["department_name"] or "(missing)"
        dept_count[d] = dept_count.get(d, 0) + 1

    rank_count: Dict[str, int] = {}
    for r in records:
        rk = r["academic_rank"] or "(missing)"
        rank_count[rk] = rank_count.get(rk, 0) + 1

    print("\n" + "=" * 60)
    print("BOOTSTRAP DRY-RUN REPORT")
    print("=" * 60)
    print(f"Total faculty rows           : {total}")
    print(f"With valid Scholar_ID        : {with_scholar}")
    print(f"With Scopus ID               : {with_scopus}")
    print(f"With ResearchGate URL        : {with_rg}")
    print(f"With DBLP URL                : {with_dblp}")
    print(f"With explicit ORCID          : {with_orcid}")
    print(f"With NO public profile       : {no_links}")
    print(f"Scholar URLs auto-fixed      : {len(url_fixes)}")

    if url_fixes:
        print("\n--- URL fixes applied ---")
        for name, raw, fixed in url_fixes:
            print(f"  • {name}")
            print(f"      raw:   {raw}")
            print(f"      fixed: {fixed}")

    print("\n--- Department distribution ---")
    for d, c in sorted(dept_count.items(), key=lambda x: -x[1]):
        print(f"  {d:35s} : {c}")

    print("\n--- Academic rank distribution ---")
    for rk, c in sorted(rank_count.items(), key=lambda x: -x[1]):
        print(f"  {rk:25s} : {c}")
    print("=" * 60 + "\n")


def run_bootstrap(csv_path: str, dry_run: bool = False) -> None:
    logging.info(f"Reading CSV: {csv_path}")
    records = read_csv_records(csv_path)
    logging.info(f"Parsed {len(records)} faculty records")

    report_dry_run(records)

    if dry_run:
        logging.info("Dry-run mode — no DB writes performed.")
        return

    stats = {"inserted": 0, "updated": 0, "errors": 0}

    with db_connection() as conn:
        with conn.cursor() as cur:
            college_id = get_or_create_college(cur, DEFAULT_COLLEGE_NAME)
            dept_cache: Dict[str, int] = {}
            unique_depts = {r["department_name"] for r in records if r["department_name"]}
            for d in unique_depts:
                dept_cache[d] = get_or_create_department(cur, d, college_id)
        conn.commit()
        logging.info(f"College + {len(dept_cache)} departments ready.")

        for r in records:
            try:
                with conn.cursor() as cur:
                    user_id, inserted = upsert_user(cur, r)
                    upsert_researcher(cur, user_id, r)

                    dept_id = dept_cache.get(r["department_name"])
                    if dept_id:
                        assign_to_department(cur, user_id, dept_id)

                conn.commit()
                stats["inserted" if inserted else "updated"] += 1
                tag = "NEW" if inserted else "UPD"
                logging.info(f"  [{tag}] {r['full_name_ar']}")
            except Exception as e:
                conn.rollback()
                stats["errors"] += 1
                logging.error(f"  [ERR] {r['full_name_ar']} :: {e}")

    logging.info(f"=== Bootstrap complete: {stats} ===")


if __name__ == "__main__":
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]
    csv_path = args[0] if args else DEFAULT_CSV_PATH
    run_bootstrap(csv_path, dry_run=dry_run)
