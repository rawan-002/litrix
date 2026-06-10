import os
import sys
import io
import csv
import re
import argparse
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import execute_values

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()


ABBREV_MAP = [
    (r'\bint\b\.?', 'international'), (r'\binte?l\b\.?', 'international'),
    (r'\bj\b\.?', 'journal'), (r'\btrans\b\.?', 'transactions'),
    (r'\bproc\b\.?', 'proceedings'), (r'\bcomput\b\.?', 'computer'),
    (r'\bsci\b\.?', 'science'), (r'\beng\b\.?', 'engineering'),
    (r'\btech\b\.?', 'technology'), (r'\binfo\b\.?', 'information'),
    (r'\bsyst\b\.?', 'systems'), (r'\bres\b\.?', 'research'),
    (r'\bappl\b\.?', 'applied'),
]


def normalize_journal_name(name):
    if not name:
        return None
    n = name.lower().strip()
    n = re.sub(r'\s*\([^)]*\)\s*', ' ', n)
    n = re.sub(r'\s*\[[^\]]*\]\s*', ' ', n)
    for pat, repl in ABBREV_MAP:
        n = re.sub(pat, repl, n)
    n = re.sub(r"[^a-z0-9\s]", ' ', n)
    tokens = n.split()
    drop = {'the', 'of', 'and', 'in', 'on', 'for', 'an', 'a'}
    tokens = [t for t in tokens if t not in drop]
    n = ' '.join(tokens).strip()
    return n if len(n) >= 3 else None


def normalize_issn(s):
    if not s:
        return None
    n = ''.join(c for c in s.upper() if c.isalnum())
    return n if len(n) == 8 else None


def parse_sjr(s):
    if not s:
        return None
    try:
        return float(s.replace(',', '.'))
    except ValueError:
        return None


# Shared DB helper (single source — see litrix_db.py at repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--year", type=int, default=2025)
    args = ap.parse_args()

    if not os.path.exists(args.csv_path):
        print(f"File not found: {args.csv_path}")
        sys.exit(1)

    print(f"Connected to: {os.getenv('DATABASE_URL', 'LOCAL').split('@')[-1].split('/')[0]}")
    print(f"Reading: {args.csv_path}\n")

    rows_to_load = []
    with open(args.csv_path, encoding='utf-8-sig') as f:
        sample = f.read(2048)
        f.seek(0)
        delim = ';' if sample.count(';') > sample.count(',') else ','
        reader = csv.DictReader(f, delimiter=delim)

        col_title = next((c for c in reader.fieldnames if c.lower() == 'title'), None)
        col_issn = next((c for c in reader.fieldnames if c.lower() == 'issn'), None)
        col_quartile = next((c for c in reader.fieldnames if 'best quartile' in c.lower()), None)
        col_sjr = next((c for c in reader.fieldnames if c.lower() == 'sjr'), None)
        col_categories = next((c for c in reader.fieldnames if c.lower() == 'categories'), None)

        for row in reader:
            title = (row.get(col_title) or '').strip()
            issn_raw = row.get(col_issn) or ''
            quartile = (row.get(col_quartile) or '').strip() or None
            sjr = parse_sjr(row.get(col_sjr) or '')
            categories = (row.get(col_categories) or '').strip() if col_categories else None

            issns = [normalize_issn(x.strip()) for x in issn_raw.split(',')]
            issns = [i for i in issns if i]
            if not issns or not title:
                continue

            norm_name = normalize_journal_name(title)
            for issn in issns:
                rows_to_load.append((
                    issn, quartile, sjr, args.year, 'SCImago', categories, norm_name
                ))

    print(f"Parsed {len(rows_to_load)} (issn, journal) pairs")

    conn = db()
    cur = conn.cursor()

    cur.execute('''
        CREATE TEMP TABLE _scimago_stage (
            issn TEXT, quartile TEXT, impact_factor DOUBLE PRECISION,
            ranking_year INT, source TEXT, category TEXT, normalized TEXT
        )
    ''')

    print(f"Bulk loading...")
    execute_values(
        cur,
        '''INSERT INTO _scimago_stage
           (issn, quartile, impact_factor, ranking_year, source, category, normalized)
           VALUES %s''',
        rows_to_load,
        page_size=2000,
    )
    conn.commit()
    print("  staging loaded")

    cur.execute('''
        UPDATE "JournalRankings" jr
        SET "Quartile"      = COALESCE(s.quartile,      jr."Quartile"),
            "ImpactFactor"  = COALESCE(s.impact_factor, jr."ImpactFactor"),
            "NormalizedName"= COALESCE(s.normalized,    jr."NormalizedName"),
            "Category"      = COALESCE(s.category,      jr."Category"),
            "RankingYear"   = s.ranking_year,
            "Source"        = 'SCImago'
        FROM _scimago_stage s
        WHERE jr."Issn" = s.issn
    ''')
    n_updated = cur.rowcount
    print(f"  updated {n_updated}")

    cur.execute('''
        INSERT INTO "JournalRankings"
            ("Issn", "Quartile", "ImpactFactor", "RankingYear",
             "Source", "Category", "NormalizedName")
        SELECT s.issn, s.quartile, s.impact_factor, s.ranking_year,
               s.source, s.category, s.normalized
        FROM _scimago_stage s
        LEFT JOIN "JournalRankings" jr ON jr."Issn" = s.issn
        WHERE jr."RankingID" IS NULL
    ''')
    n_inserted = cur.rowcount
    print(f"  inserted {n_inserted}")

    conn.commit()
    print(f"\nDone — inserted={n_inserted}, updated={n_updated}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
