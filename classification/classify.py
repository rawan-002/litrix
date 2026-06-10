import os
import sys
import io
import re
from dotenv import load_dotenv
import psycopg2

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


# Shared DB helper (single source — see litrix_db.py at repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def clean_publication_string(pub):
    if not pub:
        return pub
    cleaned = re.sub(r'\s+\d.*$', '', pub).strip().rstrip(',').strip()
    return cleaned or pub


def normalize_journal_name(name):
    if not name:
        return None
    name = clean_publication_string(name)
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


def title_case_from_norm(norm):
    if not norm:
        return None
    return ' '.join(w.capitalize() for w in norm.split())


def column_exists(cur, table, column):
    cur.execute('''
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    ''', (table, column))
    return cur.fetchone() is not None


def index_exists(cur, idx_name):
    cur.execute('SELECT 1 FROM pg_indexes WHERE indexname = %s', (idx_name,))
    return cur.fetchone() is not None


def setup_normalized_columns(cur):
    if not column_exists(cur, 'Journals', 'NormalizedName'):
        print("  + adding Journals.NormalizedName")
        cur.execute('ALTER TABLE "Journals" ADD COLUMN "NormalizedName" TEXT')
    if not index_exists(cur, 'idx_journals_normname'):
        cur.execute('CREATE INDEX idx_journals_normname ON "Journals"("NormalizedName")')

    if not column_exists(cur, 'JournalRankings', 'NormalizedName'):
        print("  + adding JournalRankings.NormalizedName")
        cur.execute('ALTER TABLE "JournalRankings" ADD COLUMN "NormalizedName" TEXT')
    if not index_exists(cur, 'idx_jrankings_normname'):
        cur.execute('CREATE INDEX idx_jrankings_normname ON "JournalRankings"("NormalizedName")')


def backfill_journals_normname(conn, force=False):
    cur = conn.cursor()
    if force:
        cur.execute('UPDATE "Journals" SET "NormalizedName" = NULL')
        print(f"  reset {cur.rowcount} rows")
    cur.execute('''
        SELECT ctid, "JournalName" FROM "Journals"
        WHERE "NormalizedName" IS NULL AND "JournalName" IS NOT NULL
    ''')
    rows = cur.fetchall()
    print(f"  backfilling {len(rows)} Journals rows")
    cur.close()

    cur = conn.cursor()
    n = 0
    for ctid, name in rows:
        norm = normalize_journal_name(name)
        cur.execute(
            'UPDATE "Journals" SET "NormalizedName" = %s WHERE ctid = %s',
            (norm, ctid),
        )
        n += 1
        if n % 500 == 0:
            conn.commit()
    conn.commit()
    cur.close()
    print(f"  done ({n} rows)")


def propagate_to_jrankings(conn, force=False):
    cur = conn.cursor()
    if force:
        cur.execute('UPDATE "JournalRankings" SET "NormalizedName" = NULL')
    cur.execute('''
        UPDATE "JournalRankings" jr
        SET "NormalizedName" = j."NormalizedName"
        FROM "Journals" j
        WHERE jr."JournalID" = j."JournalID"
          AND j."NormalizedName" IS NOT NULL
          AND (jr."NormalizedName" IS NULL OR jr."NormalizedName" <> j."NormalizedName")
    ''')
    print(f"  propagated {cur.rowcount} rows")
    conn.commit()
    cur.close()


def find_or_create_journal(cur, issn, norm):
    cur.execute(
        'SELECT "JournalID" FROM "Journals" WHERE "ISSN_Print" = %s LIMIT 1',
        (issn,),
    )
    r = cur.fetchone()
    if r:
        return r[0]

    cur.execute(
        'SELECT "JournalID", "ISSN_Print" FROM "Journals" '
        'WHERE "NormalizedName" = %s ORDER BY "JournalID" LIMIT 1',
        (norm,),
    )
    r = cur.fetchone()
    if r:
        jid, existing_issn = r
        if not existing_issn:
            cur.execute(
                'SELECT 1 FROM "Journals" WHERE "ISSN_Print" = %s AND "JournalID" <> %s LIMIT 1',
                (issn, jid),
            )
            if not cur.fetchone():
                cur.execute(
                    'UPDATE "Journals" SET "ISSN_Print" = %s WHERE "JournalID" = %s',
                    (issn, jid),
                )
        return jid

    title = title_case_from_norm(norm)
    try:
        cur.execute(
            'INSERT INTO "Journals" ("JournalName", "ISSN_Print", "NormalizedName") '
            'VALUES (%s, %s, %s) RETURNING "JournalID"',
            (title, issn, norm),
        )
        return cur.fetchone()[0]
    except psycopg2.errors.UniqueViolation:
        cur.connection.rollback()
        cur.execute(
            'SELECT "JournalID" FROM "Journals" WHERE "ISSN_Print" = %s LIMIT 1',
            (issn,),
        )
        r = cur.fetchone()
        return r[0] if r else None


