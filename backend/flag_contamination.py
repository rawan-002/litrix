"""Layer-2 contamination triage: flag Litrix papers that are NOT in a
researcher's clean OpenAlex profile.

Google Scholar profiles for common names accumulate other people's papers
([[scholar-common-name-contamination]]). OpenAlex clusters an author's work far
more cleanly. So for each researcher we take their OpenAlex work set as the
"truth" and flag any Litrix-attributed paper that isn't in it -- these are the
suspected contaminants (e.g. a pediatric-surgery paper on a CS professor).

Unlike affiliation_verifier.py this catches no-DOI papers too (matches on
normalized title), and targets the ROOT attribution, not just the dashboard.

Read-only: writes a JSON report only, never touches the DB. The human decides
what to detach. OpenAlex only (few calls/researcher -- budget-safe).
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
    print("pip install requests"); sys.exit(1)

setup_utf8_stdout()

UA = 'Litrix-ContaminationTriage/1.0 (mailto:ra20awn@gmail.com)'
ALBAHA_ROR = '0270eb240'
DELAY = 0.15
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'contamination_review.json')


def norm_title(t):
    if not t:
        return ''
    t = unicodedata.normalize('NFKD', t)
    t = ''.join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r'[^a-z0-9]', '', t)


def norm_doi(d):
    if not d:
        return ''
    d = d.strip().lower()
    for p in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if d.startswith(p):
            d = d[len(p):]
    return d


def resolve_openalex_id(name):
    """Find an Al-Baha-affiliated OpenAlex author by display name."""
    try:
        r = requests.get('https://api.openalex.org/authors',
                         params={'filter': f'affiliations.institution.ror:{ALBAHA_ROR}',
                                 'search': name, 'per-page': 5,
                                 'select': 'id,display_name,works_count'},
                         headers={'User-Agent': UA}, timeout=20)
        time.sleep(DELAY)
        if r.status_code != 200:
            return None, None
        res = (r.json() or {}).get('results') or []
        if not res:
            return None, None
        top = res[0]
        return top['id'].rsplit('/', 1)[-1], top.get('display_name')
    except (requests.RequestException, ValueError, KeyError):
        return None, None


def openalex_works(author_id):
    """All works for an OpenAlex author id -> (set of norm DOIs, set of norm titles, count)."""
    dois, titles = set(), set()
    cursor = '*'
    n = 0
    for _ in range(6):  # cap 6 pages (1200 works) -- plenty
        try:
            r = requests.get('https://api.openalex.org/works',
                             params={'filter': f'author.id:{author_id}',
                                     'select': 'doi,title', 'per-page': 200, 'cursor': cursor},
                             headers={'User-Agent': UA}, timeout=25)
            time.sleep(DELAY)
            if r.status_code != 200:
                break
            data = r.json() or {}
        except (requests.RequestException, ValueError):
            break
        for w in data.get('results') or []:
            n += 1
            if w.get('doi'):
                dois.add(norm_doi(w['doi']))
            if w.get('title'):
                titles.add(norm_title(w['title']))
        cursor = (data.get('meta') or {}).get('next_cursor')
        if not cursor:
            break
    return dois, titles, n


def main():
    ap = argparse.ArgumentParser(description='Flag papers not in a researcher OpenAlex profile')
    ap.add_argument('--users', type=str, required=True, help='comma-separated Users.UserID list')
    args = ap.parse_args()
    uids = [int(x) for x in args.users.split(',') if x.strip()]

    conn = db(); cur = conn.cursor()
    report = []
    for uid in uids:
        cur.execute('''SELECT u."ScholarDisplayName", u."FullName_Ar", r."OpenAlex_AuthorID"
                       FROM "Users" u LEFT JOIN "Researcher" r ON r."UserID"=u."UserID"
                       WHERE u."UserID"=%s''', [uid])
        row = cur.fetchone()
        if not row:
            continue
        name = row[0] or row[1] or str(uid)
        oa_id = row[2]
        resolved_name = None
        if not oa_id:
            oa_id, resolved_name = resolve_openalex_id(name)
        if not oa_id:
            report.append({'uid': uid, 'name': name, 'error': 'no OpenAlex id resolved'})
            print('UID %s (%s): could not resolve OpenAlex id' % (uid, name))
            continue

        oa_dois, oa_titles, oa_n = openalex_works(oa_id)

        cur.execute('''SELECT rp."PaperID", rp."PubYear", rp."DOI", rp."Title",
                          COALESCE(j."JournalName", rp."RawData_Log"->>'publication'),
                          rp."AffiliationVerified"
                       FROM "Authors" a JOIN "ResearchPaper" rp ON rp."PaperID"=a."PaperID"
                       LEFT JOIN "Journals" j ON j."JournalID"=rp."JournalID"
                       WHERE a."UserID"=%s''', [uid])
        papers = cur.fetchall()
        suspects = []
        for pid, yr, doi, title, venue, av in papers:
            in_oa = (norm_doi(doi) in oa_dois and doi) or (norm_title(title) in oa_titles and title)
            if not in_oa:
                suspects.append({'pid': pid, 'year': yr, 'doi': doi, 'title': title,
                                 'venue': venue, 'affiliation_verified': av})
        report.append({'uid': uid, 'name': name, 'openalex_id': oa_id,
                       'openalex_resolved_name': resolved_name, 'openalex_works': oa_n,
                       'litrix_papers': len(papers), 'suspect_count': len(suspects),
                       'suspects': suspects})
        print('UID %-4s %-28s OA_works=%-4s litrix=%-4s SUSPECTS=%s'
              % (uid, name[:28], oa_n, len(papers), len(suspects)))

    json.dump(report, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\nWrote', OUT)


if __name__ == '__main__':
    main()
