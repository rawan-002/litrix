"""
Paper-level dedup — detect duplicate ResearchPaper rows and merge them.

=============================================================================
WHY THIS EXISTS
=============================================================================
backend/find_duplicate_papers.py DETECTS duplicates (exact DOI / NormalizedTitle
matches) but never merges. Real duplicates also hide behind near-identical
titles (preprint vs published, punctuation variants) that exact matching
misses. This script does the full report-then-apply cycle:

    --dry-run : detect groups (exact SQL blocks + fuzzy SequenceMatcher >= 0.90
                inside small title blocks), pick the copy to KEEP, and write a
                reviewable JSON + CSV report. No DB writes.
    --apply   : merge each group — remap child rows from every loser onto the
                kept paper, merge citation data, AuditLog every loser, then
                delete it. Atomic transaction + full JSON snapshot beforehand.

KEEP-CHOICE (ported from the Excel pipeline): has_doi > citations > title
length, tie-break IsVerified then lowest PaperID.

=============================================================================
SAFETY
=============================================================================
  • Backup snapshot of every paper + child rows in every group is written to
    data/dedup_audit/snapshot_<ts>.json BEFORE any write.
  • Every merged loser gets an AuditLog row (Action='paper.merge.dedup') with
    the kept PaperID + child counts, so post-hoc recovery is queryable.
  • --apply --report-in <groups.json> merges EXACTLY the reviewed file (you
    can hand-edit it — delete groups you reject, or swap kept/losers).
    Without --report-in, --apply re-detects and merges everything it finds.
  • --limit-groups N caps how many groups merge in one run (use 1 first).

USAGE:
    python tools/dedup_papers.py --dry-run
    python tools/dedup_papers.py --dry-run --user 106
    # review data/dedup_audit/groups_<ts>.json, then:
    python tools/dedup_papers.py --apply --report-in data/dedup_audit/groups_<ts>.json --limit-groups 1
    python tools/dedup_papers.py --apply --report-in data/dedup_audit/groups_<ts>.json
"""
import os
import sys
import io
import csv
import json
import argparse
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import django
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
django.setup()

from django.db import connection, transaction

AUDIT_DIR = PROJECT_ROOT / "data" / "dedup_audit"

# Child tables that may reference ResearchPaper.PaperID — existence is checked
# at runtime (same convention as backend/delete_manual_orphans.py).
# "Authors" and "Citations" are handled specially (unique constraints).
SIMPLE_CHILDREN = [
    "ExternalAuthors", "PaperKeywords", "PaperGrants",
    "CitationsHistory", "ReportPaperDecision",
]

# Dashboard's per-paper citation total (same COALESCE analytics/views.py uses).
CITATIONS_EXPR = '''COALESCE(
    (rp."RawData_Log"->'cited_by'->>'value')::int,
    (rp."RawData_Log"->>'cited_by_count')::int,
    0)'''


# ---------------------------------------------------------------------------
# Normalization (ported from the Excel pipeline's norm_title — NFKD + strip
# accents + drop parenthesised text. Distinct from the DB NormalizedTitle.)
# ---------------------------------------------------------------------------

def norm_title(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)", "", s.lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def load_papers(cur, user_id):
    """All papers in scope with the fields keep-choice + matching need."""
    user_join = ''
    params = []
    if user_id:
        user_join = '''JOIN "Authors" ua ON ua."PaperID" = rp."PaperID"
                       AND ua."UserID" = %s'''
        params.append(user_id)
    cur.execute(f'''
        SELECT DISTINCT rp."PaperID", rp."Title", rp."NormalizedTitle",
               rp."DOI", rp."PubYear", rp."IsVerified", rp."Source",
               {CITATIONS_EXPR} AS citations
        FROM "ResearchPaper" rp
        {user_join}
        ORDER BY rp."PaperID"
    ''', params)
    papers = {}
    for pid, title, ntitle, doi, pub_year, verified, source, cit in cur.fetchall():
        clean_doi = (doi or '').strip().lower()
        clean_doi = clean_doi.removeprefix("https://doi.org/").removeprefix("doi.org/")
        papers[pid] = {
            "paper_id": pid, "title": title or '',
            "ntitle": (ntitle or '').strip(),
            "doi": clean_doi or None, "pub_year": pub_year,
            "is_verified": bool(verified), "source": source,
            "citations": int(cit or 0),
            "fuzzy_key": norm_title(title),
        }
    return papers


