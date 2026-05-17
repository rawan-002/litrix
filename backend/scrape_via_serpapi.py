"""
Scrape Google Scholar via SerpApi (https://serpapi.com)
========================================================
A robust scraper for researchers' Scholar profiles. Uses SerpApi to
sidestep Google's IP rate limits — SerpApi maintains a rotating
proxy pool, so you can hammer it as fast as your plan allows.

WHAT IT DOES (per Scholar ID)
  1. Fetch the author profile         (GET /search.json engine=google_scholar_author)
  2. Update Researcher.ResearchInterests + LastSyncedAt + interests-timestamp
  3. For each article in the profile:
       a. Normalise the title (lowercase, strip punctuation)
       b. Look up matching ResearchPaper rows (NormalizedTitle prefix)
       c. If found AND no Authors link yet -> insert Authors(PaperID, UserID)
       d. If NOT found -> log to "missing_papers.csv" (the regular
          scraper / disambiguation pipeline will pick them up)

WHAT IT DOES NOT DO
  * Does NOT touch Users (Email, Name, UserType, AccountStatus).
    Your researchers can stay "unregistered stubs" — paper data
    arrives without resurrecting any login info.
  * Does NOT create ResearchPaper rows. New papers belong to the
    main scraper pipeline (which has DOI lookup, journal matching,
    quartile enrichment, etc.).

USAGE
  # Set the API key once per shell:
  $env:SERP_API_KEY = "your_key_here"        # PowerShell
  export SERP_API_KEY=your_key_here           # bash

  # Dry-run (no DB writes) for the two test researchers:
  python scrape_via_serpapi.py

  # Apply:
  python scrape_via_serpapi.py --apply

  # Specific Scholar IDs:
  python scrape_via_serpapi.py --scholar-ids lURTCEIAAAAJ,JSQbyBgAAAAJ --apply

WHY NOT SCRAPELY/SCHOLARLY:
  scholarly hits Google directly and gets banned after a few dozen
  requests. SerpApi farms it across proxies so you can run a full
  institutional sync in one go.

COST
  SerpApi: $50/mo for 5,000 searches, OR 100 free/mo. Each researcher
  = 1 search. 250 researchers = ~50 cents.
"""
import os, sys, argparse, json, csv, re, time
from datetime import datetime, timezone

import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

