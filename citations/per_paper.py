import os
import sys
import time
import io
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import httpx

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()


def db():
    keepalive_kwargs = dict(
        keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=5,
    )
    return psycopg2.connect(os.getenv("DATABASE_URL"), **keepalive_kwargs)


def normalize_title(t):
    return ''.join(c for c in (t or '').lower() if c.isalnum() or c.isspace()).strip()


def extract_lastnames(authors_str):
    if not authors_str:
        return set()
    out = set()
    for chunk in authors_str.split(','):
        tokens = chunk.strip().split()
        if not tokens:
            continue
        last = ''.join(c for c in tokens[-1].lower() if c.isalpha())
        if len(last) >= 4:
            out.add(last)
    return out


def has_lastname_match(authorships, lastnames):
    if not lastnames:
        return True
    text = ' '.join(
        (a.get("author", {}).get("display_name") or '').lower()
        for a in (authorships or [])
    )
    text = ''.join(c if c.isalpha() else ' ' for c in text)
    return any(ln in text for ln in lastnames)


_local = threading.local()


def client():
    if not hasattr(_local, 'c'):
        _local.c = httpx.Client(timeout=20)
    return _local.c


def openalex_get(url, params=None, retries=5):
    p = dict(params or {})
    p["mailto"] = "ra20awn@gmail.com"
    backoff = [10, 30, 60, 120, 180]
    for attempt in range(retries):
        try:
            r = client().get(url, params=p)
            if r.status_code == 429:
                wait = backoff[min(attempt, len(backoff) - 1)]
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    wait = max(wait, min(int(ra), 300))
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(2 + attempt * 2)
    return None


def to_year_dict(counts_by_year):
    if not counts_by_year:
        return {}
    return {
        str(item['year']): int(item.get('cited_by_count') or 0)
        for item in counts_by_year if item.get('year') is not None
    }


def fetch_one(paper_row):
    pid, doi, title, authors_str = paper_row

    if doi:
        oa = openalex_get(f"https://api.openalex.org/works/doi:{doi.lower()}")
        if oa:
            cby = to_year_dict(oa.get("counts_by_year") or [])
            return (pid, cby, 'doi') if cby else (pid, {}, 'doi_empty')

    n_target = normalize_title(title)
    if len(n_target) < 15:
        return (pid, None, 'title_too_short')
    lastnames = extract_lastnames(authors_str)
    data = openalex_get(
        "https://api.openalex.org/works",
        {"search": title, "per-page": 5},
    )
    if not data:
        return (pid, None, 'no_match')

    for w in (data.get("results") or []):
        if normalize_title(w.get("title") or "") != n_target:
            continue
        if not has_lastname_match(w.get("authorships"), lastnames):
            continue
        cby = to_year_dict(w.get("counts_by_year") or [])
        return (pid, cby, 'title') if cby else (pid, {}, 'title_empty')

    return (pid, None, 'no_match')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--user", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    print(f"Connected to: {os.getenv('DATABASE_URL', 'LOCAL').split('@')[-1].split('/')[0]}")
    print(f"Workers: {args.workers}\n")

    if args.user:
        cur.execute('''
            SELECT rp."PaperID", rp."DOI", rp."Title",
                   rp."RawData_Log"->>'authors'
            FROM "ResearchPaper" rp
            JOIN "Authors" a ON a."PaperID" = rp."PaperID"
            WHERE a."UserID" = %s
              AND (rp."CitationsByYear" IS NULL OR rp."CitationsByYear"::text = '{}')
            ORDER BY rp."PaperID"
        ''', [args.user])
    else:
        cur.execute('''
            SELECT rp."PaperID", rp."DOI", rp."Title",
                   rp."RawData_Log"->>'authors'
            FROM "ResearchPaper" rp
            WHERE (rp."CitationsByYear" IS NULL OR rp."CitationsByYear"::text = '{}')
            ORDER BY rp."PaperID"
        ''')
    papers = cur.fetchall()
    if args.limit:
        papers = papers[:args.limit]

    print(f"Found {len(papers)} papers\n")
    if not papers:
        return

    n_doi = n_title = n_empty = n_failed = 0
    completed = 0
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, p): p[0] for p in papers}
        for fut in as_completed(futures):
            try:
                pid, cby, method = fut.result()
            except Exception:
                n_failed += 1
                completed += 1
                continue

            if cby:
                cur.execute(
                    'UPDATE "ResearchPaper" SET "CitationsByYear" = %s WHERE "PaperID" = %s',
                    (Json(cby), pid),
                )
                if method == 'doi': n_doi += 1
                else: n_title += 1
            elif method in ('doi_empty', 'title_empty'):
                n_empty += 1
            else:
                n_failed += 1

            completed += 1
            elapsed = time.time() - t_start
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (len(papers) - completed) / rate if rate > 0 else 0
            pct = int(100 * completed / len(papers))
            bar = '█' * (pct // 5) + '░' * (20 - pct // 5)
            print(
                f"  [{bar}] {pct:3d}%  {completed}/{len(papers)}  "
                f"doi={n_doi} title={n_title} empty={n_empty} failed={n_failed}  "
                f"ETA: {int(eta/60)}m{int(eta%60)}s",
                flush=True,
            )
            if completed % 10 == 0:
                conn.commit()

    conn.commit()
    print(f"\nDone — DOI: {n_doi}, title: {n_title}, empty: {n_empty}, failed: {n_failed}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