def detect_groups(papers, threshold):
    """Union-find over exact edges (DOI / NormalizedTitle / lower Title) and
    fuzzy edges (SequenceMatcher >= threshold inside small title blocks)."""
    pids = sorted(papers)
    idx = {pid: i for i, pid in enumerate(pids)}
    parent = list(range(len(pids)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b, reason, reasons):
        ra, rb = find(idx[a]), find(idx[b])
        if ra != rb:
            parent[ra] = rb
        reasons[frozenset((a, b))] = reason

    reasons = {}

    # -- exact blocks (cheap, definite) --
    by_doi = defaultdict(list)
    by_ntitle = defaultdict(list)
    by_lower = defaultdict(list)
    for pid, p in papers.items():
        if p["doi"]:
            by_doi[p["doi"]].append(pid)
        if p["ntitle"]:
            by_ntitle[p["ntitle"]].append(pid)
        lt = p["title"].strip().lower()
        if lt:
            by_lower[lt].append(pid)
    for key_map, reason in ((by_doi, 'same_doi'),
                            (by_ntitle, 'same_normalized_title'),
                            (by_lower, 'same_lower_title')):
        for ids in key_map.values():
            for other in ids[1:]:
                union(ids[0], other, reason, reasons)

    # -- fuzzy blocks (residual, bounded) --
    # Block by the first 8 chars of the Excel-style normalized title so
    # SequenceMatcher only runs inside tiny buckets, never across all papers.
    blocks = defaultdict(list)
    for pid, p in papers.items():
        if len(p["fuzzy_key"]) >= 15:
            blocks[p["fuzzy_key"][:8]].append(pid)
    n_pairs = 0
    for ids in blocks.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                if find(idx[a]) == find(idx[b]):
                    continue        # already grouped by an exact rule
                score = SequenceMatcher(
                    None, papers[a]["fuzzy_key"], papers[b]["fuzzy_key"]).ratio()
                n_pairs += 1
                if score >= threshold:
                    union(a, b, f'fuzzy_title_{score:.3f}', reasons)
    print(f"  fuzzy comparisons run: {n_pairs}")

    groups = defaultdict(list)
    for pid in pids:
        groups[find(idx[pid])].append(pid)
    dup_groups = [sorted(g) for g in groups.values() if len(g) > 1]

    def group_reason(g):
        rs = [r for pair, r in reasons.items() if pair <= frozenset(g)]
        return sorted(set(rs)) or ['transitive']

    return [{"members": g, "match_reasons": group_reason(g)} for g in sorted(dup_groups)]


def choose_keep(group_pids, papers):
    """has_doi > citations > title length; tie-break IsVerified, lowest PaperID."""
    ranked = sorted(
        group_pids,
        key=lambda pid: (
            bool(papers[pid]["doi"]),
            papers[pid]["citations"],
            len(papers[pid]["title"]),
            papers[pid]["is_verified"],
            -pid,
        ),
        reverse=True,
    )
    return ranked[0], ranked[1:]


# ---------------------------------------------------------------------------
# Snapshot / audit
# ---------------------------------------------------------------------------

def existing_child_tables(cur):
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = ANY(%s)
    """, [SIMPLE_CHILDREN + ["Citations"]])
    return {r[0] for r in cur.fetchall()}


def snapshot_paper(cur, pid, child_tables):
    cur.execute('SELECT row_to_json(rp) FROM "ResearchPaper" rp WHERE "PaperID" = %s', (pid,))
    row = cur.fetchone()
    snap = {"ResearchPaper": row[0] if row else None, "children": {}}
    cur.execute('SELECT row_to_json(a) FROM "Authors" a WHERE a."PaperID" = %s', (pid,))
    snap["children"]["Authors"] = [r[0] for r in cur.fetchall()]
    for t in child_tables:
        cur.execute(f'SELECT row_to_json(x) FROM "{t}" x WHERE x."PaperID" = %s', (pid,))
        snap["children"][t] = [r[0] for r in cur.fetchall()]
    return snap


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def authors_columns(cur):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'Authors'
        ORDER BY ordinal_position
    """)
    return [r[0] for r in cur.fetchall() if r[0] != 'AuthorLinkID']


