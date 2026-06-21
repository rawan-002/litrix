"""Read-only audit of paper-level venue classification (Journal vs Conference).

Surfaces the SYSTEMATIC contradictions a senior review would care about, not
one-off guesses:
  A. paper VenueType disagrees with its own Journal row's VenueType
  B. one normalized venue carries BOTH Journal and Conference papers
  C. papers marked 'Journal' whose venue NAME is unambiguously a conference
  D. papers marked 'Conference' whose venue NAME is unambiguously a journal
  E. how many papers rest only on the weak 'name-default -> Journal' fallback
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout

setup_utf8_stdout()

# Unambiguous conference markers in a venue name.
CONF = re.compile(
    r'\b(conference|conf\.|proceedings|proceeding|proc\.|symposium|symp\.|'
    r'workshop|congress|colloqu\w*|globecom|infocom|sigcomm|interspeech|'
    r'\d{4}\s+ieee|ieee\s+\d{4})\b', re.I)
# Unambiguous journal markers.
JOUR = re.compile(
    r'\b(journal|transactions|review|letters|annals|bulletin|'
    r'proceedings of the (national academy|ieee))\b', re.I)
# Conference series that hide inside a serial / "journal-like" container.
SERIAL_CONF = re.compile(
    r'(procedia|lecture\s+notes|communications in computer|'
    r'advances in intelligent systems|ceur|aip conference|iop conference|'
    r'journal of physics:\s*conference series|web of conferences)', re.I)


def venue_name_sql():
    return "COALESCE(j.\"JournalName\", rp.\"RawData_Log\"->>'publication')"


def main():
    conn = db()
    cur = conn.cursor()

    print('=' * 70)
    print('A. Paper VenueType vs its Journal row VenueType (linked papers)')
    print('=' * 70)
    cur.execute('''
        SELECT rp."VenueType", j."VenueType", COUNT(*)
        FROM "ResearchPaper" rp
        JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        WHERE rp."VenueType" IS NOT NULL AND j."VenueType" IS NOT NULL
        GROUP BY 1, 2 ORDER BY 3 DESC
    ''')
    disagree = 0
    for pvt, jvt, n in cur.fetchall():
        jvt_norm = 'Conference' if str(jvt).startswith('Conference') else jvt
        flag = ''
        if pvt in ('Journal', 'Conference') and jvt_norm in ('Journal', 'Conference') and pvt != jvt_norm:
            flag = '   <-- DISAGREE'
            disagree += n
        print(f'  paper={pvt:11} journal={jvt:22} {n:5}{flag}')
    print(f'  >> total disagreements: {disagree}')

    print()
    print('=' * 70)
    print('B. One normalized venue carrying BOTH Journal & Conference papers')
    print('=' * 70)
    cur.execute(f'''
        WITH p AS (
            SELECT rp."PaperID",
                   lower(regexp_replace({venue_name_sql()}, '[^a-zA-Z0-9]+', ' ', 'g')) AS vname,
                   rp."VenueType" AS vt
            FROM "ResearchPaper" rp
            LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
            WHERE rp."VenueType" IS NOT NULL
        )
        SELECT trim(vname) AS v,
               COUNT(*) FILTER (WHERE vt='Journal')    AS j,
               COUNT(*) FILTER (WHERE vt='Conference') AS c
        FROM p
        WHERE trim(vname) <> '' AND vname IS NOT NULL
        GROUP BY trim(vname)
        HAVING COUNT(*) FILTER (WHERE vt='Journal') > 0
           AND COUNT(*) FILTER (WHERE vt='Conference') > 0
        ORDER BY (COUNT(*) FILTER (WHERE vt='Journal') + COUNT(*) FILTER (WHERE vt='Conference')) DESC
    ''')
    mixed = cur.fetchall()
    tot_mixed_papers = sum(j + c for _, j, c in mixed)
    print(f'  mixed venues: {len(mixed)}   (papers involved: {tot_mixed_papers})')
    for v, j, c in mixed[:40]:
        print(f'    J={j:3} C={c:3}  {v[:70]}')

    print()
    print('=' * 70)
    print("C. Marked 'Journal' but venue NAME is unambiguously a conference")
    print('=' * 70)
    cur.execute(f'''
        SELECT rp."PaperID", {venue_name_sql()} AS v
        FROM "ResearchPaper" rp
        LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        WHERE rp."VenueType" = 'Journal'
    ''')
    c_errors = []
    for pid, v in cur.fetchall():
        if not v:
            continue
        if SERIAL_CONF.search(v) or (CONF.search(v) and not JOUR.search(v)):
            c_errors.append((pid, v))
    print(f'  suspects: {len(c_errors)}')
    for pid, v in c_errors[:40]:
        print(f'    P{pid}: {v[:75]}')

    print()
    print('=' * 70)
    print("D. Marked 'Conference' but venue NAME is unambiguously a journal")
    print('=' * 70)
    cur.execute(f'''
        SELECT rp."PaperID", {venue_name_sql()} AS v
        FROM "ResearchPaper" rp
        LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        WHERE rp."VenueType" = 'Conference'
    ''')
    j_errors = []
    for pid, v in cur.fetchall():
        if not v:
            continue
        if JOUR.search(v) and not CONF.search(v) and not SERIAL_CONF.search(v):
            j_errors.append((pid, v))
    print(f'  suspects: {len(j_errors)}')
    for pid, v in j_errors[:40]:
        print(f'    P{pid}: {v[:75]}')

    print()
    print('=' * 70)
    print('E. Papers with NO usable venue name at all (classified blind)')
    print('=' * 70)
    cur.execute(f'''
        SELECT rp."VenueType", COUNT(*)
        FROM "ResearchPaper" rp
        LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        WHERE COALESCE(NULLIF(TRIM({venue_name_sql()}), ''), '') = ''
        GROUP BY 1
    ''')
    print('  no-name papers by type:', cur.fetchall())

    print()
    print('SUMMARY')
    print(f'  A disagreements (paper vs journal row): {disagree}')
    print(f'  B mixed-venue papers: {tot_mixed_papers} across {len(mixed)} venues')
    print(f'  C journal->should-be-conference suspects: {len(c_errors)}')
    print(f'  D conference->should-be-journal suspects: {len(j_errors)}')


if __name__ == '__main__':
    main()
