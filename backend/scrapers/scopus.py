"""
Scopus author → publications scraper.

Architecture:
    We don't talk to Scopus directly (Scopus's API requires an
    institutional Elsevier subscription + API key). Instead we go
    through OpenAlex, which indexes Scopus and lets us query authors
    by their Scopus ID:

        GET /authors?filter=ids.scopus:<scopus_author_id>

    OpenAlex returns the OpenAlex author record, then we pull all the
    works for that author the same way we do for ORCID strategy 2.
    Result: full publication list, no Elsevier credentials needed,
    same rich metadata as the ORCID path.

Reuses the persistence + cooldown helpers from orcid.py so the DB
side stays consistent.

Usage:
    python scrapers/scopus.py --scopus 56125509600 --user 42
    python scrapers/scopus.py --scopus 56125509600 --user 42 --force
"""
import argparse
import sys

# Share helpers with the ORCID scraper rather than duplicate them.
# IMPORTANT: orcid.py wraps sys.stdout/stderr in TextIOWrapper at
# module-level. Importing it here triggers that wrap once. We do NOT
# wrap again — a second wrap orphans the first one and Python's GC
# closes the underlying buffer, blowing up later prints with
# "I/O operation on closed file." That's the entire reason this file
# does not redo the UTF-8 stdio dance even though every other scraper
# does it.
from orcid import (
    db,
    openalex_get,
    openalex_works_by_author_id,
    mode_sync,
    check_cooldown,
)


def fetch_works_by_scopus(scopus_id: str):
    """
    Resolve a Scopus author ID to its OpenAlex equivalent, then pull
    every work attached to that author.

    Returns (openalex_author_id_or_None, [works]).
    """
    sid = (scopus_id or '').strip()
    if not sid:
        return None, []

    print(f"  [scopus] resolving Scopus author {sid} via OpenAlex...")
    data = openalex_get('authors', {'filter': f'ids.scopus:{sid}'})
    if not data:
        print("  [scopus] OpenAlex request failed.")
        return None, []

    results = data.get('results') or []
    if not results:
        # Try a slightly more permissive lookup: some Scopus IDs are
        # stored on OpenAlex without the leading zeros / with different
        # formatting. The `display_name` filter is too noisy, so we
        # surface the empty result rather than guessing.
        print(f"  [scopus] no OpenAlex author found for Scopus ID {sid}.")
        print(f"  [scopus] (the author may exist on Scopus but OpenAlex "
              f"hasn't indexed them yet.)")
        return None, []

    author       = results[0]
    openalex_id  = (author.get('id') or '').replace('https://openalex.org/', '')
    display_name = author.get('display_name') or '(name unavailable)'
    works_count  = author.get('works_count', 0)
    print(f"  [scopus] resolved → OpenAlex {openalex_id}  ({display_name})  "
          f"~{works_count} works expected")

    works = openalex_works_by_author_id(openalex_id)
    print(f"  [scopus] {len(works)} works fetched from OpenAlex")
    return openalex_id, works


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scopus', required=True,
                    help='Scopus author ID (e.g. 56125509600)')
    ap.add_argument('--user',   required=True, type=int,
                    help='Internal Litrix UserID to attach works to')
    ap.add_argument('--force', action='store_true',
                    help='Bypass the cooldown gate.')
    args = ap.parse_args()

    # Cooldown gate before we burn any API calls.
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

    print(f"=== Sync (Scopus={args.scopus}, UID={args.user}) ===\n")

    openalex_id, works = fetch_works_by_scopus(args.scopus)
    if not works:
        print("No articles fetched")
        sys.exit(1)

    mode_sync(
        works, args.user,
        criteria='scopus_lookup',
        openalex_author_id=openalex_id,
    )


if __name__ == '__main__':
    main()