def remap_simple_child(cur, table, loser, keep):
    """UPDATE PaperID loser->keep; on a unique violation fall back to
    row-by-row via ctid (conflicting rows are dropped — the kept paper
    already has the equivalent row)."""
    cur.execute('SAVEPOINT child_bulk')
    try:
        cur.execute(f'UPDATE "{table}" SET "PaperID" = %s WHERE "PaperID" = %s',
                    (keep, loser))
        cur.execute('RELEASE SAVEPOINT child_bulk')
        return
    except Exception:
        cur.execute('ROLLBACK TO SAVEPOINT child_bulk')
    cur.execute(f'SELECT ctid FROM "{table}" WHERE "PaperID" = %s', (loser,))
    for (ctid,) in cur.fetchall():
        cur.execute('SAVEPOINT child_row')
        try:
            cur.execute(f'UPDATE "{table}" SET "PaperID" = %s WHERE ctid = %s',
                        (keep, ctid))
            cur.execute('RELEASE SAVEPOINT child_row')
        except Exception:
            cur.execute('ROLLBACK TO SAVEPOINT child_row')
            cur.execute(f'DELETE FROM "{table}" WHERE ctid = %s', (ctid,))


def _as_year_dict(v):
    """CitationsByYear normalizer — some legacy rows store the JSONB as a
    double-encoded JSON string instead of an object."""
    if not v:
        return {}
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return {}
    return v if isinstance(v, dict) else {}


