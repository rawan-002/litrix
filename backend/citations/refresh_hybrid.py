"""Hybrid per-paper citation refresher — OpenAlex first, SerpAPI Scholar fallback.

Unlike citations/per_paper.py (OpenAlex only), this covers the local/Arabic
venues OpenAlex misses and the Scholar counts researchers actually compare
against. Stage 1 is OpenAlex (free): DOI lookup, else a strict title+lastname
match. Stage 2 is a SerpAPI Scholar title search for whatever Stage 1 couldn't
resolve, hard-capped by --serp-budget so a run can't silently drain the quota.

Routing is source-aware: papers with "ResearchPaper"."Source" = 'Scholar' (i.e.
we originally pulled them from that researcher's own Google Scholar profile)
may fall back to Stage 2 SerpAPI Scholar search, since that's the same
authoritative source and a title search against it is trustworthy. Papers from
ORCID/manual imports have no such cross-check, so they run OpenAlex-only in
strict mode (exact title + ALL author lastnames + pub-year match) and are left
unresolved rather than risk a SerpAPI mismatch attributing another author's
citations to them.

In apply mode it writes "ResearchPaper"."CitationsByYear" (Scholar only gives a
total, so a Scholar-only result becomes {"<PubYear>": total}, flagged
`serp_total_only`) and the "RawData_Log" total. It never touches
"Researcher"."CitationsByYear" — that author-level graph is the single source
of truth for dashboard totals (see CLAUDE.md).

    python citations/refresh_hybrid.py --dry-run --user 106
    python citations/refresh_hybrid.py --apply --user 106 --limit 3 --serp-budget 2
    python citations/refresh_hybrid.py --apply --serp-budget 50
    python citations/refresh_hybrid.py --apply --all --no-serp
"""

import os
import sys
import io
import csv
import json
import time
import argparse
import threading
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import httpx

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

SERP_KEY = os.getenv("SERP_API_KEY")          # NOTE: SERP_API_KEY (repo convention)
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "noreply@example.com")
OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY")
AUDIT_DIR = PROJECT_ROOT / "data" / "citation_audit"


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def normalize_title(t):
    return ''.join(c for c in (t or '').lower() if c.isalnum() or c.isspace()).strip()


def similarity(a, b):
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


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


def has_lastname_match(authorships, lastnames, require_all=False):
    if not lastnames:
        return True
    text = ' '.join(
        (a.get("author", {}).get("display_name") or '').lower()
        for a in (authorships or [])
    )
    text = ''.join(c if c.isalpha() else ' ' for c in text)
    return all(ln in text for ln in lastnames) if require_all else any(ln in text for ln in lastnames)


_local = threading.local()


def client():
    if not hasattr(_local, 'c'):
        _local.c = httpx.Client(
            timeout=20,
            headers={"User-Agent": f"LitrixRefreshHybrid/1.0 (mailto:{CONTACT_EMAIL})"},
        )
    return _local.c


def openalex_get(url, params=None, retries=5):
    p = dict(params or {})
    p["mailto"] = CONTACT_EMAIL
    if OPENALEX_API_KEY:
        p["api_key"] = OPENALEX_API_KEY
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
            if r.status_code == 404:
                return None
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
        for item in counts_by_year
        if item.get('year') is not None and (item.get('cited_by_count') or 0) > 0
    }


def openalex_fetch_one(paper_row, strict=False):
    """Returns (pid, year_dict|None, method). DOI lookup first (deterministic,
    trusted even in strict mode), else a title search.

    strict=True is used for papers NOT sourced from Google Scholar (ORCID /
    manual imports) — these have no SerpAPI cross-check available for us, so
    a wrong OpenAlex title match would silently attribute another author's
    citations. Strict mode requires: exact normalized-title match, ALL known
    author lastnames present (not just one), AND publication year within ±1 —
    any single miss rejects the candidate instead of falling through.
    """
    pid, doi, title, authors_str, pub_year = paper_row[0], paper_row[1], paper_row[2], paper_row[3], paper_row[4]

    if doi:
        clean = str(doi).strip().lower().removeprefix("https://doi.org/").removeprefix("doi.org/")
        oa = openalex_get(f"https://api.openalex.org/works/doi:{clean}")
        if oa:
            cby = to_year_dict(oa.get("counts_by_year") or [])
            return (pid, cby, 'doi') if cby else (pid, {}, 'doi_empty')

    n_target = normalize_title(title)
    if len(n_target) < 15:
        return (pid, None, 'title_too_short')
    lastnames = extract_lastnames(authors_str)
    if strict and not lastnames:
        # No author lastnames to verify a title-only match against — too
        # risky to accept in strict mode; leave unresolved rather than guess.
        return (pid, None, 'strict_no_lastnames')

    data = openalex_get(
        "https://api.openalex.org/works",
        {"search": title, "per-page": 8 if strict else 5},
    )
    if not data:
        return (pid, None, 'no_match')

    for w in (data.get("results") or []):
        if normalize_title(w.get("title") or "") != n_target:
            continue
        if not has_lastname_match(w.get("authorships"), lastnames, require_all=strict):
            continue
        if strict and pub_year:
            w_year = w.get("publication_year")
            if w_year and abs(int(w_year) - int(pub_year)) > 1:
                continue
        cby = to_year_dict(w.get("counts_by_year") or [])
        method = 'title_strict' if strict else 'title'
        return (pid, cby, method) if cby else (pid, {}, method + '_empty')

    return (pid, None, 'no_match')


