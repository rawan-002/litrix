"""Authoritative multi-source venue verification (Journal vs Conference).

Goal: the most accurate classification achievable automatically, plus an honest
review sheet for the handful no automated source can settle.

Per paper we gather independent opinions and combine them by authority:

  0. PUBLISHER DOI-PATTERN OVERRIDE (top authority, no API call; see the
     venue_classifiers package). Wiley book chapters mint DOIs as
     10.1002/<13-digit ISBN>.ch<N> - unambiguous by construction, so this
     alone settles VenueType='BookChapter' even when Crossref/OpenAlex have
     no record or disagree. NOT extended to Springer's shared 10.1007/978-...
     prefix (also used by legitimate LNCS/CCIS proceedings) - new publisher
     rules go in venue_classifiers/publishers.py, not here.
  2. SERIAL-CONFERENCE NAME OVERRIDE. Procedia, LNCS, "Journal
     of Physics: Conference Series", IOP/E3S/MATEC/SHS Web of Conferences, CCIS,
     AISC, WIT Transactions, CEUR, AIP Conf. Proc. are conference proceedings
     that EVERY api mislabels as journal / book-series - so the venue NAME
     (stored, or the source name returned by OpenAlex/Crossref) wins outright.
     "Proceedings of the IEEE / National Academy" -> Journal the same way.
  3. OpenAlex (by DOI): work type + source.type (journal/conference/book series).
  4. Crossref (by DOI): work type + container (proceedings-article is decisive).
  5. Name keyword rule (conference/journal words).
  -> if the available opinions AGREE: assign, high confidence.
  -> if they DISAGREE: consult DBLP (authoritative for CS via conf/ vs journals/
     key); DBLP breaks the tie. No DBLP match -> FLAG for review.
  -> no opinion at all (no DOI, junk name) -> try DBLP by title, else FLAG.
  -> book chapters that are not conference proceedings -> FLAG (neither J nor C).

Only AGREE / DBLP-resolved / single-clear-source results are written. Conflicts,
book chapters and no-evidence rows are written to an Excel review sheet instead.

Resumable: every decision is appended to a JSONL checkpoint, so a crash or a
re-run skips already-processed papers without re-hitting any API. DB writes are
batched with reconnect-on-drop (Neon pooler closes idle sockets).

  python verify_venue_authoritative.py                 # dry-run, builds review sheet
  python verify_venue_authoritative.py --commit        # also writes agreed changes
"""
import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout
from venue_classifiers import BOOK as DOI_BOOK, BOOK_CHAPTER as DOI_BOOK_CHAPTER
from venue_classifiers import classify_from_doi

setup_utf8_stdout()

# venue_classifiers' verdicts -> the VenueType strings actually stored in
# ResearchPaper.VenueType.
_DOI_VERDICT_TO_VENUE_TYPE = {DOI_BOOK: 'Book', DOI_BOOK_CHAPTER: 'BookChapter'}

UA = 'Litrix/1.0 (mailto:litrix@bu.edu.sa)'
CHECKPOINT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'venue_verify_checkpoint.jsonl')
REVIEW_XLSX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'venue_review.xlsx')

# --- name authority (corrects systematic API errors) ---------------------------
SERIAL_CONF = re.compile(
    r'(procedia'
    r'|lecture\s+notes\s+in\s+(computer|networks|electrical|business|artificial|'
    r'information|bioinformatics)'
    r'|communications\s+in\s+computer\s+and\s+information\s+science'
    r'|advances\s+in\s+intelligent\s+systems'
    r'|journal\s+of\s+physics:?\s*conference\s+series'
    r'|iop\s+conf(erence)?\.?\s+series'
    r'|aip\s+conf(erence)?\.?\s+proceedings'
    r'|web\s+of\s+conferences'
    r'|wit\s+transactions'
    r'|ceur(-ws)?\b)', re.I)
JOUR_PROC = re.compile(
    r'proceedings\s+of\s+the\s+(ieee|national\s+academy|royal\s+society)', re.I)
CONF_NAME = re.compile(
    r'\b(conference|conf\.|proceedings|proceeding|proc\.|symposium|symp\.|'
    r'workshop|congress|colloqu\w*|globecom|infocom|sigcomm|interspeech|'
    r'meeting|convention)\b', re.I)
JOUR_NAME = re.compile(
    r'\b(journal|transactions|letters|annals|bulletin|ieee\s+access|'
    r'computing\s+surveys)\b', re.I)


def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def openalex(doi):
    """(work_type, source_type, source_name) or (None, None, None)."""
    if not doi:
        return None, None, None
    try:
        m = _get('https://api.openalex.org/works/https://doi.org/' + urllib.parse.quote(doi))
    except Exception:
        return None, None, None
    src = (m.get('primary_location') or {}).get('source') or {}
    return m.get('type'), src.get('type'), src.get('display_name')