def merge_citation_fields(cur, keep, loser):
    """Element-wise MAX merge of CitationsByYear; bump the kept paper's
    RawData_Log total to the larger of the two papers' displayed totals."""
    cur.execute(f'''
        SELECT rp."PaperID", rp."CitationsByYear", {CITATIONS_EXPR}
        FROM "ResearchPaper" rp WHERE rp."PaperID" = ANY(%s)
    ''', ([keep, loser],))
    rows = {pid: (_as_year_dict(cby), int(total or 0))
            for pid, cby, total in cur.fetchall()}
    keep_cby, keep_total = rows.get(keep, ({}, 0))
    loser_cby, loser_total = rows.get(loser, ({}, 0))

    merged = dict(keep_cby)
    for y, n in (loser_cby or {}).items():
        merged[y] = max(int(merged.get(y, 0)), int(n))
    new_total = max(keep_total, loser_total)

    cur.execute(
        '''
        UPDATE "ResearchPaper"
        SET "CitationsByYear" = %s::jsonb,
            "RawData_Log" = CASE
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
        (json.dumps(merged), new_total, new_total, new_total, keep),
    )
    return new_total


def merge_group(cur, keep, losers, papers_meta, child_tables, a_cols):
    """Merge every loser into the kept paper. Caller wraps in a transaction.

    PROFILE-PRESERVATION GUARANTEE (user requirement): every researcher who
    was linked to ANY copy in the group must remain linked to the kept paper
    — the paper keeps showing in all their profiles. We capture the union of
    UserIDs BEFORE touching anything and assert it AFTER the merge; a failed
    assertion raises, which aborts the whole atomic transaction (no deletes).
    """
    col_list = ', '.join(f'"{c}"' for c in a_cols)
    sel_list = ', '.join('%s' if c == 'PaperID' else f'"{c}"' for c in a_cols)

    cur.execute(
        'SELECT DISTINCT "UserID" FROM "Authors" WHERE "PaperID" = ANY(%s)',
        ([keep] + losers,),
    )
    expected_users = {r[0] for r in cur.fetchall()}

    for loser in losers:
        # 1. Authors — remap with the (UserID, PaperID) unique index respected
        cur.execute(
            f'INSERT INTO "Authors" ({col_list}) '
            f'SELECT {sel_list} FROM "Authors" WHERE "PaperID" = %s '
            f'ON CONFLICT ("UserID", "PaperID") DO NOTHING',
            (keep, loser),
        )
        cur.execute('DELETE FROM "Authors" WHERE "PaperID" = %s', (loser,))

        # 2. Citations (PK = PaperID) — keep the larger count
        if "Citations" in child_tables:
            cur.execute('SELECT "CitationsCount" FROM "Citations" WHERE "PaperID" = %s',
                        (loser,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    '''
                    INSERT INTO "Citations" ("PaperID", "CitationsCount", "LastUpdate")
                    VALUES (%s, %s, NOW())
                    ON CONFLICT ("PaperID")
                    DO UPDATE SET "CitationsCount" =
                          GREATEST("Citations"."CitationsCount", EXCLUDED."CitationsCount"),
                        "LastUpdate" = NOW()
                    ''',
                    (keep, row[0]),
                )
                cur.execute('DELETE FROM "Citations" WHERE "PaperID" = %s', (loser,))

        # 3. Remaining child tables — generic remap
        for t in SIMPLE_CHILDREN:
            if t in child_tables:
                remap_simple_child(cur, t, loser, keep)

        # 4. Citation fields on the kept row
        new_total = merge_citation_fields(cur, keep, loser)

        # 5. AuditLog then delete the loser
        lm = papers_meta[loser]
        audit_meta = {
            "kept_paper_id": keep,
            "loser_title": lm["title"][:200],
            "loser_doi": lm["doi"],
            "loser_source": lm["source"],
            "loser_citations": lm["citations"],
            "merged_total": new_total,
        }
        cur.execute(
            'INSERT INTO "AuditLog" '
            '("TenantID","UserID","Action","TargetType","TargetID",'
            ' "Metadata","IpAddress","UserAgent") '
            'VALUES (1, NULL, %s, %s, %s, %s::jsonb, NULL, %s)',
            ["paper.merge.dedup", "ResearchPaper", loser,
             json.dumps(audit_meta, ensure_ascii=False),
             "script:tools/dedup_papers.py"],
        )
        cur.execute('DELETE FROM "ResearchPaper" WHERE "PaperID" = %s', (loser,))

    # Profile-preservation assertion: the kept paper must now carry EVERY
    # researcher who was linked to any copy. Raising here aborts the atomic
    # transaction — nothing gets deleted on failure.
    cur.execute('SELECT DISTINCT "UserID" FROM "Authors" WHERE "PaperID" = %s', (keep,))
    actual_users = {r[0] for r in cur.fetchall()}
    missing = expected_users - actual_users
    if missing:
        raise RuntimeError(
            f"PROFILE-PRESERVATION VIOLATION on kept paper {keep}: "
            f"UserIDs {sorted(missing)} lost their link — rolling back."
        )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def build_report(groups, papers):
    out = []
    for i, g in enumerate(groups, 1):
        keep, losers = choose_keep(g["members"], papers)
        entry = {
            "group_id": i,
            "match_reasons": g["match_reasons"],
            "kept": {k: papers[keep][k] for k in
                     ("paper_id", "title", "doi", "pub_year", "citations",
                      "is_verified", "source")},
            "losers": [
                {k: papers[pid][k] for k in
                 ("paper_id", "title", "doi", "pub_year", "citations",
                  "is_verified", "source")}
                for pid in losers
            ],
        }
        out.append(entry)
    return out


def write_reports(report, ts):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = AUDIT_DIR / f"groups_{ts}.json"
    json_path.write_text(
        json.dumps({"generated_at": ts, "groups": report},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    csv_path = AUDIT_DIR / f"groups_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["group_id", "role", "paper_id", "title", "doi",
                    "pub_year", "citations", "source", "match_reasons"])
        for g in report:
            w.writerow([g["group_id"], "KEEP", g["kept"]["paper_id"],
                        g["kept"]["title"][:80], g["kept"]["doi"] or "",
                        g["kept"]["pub_year"], g["kept"]["citations"],
                        g["kept"]["source"], "; ".join(g["match_reasons"])])
            for l in g["losers"]:
                w.writerow([g["group_id"], "loser", l["paper_id"],
                            l["title"][:80], l["doi"] or "",
                            l["pub_year"], l["citations"], l["source"], ""])
    return json_path, csv_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Detect + report only. No DB writes.")
    mode.add_argument("--apply", action="store_true",
                      help="Merge duplicate groups (snapshot + AuditLog first).")
    ap.add_argument("--user", type=int, default=None,
                    help="Scope detection to one researcher's papers.")
    ap.add_argument("--title-threshold", type=float, default=0.90,
                    help="Fuzzy title similarity threshold (default 0.90).")
    ap.add_argument("--report-in", type=str, default=None,
                    help="Reviewed groups JSON from a previous --dry-run; "
                         "--apply merges exactly these groups.")
    ap.add_argument("--limit-groups", type=int, default=None,
                    help="Merge at most N groups this run (use 1 first).")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    with connection.cursor() as cur:
        print("Loading papers...")
        papers = load_papers(cur, args.user)
        print(f"  {len(papers)} papers in scope")

        if args.report_in:
            data = json.loads(Path(args.report_in).read_text(encoding="utf-8"))
            report = data["groups"]
            # Drop papers that no longer exist (already merged in a prior run)
            valid = []
            for g in report:
                members = [g["kept"]["paper_id"]] + [l["paper_id"] for l in g["losers"]]
                alive = [m for m in members if m in papers]
                if g["kept"]["paper_id"] in papers and len(alive) > 1:
                    g["losers"] = [l for l in g["losers"] if l["paper_id"] in papers]
                    valid.append(g)
            report = valid
            print(f"  groups from {args.report_in}: {len(report)} still mergeable")
        else:
            print(f"Detecting duplicates (fuzzy threshold {args.title_threshold})...")
            groups = detect_groups(papers, args.title_threshold)
            report = build_report(groups, papers)
            print(f"  duplicate groups found: {len(report)}")

    if not report:
        print("No duplicate groups. Nothing to do.")
        return

    if args.limit_groups:
        report = report[:args.limit_groups]

    if args.dry_run:
        json_path, csv_path = write_reports(report, ts)
        n_losers = sum(len(g["losers"]) for g in report)
        print(f"\n[DRY-RUN] {len(report)} groups / {n_losers} papers would be merged away.")
        for g in report[:10]:
            print(f"\n  Group {g['group_id']} ({'; '.join(g['match_reasons'])}):")
            print(f"    KEEP  {g['kept']['paper_id']}  cit={g['kept']['citations']}  "
                  f"doi={g['kept']['doi'] or '-'}  {g['kept']['title'][:60]}")
            for l in g["losers"]:
                print(f"    drop  {l['paper_id']}  cit={l['citations']}  "
                      f"doi={l['doi'] or '-'}  {l['title'][:60]}")
        if len(report) > 10:
            print(f"\n  ... and {len(report) - 10} more groups (see report)")
        print(f"\nReview then apply:")
        print(f"  JSON : {json_path}")
        print(f"  CSV  : {csv_path}")
        print(f"  python tools/dedup_papers.py --apply --report-in \"{json_path}\" --limit-groups 1")
        return

    # ----------------------- APPLY -----------------------
    with transaction.atomic():
        with connection.cursor() as cur:
            child_tables = existing_child_tables(cur)
            print(f"Child tables present: {sorted(child_tables)}")
            a_cols = authors_columns(cur)

            # Full snapshot BEFORE any write
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            snapshot = {"taken_at": ts, "groups": []}
            for g in report:
                members = [g["kept"]["paper_id"]] + [l["paper_id"] for l in g["losers"]]
                snapshot["groups"].append({
                    "group_id": g["group_id"],
                    "kept_paper_id": g["kept"]["paper_id"],
                    "papers": {str(pid): snapshot_paper(cur, pid, child_tables)
                               for pid in members},
                })
            snap_path = AUDIT_DIR / f"snapshot_{ts}.json"
            snap_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            print(f"Snapshot written: {snap_path}")

            n_groups = n_losers = 0
            for g in report:
                keep = g["kept"]["paper_id"]
                losers = [l["paper_id"] for l in g["losers"]]
                merge_group(cur, keep, losers, papers, child_tables, a_cols)
                n_groups += 1
                n_losers += len(losers)
                print(f"  group {g['group_id']}: kept {keep}, merged {losers}")

            print(f"\nMerged {n_losers} duplicates across {n_groups} groups.")
            print("Recovery: AuditLog WHERE \"Action\"='paper.merge.dedup' "
                  f"+ snapshot {snap_path.name}")


if __name__ == "__main__":
    main()
