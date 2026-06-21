"""Deterministic, name-based venue reclassification (Journal vs Conference).

A senior pass over the per-paper VenueType set by classification/dblp_venue.py.
The venue NAME (Scholar's publication field, or the linked Journal name) is the
most reliable signal when it is unambiguous - more reliable than a per-paper
Crossref-by-DOI lookup, which was fooled by mismatched DOIs (e.g. IJCSNS and
IEEE Transactions papers wrongly tagged Conference).

Precedence (first match wins) handles the classic traps both ways:
  1. SERIAL_CONF  -> Conference  (conference proceedings hiding inside a serial:
                                  Procedia, LNCS, "Journal of Physics: Conference
                                  Series", IOP Conf. Series, *Web of Conferences,
                                  WIT Transactions, CCIS, AISC, CEUR, AIP)
  2. JOUR_PROC    -> Journal      (journals whose title contains "proceedings":
                                  "Proceedings of the IEEE / National Academy")
  3. CONF         -> Conference   (conference / proceedings / symposium / workshop
                                  / congress / colloquium / ...)
  4. JOUR         -> Journal      (journal / transactions / letters / ...)
  5. (no match)   -> leave the existing classification untouched

Idempotent. Dry-run by default; pass --commit to write. Only rows whose correct
type DIFFERS from the stored one are touched.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout

setup_utf8_stdout()

# 1. Conference proceedings published inside a serial / "journal-like" container.
SERIAL_CONF = re.compile(
    r'(procedia'
    r'|lecture\s+notes\s+in\s+(computer|networks|electrical|business|'
    r'artificial|information|bioinformatics)'
    r'|communications\s+in\s+computer\s+and\s+information\s+science'
    r'|advances\s+in\s+intelligent\s+systems'
    r'|journal\s+of\s+physics:\s*conference\s+series'
    r'|iop\s+conf(erence)?\.?\s+series'
    r'|aip\s+conf(erence)?\.?\s+proceedings'
    r'|web\s+of\s+conferences'
    r'|wit\s+transactions'
    r'|\bceur\b'
    r'|\bceur-ws\b)', re.I)

# 2. Journals that contain the word "proceedings" in their title.
JOUR_PROC = re.compile(
    r'proceedings\s+of\s+the\s+(ieee|national\s+academy|royal\s+society)', re.I)

# 3. Unambiguous conference markers.
CONF = re.compile(
    r'\b(conference|conf\.|proceedings|proceeding|proc\.|symposium|symp\.|'
    r'workshop|congress|colloqu\w*|globecom|infocom|sigcomm|interspeech|'
    r'meeting|convention)\b', re.I)

# 4. Unambiguous journal markers.
JOUR = re.compile(
    r'\b(journal|transactions|letters|annals|bulletin|'
    r'ieee\s+access|computing\s+surveys)\b', re.I)


def classify_by_name(name):
    """Return 'Conference'/'Journal' if the name is unambiguous, else None."""
    if not name:
        return None
    if SERIAL_CONF.search(name):
        return 'Conference'
    if JOUR_PROC.search(name):
        return 'Journal'
    if CONF.search(name):
        return 'Conference'
    if JOUR.search(name):
        return 'Journal'
    return None


def main():
    ap = argparse.ArgumentParser(description='Name-based venue reclassification')
    ap.add_argument('--commit', action='store_true', help='write changes (default: dry-run)')
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    cur.execute('''
        SELECT rp."PaperID",
               COALESCE(j."JournalName", rp."RawData_Log"->>'publication') AS vname,
               rp."VenueType"
        FROM "ResearchPaper" rp
        LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        WHERE rp."VenueType" IS NOT NULL
        ORDER BY rp."PaperID"
    ''')
    rows = cur.fetchall()

    to_conf, to_jour = [], []
    for pid, vname, current in rows:
        verdict = classify_by_name(vname)
        if verdict and verdict != current:
            (to_conf if verdict == 'Conference' else to_jour).append((pid, vname, current))

    print('=' * 70)
    print(f'Journal -> Conference : {len(to_conf)} papers')
    print('=' * 70)
    for pid, v, _ in to_conf:
        print(f'  P{pid}: {(v or "")[:78]}')
    print()
    print('=' * 70)
    print(f'Conference -> Journal : {len(to_jour)} papers')
    print('=' * 70)
    for pid, v, _ in to_jour:
        print(f'  P{pid}: {(v or "")[:78]}')

    changes = [(p, 'Conference') for p, _, _ in to_conf] + \
              [(p, 'Journal') for p, _, _ in to_jour]
    print()
    print(f'TOTAL changes: {len(changes)}  '
          f'(-> Conference {len(to_conf)}, -> Journal {len(to_jour)})')

    if not args.commit:
        print('\nDRY-RUN. Re-run with --commit to apply.')
        return

    for pid, vt in changes:
        cur.execute('UPDATE "ResearchPaper" SET "VenueType"=%s WHERE "PaperID"=%s', (vt, pid))
    conn.commit()
    print(f'\nCOMMITTED {len(changes)} updates.')

    cur.execute('SELECT "VenueType", COUNT(*) FROM "ResearchPaper" GROUP BY 1 ORDER BY 2 DESC')
    print('New distribution:', cur.fetchall())


if __name__ == '__main__':
    main()