def crossref(doi):
    """(work_type, container_name) or (None, None)."""
    if not doi:
        return None, None
    try:
        m = _get('https://api.crossref.org/works/' + urllib.parse.quote(doi))['message']
    except Exception:
        return None, None
    ct = m.get('container-title') or ['']
    return m.get('type'), (ct[0] if ct else '')


def dblp(title, doi, sleep=1.5):
    """'Conference'/'Journal'/None via DBLP key prefix (CS authority)."""
    if not title:
        return None
    time.sleep(sleep)
    q = urllib.parse.urlencode({'q': title, 'format': 'json', 'h': 6})
    data = None
    for _ in range(4):
        try:
            data = _get('https://dblp.org/search/publ/api?' + q)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get('Retry-After', '5')) + 1)
                continue
            time.sleep(2)
        except Exception:
            time.sleep(2)
    if not data:
        return None
    nt = norm(title)
    ourdoi = (doi or '').lower().strip()
    best = None
    for h in data.get('result', {}).get('hits', {}).get('hit', []):
        info = h.get('info', {})
        ratio = difflib.SequenceMatcher(None, nt, norm(info.get('title', ''))).ratio()
        hdoi = (info.get('doi', '') or '').lower().strip()
        dm = bool(ourdoi and hdoi and ourdoi == hdoi)
        if dm or ratio >= 0.92:
            pref = info.get('key', '').split('/')[0]
            vt = 'Conference' if pref == 'conf' else 'Journal' if pref == 'journals' else None
            if vt and (best is None or (dm, ratio) > best[0]):
                best = ((dm, ratio), vt)
    return best[1] if best else None


def oa_verdict(work_type, source_type):
    if work_type == 'proceedings-article' or source_type == 'conference':
        return 'Conference'
    if source_type == 'journal' and work_type in (
            'article', 'review', 'letter', 'editorial', 'erratum', 'paratext'):
        return 'Journal'
    if work_type in ('book-chapter', 'book', 'book-part') or source_type in ('book series', 'ebook platform'):
        return 'BOOK'
    return None


def cr_verdict(work_type):
    if work_type in ('proceedings-article', 'proceedings'):
        return 'Conference'
    if work_type == 'journal-article':
        return 'Journal'
    if work_type in ('book-chapter', 'book-part', 'book'):
        return 'BOOK'
    return None


def name_verdict(names):
    if CONF_NAME.search(names):
        return 'Conference'
    if JOUR_NAME.search(names):
        return 'Journal'
    return None


def classify(title, doi, stored_name, dblp_sleep):
    """Return dict: verdict, review(bool), reason, and per-source opinions.

    The STORED venue name (Scholar's own publication field / the linked Journal)
    is the primary authority: a stored DOI is sometimes wrong/contaminated and
    points at a different work, so OpenAlex/Crossref metadata derived from it
    must NEVER override an informative stored name (this is what wrongly flipped
    IEEE TKDE / JMLR / IJCSNS to Conference). The DOI is consulted ONLY when the
    stored name carries no venue signal at all.
    """
    sn = stored_name or ''
    info = dict(oa='', cr='', names=sn, dblp='')

    # 0. Publisher DOI-pattern authority (highest confidence, no API call).
    # Delegates to venue_classifiers so new publisher rules are added there,
    # not as more branches here.
    doi_verdict = classify_from_doi(doi)
    if doi_verdict in _DOI_VERDICT_TO_VENUE_TYPE:
        return dict(verdict=_DOI_VERDICT_TO_VENUE_TYPE[doi_verdict], review=False,
                     reason='doi-pattern', **info)

    # 1. STORED-name authority.
    if SERIAL_CONF.search(sn):
        return dict(verdict='Conference', review=False, reason='stored-serial', **info)
    if JOUR_PROC.search(sn):
        return dict(verdict='Journal', review=False, reason='stored-jproc', **info)
    snv = name_verdict(sn)
    if snv:
        return dict(verdict=snv, review=False, reason='stored-name', **info)

    # 2. Stored name uninformative -> the DOI is the only signal we have.
    oa_t, oa_st, oa_name = openalex(doi)
    if doi:
        time.sleep(0.12)
    cr_t, cr_cont = crossref(doi)
    if doi:
        time.sleep(0.12)
    api_names = ' || '.join(x for x in (oa_name, cr_cont) if x)
    info['oa'] = f'{oa_t}/{oa_st}/{oa_name or ""}'.strip('/')
    info['cr'] = f'{cr_t}/{cr_cont or ""}'.strip('/')
    info['names'] = ' || '.join(x for x in (sn, api_names) if x)

    if api_names and SERIAL_CONF.search(api_names):
        return dict(verdict='Conference', review=False, reason='doi-serial', **info)
    if api_names and JOUR_PROC.search(api_names):
        return dict(verdict='Journal', review=False, reason='doi-jproc', **info)

    oa_v, cr_v, an_v = oa_verdict(oa_t, oa_st), cr_verdict(cr_t), name_verdict(api_names)
    votes = [v for v in (oa_v, cr_v, an_v) if v in ('Conference', 'Journal')]
    book = 'BOOK' in (oa_v, cr_v)

    if votes and all(v == votes[0] for v in votes):
        return dict(verdict=votes[0], review=False, reason='doi-agree', **info)

    if votes:  # disagreement -> DBLP tiebreak (authoritative for CS)
        d = dblp(title, doi, dblp_sleep)
        info['dblp'] = d or '(no match)'
        if d:
            return dict(verdict=d, review=False, reason='dblp-tiebreak', **info)
        return dict(verdict=(oa_v or cr_v or an_v), review=True, reason='conflict', **info)

    if book:
        # Crossref/OpenAlex agree it's a book of some kind, but "book" still
        # needs a human before it's trusted (unlike the DOI-pattern override
        # above) - 'chapter' vs standalone volume is worth flagging in the
        # suggested verdict so the review sheet doesn't need re-deriving it.
        is_chapter = 'chapter' in (oa_t or '') or 'chapter' in (cr_t or '')
        suggestion = 'BookChapter' if is_chapter else 'Book'
        return dict(verdict=suggestion, review=True, reason='book-review', **info)

    d = dblp(title, doi, dblp_sleep)
    info['dblp'] = d or '(no match)'
    if d:
        return dict(verdict=d, review=False, reason='dblp-only', **info)

    return dict(verdict=None, review=True, reason='no-evidence', **info)