def serp_scholar_search(title, retries=5):
    # organic_results for a Scholar title query, [] on failure. Retry/backoff
    # mirrors scrapers/scholar.py serp_page().
    from serpapi import GoogleSearch
    for attempt in range(retries):
        try:
            result = GoogleSearch({
                "engine": "google_scholar",
                "q": title,
                "num": 5,
                "hl": "en",
                "api_key": SERP_KEY,
            }).get_dict()
            if result.get("search_metadata", {}).get("status") == "Error":
                return []
            if "error" in result:
                # rate limit / quota messages land here — back off and retry
                time.sleep(2 * (attempt + 1))
                continue
            return result.get("organic_results", []) or []
        except Exception:
            time.sleep(2 * (attempt + 1))
    return []


def serp_best_match(title, results):
    # Returns (cited_by_total|None, matched_title, score) for the highest-
    # similarity title.
    best = (None, "", 0.0)
    for res in results:
        cand = res.get("title", "")
        score = similarity(title, cand)
        if score > best[2]:
            cited = (res.get("inline_links", {}) or {}).get("cited_by", {}).get("total")
            best = (cited, cand, score)
    return best


def write_paper(cur, pid, year_dict, total, write_citations_table):
    """Update CitationsByYear (when we have one) and the RawData_Log total.

    The dashboard reads
        COALESCE((RawData_Log->'cited_by'->>'value')::int,
                 (RawData_Log->>'cited_by_count')::int, 0)
    so for Scholar-scraped papers that carry a cited_by.value object COALESCE
    reads it first — we have to patch cited_by.value too, otherwise the
    displayed number would never move.
    """
    if year_dict is not None:
        cur.execute(
            'UPDATE "ResearchPaper" SET "CitationsByYear" = %s WHERE "PaperID" = %s',
            (Json(year_dict), pid),
        )
    cur.execute(
        '''
        UPDATE "ResearchPaper"
        SET "RawData_Log" = CASE
            WHEN jsonb_typeof(COALESCE("RawData_Log", '{}'::jsonb)->'cited_by') = 'object'
            THEN jsonb_set(
                     jsonb_set(COALESCE("RawData_Log", '{}'::jsonb),
                               '{cited_by_count}', to_jsonb(%s::int)),
                     '{cited_by,value}', to_jsonb(%s::int))
            ELSE jsonb_set(COALESCE("RawData_Log", '{}'::jsonb),
                           '{cited_by_count}', to_jsonb(%s::int))
        END
        WHERE "PaperID" = %s
        ''',
        (total, total, total, pid),
    )
    if write_citations_table:
        cur.execute('SAVEPOINT cit_tables')
        try:
            cur.execute(
                '''
                INSERT INTO "Citations" ("PaperID", "CitationsCount", "LastUpdate")
                VALUES (%s, %s, NOW())
                ON CONFLICT ("PaperID")
                DO UPDATE SET "CitationsCount" = EXCLUDED."CitationsCount",
                              "LastUpdate" = NOW()
                ''',
                (pid, total),
            )
            cur.execute(
                'INSERT INTO "CitationsHistory" ("PaperID", "Count", "CaptureDate") '
                'VALUES (%s, %s, CURRENT_DATE)',
                (pid, total),
            )
            cur.execute('RELEASE SAVEPOINT cit_tables')
        except Exception as e:
            cur.execute('ROLLBACK TO SAVEPOINT cit_tables')
            print(f"     [warn] Citations table upsert failed for {pid}: {e}")