DEFAULT_SCHOLAR_IDS = [
    "lURTCEIAAAAJ",   # Ahlam
    "JSQbyBgAAAAJ",   # Abdulkareem
]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Mirrors
    the NormalizedTitle scheme already used by the main scraper."""
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r'[^\w\s]', ' ', t, flags=re.UNICODE)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _fetch_serpapi(api_key: str, scholar_id: str, start: int = 0,
                   num: int = 100) -> dict:
    """Call SerpApi for one author. Paginated via start+num."""
    import urllib.request, urllib.parse
    params = {
        "engine":    "google_scholar_author",
        "author_id": scholar_id,
        "api_key":   api_key,
        "start":     start,
        "num":       num,
    }
    url = SERPAPI_ENDPOINT + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_all_articles(api_key: str, scholar_id: str) -> tuple:
    """Returns (author_payload, list_of_articles). Handles pagination."""
    first = _fetch_serpapi(api_key, scholar_id, start=0, num=100)
    if "error" in first:
        raise RuntimeError(first["error"])
    author = first.get("author", {})
    articles = list(first.get("articles", []) or [])
    # Naive pagination — SerpApi caps at 100 per page; rare to exceed.
    start = 100
    while True:
        page = _fetch_serpapi(api_key, scholar_id, start=start, num=100)
        more = page.get("articles", []) or []
        if not more:
            break
        articles.extend(more)
        if len(more) < 100:
            break
        start += 100
        time.sleep(0.5)
    return author, articles


def _clean_interests(raw_interests: list) -> list:
    """SerpApi: [{title: '...'}, ...] → ['...', ...] — dedupe."""
    seen, out = set(), []
    for item in (raw_interests or []):
        t = (item.get("title") or "").strip() if isinstance(item, dict) else str(item).strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _find_user_id(cur, scholar_id: str) -> int | None:
    cur.execute('SELECT "UserID" FROM "Users" WHERE "Scholar_ID" = %s', [scholar_id])
    row = cur.fetchone()
    return row[0] if row else None


def _find_matching_paper(cur, title: str, year=None) -> int | None:
    """
    Find ResearchPaper by NormalizedTitle. Optional year filter to
    disambiguate same-titled papers across years.
    """
    norm = _normalize_title(title)
    if not norm or len(norm) < 8:
        return None
    if year:
        cur.execute(
            'SELECT "PaperID" FROM "ResearchPaper" '
            'WHERE "NormalizedTitle" = %s AND "PubYear" = %s LIMIT 1',
            [norm, year],
        )
    else:
        cur.execute(
            'SELECT "PaperID" FROM "ResearchPaper" '
            'WHERE "NormalizedTitle" = %s LIMIT 1',
            [norm],
        )
    row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------
# Main per-researcher routine
# ---------------------------------------------------------------------

def scrape_one(api_key: str, scholar_id: str, apply: bool,
               missing_writer=None) -> dict:
    print()
    print(f"== Scholar_ID = {scholar_id} ==")
    print(f"  fetching profile from SerpApi…")

    try:
        author, articles = _fetch_all_articles(api_key, scholar_id)
    except Exception as e:
        print(f"  [FAIL] SerpApi: {e}")
        return {"error": str(e)}

    name = author.get("name", "<unknown>")
    affil = author.get("affiliations", "")
    interests = _clean_interests(author.get("interests"))

    print(f"  author      : {name}")
    print(f"  affiliation : {affil}")
    print(f"  interests   : {len(interests)} -> {', '.join(interests[:5])}"
          + (f" (+{len(interests)-5})" if len(interests) > 5 else ""))
    print(f"  articles    : {len(articles)}")

    with connection.cursor() as cur:
        user_id = _find_user_id(cur, scholar_id)
        if not user_id:
            print(f"  [WARN] No Users row with this Scholar_ID — skipping.")
            return {"error": "no user"}

        # Match articles
        matched, missing, already_linked = [], [], []
        for art in articles:
            title = (art.get("title") or "").strip()
            year_raw = (art.get("year") or "").strip()
            try:
                year = int(year_raw) if year_raw else None
            except ValueError:
                year = None

            paper_id = _find_matching_paper(cur, title, year)
            if not paper_id:
                missing.append({"title": title, "year": year_raw,
                                 "scholar_id": scholar_id})
                continue

            # Check existing link
            cur.execute(
                'SELECT 1 FROM "Authors" WHERE "PaperID" = %s AND "UserID" = %s',
                [paper_id, user_id],
            )
            if cur.fetchone():
                already_linked.append(paper_id)
            else:
                matched.append((paper_id, title))

        print(f"  matched (will link)    : {len(matched)}")
        print(f"  already linked         : {len(already_linked)}")
        print(f"  missing (no match)     : {len(missing)}")

        if not apply:
            # Still write missing papers report in dry-run
            if missing_writer:
                for m in missing:
                    missing_writer.writerow(m)
            return {"matched": len(matched), "missing": len(missing),
                    "already": len(already_linked)}

        # ---- Write to DB ----
        with transaction.atomic():
            # Update Researcher.ResearchInterests + LastSyncedAt
            cur.execute(
                '''
                UPDATE "Researcher"
                   SET "ResearchInterests" = %s::jsonb,
                       "ResearchInterestsUpdatedAt" = %s,
                       "LastSyncedAt" = %s
                 WHERE "UserID" = %s
                ''',
                [json.dumps(interests, ensure_ascii=False),
                 datetime.now(timezone.utc),
                 datetime.now(timezone.utc),
                 user_id],
            )

            # Bulk insert Authors links
            if matched:
                values = [(pid, user_id) for pid, _ in matched]
                args_str = ",".join(
                    cur.mogrify("(%s,%s)", v).decode("utf-8") for v in values
                )
                cur.execute(
                    f'INSERT INTO "Authors" ("PaperID", "UserID") VALUES {args_str} '
                    f'ON CONFLICT DO NOTHING'
                )

        if missing_writer:
            for m in missing:
                missing_writer.writerow(m)

        print(f"  [ok] linked {len(matched)} papers, updated interests.")
        return {"matched": len(matched), "missing": len(missing),
                "already": len(already_linked)}


# ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scholar-ids", type=str, default=None,
                    help="Comma-separated Scholar_IDs.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write to the DB.")
    ap.add_argument("--api-key", type=str, default=None,
                    help="SerpApi key. Defaults to env SERP_API_KEY.")
    ap.add_argument("--missing-csv", type=str, default="missing_papers.csv",
                    help="Path for the missing-papers report.")
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("SERP_API_KEY")
    if not api_key:
        print("[ERROR] No SerpApi key. Set SERP_API_KEY env var or pass --api-key.")
        print("        Sign up: https://serpapi.com  (100 free searches/month)")
        sys.exit(1)

    scholar_ids = (args.scholar_ids.split(",") if args.scholar_ids
                   else DEFAULT_SCHOLAR_IDS)

    print("=" * 70)
    print(f"SerpApi scrape — {len(scholar_ids)} researcher(s)")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print("=" * 70)

    # Open missing-papers report
    missing_file = open(args.missing_csv, "w", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(missing_file,
                            fieldnames=["scholar_id", "title", "year"])
    writer.writeheader()

    totals = {"matched": 0, "missing": 0, "already": 0}
    for sid in scholar_ids:
        res = scrape_one(api_key, sid.strip(), apply=args.apply,
                         missing_writer=writer)
        if "error" not in res:
            for k, v in res.items():
                totals[k] = totals.get(k, 0) + v
        time.sleep(1)   # gentle pacing

    missing_file.close()

    print()
    print("=" * 70)
    print(f"Summary: matched={totals['matched']}, "
          f"already_linked={totals['already']}, "
          f"missing={totals['missing']}")
    if totals['missing']:
        print(f"Missing papers report -> {args.missing_csv}")
    print()
    if not args.apply:
        print("DRY RUN — nothing was written to the DB.")


if __name__ == "__main__":
    main()
