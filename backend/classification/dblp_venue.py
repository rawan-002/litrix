import os
import sys
import io
import re
import json
import time
import argparse
import difflib
import urllib.request
import urllib.parse
import urllib.error
import psycopg2
from dotenv import load_dotenv

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db

UA = 'Litrix/1.0 (mailto:litrix@bu.edu.sa)'

# Crossref labels conference papers published inside a serial (Procedia, LNCS,
# AIP/IOP conference series, ...) as 'journal-article' or 'book-chapter'. When a
# Crossref container name matches this, we ask DBLP for the authoritative call
# instead of trusting Crossref's type.
AMBIGUOUS_CONTAINER = re.compile(
    r'(procedia|lecture\s+notes|conference|proceedings|symposium|workshop|'
    r'congress|colloqu|ceur|communications in computer and information science|'
    r'advances in intelligent systems)', re.I)


def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def crossref_type(doi):
    """Return (crossref_type, container_name) or (None, None)."""
    try:
        m = _get('https://api.crossref.org/works/' + urllib.parse.quote(doi))['message']
    except Exception:
        return None, None
    ct = (m.get('container-title') or [''])
    return m.get('type'), (ct[0] if ct else '')


def dblp_venue(title, doi, retries=6):
    """Authoritative CS lookup. Returns (verdict, reached) where verdict is
    'Conference'/'Journal'/None and reached is False when DBLP could not be
    contacted at all (so the caller can trip a circuit breaker)."""
    q = urllib.parse.urlencode({'q': title, 'format': 'json', 'h': 6})
    url = 'https://dblp.org/search/publ/api?' + q
    data = None
    for attempt in range(retries):
        try:
            data = _get(url)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get('Retry-After', '5')) + 1
                time.sleep(wait)
                continue
            time.sleep(2)
        except Exception:
            time.sleep(2)
    if not data:
        return None, False
    hits = data.get('result', {}).get('hits', {}).get('hit', [])
    nt = norm(title)
    ourdoi = (doi or '').lower().strip()
    best = None
    for h in hits:
        info = h.get('info', {})
        ratio = difflib.SequenceMatcher(None, nt, norm(info.get('title', ''))).ratio()
        hdoi = (info.get('doi', '') or '').lower().strip()
        doi_match = bool(ourdoi and hdoi and ourdoi == hdoi)
        if doi_match or ratio >= 0.92:
            pref = info.get('key', '').split('/')[0]
            vt = 'Conference' if pref == 'conf' else 'Journal' if pref == 'journals' else None
            if vt and (best is None or (doi_match, ratio) > best[0]):
                best = ((doi_match, ratio), vt)
    return (best[1] if best else None), True


# High-precision conference markers in a venue NAME (used when there's no usable
# DOI, so no network call is needed).
CONF_NAME = re.compile(
    r'\b(conference|conf\.|proceedings|proc\.|symposium|workshop|congress|'
    r'colloqu\w*|globecom|infocom)\b', re.I)


def name_venue(venue_name):
    if venue_name and CONF_NAME.search(venue_name):
        return 'Conference'
    return None


def decide(title, doi, venue_name, dblp_sleep, dblp_state):
    """Return (venue_type, source) for one paper.

    DOI -> Crossref (definitive for journals + standalone conference papers).
    Conference proceedings published inside a serial (Procedia, LNCS, ...) are
    flagged 'journal-article'/'book-chapter' by Crossref, so for those ambiguous
    containers we ask DBLP; if DBLP is unreachable we fall back to Conference
    (the ambiguous-container set is conference proceedings by definition).
    No usable DOI -> classify from the venue NAME (no network)."""
    if doi:
        ctype, container = crossref_type(doi)
        if ctype in ('proceedings-article', 'proceedings'):
            return 'Conference', 'crossref'
        if ctype in ('journal-article', 'book-chapter', 'book-part'):
            if AMBIGUOUS_CONTAINER.search(container or ''):
                if dblp_state['ok']:
                    time.sleep(dblp_sleep)
                    v, reached = dblp_venue(title, doi)
                    if not reached:
                        dblp_state['fails'] += 1
                        if dblp_state['fails'] >= 5:
                            dblp_state['ok'] = False
                    else:
                        dblp_state['fails'] = 0
                        if v:
                            return v, 'dblp'
                # DBLP off / unreachable / no match: ambiguous container => Conference
                return 'Conference', 'container'
            if ctype == 'journal-article':
                return 'Journal', 'crossref'
            return 'Journal', 'crossref'
        # crossref 404 / dataset / report / preprint -> fall through to name
    nm = name_venue(venue_name)
    if nm:
        return nm, 'name'
    return 'Journal', 'name-default'


def main():
    ap = argparse.ArgumentParser(description='Classify paper venue type via Crossref + DBLP')
    ap.add_argument('--force', action='store_true', help='reclassify rows that already have a VenueType')
    ap.add_argument('--limit', type=int, default=0, help='process at most N papers (0 = all)')
    ap.add_argument('--dblp-sleep', type=float, default=2.0, help='seconds to wait before each DBLP call')
    ap.add_argument('--commit-every', type=int, default=25)
    args = ap.parse_args()

    conn = db()
    print('Connected. force=%s limit=%s dblp_sleep=%ss' % (args.force, args.limit, args.dblp_sleep))
    cur = conn.cursor()
    where = '' if args.force else 'WHERE rp."VenueType" IS NULL'
    limit = ('LIMIT %d' % args.limit) if args.limit else ''
    cur.execute(f'''
        SELECT rp."PaperID", rp."Title", rp."DOI",
               COALESCE(j."JournalName", rp."RawData_Log"->>'publication') AS venue_name
        FROM "ResearchPaper" rp
        LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        {where}
        ORDER BY rp."PaperID"
        {limit}
    ''')
    rows = cur.fetchall()
    total = len(rows)
    print('papers to classify: %d' % total)

    counts = {'Conference': 0, 'Journal': 0, None: 0}
    src = {}
    batch = []
    dblp_state = {'ok': True, 'fails': 0}

    def flush():
        # Neon's pooler drops the connection during long DBLP waits; reconnect
        # and retry so a dropped socket never aborts the whole run.
        nonlocal conn
        if not batch:
            return
        for attempt in range(4):
            try:
                c = conn.cursor()
                c.executemany('UPDATE "ResearchPaper" SET "VenueType"=%s WHERE "PaperID"=%s', batch)
                conn.commit()
                batch.clear()
                return
            except psycopg2.OperationalError as e:
                print('  ! DB connection lost, reconnecting (%s)' % str(e).strip()[:60])
                try:
                    conn.close()
                except Exception:
                    pass
                time.sleep(3)
                conn = db()
        print('  ! flush failed after retries; rows kept for next batch')

    for i, (pid, title, doi, venue_name) in enumerate(rows, 1):
        if not title:
            counts[None] += 1
            continue
        vt, source = decide(title, doi, venue_name, args.dblp_sleep, dblp_state)
        counts[vt] = counts.get(vt, 0) + 1
        src[source] = src.get(source, 0) + 1
        if vt:
            batch.append((vt, pid))
        if i % args.commit_every == 0:
            flush()
            print('  %d/%d  conf=%d journal=%d  dblp_ok=%s  (%s)'
                  % (i, total, counts['Conference'], counts['Journal'], dblp_state['ok'], src))
    flush()
    print('DONE  conf=%d journal=%d unresolved=%d' % (counts['Conference'], counts['Journal'], counts[None]))
    print('sources:', src)


if __name__ == '__main__':
    main()
