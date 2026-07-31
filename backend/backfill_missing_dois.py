"""Backfill missing DOIs for papers that have none, via OpenAlex title search.

Purpose: ~370 attributed papers have no stored DOI, so the affiliation
verifier (all tiers need a DOI) can never resolve them -- they sit pending and
count as Al-Baha by default. This finds the DOI so a later verifier run can
inspect the real per-author affiliations.

SAFETY (this is the dangerous part): assigning the WRONG DOI makes the verifier
read a DIFFERENT paper's affiliation -- the exact class of error behind the
602-paper contamination. So a candidate DOI is accepted ONLY when:
  * normalized title matches near-exactly (token Jaccard >= 0.92, and the
    candidate is not obviously a different work), AND
  * publication year is within +/-1 of ours (when we have a year), AND
  * the candidate actually has a DOI.
Corrigenda/errata are handled: an 'x' paper whose title starts with
"corrigendum"/"erratum" only matches a candidate with the same prefix.
Everything else is left NULL and reported for manual review -- we NEVER guess.

Idempotent + guarded: UPDATE ... WHERE "PaperID"=%s AND ("DOI" IS NULL OR "DOI"='').
Writes an evidence column note. Dry-run by default; --commit to write.
Polite OpenAlex rate limit; resumable via its own checkpoint.
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

setup_utf8_stdout()

CONTACT = os.environ.get('LITRIX_CONTACT_EMAIL', 'ra20awn@gmail.com')
UA = f'Litrix-DOIBackfill/1.0 (mailto:{CONTACT})'
OPENALEX_DELAY = 0.15
CHECKPOINT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'doi_backfill_checkpoint.jsonl')

_STOP = {'a', 'an', 'the', 'of', 'for', 'and', 'in', 'on', 'to', 'with', 'via', 'using'}


def norm_tokens(title):
    """Lowercase, strip diacritics + punctuation, drop stopwords -> token set."""
    if not title:
        return set()
    t = unicodedata.normalize('NFKD', title)
    t = ''.join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    toks = [w for w in t.split() if w and w not in _STOP]
    return set(toks)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _prefix_kind(title):
    t = (title or '').strip().lower()
    for k in ('corrigendum', 'erratum', 'correction to', 'retraction'):
        if t.startswith(k):
            return k
    return ''


def _cand(title, doi, year):
    return {'title': title or '', 'doi': doi, 'publication_year': year}


def crossref_search(title):
    """Title search via Crossref (free, no daily budget). Returns candidate
    dicts {title, doi, publication_year} or None on failure."""
    url = 'https://api.crossref.org/works'
    params = {'query.bibliographic': title[:300], 'rows': 5,
              'select': 'DOI,title,issued'}
    try:
        r = requests.get(url, params=params, headers={'User-Agent': UA}, timeout=20)
        if r.status_code != 200:
            return None
        items = ((r.json() or {}).get('message') or {}).get('items') or []
    except (requests.RequestException, ValueError):
        return None
    out = []
    for it in items:
        doi = it.get('DOI')
        t = (it.get('title') or [''])[0]
        yr = None
        try:
            yr = (it.get('issued', {}).get('date-parts') or [[None]])[0][0]
        except (IndexError, TypeError):
            yr = None
        out.append(_cand(t, doi, yr))
    return out


def openalex_search(title):
    """Fallback: OpenAlex title search (subject to a daily $ budget now).
    Returns candidate dicts or None."""
    url = 'https://api.openalex.org/works'
    params = {'search': title[:300], 'per-page': 5,
              'select': 'id,title,doi,publication_year'}
    try:
        r = requests.get(url, params=params, headers={'User-Agent': UA}, timeout=20)
        if r.status_code != 200:
            return None
        res = (r.json() or {}).get('results') or []
    except (requests.RequestException, ValueError):
        return None
    return [_cand(c.get('title'), (c.get('doi') or '').replace('https://doi.org/', ''),
                  c.get('publication_year')) for c in res]


def search_candidates(title):
    """Crossref first (free); OpenAlex only as a fallback when Crossref fails."""
    c = crossref_search(title)
    if c is not None:
        return c, 'crossref'
    c = openalex_search(title)
    if c is not None:
        return c, 'openalex'
    return None, None


def best_match(our_title, our_year, candidates):
    """Return (doi, evidence) for the best acceptable candidate, or (None, why)."""
    our_tok = norm_tokens(our_title)
    our_kind = _prefix_kind(our_title)
    best = None
    for c in candidates:
        doi = (c.get('doi') or '').replace('https://doi.org/', '')
        if not doi:
            continue
        cand_title = c.get('title') or ''
        if _prefix_kind(cand_title) != our_kind:
            continue  # don't match a corrigendum to the original, or vice-versa
        sim = jaccard(our_tok, norm_tokens(cand_title))
        yr = c.get('publication_year')
        year_ok = (our_year is None or yr is None or abs(int(yr) - int(our_year)) <= 1)
        if sim >= 0.92 and year_ok:
            if best is None or sim > best[1]:
                best = (doi, sim, cand_title, yr)
    if best:
        return best[0], {'sim': round(best[1], 3), 'cand_title': best[2][:120],
                         'cand_year': best[3]}
    return None, {'reason': 'no_confident_match',
                  'n_candidates': len(candidates)}


def load_done():
    done = {}
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    done[r['pid']] = r
    return done


def main():
    ap = argparse.ArgumentParser(description='Backfill missing DOIs via OpenAlex title search')
    ap.add_argument('--commit', action='store_true', help='write DOIs (default: dry-run)')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    cur.execute('''
        SELECT rp."PaperID", rp."Title", rp."PubYear"
        FROM "ResearchPaper" rp
        WHERE (rp."DOI" IS NULL OR rp."DOI" = '')
          AND EXISTS (SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")
        ORDER BY rp."PaperID"
        %s
    ''' % (('LIMIT %d' % args.limit) if args.limit else ''))
    rows = cur.fetchall()
    done = load_done()
    print('No-DOI attributed papers: %d | cached: %d' % (len(rows), len(done)))

    cp = open(CHECKPOINT, 'a', encoding='utf-8')
    found, updated = 0, 0
    for i, (pid, title, year) in enumerate(rows, 1):
        if pid in done and done[pid].get('doi'):
            r = done[pid]  # reuse only confirmed matches; retry every miss/failure via Crossref
        else:
            cands, src = search_candidates(title or '')
            time.sleep(OPENALEX_DELAY)
            if cands is None:
                r = {'pid': pid, 'doi': None, 'ev': {'reason': 'search_failed'}}
            else:
                doi, ev = best_match(title, year, cands)
                ev['src'] = src
                r = {'pid': pid, 'doi': doi, 'ev': ev, 'title': (title or '')[:120]}
            cp.write(json.dumps(r, ensure_ascii=False) + '\n')
            cp.flush()

        if r.get('doi'):
            found += 1
            print('  P%-6s FOUND %-40s sim=%s' % (pid, r['doi'][:40], r['ev'].get('sim')))
            if args.commit:
                cur.execute(
                    'UPDATE "ResearchPaper" SET "DOI"=%s '
                    'WHERE "PaperID"=%s AND ("DOI" IS NULL OR "DOI"=\'\')',
                    [r['doi'], pid])
                updated += cur.rowcount
        if i % 50 == 0:
            if args.commit:
                conn.commit()
            print('  ... %d/%d  found=%d' % (i, len(rows), found))
    if args.commit:
        conn.commit()
    cp.close()

    print('=' * 60)
    print('Confident DOIs found: %d of %d' % (found, len(rows)))
    print('Rows updated: %d' % updated if args.commit else 'DRY-RUN (no writes).')
    print('Left without DOI (manual review): %d' % (len(rows) - found))


if __name__ == '__main__':
    main()