def phase_classify_papers(conn):
    cur = conn.cursor()
    cur.execute('''
        SELECT "PaperID",
               COALESCE("RawData_Log"->>'publication',
                        "RawData_Log"->>'journal',
                        "RawData_Log"->>'venue') AS pub
        FROM "ResearchPaper"
        WHERE "JournalID" IS NULL AND "RawData_Log" IS NOT NULL
        ORDER BY "PaperID"
    ''')
    papers = cur.fetchall()
    print(f"  {len(papers)} papers without JournalID")

    n_matched = 0
    for i, (pid, pub) in enumerate(papers, 1):
        norm = normalize_journal_name(pub)
        if not norm:
            continue

        cur.execute(
            'SELECT "Issn" FROM "JournalRankings" '
            'WHERE "NormalizedName" = %s AND "Issn" IS NOT NULL LIMIT 1',
            (norm,),
        )
        r = cur.fetchone()
        if not r:
            continue
        issn = r[0]

        try:
            cur.execute('SAVEPOINT sp')
            jid = find_or_create_journal(cur, issn, norm)
            if not jid:
                cur.execute('ROLLBACK TO SAVEPOINT sp')
                continue
            cur.execute(
                'UPDATE "ResearchPaper" SET "JournalID" = %s WHERE "PaperID" = %s',
                (jid, pid),
            )
            cur.execute('RELEASE SAVEPOINT sp')
            n_matched += 1
        except Exception:
            try: cur.execute('ROLLBACK TO SAVEPOINT sp')
            except: pass

        if i % 200 == 0:
            conn.commit()
            print(f"    {i}/{len(papers)}  matched={n_matched}")

    conn.commit()
    cur.close()
    print(f"  matched {n_matched} papers")


def phase_backfill_issn(conn):
    cur = conn.cursor()
    cur.execute('''
        WITH all_candidates AS (
            SELECT j."JournalID", j."NormalizedName",
                   (SELECT COUNT(DISTINCT jr."Issn") FROM "JournalRankings" jr
                     WHERE jr."NormalizedName" = j."NormalizedName") AS distinct_issns,
                   (SELECT jr."Issn" FROM "JournalRankings" jr
                     WHERE jr."NormalizedName" = j."NormalizedName"
                     LIMIT 1) AS issn_pick
            FROM "Journals" j
            WHERE j."ISSN_Print" IS NULL AND j."NormalizedName" IS NOT NULL
        ),
        deduped AS (
            SELECT DISTINCT ON (issn_pick) "JournalID", issn_pick
            FROM all_candidates
            WHERE distinct_issns = 1 AND issn_pick IS NOT NULL
            ORDER BY issn_pick, "JournalID"
        )
        UPDATE "Journals" j
        SET "ISSN_Print" = d.issn_pick
        FROM deduped d
        WHERE j."JournalID" = d."JournalID"
          AND NOT EXISTS (
              SELECT 1 FROM "Journals" j2
              WHERE j2."ISSN_Print" = d.issn_pick AND j2."JournalID" <> d."JournalID"
          )
    ''')
    print(f"  backfilled {cur.rowcount} Journals with ISSN")
    conn.commit()
    cur.close()


def phase_link_rankings(conn):
    cur = conn.cursor()
    cur.execute('''
        UPDATE "JournalRankings" jr
        SET "JournalID" = j."JournalID"
        FROM "Journals" j
        WHERE j."ISSN_Print" IS NOT NULL
          AND regexp_replace(UPPER(jr."Issn"), '[^A-Z0-9]', '', 'g')
              = regexp_replace(UPPER(j."ISSN_Print"), '[^A-Z0-9]', '', 'g')
          AND (jr."JournalID" IS NULL OR jr."JournalID" <> j."JournalID")
          AND NOT EXISTS (
              SELECT 1 FROM "JournalRankings" jr2
              WHERE jr2."JournalID" = j."JournalID"
                AND jr2."RankingYear" = jr."RankingYear"
                AND jr2."RankingID" <> jr."RankingID"
          )
    ''')
    print(f"  linked {cur.rowcount} JournalRankings rows")
    conn.commit()
    cur.close()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="Reset NormalizedName and re-compute")
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    print(f"Connected to: {os.getenv('DATABASE_URL', 'LOCAL').split('@')[-1].split('/')[0]}\n")

    print("[1] Setup NormalizedName columns")
    setup_normalized_columns(cur)
    conn.commit()
    cur.close()

    print("\n[2] Backfill Journals.NormalizedName")
    backfill_journals_normname(conn, force=args.force)

    print("\n[3] Propagate to JournalRankings")
    propagate_to_jrankings(conn, force=args.force)

    print("\n[4] Classify papers (auto-create Journals if needed)")
    phase_classify_papers(conn)

    print("\n[5] Backfill ISSN_Print on Journals")
    phase_backfill_issn(conn)

    print("\n[6] Link JournalRankings.JournalID via ISSN")
    phase_link_rankings(conn)

    cur = conn.cursor()
    cur.execute('''
        SELECT
          COUNT(*) FILTER (WHERE "JournalID" IS NOT NULL) AS with_jid,
          COUNT(*) FILTER (WHERE "JournalID" IS NULL) AS without_jid
        FROM "ResearchPaper"
    ''')
    r = cur.fetchone()
    print(f"\n[Final stats]")
    print(f"  Papers with JournalID:    {r[0]}")
    print(f"  Papers without JournalID: {r[1]}")

    cur.execute('''
        SELECT COUNT(*) FROM "ResearchPaper" rp
        JOIN "JournalRankings" jr ON jr."JournalID" = rp."JournalID"
        WHERE jr."Quartile" IS NOT NULL
    ''')
    print(f"  Papers with Quartile:     {cur.fetchone()[0]}")

    cur.close()
    conn.close()
    print("\nDone")


if __name__ == "__main__":
    main()
