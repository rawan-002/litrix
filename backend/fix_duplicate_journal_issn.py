"""One-off fix: merge duplicate Journal rows created by an ISSN-format
mismatch during the Scopus migration.

Root cause: scopus_attribution_fix.py's upsert_journal() stores the raw
Scopus ISSN with an "ISSN-" prefix (e.g. "ISSN-20711050") and looks up
existing Journals by an EXACT string match on ISSN_Print. The DB's
dominant existing convention has no prefix (1135 of 1233 rows, e.g.
"20711050"), so the lookup missed every already-imported journal and
Scopus quietly INSERTed a second Journal row for it — usually under a
garbled name, because an earlier/unrelated pipeline had mis-parsed a
citation string as the journal name for the original row (e.g.
"Sustainability 14 (22), 15328, 2022" instead of "Sustainability
(Switzerland)").

Net effect per affected journal: papers ended up split across two
Journal rows, and the canonical SCImago quartile (attached to the OLD,
garbled-name row) never reaches the papers linked to the NEW,
correctly-named row — which instead falls back to an inferior
single-paper, single-year Scopus-derived quartile. First caught via
PaperID 6287 ("Sustainability (Switzerland)", showing Q4 instead of the
correct Scimago Q1).

For each duplicate ISSN pair:
  - winner = the "ISSN-"-prefixed row (verified clean-named in all 49
    groups audited before writing this script).
  - loser  = the plain-ISSN row (garbled/citation-parsed name).
  - Move every ResearchPaper + JournalRankings row from loser to winner.
  - On a (winner, RankingYear) collision, keep SCImago over Scopus (the
    project's stated ranking-source policy — see
    scopus_attribution_fix.py's upsert_journal_ranking docstring); drop
    the losing ranking row.
  - Normalize winner's ISSN_Print to the plain 8-char form (the DB's
    dominant convention) so future imports match on the first try.
  - Delete the now-empty loser Journals row.

Idempotent — re-running after a partial run just finds fewer/no
duplicate groups left. Read-only unless --confirm is passed.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout

SOURCE_RANK = {'scimago': 0, 'SCImago': 0, 'Scopus': 1}

# The full ISSN normalization: strip a leading "ISSN" tag, then drop any
# remaining non-alphanumeric formatting (dashes, spaces). Mirrors
# normalize_issn() in scopus_attribution_fix.py and the unique index in
# migrations/20260804_unique_normalized_issn.sql. An earlier version of
# this script only stripped the "ISSN-" prefix, which missed groups that
# differed only by dash formatting (e.g. "2156-5570" vs "21565570").
_NORM_ISSN_SQL = (
    "regexp_replace(regexp_replace(upper(COALESCE(\"ISSN_Print\", '')), "
    "'^ISSN-?', ''), '[^A-Z0-9]', '', 'g')"
)


def fetch_groups(cur):
    cur.execute(f'''
        SELECT {_NORM_ISSN_SQL} AS norm_issn,
               array_agg("JournalID") AS jids
        FROM "Journals"
        WHERE "ISSN_Print" IS NOT NULL AND "ISSN_Print" != ''
        GROUP BY 1
        HAVING length({_NORM_ISSN_SQL}) = 8 AND COUNT(DISTINCT "JournalID") > 1
        ORDER BY 1
    ''')
    return cur.fetchall()


def _looks_like_citation_string(name):
    """Heuristic for the garbled "Journal 13 (5), 2022"-style names an
    earlier/unrelated pipeline bug produced instead of a real journal name."""
    if not name:
        return True
    if re.search(r'\d+\s*\(\d+\)', name):
        return True
    if re.search(r',\s*\d{4}\s*$', name):
        return True
    if '…' in name:
        return True
    return False


def merge_group(cur, norm_issn, jids, confirm):
    if len(jids) != 2:
        print(f"  SKIP {norm_issn}: {len(jids)}-way duplicate — needs manual review")
        return False

    cur.execute(
        'SELECT "JournalID", "JournalName", "ISSN_Print", '
        '       (SELECT COUNT(*) FROM "ResearchPaper" WHERE "JournalID" = j."JournalID") AS papers '
        'FROM "Journals" j WHERE "JournalID" = ANY(%s)', (jids,)
    )
    rows = {r[0]: {'name': r[1], 'issn': r[2], 'papers': r[3]} for r in cur.fetchall()}
    a, b = jids

    # Winner = the one with a real journal name (not a mangled citation
    # string); if both/neither qualify, prefer whichever has more linked
    # papers; final tiebreak is the lower JournalID for determinism.
    a_bad = _looks_like_citation_string(rows[a]['name'])
    b_bad = _looks_like_citation_string(rows[b]['name'])
    if a_bad != b_bad:
        winner, loser = (b, a) if a_bad else (a, b)
    elif rows[a]['papers'] != rows[b]['papers']:
        winner, loser = (a, b) if rows[a]['papers'] > rows[b]['papers'] else (b, a)
    else:
        winner, loser = (a, b) if a < b else (b, a)

    w_name, w_issn = rows[winner]['name'], rows[winner]['issn']
    l_name = rows[loser]['name']
    loser_papers = rows[loser]['papers']

    print(f"ISSN {norm_issn}: winner={winner} {w_name!r} <- loser={loser} {l_name!r} "
          f"({loser_papers} papers to move)")

    if not confirm:
        return True

    savepoint = f"sp_merge_{winner}_{loser}"
    cur.execute(f'SAVEPOINT {savepoint}')
    try:
        # 1. Move papers.
        cur.execute(
            'UPDATE "ResearchPaper" SET "JournalID" = %s WHERE "JournalID" = %s',
            (winner, loser),
        )

        # 2. Move/merge rankings, resolving (JournalID, RankingYear) collisions
        #    by source priority (SCImago > Scopus).
        cur.execute(
            'SELECT "RankingID", "RankingYear", "Source" FROM "JournalRankings" '
            'WHERE "JournalID" = %s', (loser,),
        )
        loser_rankings = cur.fetchall()
        for rid, year, source in loser_rankings:
            cur.execute(
                'SELECT "RankingID", "Source" FROM "JournalRankings" '
                'WHERE "JournalID" = %s AND "RankingYear" = %s',
                (winner, year),
            )
            clash = cur.fetchone()
            if clash is None:
                cur.execute(
                    'UPDATE "JournalRankings" SET "JournalID" = %s WHERE "RankingID" = %s',
                    (winner, rid),
                )
            else:
                w_rid, w_source = clash
                if SOURCE_RANK.get(source, 1) < SOURCE_RANK.get(w_source, 1):
                    # Loser's row outranks the winner's existing one for
                    # this year — replace it in place, then drop the loser row.
                    cur.execute(
                        'UPDATE "JournalRankings" jr SET '
                        '"Source" = old."Source", "Quartile" = old."Quartile", '
                        '"ImpactFactor" = old."ImpactFactor", "Category" = old."Category", '
                        '"NormalizedName" = old."NormalizedName" '
                        'FROM "JournalRankings" old '
                        'WHERE jr."RankingID" = %s AND old."RankingID" = %s',
                        (w_rid, rid),
                    )
                # ISSN_Mapping rows can point straight at a RankingID
                # (used by scrapers/scholar.py as an ISSN -> ranking
                # shortcut). Repoint them to the surviving row before
                # deleting, or the FK blocks the delete.
                cur.execute(
                    'UPDATE "ISSN_Mapping" SET "RankingID" = %s WHERE "RankingID" = %s',
                    (w_rid, rid),
                )
                cur.execute('DELETE FROM "JournalRankings" WHERE "RankingID" = %s', (rid,))

        # 3. Delete the now-empty loser Journal row.
        cur.execute('SELECT COUNT(*) FROM "ResearchPaper" WHERE "JournalID" = %s', (loser,))
        remaining_papers = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM "JournalRankings" WHERE "JournalID" = %s', (loser,))
        remaining_rankings = cur.fetchone()[0]
        if remaining_papers or remaining_rankings:
            raise RuntimeError(
                f"loser {loser} still referenced "
                f"(papers={remaining_papers}, rankings={remaining_rankings}) — aborting group"
            )
        cur.execute('DELETE FROM "Journals" WHERE "JournalID" = %s', (loser,))

        # 4. Normalize winner's ISSN_Print to the DB's dominant plain form.
        if w_issn != norm_issn:
            cur.execute(
                'UPDATE "Journals" SET "ISSN_Print" = %s WHERE "JournalID" = %s',
                (norm_issn, winner),
            )

        cur.execute(f'RELEASE SAVEPOINT {savepoint}')
        return True
    except Exception as e:
        cur.execute(f'ROLLBACK TO SAVEPOINT {savepoint}')
        print(f"  FAILED ({e}) — group left untouched")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--confirm', action='store_true', help='Write changes (default: dry-run report only)')
    args = ap.parse_args()

    setup_utf8_stdout()
    conn = db()
    cur = conn.cursor()

    groups = fetch_groups(cur)
    print(f"{len(groups)} duplicate ISSN group(s) found\n")

    ok = 0
    for norm_issn, jids in groups:
        if merge_group(cur, norm_issn, jids, args.confirm):
            ok += 1

    if args.confirm:
        conn.commit()
        print(f"\nCommitted. {ok}/{len(groups)} group(s) merged.")
    else:
        conn.rollback()
        print(f"\nDry-run only ({ok}/{len(groups)} group(s) would merge) — re-run with --confirm to apply.")

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
