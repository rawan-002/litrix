"""Detect duplicate ResearchPaper rows and merge them from a reviewed plan.

backend/find_duplicate_papers.py only detects exact DOI/NormalizedTitle dups;
this also catches near-identical titles (a leading "Research Article"/
"Review Article" tag, preprint vs published, punctuation) via fuzzy
matching -- blocked on significant title words (not a raw character
prefix, which a boilerplate tag defeats) and gated on shared authorship.

Two-stage pipeline, deliberately separated so execution never re-derives a
decision it should just be replaying:

    detect --dry-run --> merge_plan_<ts>.json/.csv --> human review (edit
    the JSON if you disagree with a group) --> apply --plan <file>

Every group in the plan is tagged confidence 'high' or 'review'
(group_confidence()): 'high' only for the narrow, checked-safe profile --
shared author, >=95% title similarity, a compatible pub year, and EXACTLY
one side carrying a DOI. A Corrigendum/Erratum/Retraction is NEVER 'high'
regardless of those signals (it's a formally distinct scholarly record,
not a duplicate). --apply reads confidence straight from the plan file and
ONLY ever merges 'high' groups -- it does not recompute confidence or
re-run choose_keep() against live data, so what you reviewed in the plan
is exactly what gets executed. 'review' groups stay in the plan for a
human to resolve separately; nothing merges them automatically, ever.

Merges run inside one atomic transaction per --apply call, after dumping a
full pre-merge snapshot to data/dedup_audit/snapshot_<ts>.json. Recovery is
queryable: every merged loser leaves an AuditLog row
(Action='paper.merge.dedup'). Use --limit-groups 1 on your first --apply.
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
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


# Leading boilerplate that scrapers sometimes fold into the title itself
# (Scholar PDF-header noise, journal section labels). Stripped so it can't
# shift a blocking key and hide a real duplicate -- caught via the Nizar
# Alsharif case (PaperID 6086/6088: "Research Article Prediction
# Approaches..." vs "Prediction approaches..." never got compared because
# the old blocking key was just the first 8 chars).
_BOILERPLATE_PREFIXES = (
    "research article", "review article", "original article",
    "conference paper", "short communication", "case report",
    "technical note", "brief report", "editorial", "letter",
    "corrigendum to", "erratum to", "erratum", "retraction of",
    "retraction:", "commentary",
)

_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "for", "and", "to", "using",
    "with", "from", "by", "into", "via", "towards", "toward",
}


# Ported from the Excel pipeline's norm_title (NFKD + strip accents + drop
# parenthesised text). Deliberately distinct from the DB NormalizedTitle column.
def norm_title(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)", "", s.lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip ONE leading boilerplate tag at a time -- a title could in theory
    # carry more than one, though that hasn't been observed in practice.
    changed = True
    while changed:
        changed = False
        for prefix in _BOILERPLATE_PREFIXES:
            if s.startswith(prefix + " "):
                s = s[len(prefix):].strip()
                changed = True
                break
    return s


def block_key(fuzzy_key, n=3):
    """Blocking key for the fuzzy pass: the first N non-stopword words of
    the (boilerplate-stripped) normalized title, not a raw character
    prefix. A raw fuzzy_key[:8] prefix is brittle -- any leading noise
    (a boilerplate tag norm_title() didn't know about, an extra word)
    shifts every character after it and the two titles land in different
    buckets, so they're never even compared. Blocking on significant WORDS
    survives a stray leading word much better than blocking on characters."""
    words = [w for w in fuzzy_key.split() if w not in _STOPWORDS]
    return " ".join(words[:n])


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
            "authors": frozenset(),
            "first_author": None,
        }

    if papers:
        cur.execute(
            'SELECT "PaperID", "UserID" FROM "Authors" WHERE "PaperID" = ANY(%s)',
            (list(papers),),
        )
        by_paper = defaultdict(set)
        for pid, uid in cur.fetchall():
            by_paper[pid].add(uid)
        for pid, uids in by_paper.items():
            if pid in papers:
                papers[pid]["authors"] = frozenset(uids)

        # First author by AuthorOrder -- NULLS LAST (matches the convention
        # in migrations/20260615_fix_journalrankings_duplicate_rows.sql's
        # first_author CTE), since AuthorOrder isn't always populated.
        cur.execute(
            '''
            SELECT DISTINCT ON ("PaperID") "PaperID", "UserID"
            FROM "Authors" WHERE "PaperID" = ANY(%s)
            ORDER BY "PaperID", "AuthorOrder", "UserID"
            ''',
            (list(papers),),
        )
        for pid, uid in cur.fetchall():
            if pid in papers:
                papers[pid]["first_author"] = uid

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
    # Block by the first few significant WORDS of the (boilerplate-stripped)
    # normalized title, not a raw character prefix -- see block_key()'s
    # docstring for why the old fuzzy_key[:8] scheme silently missed real
    # duplicates.
    blocks = defaultdict(list)
    for pid, p in papers.items():
        if len(p["fuzzy_key"]) >= 15:
            bk = block_key(p["fuzzy_key"])
            if bk:
                blocks[bk].append(pid)
    n_pairs = 0
    for ids in blocks.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                if find(idx[a]) == find(idx[b]):
                    continue        # already grouped by an exact rule
                # Require overlapping authorship before even considering a
                # fuzzy title match -- two papers on a similar topic with NO
                # shared author are almost never the same publication, and
                # this cheaply rules out most of the false positives a pure
                # text-similarity check finds (e.g. a journal's "Acknowledgment
                # to the Reviewers" notice recurring across different years).
                if not (papers[a]["authors"] & papers[b]["authors"]):
                    continue
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


def choose_keep_reason(keep, losers, papers):
    """Which choose_keep() tie-break factor actually decided, against the
    strongest runner-up -- the merge plan's audit trail records this as
    'سبب الاختيار', not just which paper won."""
    if not losers:
        return "only_member"
    k = papers[keep]
    r = papers[losers[0]]  # ranked[1], i.e. the closest contender
    if bool(k["doi"]) != bool(r["doi"]):
        return "has_doi"
    if k["citations"] != r["citations"]:
        return "citations"
    if len(k["title"]) != len(r["title"]):
        return "title_length"
    if k["is_verified"] != r["is_verified"]:
        return "is_verified"
    return "lowest_paper_id"


# Unlike "Research Article"/"Review Article" (cosmetic scraper noise with
# no independent existence -- safe to strip and treat as pure duplicates),
# these denote a FORMALLY DISTINCT scholarly record even when it's about
# the same underlying paper. Auto-merging one away would delete real
# correction/retraction history. Force manual review regardless of any
# other signal (a corrigendum missing its own DOI in our data is still a
# corrigendum, not "the same paper without a DOI yet").
_DISTINCT_RECORD_PREFIXES = ("corrigendum", "erratum", "retraction")


def _is_distinct_record(title):
    t = (title or "").strip().lower()
    return any(t.startswith(p) for p in _DISTINCT_RECORD_PREFIXES)


def _years_compatible(y1, y2):
    """Same year, or one/both missing (can't contradict what isn't there),
    or off by exactly one (handles a preprint-year vs published-year gap)."""
    if y1 is None or y2 is None:
        return True
    return abs(y1 - y2) <= 1


def _year_gap_exceeds(y1, y2, max_gap):
    if y1 is None or y2 is None:
        return False
    return abs(y1 - y2) > max_gap


def hard_exclusion_reason(keep, loser, papers):
    """Named, independently-testable guardrails against merging
    'evolutionary papers' -- genuinely different, independently registered
    works that can still score >=95% on raw title/abstract similarity (a
    conference paper and its later, separately-DOI'd journal extension; two
    narrow papers by an overlapping team a few years apart). High text
    similarity alone was never sufficient -- DOI, publication year, and
    first author settle it immediately once you look.

    Both rules require DOIs that are BOTH present and DIFFERENT, not just
    one side missing one -- two distinct, real DOIs is the strongest signal
    two catalog entries are actually separate publications:
      1. different DOI + pub year gap > 2  -> never auto-merge
      2. different DOI + different first author -> force manual review

    Checked first in pair_confidence(), ahead of the softer similarity
    scoring, so a future retune of that scoring can't accidentally let an
    evolutionary-papers pair back into [HIGH]. Returns the reason string if
    a rule fires, else None -- see test_dedup_papers.py for the regression
    case this codifies."""
    k, l = papers[keep], papers[loser]
    if not (k["doi"] and l["doi"] and k["doi"] != l["doi"]):
        return None
    if _year_gap_exceeds(k["pub_year"], l["pub_year"], max_gap=2):
        return "different_doi_and_year_gap_gt_2"
    if k["first_author"] and l["first_author"] and k["first_author"] != l["first_author"]:
        return "different_doi_and_different_first_author"
    return None


def pair_confidence(keep, loser, papers):
    """'high' only for the exact auto-mergeable profile: shared author,
    >=95% title similarity, a compatible pub year, and EXACTLY one side
    carrying a DOI (the asymmetry that says "same paper, one copy just
    hasn't been enriched yet" -- two DIFFERENT real DOIs, or neither
    side having one, isn't that signal and goes to manual review instead.
    Everything else -- a real venue/edition difference, a year that
    doesn't line up -- also goes to 'review'. This is deliberately
    stricter than the fuzzy-match threshold that puts a pair in a group
    at all: grouping casts a wide net for a human to look at, confidence
    decides what's safe to merge unattended."""
    k, l = papers[keep], papers[loser]
    if _is_distinct_record(k["title"]) or _is_distinct_record(l["title"]):
        return "review"
    if hard_exclusion_reason(keep, loser, papers):
        return "review"
    if not (k["authors"] & l["authors"]):
        return "review"
    sim = SequenceMatcher(None, k["fuzzy_key"], l["fuzzy_key"]).ratio()
    if sim < 0.95:
        return "review"
    if not _years_compatible(k["pub_year"], l["pub_year"]):
        return "review"
    if bool(k["doi"]) == bool(l["doi"]):
        return "review"
    return "high"


def group_confidence(keep, losers, match_reasons, papers):
    """Exact-identifier groups (same DOI / same normalized title) are
    unambiguous regardless of author/year/DOI shape -- only groups that
    include a fuzzy-title edge go through the stricter per-pair check."""
    if not any(r.startswith("fuzzy_title_") for r in match_reasons):
        return "high"
    return "high" if all(
        pair_confidence(keep, l, papers) == "high" for l in losers
    ) else "review"


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

    Every researcher linked to any copy in the group must stay linked to the
    kept paper, so it keeps showing in all their profiles. We snapshot the union
    of UserIDs before touching anything and assert it afterwards — a failed
    assertion raises and aborts the whole transaction, so nothing gets deleted.
    """
    col_list = ', '.join(f'"{c}"' for c in a_cols)
    sel_list = ', '.join('%s' if c == 'PaperID' else f'"{c}"' for c in a_cols)

    cur.execute(
        'SELECT DISTINCT "UserID" FROM "Authors" WHERE "PaperID" = ANY(%s)',
        ([keep] + losers,),
    )
    expected_users = {r[0] for r in cur.fetchall()}

    for loser in losers:
        # Authors: remap, respecting the (UserID, PaperID) unique index
        cur.execute(
            f'INSERT INTO "Authors" ({col_list}) '
            f'SELECT {sel_list} FROM "Authors" WHERE "PaperID" = %s '
            f'ON CONFLICT ("UserID", "PaperID") DO NOTHING',
            (keep, loser),
        )
        cur.execute('DELETE FROM "Authors" WHERE "PaperID" = %s', (loser,))

        # Citations is keyed by PaperID alone, so keep the larger count
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

        for t in SIMPLE_CHILDREN:
            if t in child_tables:
                remap_simple_child(cur, t, loser, keep)

        new_total = merge_citation_fields(cur, keep, loser)

        # AuditLog the loser before deleting it, for recovery
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


def build_report(groups, papers):
    """Produces the merge PLAN, not just a report -- every field a human
    (or a later --apply run) needs to trust the decision without re-deriving
    it: which paper is kept and WHY (keep_reason), why the group was formed
    (match_reasons), and how confident the plan is in auto-merging it
    (confidence). --apply executes exactly this plan; it never re-runs
    group_confidence() or choose_keep() against live data."""
    out = []
    for i, g in enumerate(groups, 1):
        keep, losers = choose_keep(g["members"], papers)
        entry = {
            "group_id": i,
            "match_reasons": g["match_reasons"],
            "keep_reason": choose_keep_reason(keep, losers, papers),
            "confidence": group_confidence(keep, losers, g["match_reasons"], papers),
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


def write_merge_plan(report, ts):
    """Writes the merge plan (JSON + CSV) -- the frozen, reviewable record
    of exactly what --apply --plan will do. Not a log of what happened;
    a plan of what WILL happen, to be reviewed (and hand-edited if needed)
    before that."""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = AUDIT_DIR / f"merge_plan_{ts}.json"
    json_path.write_text(
        json.dumps({"generated_at": ts, "groups": report},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    csv_path = AUDIT_DIR / f"merge_plan_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["group_id", "confidence", "role", "paper_id", "title", "doi",
                    "pub_year", "citations", "source", "keep_reason", "match_reasons"])
        for g in report:
            conf = g.get("confidence", "review")
            w.writerow([g["group_id"], conf, "KEEP", g["kept"]["paper_id"],
                        g["kept"]["title"][:80], g["kept"]["doi"] or "",
                        g["kept"]["pub_year"], g["kept"]["citations"],
                        g["kept"]["source"], g.get("keep_reason", ""),
                        "; ".join(g["match_reasons"])])
            for l in g["losers"]:
                w.writerow([g["group_id"], conf, "MERGE", l["paper_id"],
                            l["title"][:80], l["doi"] or "",
                            l["pub_year"], l["citations"], l["source"], "", ""])
    return json_path, csv_path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Detect duplicates and write a merge plan. No DB writes.")
    mode.add_argument("--apply", action="store_true",
                      help="Execute a merge plan from --plan (snapshot + AuditLog first). "
                           "Never re-detects or re-decides -- requires --plan.")
    ap.add_argument("--user", type=int, default=None,
                    help="Scope detection to one researcher's papers.")
    ap.add_argument("--title-threshold", type=float, default=0.90,
                    help="Fuzzy title similarity threshold (default 0.90).")
    ap.add_argument("--plan", type=str, default=None,
                    help="A merge_plan_*.json from a previous --dry-run. Required for "
                         "--apply: execution always comes from a reviewed plan file, "
                         "never straight from a fresh detection run.")
    ap.add_argument("--limit-groups", type=int, default=None,
                    help="Merge at most N groups this run (use 1 first).")
    args = ap.parse_args()

    if args.apply and not args.plan:
        ap.error("--apply requires --plan <merge_plan.json>. Run --dry-run first, "
                 "review the plan it writes, then --apply --plan <that file>.")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    with connection.cursor() as cur:
        print("Loading papers...")
        papers = load_papers(cur, args.user)
        print(f"  {len(papers)} papers in scope")

        if args.plan:
            data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            report = data["groups"]
            # The ONLY thing checked against live data is whether a paper
            # still exists (it may have been merged away by a prior partial
            # run). The plan's "confidence" and "keep_reason" are NOT
            # recomputed -- the plan is the frozen decision; --apply
            # executes it, it doesn't re-decide it.
            valid = []
            for g in report:
                members = [g["kept"]["paper_id"]] + [l["paper_id"] for l in g["losers"]]
                alive = [m for m in members if m in papers]
                if g["kept"]["paper_id"] in papers and len(alive) > 1:
                    g["losers"] = [l for l in g["losers"] if l["paper_id"] in papers]
                    valid.append(g)
            report = valid
            print(f"  groups from {args.plan}: {len(report)} still mergeable")
        else:
            print(f"Detecting duplicates (fuzzy threshold {args.title_threshold})...")
            groups = detect_groups(papers, args.title_threshold)
            report = build_report(groups, papers)
            print(f"  duplicate groups found: {len(report)}")

    if not report:
        print("No duplicate groups. Nothing to do.")
        return

    if not args.dry_run:
        # --apply NEVER auto-merges a [REVIEW] group -- those are the ones
        # that turned out, on inspection, to sometimes be genuinely
        # different papers (see group_confidence's docstring). A human
        # decides those separately; this tool only executes what the plan
        # already marked [HIGH].
        skipped = [g for g in report if g.get("confidence") != "high"]
        report = [g for g in report if g.get("confidence") == "high"]
        if skipped:
            print(f"Skipping {len(skipped)} [REVIEW] group(s) from the plan -- not auto-merged:")
            for g in skipped:
                print(f"  group {g['group_id']}: kept={g['kept']['paper_id']} "
                      f"losers={[l['paper_id'] for l in g['losers']]} "
                      f"({'; '.join(g['match_reasons'])})")
        if not report:
            print("No [HIGH]-confidence groups left to merge. Nothing to do.")
            return

    if args.limit_groups:
        report = report[:args.limit_groups]

    if args.dry_run:
        json_path, csv_path = write_merge_plan(report, ts)
        n_losers = sum(len(g["losers"]) for g in report)
        n_high = sum(1 for g in report if g.get("confidence") == "high")
        print(f"\n[MERGE PLAN] {len(report)} groups / {n_losers} papers would be merged away "
              f"({n_high} high-confidence, {len(report) - n_high} need manual review).")
        for g in report[:10]:
            conf = g.get("confidence", "review")
            print(f"\n  Group {g['group_id']} [{conf.upper()}] "
                  f"keep_reason={g.get('keep_reason', '?')} "
                  f"({'; '.join(g['match_reasons'])}):")
            print(f"    KEEP  {g['kept']['paper_id']}  cit={g['kept']['citations']}  "
                  f"doi={g['kept']['doi'] or '-'}  {g['kept']['title'][:60]}")
            for l in g["losers"]:
                print(f"    drop  {l['paper_id']}  cit={l['citations']}  "
                      f"doi={l['doi'] or '-'}  {l['title'][:60]}")
        if len(report) > 10:
            print(f"\n  ... and {len(report) - 10} more groups (see plan)")
        print(f"\n--apply only ever merges [HIGH] groups from this plan; [REVIEW] ones are "
              f"recorded for a human to resolve and are never auto-merged, even by --apply.")
        print(f"\nReview the plan (hand-edit it if you disagree with a decision), then:")
        print(f"  JSON : {json_path}")
        print(f"  CSV  : {csv_path}")
        print(f"  python tools/dedup_papers.py --apply --plan \"{json_path}\" --limit-groups 1")
        return

    # --apply: everything below runs in one transaction
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