def load_checkpoint():
    done = {}
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    done[r['pid']] = r
    return done


def write_review_sheet(results):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'review'
    ws.append(['PaperID', 'Title', 'StoredName', 'Current', 'Suggested',
               'Reason', 'OpenAlex', 'Crossref', 'DBLP', 'AllNames'])
    for r in results:
        if not r.get('review'):
            continue
        ws.append([r['pid'], (r.get('title') or '')[:120], (r.get('stored_name') or '')[:80],
                   r.get('current'), r.get('verdict') or '(keep)', r.get('reason'),
                   r.get('oa'), r.get('cr'), r.get('dblp'), (r.get('names') or '')[:120]])
    wb.save(REVIEW_XLSX)


def main():
    ap = argparse.ArgumentParser(description='Authoritative multi-source venue verification')
    ap.add_argument('--commit', action='store_true', help='write agreed changes to the DB')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dblp-sleep', type=float, default=1.5)
    ap.add_argument('--commit-every', type=int, default=100)
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    cur.execute('''
        SELECT rp."PaperID", rp."Title", rp."DOI",
               COALESCE(j."JournalName", rp."RawData_Log"->>'publication') AS vname,
               rp."VenueType"
        FROM "ResearchPaper" rp
        LEFT JOIN "Journals" j ON j."JournalID" = rp."JournalID"
        ORDER BY rp."PaperID"
        %s
    ''' % (('LIMIT %d' % args.limit) if args.limit else ''))
    rows = cur.fetchall()

    done = load_checkpoint()
    print('papers: %d  | already in checkpoint: %d' % (len(rows), len(done)))

    results = []
    batch = []
    stats = {}
    changed = 0

    def flush():
        nonlocal conn
        if not batch:
            return
        for _ in range(4):
            try:
                c = conn.cursor()
                c.executemany('UPDATE "ResearchPaper" SET "VenueType"=%s WHERE "PaperID"=%s', batch)
                conn.commit()
                batch.clear()
                return
            except psycopg2.OperationalError as e:
                print('  ! DB lost, reconnecting (%s)' % str(e).strip()[:50])
                try:
                    conn.close()
                except Exception:
                    pass
                time.sleep(3)
                conn = db()
        print('  ! flush failed; rows kept')

    cp = open(CHECKPOINT, 'a', encoding='utf-8')
    for i, (pid, title, doi, vname, current) in enumerate(rows, 1):
        if pid in done:
            r = done[pid]
        else:
            r = classify(title, doi, vname, args.dblp_sleep)
            r.update(pid=pid, title=title, stored_name=vname, current=current)
            cp.write(json.dumps(r, ensure_ascii=False) + '\n')
            cp.flush()
        results.append(r)
        stats[r['reason']] = stats.get(r['reason'], 0) + 1
        if not r['review'] and r['verdict'] and r['verdict'] != current:
            changed += 1
            if args.commit:
                batch.append((r['verdict'], pid))
        if i % args.commit_every == 0:
            if args.commit:
                flush()
            print('  %d/%d  changed=%d  %s' % (i, len(rows), changed, stats))
    if args.commit:
        flush()
    cp.close()

    write_review_sheet(results)
    review_n = sum(1 for r in results if r.get('review'))
    print('\nDONE. reasons:', stats)
    print('auto-changes (high confidence): %d  | flagged for review: %d' % (changed, review_n))
    print('review sheet -> %s' % REVIEW_XLSX)
    if not args.commit:
        print('DRY-RUN: no DB writes. Re-run with --commit to apply the auto-changes.')


if __name__ == '__main__':
    main()
