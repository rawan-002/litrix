import os
import sys
import time
import io
import argparse
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json
import httpx

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()
OPENALEX_MAILTO = "ra20awn@gmail.com"


# Shared DB helper (single source — see litrix_db.py at repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def normalize_title(t):
    return ''.join(c for c in (t or '').lower() if c.isalnum() or c.isspace()).strip()


def normalize_issn(issn):
    if not issn:
        return None
    return ''.join(c for c in str(issn).upper() if c.isalnum())


def normalize_orcid(orcid):
    if not orcid:
        return None
    s = orcid.strip()
    for prefix in ("https://orcid.org/", "http://orcid.org/", "orcid.org/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.upper()


def openalex_get(path, params=None, retries=3):
    p = dict(params or {})
    p["mailto"] = OPENALEX_MAILTO
    for attempt in range(retries):
        try:
            r = httpx.get(f"https://api.openalex.org/{path}", params=p, timeout=25)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            return None
        except Exception:
            time.sleep(1)
    return None


def openalex_search_authors(name, affiliation=None):
    params = {"search": name, "per-page": 10}
    if affiliation:
        params["filter"] = f"affiliations.institution.display_name.search:{affiliation}"
    data = openalex_get("authors", params)
    return (data or {}).get("results", []) or []


def openalex_works_by_author_id(author_id, max_pages=10):
    works = []
    cursor = "*"
    aid = author_id.strip()
    if not aid.startswith("A") and not aid.startswith("https://"):
        aid = "A" + aid.lstrip("A")
    for _ in range(max_pages):
        data = openalex_get("works", {
            "filter": f"author.id:{aid}",
            "per-page": 200,
            "cursor": cursor,
        })
        if not data:
            break
        results = data.get("results") or []
        works.extend(results)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor or not results:
            break
        time.sleep(0.2)
    return works


def orcid_api_works(orcid):
    """
    Hit ORCID's PUBLIC API. Two failure modes worth distinguishing:
      • 404 → ORCID identifier doesn't exist or is suspended.
      • 200 with empty `group` → the profile EXISTS but the works
        section is private (or genuinely empty).
    We log each so the operator can immediately see which case
    applies. The public API can't read private works — that needs
    an OAuth token from the researcher themselves.
    """
    try:
        r = httpx.get(
            f"https://pub.orcid.org/v3.0/{orcid}/works",
            headers={"Accept": "application/json"},
            timeout=25,
        )
        if r.status_code == 404:
            print(f"  [orcid api] 404 — ORCID {orcid} not found.")
            return []
        if r.status_code != 200:
            print(f"  [orcid api] HTTP {r.status_code} — {r.text[:200]}")
            return []
        data = r.json()
    except Exception as e:
        print(f"  [orcid api] request failed: {e}")
        return []

    groups = data.get("group") or []
    if not groups:
        # Distinguish "private works section" from "no works at all"
        # by also probing the person record. If person exists but
        # works are empty, it's almost certainly a privacy setting.
        try:
            p = httpx.get(
                f"https://pub.orcid.org/v3.0/{orcid}/person",
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if p.status_code == 200:
                pd = p.json()
                given  = (((pd.get('name') or {}).get('given-names') or {}).get('value') or '')
                family = (((pd.get('name') or {}).get('family-name') or {}).get('value') or '')
                full   = f'{given} {family}'.strip() or '(name hidden)'
                print(f"  [orcid api] profile resolves to '{full}' but the "
                      f"works section is empty — likely set to PRIVATE in "
                      f"the researcher's ORCID visibility settings.")
            else:
                print("  [orcid api] no public works and the profile is "
                      "unreadable — fully private or non-existent.")
        except Exception:
            print("  [orcid api] no public works (probe failed).")
        return []

    print(f"  [orcid api] {len(groups)} public work groups returned.")

    works = []
    for group in (data.get("group") or []):
        ext_ids = ((group.get("external-ids") or {}).get("external-id") or [])
        doi_val = None
        for ext in ext_ids:
            if (ext.get("external-id-type") or "").lower() == "doi":
                v = (ext.get("external-id-value") or "").strip()
                v = v.replace("https://doi.org/", "").replace("http://doi.org/", "").replace("doi.org/", "").lower()
                if v:
                    doi_val = v
                    break
        summaries = group.get("work-summary") or []
        if not summaries:
            continue
        s = summaries[0]
        title = (((s.get("title") or {}).get("title") or {}).get("value") or "").strip()
        if not title:
            continue
        year = ((s.get("publication-date") or {}).get("year") or {}).get("value")
        try:
            year = int(year) if year else None
        except Exception:
            year = None
        journal_title = (s.get("journal-title") or {}).get("value")
        works.append({
            "title": title,
            "year": year,
            "journal_title": journal_title,
            "doi": doi_val,
        })
    return works


def orcid_to_openalex_shape(ow):
    return {
        "title": ow.get("title"),
        "doi": ("https://doi.org/" + ow["doi"]) if ow.get("doi") else None,
        "publication_year": ow.get("year"),
        "cited_by_count": 0,
        "primary_location": {
            "source": {"display_name": ow.get("journal_title"), "issn_l": None}
        },
        "authorships": [],
        "id": None,
        "_orcid_fallback": True,
    }


def fetch_works_by_orcid(orcid, strict=False):
    """
    Pull a researcher's full publication list.

    Two modes:
      • Default (strict=False) — UNION across three sources:
            1. OpenAlex `author.orcid:` filter
            2. OpenAlex author lookup
            3. ORCID canonical record
        Maximum coverage; may include OpenAlex auto-attributions the
        researcher never personally claimed.
      • Strict  (strict=True)  — ORCID canonical ONLY (strategy 3),
        with OpenAlex DOI lookups used purely for metadata enrichment.
        Highest precision; only papers the researcher explicitly
        claimed in their ORCID profile land in the DB.

    Use strict mode when you want to mirror the researcher's curated
    ORCID record exactly (no name-collision false positives).
    """

    def _key(w):
        # Prefer DOI as the dedup key — globally unique, tolerant of
        # case + URL prefix differences. Fall back to a title hash
        # when DOI is absent so two ORCID-only entries don't collide.
        doi = (w.get("doi") or "").lower().strip()
        if doi:
            doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
            return ("doi", doi)
        title = (w.get("title") or "").strip().lower()
        title = "".join(c for c in title if c.isalnum() or c.isspace()).strip()
        return ("title", title) if title else ("noop", id(w))

    merged = {}

    if strict:
        print("  [STRICT mode] OpenAlex orcid-filter / author-lookup skipped — "
              "ORCID-claimed papers only.")
    else:
        # ---- Strategy 1: OpenAlex filter on ORCID -----------------
        cursor = "*"
        s1 = []
        for _ in range(10):
            data = openalex_get("works", {
                "filter":   f"author.orcid:{orcid}",
                "per-page": 200,
                "cursor":   cursor,
            })
            if not data:
                break
            results = data.get("results") or []
            s1.extend(results)
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not cursor or not results:
                break
            time.sleep(0.2)
        print(f"  [strategy 1] OpenAlex orcid filter → {len(s1)} works")
        for w in s1:
            merged[_key(w)] = w

        # ---- Strategy 2: OpenAlex author lookup -------------------
        s2_added = 0
        author_data = openalex_get(f"authors/orcid:{orcid}")
        if author_data:
            aid = (author_data.get("id") or "").replace("https://openalex.org/", "")
            if aid:
                s2 = openalex_works_by_author_id(aid)
                for w in s2:
                    k = _key(w)
                    if k not in merged:
                        merged[k] = w
                        s2_added += 1
        print(f"  [strategy 2] OpenAlex author lookup → +{s2_added} new works")

    # ---- Strategy 3: ORCID canonical record -----------------------
    # This is the authoritative list. Anything OpenAlex didn't surface
    # gets enriched via DOI lookup when possible; otherwise we keep
    # the ORCID-only shape so the paper still lands in the DB.
    orcid_works = orcid_api_works(orcid) or []
    print(f"  [strategy 3] ORCID API → {len(orcid_works)} canonical entries")
    s3_added = 0
    for i, ow in enumerate(orcid_works, 1):
        k = _key(ow)
        if k in merged:
            continue
        # Try to upgrade to OpenAlex shape via DOI for richer metadata.
        if ow.get("doi"):
            time.sleep(0.15)
            w = openalex_get(f"works/doi:{ow['doi'].lower()}")
            if w:
                k2 = _key(w)
                if k2 not in merged:
                    merged[k2] = w
                    s3_added += 1
                continue
        merged[k] = orcid_to_openalex_shape(ow)
        s3_added += 1
        if i % 50 == 0:
            print(f"    enriched {i}/{len(orcid_works)} ORCID-only entries")
    print(f"  [strategy 3] added {s3_added} new works after DOI enrichment")

    works = list(merged.values())
    print(f"  [merged] total unique works: {len(works)}")
    return works
    return works


def find_journal_id(cur, issn, journal_name):
    issn_norm = normalize_issn(issn)
    if issn_norm:
        cur.execute('SELECT "JournalID" FROM "Journals" WHERE "ISSN_Print" = %s LIMIT 1', (issn_norm,))
        r = cur.fetchone()
        if r:
            return r[0]
        cur.execute('SELECT "JournalID" FROM "JournalRankings" WHERE "Issn" = %s LIMIT 1', (issn_norm,))
        r = cur.fetchone()
        if r and r[0]:
            return r[0]
    if journal_name:
        cur.execute(
            'SELECT "JournalID" FROM "Journals" WHERE LOWER("JournalName") = LOWER(%s) LIMIT 1',
            (journal_name,),
        )
        r = cur.fetchone()
        if r:
            return r[0]
    return None


def reconstruct_abstract(inverted_index):
    """
    OpenAlex stores abstracts as a word→positions inverted index for
    licensing reasons. This rebuilds plain prose by sorting positions
    and emitting the words in order. Returns None when the index is
    missing/empty so the caller can NULL the column cleanly.
    """
    if not inverted_index or not isinstance(inverted_index, dict):
        return None
    pairs = []
    for word, positions in inverted_index.items():
        if not positions:
            continue
        for pos in positions:
            try:
                pairs.append((int(pos), word))
            except (TypeError, ValueError):
                continue
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    return ' '.join(w for _, w in pairs).strip() or None


def upsert_work(cur, w, user_id):
    title = (w.get("title") or "").strip()
    if not title:
        return None, False
    norm = normalize_title(title)
    doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
    pub_year = w.get("publication_year")
    cited = w.get("cited_by_count") or 0
    primary_loc = w.get("primary_location") or {}
    source = primary_loc.get("source") or {}
    issn = source.get("issn_l")
    journal_name = source.get("display_name")
    abstract = reconstruct_abstract(w.get("abstract_inverted_index"))
    authorships = w.get("authorships") or []
    authors_str = ", ".join(
        (a.get("author", {}).get("display_name") or "")
        for a in authorships
    )

    cur.execute(
        'SELECT "PaperID", "DOI", "JournalID" FROM "ResearchPaper" '
        'WHERE "NormalizedTitle" = %s OR LOWER("Title") = LOWER(%s) LIMIT 1',
        (norm, title),
    )
    row = cur.fetchone()

    raw_log = {
        "title": title, "authors": authors_str,
        "publication": journal_name, "year": pub_year,
        "doi": doi, "issn_l": issn,
        "cited_by_count": cited,
        "openalex_id": w.get("id"),
        # Persist the full authorships array (with institutions +
        # raw_affiliation_strings). Powers the paper-detail modal's
        # "Authors & Affiliations" section without an extra API call
        # at view time.
        "authorships":  authorships,
    }

    if row:
        paper_id, existing_doi, existing_journal = row
        jid = existing_journal or find_journal_id(cur, issn, journal_name)
        # Backfill Abstract on existing rows when we now have one and
        # the row was missing it. COALESCE preserves whatever was
        # already there if both sides have data.
        cur.execute('''
            UPDATE "ResearchPaper"
            SET "DOI"       = COALESCE("DOI", %s),
                "JournalID" = COALESCE("JournalID", %s),
                "Abstract"  = COALESCE("Abstract", %s),
                "RawData_Log" = jsonb_set(
                    COALESCE("RawData_Log", '{}'::jsonb),
                    '{cited_by_count}', to_jsonb(%s::int)
                )
            WHERE "PaperID" = %s
        ''', (doi, jid, abstract, cited, paper_id))
        return paper_id, False

    jid = find_journal_id(cur, issn, journal_name)
    src = 'Manual' if w.get("_orcid_fallback") else 'OpenAlex'
    try:
        cur.execute('SAVEPOINT sp_paper')
        cur.execute('''
            INSERT INTO "ResearchPaper" (
                "Title", "NormalizedTitle", "PubYear", "DOI", "JournalID",
                "Abstract", "Source", "RawData_Log", "ScrapedAt", "IsVerified"
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), TRUE)
            RETURNING "PaperID"
        ''', (title, norm, pub_year, doi, jid, abstract, src, Json(raw_log)))
        paper_id = cur.fetchone()[0]
        cur.execute('RELEASE SAVEPOINT sp_paper')
        return paper_id, True
    except psycopg2.errors.UniqueViolation:
        cur.execute('ROLLBACK TO SAVEPOINT sp_paper')
        cur.execute(
            'SELECT "PaperID" FROM "ResearchPaper" WHERE LOWER("Title") = LOWER(%s) LIMIT 1',
            (title,),
        )
        r = cur.fetchone()
        return (r[0] if r else None), False


def link_author(cur, user_id, paper_id, criteria, name_raw):
    cur.execute('''
        INSERT INTO "Authors" (
            "UserID", "PaperID", "AuthorOrder",
            "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
            "AuthorNameRaw", "Is_Verified"
        )
        VALUES (%s, %s, 1, FALSE, 1.0, %s, %s, TRUE)
        ON CONFLICT ("UserID", "PaperID") DO NOTHING
    ''', (user_id, paper_id, criteria, name_raw or ''))


def determine_author_order(work, orcid=None, openalex_author_id=None):
    target_orcid = normalize_orcid(orcid) if orcid else None
    target_aid = openalex_author_id
    for idx, ship in enumerate(work.get("authorships") or [], start=1):
        a = ship.get("author") or {}
        a_orcid = normalize_orcid((a.get("orcid") or "").replace("https://orcid.org/", ""))
        a_id = (a.get("id") or "").replace("https://openalex.org/", "")
        if target_orcid and a_orcid == target_orcid:
            return idx, (a.get("display_name") or "")
        if target_aid and a_id == target_aid:
            return idx, (a.get("display_name") or "")
    return 1, ""


def mode_search(name, affiliation):
    print(f"Searching: {name!r}")
    candidates = openalex_search_authors(name, affiliation)
    if not candidates:
        print("  no results")
        return
    print(f"\n{len(candidates)} candidate(s):\n")
    for i, a in enumerate(candidates, 1):
        aid = (a.get("id") or "").replace("https://openalex.org/", "")
        works_n = a.get("works_count") or 0
        cited_n = a.get("cited_by_count") or 0
        orcid = a.get("orcid") or "—"
        last_inst = a.get("last_known_institutions") or []
        inst_names = ", ".join((x.get("display_name") or "") for x in last_inst[:2]) or "—"
        print(f"[{i}] {a.get('display_name')}")
        print(f"    OpenAlex : {aid}")
        print(f"    ORCID    : {orcid}")
        print(f"    Works    : {works_n}  Cites: {cited_n}")
        print(f"    Affil    : {inst_names}\n")
    print("Run: python scrapers/orcid.py --openalex-id <ID> --user <UID>")


def mode_sync(works, user_id, criteria, orcid=None, openalex_author_id=None):
    if not works:
        print("  No works")
        return
    print(f"  {len(works)} works\n")
    conn = db()
    cur = conn.cursor()
    cur.execute('SELECT "FullName_Ar" FROM "Users" WHERE "UserID" = %s', (user_id,))
    u = cur.fetchone()
    if not u:
        print(f"  UserID {user_id} not found")
        return
    print(f"  Researcher: {u[0]}\n")

    n_new = n_existing = n_linked = 0
    for w in works:
        try:
            cur.execute('SAVEPOINT outer_sp')
            paper_id, was_new = upsert_work(cur, w, user_id)
            if not paper_id:
                cur.execute('ROLLBACK TO SAVEPOINT outer_sp')
                continue
            order, name_raw = determine_author_order(
                w, orcid=orcid, openalex_author_id=openalex_author_id
            )
            link_author(cur, user_id, paper_id, criteria, name_raw)
            cur.execute('RELEASE SAVEPOINT outer_sp')
            if was_new:
                n_new += 1
            else:
                n_existing += 1
            n_linked += 1
        except Exception as e:
            cur.execute('ROLLBACK TO SAVEPOINT outer_sp')
            print(f"  error: {e}")
        if (n_new + n_existing) % 25 == 0 and (n_new + n_existing) > 0:
            conn.commit()

    conn.commit()
    cur.close()
    conn.close()
    print(f"\nDone — new={n_new} existing={n_existing} linked={n_linked}")


# ----------------------------------------------------------------------------
# Cooldown guard — mirrors backend/accounts/sync_views.py.SYNC_COOLDOWN_DAYS.
# Refuses to call OpenAlex / the ORCID API when the researcher has been
# synced within this window unless --force is supplied.
# ----------------------------------------------------------------------------
SYNC_COOLDOWN_DAYS = 7


def check_cooldown(conn, user_id):
    cur = conn.cursor()
    cur.execute('''
        SELECT
            r."LastSyncedAt",
            (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = %s) AS papers
        FROM "Users" u
        LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
        WHERE u."UserID" = %s
    ''', (user_id, user_id))
    row = cur.fetchone()
    cur.close()
    if not row:
        return False, {'reason': 'user_not_found'}
    last_synced, papers = row[0], row[1] or 0
    if last_synced is None:
        return True, {'reason': 'never_synced', 'papers': papers}
    from datetime import datetime, timedelta, timezone as tz
    now = datetime.now(tz.utc) if last_synced.tzinfo else datetime.utcnow()
    cooldown_until = last_synced + timedelta(days=SYNC_COOLDOWN_DAYS)
    if now < cooldown_until:
        return False, {
            'reason': 'in_cooldown', 'last_synced_at': last_synced.isoformat(),
            'papers': papers, 'cooldown_until': cooldown_until.isoformat(),
        }
    return True, {
        'reason': 'cooldown_elapsed',
        'last_synced_at': last_synced.isoformat(), 'papers': papers,
    }


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--orcid", help="ORCID")
    g.add_argument("--openalex-id", help="OpenAlex author ID")
    g.add_argument("--search", help="Search OpenAlex authors")
    ap.add_argument("--user", type=int)
    ap.add_argument("--affiliation")
    ap.add_argument("--force", action="store_true",
                    help="Bypass the cooldown gate (admin override).")
    ap.add_argument("--strict", action="store_true",
                    help="ORCID-claimed only — skip OpenAlex orcid-filter "
                         "and author-lookup strategies. Use when you want "
                         "the researcher's curated ORCID record exactly.")
    args = ap.parse_args()

    if args.search:
        mode_search(args.search, args.affiliation)
        return

    if not args.user:
        print("--user required")
        sys.exit(1)

    # Cooldown gate before any external API call.
    guard_conn = db()
    eligible, info = check_cooldown(guard_conn, args.user)
    guard_conn.close()
    if not eligible and not args.force:
        print(f"[SKIP] {info['reason']}: papers={info.get('papers', 0)}, "
              f"last_synced_at={info.get('last_synced_at')}, "
              f"cooldown_until={info.get('cooldown_until')}")
        print("[SKIP] Pass --force to override.")
        sys.exit(0)
    if args.force and info.get('reason') == 'in_cooldown':
        print(f"[FORCE] Overriding cooldown — papers already stored: "
              f"{info.get('papers', 0)}")

    if args.orcid:
        orcid = normalize_orcid(args.orcid)
        print(f"ORCID: {orcid}{'  [STRICT]' if args.strict else ''}")
        works = fetch_works_by_orcid(orcid, strict=args.strict)
        mode_sync(works, args.user, "orcid_lookup", orcid=orcid)

    if args.openalex_id:
        aid = args.openalex_id.strip()
        print(f"OpenAlex: {aid}")
        works = openalex_works_by_author_id(aid)
        full_aid = aid if aid.startswith("A") else "A" + aid.lstrip("A")
        mode_sync(works, args.user, "openalex_author_id", openalex_author_id=full_aid)


if __name__ == "__main__":
    main()