def select_papers(cur, user_id, only_missing, limit, years=None):
    # Rows: (PaperID, DOI, Title, authors_str, PubYear, old_total, has_year_dict, Source).
    where = ['TRUE']
    params = []
    if only_missing:
        where.append('(rp."CitationsByYear" IS NULL OR rp."CitationsByYear"::text = \'{}\')')
    if years:
        where.append('rp."PubYear" = ANY(%s)')
        params.append(list(years))

    join = ''
    if user_id is not None:
        join = 'JOIN "Authors" a ON a."PaperID" = rp."PaperID"'
        where.append('a."UserID" = %s')
        params.append(user_id)

    sql = f'''
        SELECT DISTINCT rp."PaperID", rp."DOI", rp."Title",
               rp."RawData_Log"->>'authors',
               rp."PubYear",
               COALESCE(
                   (rp."RawData_Log"->'cited_by'->>'value')::int,
                   (rp."RawData_Log"->>'cited_by_count')::int,
                   0) AS old_total,
               (rp."CitationsByYear" IS NOT NULL
                AND rp."CitationsByYear"::text <> '{{}}') AS has_year_dict,
               rp."Source"
        FROM "ResearchPaper" rp
        {join}
        WHERE {' AND '.join(where)}
        ORDER BY rp."PaperID"
    '''
    if limit:
        sql += f' LIMIT {int(limit)}'
    cur.execute(sql, params)
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Build the change report only — no DB writes. "
                           "OpenAlex calls still happen (free); SerpAPI stays off "
                           "unless --serp-budget is passed explicitly.")
    mode.add_argument("--apply", action="store_true",
                      help="Fetch and write CitationsByYear + RawData_Log totals.")
    ap.add_argument("--user", type=int, default=None, help="Scope to one researcher (UserID).")
    ap.add_argument("--year", type=str, default=None,
                    help="Restrict to PubYear(s), comma-separated (e.g. 2025,2026).")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N papers.")
    ap.add_argument("--all", action="store_true",
                    help="Refresh every paper in scope, not just those missing "
                         "CitationsByYear (default = only missing).")
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="Min title-similarity to accept a Scholar match (default 0.85).")
    ap.add_argument("--serp-budget", type=int, default=None,
                    help="HARD cap on SerpAPI calls this run "
                         "(default: 50 in --apply, 0 in --dry-run).")
    ap.add_argument("--no-serp", action="store_true", help="OpenAlex only — skip Stage 2.")
    ap.add_argument("--write-citations-table", action="store_true",
                    help='Also UPSERT "Citations" + append "CitationsHistory".')
    ap.add_argument("--report", type=str, default=None, help="CSV report path override.")
    ap.add_argument("--workers", type=int, default=4, help="OpenAlex thread pool size.")
    ap.add_argument("--skip-openalex", action="store_true",
                    help="Skip Stage 1 OpenAlex entirely (e.g. daily OpenAlex quota "
                         "exhausted). Scholar-sourced papers go straight to Stage 2 "
                         "SerpAPI; non-Scholar papers are left unresolved (no OpenAlex "
                         "cross-check available, so strict mode has nothing to run).")
    args = ap.parse_args()

    serp_budget = args.serp_budget
    if serp_budget is None:
        serp_budget = 50 if args.apply else 0
    use_serp = not args.no_serp and serp_budget > 0
    if use_serp and not SERP_KEY:
        sys.exit("ERROR: SERP_API_KEY missing from .env (or pass --no-serp).")

    years = None
    if args.year:
        years = [int(y.strip()) for y in args.year.split(',') if y.strip()]

    conn = db()
    cur = conn.cursor()
    target = os.getenv('DATABASE_URL', 'LOCAL')
    print(f"Connected to: {target.split('@')[-1].split('/')[0] if '@' in target else 'LOCAL'}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'} | "
          f"scope: {'all papers' if args.all else 'missing CitationsByYear only'}"
          f"{f' | user {args.user}' if args.user else ''}"
          f"{f' | years {years}' if years else ''}")
    print(f"SerpAPI fallback: {'ON (budget ' + str(serp_budget) + ' calls)' if use_serp else 'OFF'}")
    print(f"OpenAlex API key: {'loaded (higher quota)' if OPENALEX_API_KEY else 'none (free polite pool)'}\n")

    papers = select_papers(cur, args.user, only_missing=not args.all, limit=args.limit, years=years)
    print(f"Found {len(papers)} papers in scope\n")
    if not papers:
        return

    meta = {p[0]: {"doi": p[1], "title": p[2], "pub_year": p[4],
                   "old_total": p[5] or 0, "has_year_dict": p[6], "source": p[7]}
            for p in papers}
    n_scholar = sum(1 for p in papers if p[7] == 'Scholar')
    print(f"Source split: {n_scholar} from Scholar (SerpAPI-eligible for residuals) | "
          f"{len(papers) - n_scholar} from ORCID/Manual/other "
          f"(OpenAlex strict-match only, no SerpAPI cross-check)\n")

    report_rows = []
    stats = {"oa_doi": 0, "oa_title": 0, "oa_empty": 0,
             "serp_ok": 0, "serp_flagged": 0, "unresolved": 0, "serp_calls": 0,
             "strict_unresolved": 0}

    residuals = []              # pids eligible for Stage 2 (Scholar-sourced only)
    strict_unresolved = []      # non-Scholar pids OpenAlex couldn't resolve — left as-is
    oa_results = {}              # pid -> (year_dict, method)

    if args.skip_openalex:
        print("=== Stage 1: OpenAlex SKIPPED (--skip-openalex) ===")
        for p in papers:
            pid, source = p[0], p[7]
            oa_results[pid] = (None, 'skipped_openalex')
            if source == 'Scholar':
                residuals.append(pid)
            else:
                strict_unresolved.append(pid)
        print(f"  {len(residuals)} Scholar papers routed straight to Stage 2, "
              f"{len(strict_unresolved)} non-Scholar papers left unresolved "
              f"(no OpenAlex cross-check available)\n")
    else:
        print(f"=== Stage 1: OpenAlex ({args.workers} workers) ===")
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(openalex_fetch_one, p, p[7] != 'Scholar'): p[0] for p in papers}
            for fut in as_completed(futures):
                pid_fallback = futures[fut]
                try:
                    pid, cby, method = fut.result()
                except Exception:
                    pid, cby, method = pid_fallback, None, 'error'
                oa_results[pid] = (cby, method)
                if not cby:
                    if meta[pid]["source"] == 'Scholar':
                        residuals.append(pid)
                    else:
                        strict_unresolved.append(pid)
                done += 1
                if done % 25 == 0 or done == len(papers):
                    print(f"  {done}/{len(papers)} processed "
                          f"(Scholar residuals so far: {len(residuals)}, "
                          f"strict/unresolved: {len(strict_unresolved)})", flush=True)

    residuals.sort()
    stats["strict_unresolved"] = len(strict_unresolved)
    print(f"OpenAlex resolved {len(papers) - len(residuals) - len(strict_unresolved)}/{len(papers)} — "
          f"{len(residuals)} Scholar residuals go to Stage 2, "
          f"{len(strict_unresolved)} non-Scholar papers left unresolved "
          f"(strict mode, no SerpAPI fallback to avoid mismatches)\n")

    def keep_alive():
        # SerpAPI calls are pure network I/O — the DB connection sits idle for
        # the whole Stage 2 pass (minutes), which is long enough for Neon's
        # pooler to drop it. A periodic no-op query keeps it (or a reconnect
        # here, cheaply, catches the drop before it kills a real write).
        nonlocal conn, cur
        try:
            cur.execute("SELECT 1")
        except psycopg2.OperationalError:
            try:
                conn.close()
            except Exception:
                pass
            conn = db()
            cur = conn.cursor()

    serp_results = {}       # pid -> (total|None, matched_title, score, status)
    if use_serp and residuals:
        print(f"=== Stage 2: SerpAPI Scholar title search — Scholar-sourced papers only "
              f"(budget {serp_budget} of {len(residuals)} residuals) ===")
        for pid in residuals:
            if stats["serp_calls"] >= serp_budget:
                print(f"  budget exhausted — {len(residuals) - stats['serp_calls']} "
                      f"residuals left untouched")
                break
            title = meta[pid]["title"]
            results = serp_scholar_search(title)
            stats["serp_calls"] += 1
            cited, matched, score = serp_best_match(title, results)
            if not results:
                status = "NOT_FOUND_ON_SCHOLAR"
            elif cited is None:
                status = "NO_CITED_BY_FIELD"
            elif score < args.threshold:
                status = "LOW_CONFIDENCE_MATCH"
            else:
                status = "OK"
            serp_results[pid] = (int(cited) if status == "OK" else None,
                                 matched, score, status)
            print(f"  [{stats['serp_calls']}/{serp_budget}] paper {pid}: {status}"
                  f"{f' total={cited} score={score:.2f}' if status == 'OK' else ''}",
                  flush=True)
            if stats["serp_calls"] % 15 == 0:
                keep_alive()
            time.sleep(1.0)
        print()

    def commit_write(pid, year_dict, total, write_citations_table):
        # One retry with a fresh connection — a dropped connection here would
        # otherwise silently discard an entire batch of already-paid-for
        # SerpAPI/OpenAlex results.
        nonlocal conn, cur
        for attempt in range(2):
            try:
                write_paper(cur, pid, year_dict, total, write_citations_table)
                conn.commit()
                return True
            except psycopg2.OperationalError as e:
                print(f"     [warn] DB connection dropped writing paper {pid}, "
                      f"reconnecting (attempt {attempt + 1}/2): {e}")
                try:
                    conn.close()
                except Exception:
                    pass
                conn = db()
                cur = conn.cursor()
        print(f"     [error] paper {pid} not written after retry — will still be "
              f"'missing' next run")
        return False

    n_written = 0
    for p in papers:
        pid = p[0]
        m = meta[pid]
        cby, oa_method = oa_results.get(pid, (None, 'error'))
        row = {
            "paper_id": pid, "doi": m["doi"] or "",
            "title": (m["title"] or "")[:80],
            "stage": "none", "status": oa_method,
            "old_total": m["old_total"], "new_total": m["old_total"], "delta": 0,
            "score": "", "matched_title": "",
            "year_dict_source": "unchanged", "accepted": "no",
        }

        if cby:
            total = sum(cby.values())
            row.update(stage="openalex", status=oa_method,
                       new_total=total, delta=total - m["old_total"],
                       year_dict_source="openalex", accepted="yes")
            if oa_method == 'doi': stats["oa_doi"] += 1
            else: stats["oa_title"] += 1
            if args.apply and commit_write(pid, cby, total, args.write_citations_table):
                n_written += 1

        elif pid in serp_results:
            total, matched, score, status = serp_results[pid]
            row.update(stage="serpapi", status=status,
                       score=round(score, 3), matched_title=(matched or "")[:80])
            if status == "OK":
                stats["serp_ok"] += 1
                row.update(new_total=total, delta=total - m["old_total"], accepted="yes")
                # Scholar gives a total only, so keep any existing per-year
                # dict and synthesize {"PubYear": total} only when none exists.
                if m["has_year_dict"]:
                    year_dict = None
                    row["year_dict_source"] = "unchanged"
                elif m["pub_year"]:
                    year_dict = {str(m["pub_year"]): total}
                    row["year_dict_source"] = "serp_total_only"
                else:
                    year_dict = None
                    row["year_dict_source"] = "none"
                if args.apply and total >= m["old_total"]:
                    if commit_write(pid, year_dict, total, args.write_citations_table):
                        n_written += 1
                elif args.apply:
                    row["accepted"] = "no (lower than current)"
            else:
                stats["serp_flagged"] += 1
        else:
            if oa_method in ('doi_empty', 'title_empty', 'title_strict_empty'):
                stats["oa_empty"] += 1
                row.update(stage="openalex", status=oa_method)
            elif m["source"] == 'Scholar':
                # Genuinely unresolved Scholar-sourced paper (e.g. SerpAPI
                # budget ran out before reaching it) — NOT the non-Scholar
                # strict_unresolved set, which is already counted separately.
                stats["unresolved"] += 1

        report_rows.append(row)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = Path(args.report) if args.report else AUDIT_DIR / f"refresh_hybrid_{ts}.csv"
    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(report_rows[0].keys()))
        w.writeheader()
        w.writerows(report_rows)

    audit_path = AUDIT_DIR / f"refresh_hybrid_{ts}.json"
    audit_path.write_text(
        json.dumps({"args": vars(args), "stats": stats, "written": n_written,
                    "rows": report_rows}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print("=" * 70)
    print(f"  OpenAlex via DOI      : {stats['oa_doi']}")
    print(f"  OpenAlex via title    : {stats['oa_title']}")
    print(f"  OpenAlex found, 0 cit : {stats['oa_empty']}")
    print(f"  SerpAPI accepted (OK) : {stats['serp_ok']}  "
          f"(calls used: {stats['serp_calls']}/{serp_budget if use_serp else 0})")
    print(f"  SerpAPI flagged       : {stats['serp_flagged']}")
    print(f"  Unresolved (Scholar)  : {stats['unresolved']}")
    print(f"  Unresolved (strict, non-Scholar, no SerpAPI attempted): {stats['strict_unresolved']}")
    print(f"  DB rows written       : {n_written}{'' if args.apply else '  (dry-run)'}")
    print(f"\n  CSV report : {report_path}")
    print(f"  JSON audit : {audit_path}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
