"""Phase 4C -- READ-ONLY duplicate-ResearchPaper merge-plan generator.

Given a (winner, loser) pair already identified as a likely duplicate
(e.g. by dedup_papers.py's own detect_groups()/pair_confidence()), produces
a structured, human-reviewable JSON plan describing exactly what a future
merge WOULD do -- field by field, dependency table by dependency table --
without ever performing that merge.

Hard boundary, by design:
  - This module never calls merge_group(), never opens a write transaction,
    never runs with --apply, and never touches dedup_papers.py's own code.
  - Every plan this module produces carries "execution_permitted": false.
    There is no flag, mode, or code path in this file that flips it.
  - All DB access here is SELECT-only (see fetch_paper_row/fetch_dependency_counts).

Survivor selection is NOT reinvented here -- winner/loser are supplied by
the caller (typically dedup_papers.py's own choose_keep()), and this module
only explains/labels that choice (see build_survivor_reason). The one thing
genuinely new in this module is FIELD-LEVEL and DEPENDENCY-LEVEL plan
classification: for every ResearchPaper column and every FK-referencing
table, decide what a safe merge implementation would need to do, and flag
anything that isn't a deterministic, evidence-backed action as requiring
human review. See PHASE4C_NOTES.md-equivalent detail in the accompanying
report (backend/reports/phase4c_merge_plan_report.md) for the reasoning
behind each policy table below.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "tools"))

REPORT_DIR = BACKEND_DIR / "reports"

# ---------------------------------------------------------------------------
# ResearchPaper column inventory (33 columns, gathered read-only this phase
# via information_schema.columns -- see the Phase 4C report §3 for the full
# nullable/type table this list is derived from).
# ---------------------------------------------------------------------------
RP_COLUMNS = [
    "JournalID", "Title", "Title_En", "Abstract", "Abstract_En", "Language",
    "DOI", "PubYear", "Volume", "Issue", "Pages", "IsVerified", "ScrapedAt",
    "Source", "RawData_Log", "SearchVector_En", "SearchVector_Ar",
    "NormalizedTitle", "Indexing", "CitationsByYear", "TenantID",
    "AffiliationVerified", "VerificationSource", "VerifiedAt",
    "VerificationDetails", "VenueType", "DoiResolvedBy", "DoiResolvedAt",
    "OpenAlexWorkID", "AbstractSource", "PdfUrl", "PdfAccessType",
    "PublicationType",
]  # PaperID intentionally excluded from this list -- it's the join key,
   # handled separately as an IDENTITY field, not a content field to diff.

DOI_FIELD = "DOI"
# Handled by the doi_state object instead of a plain field_actions verdict --
# DOI changes are explicitly out of scope for this phase (and for automatic
# planning generally: a wrong DOI merge is one of the highest-cost mistakes
# this whole project exists to prevent). field_actions still lists DOI (never
# silently omitted) but always defers its action to doi_state.

JOURNAL_ID_FIELD = "JournalID"
# Phase 4E, Gap 1: same deferral pattern as DOI_FIELD above -- JournalID
# gets its own dedicated, explicit decision object (journal_state, built by
# dedup_papers.py's journal_id_decision()) rather than being resolved by
# the generic classify_field() rules. field_actions still lists JournalID
# (never silently omitted) but always defers its action to journal_state.

# Derived/generated -- not meaningful to diff as literal values.
DERIVED_FIELDS = {"SearchVector_En", "SearchVector_Ar"}

# Already reconciled by dedup_papers.py's own merge_citation_fields()
# (element-wise MAX per year) -- a real, existing, repository-backed rule,
# so this is classified MERGE rather than CONFLICT/COPY_LOSER.
MERGE_HANDLED_FIELDS = {"CitationsByYear"}

# The title columns ARE the duplicate signal -- a CONFLICT here is expected,
# not a data-loss risk (the loser's literal text stays in the pre-merge
# snapshot). Never eligible for COPY_LOSER: if the winner is ever missing a
# title (Title is NOT NULL in the schema, so this can only happen for the
# nullable NormalizedTitle), the answer is "recompute via norm_title()", not
# "copy the loser's raw string" -- flagged HUMAN_REVIEW instead of an
# automatic action.
TITLE_FIELDS = {"Title", "NormalizedTitle"}

# Process/ingestion timestamps: they describe WHEN this specific row was
# scraped or verified, not a fact about the underlying paper. Two
# independently-ingested duplicate rows will almost always carry different
# ScrapedAt/VerifiedAt/DoiResolvedAt values -- that is expected and not a
# data conflict to resolve. The survivor's own timestamp remains accurate
# and authoritative for the surviving row after a merge (this matches what
# merge_group() already does today: it never touches these columns on the
# kept row). Only relevant when BOTH sides are populated and differ; a
# one-sided value (only the loser was ever verified/scraped) still goes
# through the normal COPY_LOSER path below since it's informative there.
PROCESS_TIMESTAMP_FIELDS = {"ScrapedAt", "VerifiedAt", "DoiResolvedAt"}

# One-sided values that should NOT be silently backfilled from the loser
# even though they're "deterministic" in the trivial sense of "only one
# side has a value" -- copying them would misrepresent the survivor's own
# identity/provenance rather than filling in a missing fact about the paper:
#   - TenantID: multi-tenant isolation boundary. A NULL/differing TenantID
#     is a data-integrity anomaly, not something to paper over automatically.
#   - RawData_Log: the raw scrape payload is specific to which source/run
#     produced THAT row; attaching the loser's raw log to the survivor would
#     misrepresent where the survivor's own data came from.
NO_AUTO_COPY_FIELDS = {"TenantID", "RawData_Log"} | TITLE_FIELDS | {DOI_FIELD} | MERGE_HANDLED_FIELDS | DERIVED_FIELDS

STATUS_KEEP_WINNER = "KEEP_WINNER"
STATUS_COPY_LOSER = "COPY_LOSER"
STATUS_CONFLICT = "CONFLICT"
STATUS_EQUAL = "EQUAL"
STATUS_EMPTY_BOTH = "EMPTY_BOTH"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_HUMAN_REVIEW = "HUMAN_REVIEW"
STATUS_MERGE = "MERGE"          # existing repo-backed reconciliation (CitationsByYear)
STATUS_DERIVED = "DERIVED"      # tsvector columns, not diffed
STATUS_IDENTITY = "IDENTITY"    # PaperID
STATUS_CONFLICT_EXPECTED = "CONFLICT_EXPECTED"  # differs by construction, not a blocker

# Fields where "both populated, different" is the EXPECTED shape of a
# duplicate pair, not an ambiguity a human needs to resolve:
#   - Title/NormalizedTitle: the wording difference (boilerplate prefix,
#     rephrasing) IS the duplicate signal itself. The loser's exact text
#     stays recoverable via the pre-merge JSON snapshot even though it's
#     not visible in live queries after a merge -- same conclusion Phase
#     4B.1 reached by hand for all 10 forensic pairs.
#   - RawData_Log: each row's raw scrape payload is specific to that row's
#     own ingestion; two independently-scraped duplicate rows differing
#     here is normal, not evidence of a data problem needing a human
#     decision. The survivor's own raw log stays accurate for itself.
NON_BLOCKING_EXPECTED_DIFF_FIELDS = TITLE_FIELDS | {"RawData_Log"}


def _is_empty(v):
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    if isinstance(v, dict) and len(v) == 0:
        return True
    return False


def classify_field(field, winner_val, loser_val):
    """Pure function: classify one ResearchPaper column across a
    (winner, loser) pair. Returns (status, reason, recommended_action)."""
    if field in DERIVED_FIELDS:
        return STATUS_DERIVED, "tsvector regenerated from Title/Abstract by application code, not an independent data field", "NO_ACTION"

    if field in MERGE_HANDLED_FIELDS:
        if winner_val == loser_val:
            return STATUS_EQUAL, "identical on both sides", "NO_ACTION"
        return STATUS_MERGE, "reconciled today by dedup_papers.py's merge_citation_fields() (element-wise MAX per year) -- existing, repository-backed rule, not a conflict", "MERGE_ELEMENTWISE_MAX"

    if field == DOI_FIELD:
        # Real verdict lives in doi_state; field_actions still reports it
        # rather than silently omitting the column.
        we, le = _is_empty(winner_val), _is_empty(loser_val)
        if we and le:
            return STATUS_EMPTY_BOTH, "neither side has a DOI", "SEE_DOI_STATE"
        if winner_val == loser_val:
            return STATUS_EQUAL, "same DOI on both sides", "SEE_DOI_STATE"
        return STATUS_UNKNOWN, "DOI decisions are governed by doi_state, never by a generic field action", "SEE_DOI_STATE"

    if field == JOURNAL_ID_FIELD:
        # Real verdict lives in journal_state (Phase 4E, Gap 1); field_actions
        # still reports it rather than silently omitting the column.
        we, le = _is_empty(winner_val), _is_empty(loser_val)
        if we and le:
            return STATUS_EMPTY_BOTH, "neither side has a JournalID", "SEE_JOURNAL_STATE"
        if winner_val == loser_val:
            return STATUS_EQUAL, "same JournalID on both sides", "SEE_JOURNAL_STATE"
        if we:
            return STATUS_UNKNOWN, "JournalID decisions are governed by journal_state, never by a generic field action", "SEE_JOURNAL_STATE"
        return STATUS_UNKNOWN, "JournalID decisions are governed by journal_state, never by a generic field action", "SEE_JOURNAL_STATE"

    we, le = _is_empty(winner_val), _is_empty(loser_val)

    if we and le:
        return STATUS_EMPTY_BOTH, "both empty", "NO_ACTION"

    if we != le:
        # Exactly one side has a value.
        if we:
            # winner empty, loser has it
            if field in NO_AUTO_COPY_FIELDS:
                return STATUS_HUMAN_REVIEW, f"loser has a value the winner lacks, but {field} is not in the auto-copy-safe policy (see NO_AUTO_COPY_FIELDS)", "HUMAN_REVIEW"
            return STATUS_COPY_LOSER, "winner is empty, loser has a real value -- deterministic backfill candidate, no ambiguity about which value to use", "BACKFILL_FROM_LOSER"
        else:
            # winner has it, loser doesn't -- nothing to lose either way
            return STATUS_KEEP_WINNER, "loser has no value to contribute", "NO_ACTION"

    # both populated
    if winner_val == loser_val:
        return STATUS_EQUAL, "equal values", "NO_ACTION"

    if field in PROCESS_TIMESTAMP_FIELDS:
        return STATUS_KEEP_WINNER, "process/ingestion timestamp describing when THIS row was scraped/verified -- the survivor's own timestamp stays authoritative for the surviving row after a merge; a differing loser timestamp is not a data conflict to resolve (matches merge_group()'s existing, unmodified behavior of never touching this column on the kept row)", "NO_ACTION"

    if field in NON_BLOCKING_EXPECTED_DIFF_FIELDS:
        return STATUS_CONFLICT_EXPECTED, "differing values here are the expected shape of a genuine duplicate pair, not an ambiguity requiring a human decision -- the loser's value stays recoverable via the pre-merge snapshot even though it won't be visible in live queries after a merge", "NO_ACTION"

    if field == "TenantID":
        return STATUS_CONFLICT, "TenantID differs between winner and loser -- a multi-tenant isolation boundary violation, not an ordinary field conflict", "BLOCK_PAIR"

    return STATUS_CONFLICT, "both populated, different values, no repository-backed deterministic rule exists to pick a winner", "HUMAN_DECISION_REQUIRED"


def build_field_actions(winner_row, loser_row):
    """Pure function. winner_row/loser_row: dicts keyed by RP_COLUMNS
    (+ optionally PaperID, ignored here)."""
    actions = []
    for field in RP_COLUMNS:
        wv = winner_row.get(field)
        lv = loser_row.get(field)
        status, reason, action = classify_field(field, wv, lv)
        actions.append({
            "field": field,
            "winner_value": _preview(wv),
            "loser_value": _preview(lv),
            "status": status,
            "recommended_action": action,
            "reason": reason,
        })
    return actions


def _preview(v, limit=200):
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= limit else s[:limit] + "..."


def build_journal_state(winner_journal_id, loser_journal_id):
    """Pure function -- Phase 4E, Gap 1. Wraps dedup_papers.py's own
    journal_id_decision() (the repository-native decision model) into the
    plan schema, parallel to build_doi_state() above. This does not
    duplicate the decision logic -- it only shapes it for the plan.

    execution_permitted is False only for the CONFLICT state (two
    different, real JournalIDs -- never auto-resolved); every other state
    is either nothing-to-do or a single deterministic backfill action."""
    from dedup_papers import journal_id_decision, JOURNAL_CONFLICT
    state, action = journal_id_decision(winner_journal_id, loser_journal_id)
    blocked = state == JOURNAL_CONFLICT
    return {
        "state": state,
        "winner_value": winner_journal_id,
        "loser_value": loser_journal_id,
        "planned_action": action,
        "execution_permitted": not blocked,
        "blocking_reason": action if blocked else None,
    }


def build_author_conflict_report(winner_authors, loser_authors):
    """Pure function -- Phase 4E, Gap 2. Wraps dedup_papers.py's own
    author_content_conflicts() (same (UserID,PaperID) identity key
    merge_group()'s real SQL already uses) into the plan schema.

    Any non-empty conflict list blocks automatic execution for the pair --
    no formatting preference is invented here or in dedup_papers.py; a
    human (or a future, explicit, deterministic rule this phase does not
    build) must resolve which AuthorNameRaw survives.

    Phase 4Y (Policy B, CASE_ONLY_ALLOWED_FOR_HUMAN_REVIEW_ONLY): each
    conflict dict is additionally annotated with author_conflict_type /
    author_conflict_type_reason via merge_execution_safety.py's narrow
    is_case_only_difference() classifier. This is a LABEL ONLY -- it never
    changes execution_permitted or blocking_reason below, never triggers
    AUTO_MATCH, and does not touch author_content_conflicts()'s own
    exact-equality behavior. A case-only conflict still blocks automatic
    execution exactly like a genuine one; a human reviewer sees which kind
    they are looking at, nothing more."""
    from dedup_papers import author_content_conflicts
    from merge_execution_safety import is_case_only_difference
    conflicts = author_content_conflicts(winner_authors, loser_authors)
    for c in conflicts:
        is_case_only, reason = is_case_only_difference(
            c["winner_author_name_raw"], c["loser_author_name_raw"])
        c["author_conflict_type"] = (
            "CASE_ONLY_FORMATTING_DIFFERENCE" if is_case_only else "GENUINE_CONFLICT")
        c["author_conflict_type_reason"] = reason
    return {
        "conflicts": conflicts,
        "execution_permitted": len(conflicts) == 0,
        "blocking_reason": (
            f"{len(conflicts)} Authors row(s) share a UserID with the loser but have a "
            f"different AuthorNameRaw -- merge_group()'s ON CONFLICT DO NOTHING would "
            f"silently keep only the winner's variant; no automatic formatting choice "
            f"is made, a human must resolve which value survives"
        ) if conflicts else None,
    }


def build_doi_state(winner_doi, loser_doi):
    """Pure function. Winner/loser DOI values already lower-cased/stripped
    of a leading 'https://doi.org/' by the caller (same normalization
    dedup_papers.py's own load_papers() applies)."""
    we, le = _is_empty(winner_doi), _is_empty(loser_doi)
    if not we and le:
        return {"winner": winner_doi, "loser": loser_doi, "action": "KEEP_EXISTING_WINNER_DOI",
                "reason": "winner already has a DOI, loser has none -- nothing to change"}
    if we and le:
        return {"winner": winner_doi, "loser": loser_doi, "action": "NO_CHANGE",
                "reason": "neither side has a DOI -- no DOI action possible or needed"}
    if not we and not le and winner_doi == loser_doi:
        return {"winner": winner_doi, "loser": loser_doi, "action": "NO_CHANGE",
                "reason": "both sides already carry the same DOI"}
    if we and not le:
        return {"winner": winner_doi, "loser": loser_doi, "action": "HUMAN_REVIEW",
                "reason": "loser has a DOI the winner lacks -- unexpected given choose_keep()'s has_doi priority; needs a human to confirm survivor selection before any DOI action"}
    return {"winner": winner_doi, "loser": loser_doi, "action": "HUMAN_REVIEW",
            "reason": "both sides carry different, real DOIs -- two independently registered identifiers; this is exactly what dedup_papers.py's hard_exclusion_reason() treats as a signal of possibly-distinct works, never auto-resolved"}


# ---------------------------------------------------------------------------
# Child-table / FK dependency plan
# ---------------------------------------------------------------------------
# (table, fk_column, on_delete_rule, remapped_by_existing_merge_group_today, note)
# Gathered read-only this phase via information_schema.table_constraints +
# referential_constraints (see Phase 4C report §3) -- NOT assumed from
# dedup_papers.py's SIMPLE_CHILDREN list, which is checked against this
# independently-derived set below.
CHILD_TABLE_SPECS = [
    ("Authors", "PaperID", "NO ACTION", True, "authors_union",
     "Special-cased in merge_group(): INSERT...ON CONFLICT(UserID,PaperID) DO NOTHING, then DELETE loser rows. A shared UserID is not an error -- ON CONFLICT silently keeps the survivor's existing link."),
    ("Citations", "PaperID", "NO ACTION", True, "citations_greatest",
     "Special-cased in merge_group(): GREATEST()-merge of CitationsCount via INSERT...ON CONFLICT(PaperID) DO UPDATE."),
    ("ExternalAuthors", "PaperID", "NO ACTION", True, "simple_children_remap",
     "In dedup_papers.py's SIMPLE_CHILDREN -- remap_simple_child() bulk UPDATE with a per-row SAVEPOINT/conflict-drop fallback."),
    ("PaperKeywords", "PaperID", "NO ACTION", True, "simple_children_remap",
     "Listed in SIMPLE_CHILDREN, but the table does not exist in this database today (confirmed via information_schema.tables) -- dead list entry, harmless."),
    ("CitationsHistory", "PaperID", "NO ACTION", True, "simple_children_remap",
     "In SIMPLE_CHILDREN -- remap_simple_child() bulk UPDATE with conflict-drop fallback."),
    ("ReportPaperDecision", "PaperID", "SET NULL", True, "simple_children_remap",
     "In SIMPLE_CHILDREN -- remap_simple_child() runs BEFORE the loser ResearchPaper row is deleted, so the ON DELETE SET NULL rule is a fallback that should not normally fire; still worth reporting precisely (corrects an earlier, less precise Phase 4B read that said NO ACTION)."),
    ("ReportPaperDecision", "MissingResolvedToPaperID", "SET NULL", False, "unhandled_second_fk",
     "NOT remapped by any existing logic -- only the table's primary PaperID column is in SIMPLE_CHILDREN. If a loser PaperID is ever referenced here, a real merge today would let it SET NULL silently on delete (not error, not cascade -- just silently lose the 'resolved to' link)."),
    ("AuthorReviewQueue", "PaperID", "CASCADE", False, "unhandled_cascade",
     "NOT in SIMPLE_CHILDREN, NOT special-cased, NOT included in snapshot_paper()'s captured child tables. If a loser PaperID ever has rows here, deleting the loser ResearchPaper row would CASCADE-delete them with zero recovery snapshot specific to this table."),
]


def plan_child_action(table, fk_col, on_delete, remapped_today, special, note, winner_rows, loser_rows):
    """Pure function -- counts are supplied by the caller (already fetched
    read-only), this function only decides the plan verdict."""
    risk = "none"
    if loser_rows == 0:
        planned_action = "NO_ACTION"
        risk = "none -- loser has 0 rows in this table, nothing to remap"
    elif special == "authors_union":
        planned_action = "MERGE"
        risk = "low -- existing merge_group() code handles UserID overlap via ON CONFLICT DO NOTHING"
    elif special == "citations_greatest":
        planned_action = "MERGE"
        risk = "low -- existing merge_group() code takes GREATEST(winner, loser) via ON CONFLICT DO UPDATE"
    elif special == "simple_children_remap" and remapped_today:
        planned_action = "REMAP_TO_SURVIVOR"
        risk = "low -- existing remap_simple_child() bulk UPDATE with a per-row SAVEPOINT/conflict-drop fallback on a unique-constraint violation"
    elif special == "unhandled_second_fk":
        planned_action = "REVIEW"
        risk = "medium -- no existing remap logic; ON DELETE SET NULL means this fails silently (not an error) but a real reference gets orphaned"
    elif special == "unhandled_cascade":
        planned_action = "REVIEW"
        risk = "high -- ON DELETE CASCADE with zero remap and zero recovery snapshot; rows would be silently and irrecoverably deleted by an unmodified merge_group() run today"
    else:
        planned_action = "UNKNOWN"
        risk = "unclassified dependency pattern -- needs explicit review before any plan involving it is trusted"

    return {
        "table": table,
        "foreign_key": fk_col,
        "on_delete": on_delete,
        "winner_rows": winner_rows,
        "loser_rows": loser_rows,
        "handled_by_merge_group_today": bool(remapped_today and loser_rows == 0 or (remapped_today and special in ("authors_union", "citations_greatest", "simple_children_remap"))),
        "planned_action": planned_action,
        "risk": risk,
        "note": note,
    }


def build_child_table_actions(dep_counts):
    """dep_counts: {(table, fk_col): {"winner": int, "loser": int}}"""
    out = []
    for table, fk_col, on_delete, remapped_today, special, note in CHILD_TABLE_SPECS:
        counts = dep_counts.get((table, fk_col), {"winner": 0, "loser": 0})
        out.append(plan_child_action(
            table, fk_col, on_delete, remapped_today, special, note,
            counts["winner"], counts["loser"]))
    return out


# ---------------------------------------------------------------------------
# Whole-pair plan assembly
# ---------------------------------------------------------------------------

def build_survivor_reason(keep_reason):
    labels = {
        "has_doi": "winner has a populated DOI, loser does not -- choose_keep()'s priority-1 tiebreak (PROVEN BY REPOSITORY: this is exactly the existing, unmodified rule)",
        "citations": "DOI presence tied; winner has more citations -- choose_keep()'s priority-2 tiebreak",
        "title_length": "DOI presence and citations tied; winner has the longer title string -- choose_keep()'s priority-3 tiebreak",
        "is_verified": "DOI/citations/title-length tied; winner is IsVerified and loser is not -- choose_keep()'s priority-4 tiebreak",
        "lowest_paper_id": "all other tiebreaks tied; winner has the lower PaperID -- choose_keep()'s final tiebreak",
        "only_member": "single-member group -- no loser to compare against",
    }
    return labels.get(keep_reason, f"unrecognized choose_keep_reason value: {keep_reason!r}")


def compute_classification(pair_confidence_val, hard_excl, is_distinct, field_actions, child_actions, doi_state,
                            missing_row=False, tenant_blocked=False, journal_state=None, author_conflict_report=None):
    if missing_row:
        return "BLOCKED", ["one or both PaperIDs not found in ResearchPaper"]
    if tenant_blocked:
        return "BLOCKED", ["TenantID differs between winner and loser -- cross-tenant merge is never planned automatically"]
    if hard_excl == "different_doi_and_year_gap_gt_2":
        return "BLOCKED", [f"hard_exclusion_reason={hard_excl} -- dedup_papers.py documents this as 'never auto-merge'"]

    blockers = []
    if pair_confidence_val != "high":
        blockers.append(f"pair_confidence={pair_confidence_val!r} (not 'high')")
    if hard_excl:
        blockers.append(f"hard_exclusion_reason={hard_excl}")
    if is_distinct:
        blockers.append("one side is a Corrigendum/Erratum/Retraction (formally distinct record per dedup_papers.py's _is_distinct_record())")
    for fa in field_actions:
        if fa["status"] in (STATUS_CONFLICT, STATUS_HUMAN_REVIEW):
            blockers.append(f"field {fa['field']}: {fa['status']} -- {fa['reason']}")
    for ca in child_actions:
        if ca["planned_action"] in ("REVIEW", "UNKNOWN"):
            blockers.append(f"dependency {ca['table']}.{ca['foreign_key']}: {ca['planned_action']} -- {ca['risk']}")
    if doi_state["action"] == "HUMAN_REVIEW":
        blockers.append(f"doi_state: {doi_state['reason']}")
    if journal_state is not None and not journal_state["execution_permitted"]:
        blockers.append(f"journal_state: {journal_state['blocking_reason']}")
    if author_conflict_report is not None and not author_conflict_report["execution_permitted"]:
        blockers.append(f"author_content_conflicts: {author_conflict_report['blocking_reason']}")

    if blockers:
        return "PLAN_REQUIRES_HUMAN_APPROVAL", blockers
    return "SAFE_PLAN_CANDIDATE", []


def generate_pair_plan(winner_id, loser_id, winner_row, loser_row, dep_counts,
                        pair_confidence_val, hard_excl, is_distinct, keep_reason,
                        missing_row=False, winner_authors=None, loser_authors=None):
    if missing_row:
        classification, blockers = compute_classification(None, None, None, [], [], {"action": "NO_CHANGE"}, missing_row=True)
        return {
            "pair": [winner_id, loser_id], "survivor": winner_id, "loser": loser_id,
            "confidence": None, "classification": classification,
            "survivor_reason": None, "doi_state": None, "journal_state": None,
            "field_actions": [], "child_table_actions": [],
            "author_content_conflicts": [], "unresolved_conflicts": blockers,
            "data_loss_risks": [], "requires_human_approval": True,
            "execution_permitted": False,
        }

    field_actions = build_field_actions(winner_row, loser_row)
    doi_state = build_doi_state(winner_row.get("DOI"), loser_row.get("DOI"))
    journal_state = build_journal_state(winner_row.get("JournalID"), loser_row.get("JournalID"))
    child_actions = build_child_table_actions(dep_counts)
    author_conflict_report = build_author_conflict_report(winner_authors or [], loser_authors or [])

    tenant_blocked = winner_row.get("TenantID") != loser_row.get("TenantID") \
        and winner_row.get("TenantID") is not None and loser_row.get("TenantID") is not None

    classification, blockers = compute_classification(
        pair_confidence_val, hard_excl, is_distinct, field_actions, child_actions,
        doi_state, missing_row=False, tenant_blocked=tenant_blocked,
        journal_state=journal_state, author_conflict_report=author_conflict_report)

    data_loss_risks = [
        f"{fa['field']}: loser has a value ({fa['loser_value']!r}) the winner lacks; an unmodified merge_group() run today would discard it (no backfill logic exists yet)"
        for fa in field_actions if fa["status"] == STATUS_COPY_LOSER
        # (JournalID can never appear here -- classify_field() always defers
        # it to journal_state below, Phase 4E Gap 1, same pattern as DOI.)
    ] + [
        f"{ca['table']}.{ca['foreign_key']}: {ca['loser_rows']} loser row(s), {ca['risk']}"
        for ca in child_actions if ca["planned_action"] == "REVIEW" and ca["loser_rows"] > 0
    ]
    if journal_state["state"] == "LOSER_ONLY_BACKFILL":
        data_loss_risks.append(
            f"JournalID: loser has {journal_state['loser_value']!r} the winner lacks; "
            f"an unmodified merge_group() run today would discard it (no backfill logic exists yet)")
    for c in author_conflict_report["conflicts"]:
        data_loss_risks.append(
            f"Authors.AuthorNameRaw (UserID={c['UserID']}): winner={c['winner_author_name_raw']!r}, "
            f"loser={c['loser_author_name_raw']!r} -- an unmodified merge_group() run today would silently "
            f"discard the loser's variant via ON CONFLICT DO NOTHING with zero detection")

    return {
        "pair": [winner_id, loser_id],
        "survivor": winner_id,
        "loser": loser_id,
        "confidence": pair_confidence_val,
        "classification": classification,
        "survivor_reason": build_survivor_reason(keep_reason),
        "doi_state": doi_state,
        "journal_state": journal_state,
        "field_actions": field_actions,
        "child_table_actions": child_actions,
        "author_content_conflicts": author_conflict_report["conflicts"],
        "unresolved_conflicts": blockers,
        "data_loss_risks": data_loss_risks,
        "requires_human_approval": classification != "SAFE_PLAN_CANDIDATE",
        "execution_permitted": False,
    }


# ---------------------------------------------------------------------------
# DB-facing helpers (SELECT only)
# ---------------------------------------------------------------------------

def fetch_paper_row(cur, pid):
    cols_sql = ', '.join(f'"{c}"' for c in (["PaperID"] + RP_COLUMNS))
    cur.execute(f'SELECT {cols_sql} FROM "ResearchPaper" WHERE "PaperID" = %s', (pid,))
    row = cur.fetchone()
    if row is None:
        return None
    d = dict(zip(["PaperID"] + RP_COLUMNS, row))
    doi = (d.get("DOI") or "").strip().lower()
    doi = doi.removeprefix("https://doi.org/").removeprefix("doi.org/")
    d["DOI"] = doi or None
    return d


def fetch_dependency_counts(cur, winner_id, loser_id):
    out = {}
    seen_tables = set(t for t, *_ in CHILD_TABLE_SPECS)
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public' AND table_name = ANY(%s)""",
                (list(seen_tables),))
    existing = {r[0] for r in cur.fetchall()}

    for table, fk_col, *_ in CHILD_TABLE_SPECS:
        key = (table, fk_col)
        if table not in existing:
            out[key] = {"winner": 0, "loser": 0}
            continue
        cur.execute(f'SELECT "{fk_col}", COUNT(*) FROM "{table}" '
                    f'WHERE "{fk_col}" = ANY(%s) GROUP BY "{fk_col}"',
                    ([winner_id, loser_id],))
        counts = {r[0]: r[1] for r in cur.fetchall()}
        out[key] = {"winner": counts.get(winner_id, 0), "loser": counts.get(loser_id, 0)}
    return out


def fetch_authors_rows(cur, pid):
    """SELECT-only. Returns Authors rows for one PaperID as dicts with at
    least UserID and AuthorNameRaw -- the shape author_content_conflicts()
    (dedup_papers.py, Phase 4E Gap 2) expects."""
    cur.execute('SELECT "UserID", "AuthorNameRaw" FROM "Authors" WHERE "PaperID" = %s', (pid,))
    return [{"UserID": uid, "AuthorNameRaw": raw} for uid, raw in cur.fetchall()]


def build_papers_dict_for_pure_functions(cur, ids):
    """Mirrors dedup_papers.py's own load_papers() shape closely enough to
    feed pair_confidence()/hard_exclusion_reason()/choose_keep() correctly."""
    from dedup_papers import norm_title
    cur.execute('''SELECT "PaperID","Title","DOI","PubYear","IsVerified","Source",
        COALESCE(("RawData_Log"->'cited_by'->>'value')::int,
                  ("RawData_Log"->>'cited_by_count')::int, 0)
        FROM "ResearchPaper" WHERE "PaperID" = ANY(%s)''', (ids,))
    papers = {}
    for pid, title, doi, pub_year, verified, source, cit in cur.fetchall():
        clean_doi = (doi or '').strip().lower()
        clean_doi = clean_doi.removeprefix('https://doi.org/').removeprefix('doi.org/')
        papers[pid] = {
            'paper_id': pid, 'title': title or '', 'doi': clean_doi or None,
            'pub_year': pub_year, 'is_verified': bool(verified), 'source': source,
            'citations': int(cit or 0), 'fuzzy_key': norm_title(title),
            'authors': frozenset(), 'first_author': None,
        }
    cur.execute('SELECT "PaperID","UserID","AuthorOrder" FROM "Authors" WHERE "PaperID" = ANY(%s)', (ids,))
    by_pid = {}
    for pid, uid, order in cur.fetchall():
        by_pid.setdefault(pid, []).append((uid, order))
    for pid, rows in by_pid.items():
        if pid in papers:
            papers[pid]['authors'] = frozenset(uid for uid, _ in rows)
            ordered = sorted(rows, key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0, r[0]))
            papers[pid]['first_author'] = ordered[0][0] if ordered else None
    return papers


def generate_plan_for_real_pair(cur, id_a, id_b):
    """Given two real PaperIDs (order not assumed), determine winner/loser
    via dedup_papers.py's own choose_keep(), compute pair_confidence()/
    hard_exclusion_reason()/_is_distinct_record() the same way, then build
    the full plan. Read-only throughout."""
    from dedup_papers import choose_keep, choose_keep_reason, pair_confidence, hard_exclusion_reason, _is_distinct_record

    papers = build_papers_dict_for_pure_functions(cur, [id_a, id_b])
    if id_a not in papers or id_b not in papers:
        winner_id = id_a if id_a in papers else id_b
        loser_id = id_b if id_a in papers else id_a
        return generate_pair_plan(winner_id, loser_id, {}, {}, {}, None, None, None, None, missing_row=True)

    winner_id, losers = choose_keep([id_a, id_b], papers)
    loser_id = losers[0]
    keep_reason = choose_keep_reason(winner_id, losers, papers)
    pc = pair_confidence(winner_id, loser_id, papers)
    hard = hard_exclusion_reason(winner_id, loser_id, papers)
    distinct = _is_distinct_record(papers[winner_id]['title']) or _is_distinct_record(papers[loser_id]['title'])

    winner_row = fetch_paper_row(cur, winner_id)
    loser_row = fetch_paper_row(cur, loser_id)
    dep_counts = fetch_dependency_counts(cur, winner_id, loser_id)
    winner_authors = fetch_authors_rows(cur, winner_id)
    loser_authors = fetch_authors_rows(cur, loser_id)

    return generate_pair_plan(winner_id, loser_id, winner_row, loser_row, dep_counts,
                               pc, hard, distinct, keep_reason,
                               winner_authors=winner_authors, loser_authors=loser_authors)


# ---------------------------------------------------------------------------
# Phase 4Z -- case-only AuthorNameRaw human-review representation.
#
# Pure data contract + pure state machine ONLY. No DB access, no writes, no
# reference to MergeApproval/merge_approval.py/merge_executor.py anywhere in
# this section. A review decision produced here is a decision/evidence
# record, never an execution authorization -- see
# backend/reports/phase4z_case_only_human_review.md, Task D, for the full
# reasoning. Bridging REVIEWED_APPROVED to an actual MergeApproval write is
# an explicit, separate, NOT-built future step (Task H of that report).
#
# Phase 4Z, Task A finding: the existing AuthorReviewQueue table/endpoint
# (analytics/migrations/sprint8_author_review_queue.sql,
# analytics/reconciliation_views.py) is schematically shaped for a
# different question -- "does this ScrapedName on ONE PaperID belong to
# SuggestedUserID" -- not "do these two AuthorNameRaw variants on a
# SURVIVOR/LOSER PaperID pair, already sharing one UserID, differ only in
# formatting". Its CONFIRMED decision path also performs an immediate
# Authors INSERT, coupling decision and write in a way this phase's hard
# safety boundary (a review decision must never change execution
# permission) cannot reuse directly. This module therefore does not call
# into AuthorReviewQueue at all; it defines the narrower, self-contained
# representation this specific question needs, with no new table, no
# migration, and no new production approval system.
# ---------------------------------------------------------------------------

REVIEW_STATE_PLAN_GENERATED = "PLAN_GENERATED"
REVIEW_STATE_CASE_ONLY_FORMATTING_DIFFERENCE = "CASE_ONLY_FORMATTING_DIFFERENCE"
REVIEW_STATE_HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
REVIEW_STATE_REVIEWED_APPROVED = "REVIEWED_APPROVED"
REVIEW_STATE_REVIEWED_REJECTED = "REVIEWED_REJECTED"

CASE_ONLY_REVIEW_DISCLAIMER = "This review does NOT authorize merge execution."


def build_case_only_review_display(plan, conflict, tenant_id=None):
    """Pure function -- Phase 4Z, Task C. Builds the exact reviewer-facing
    data contract for ONE case-only-labeled author conflict. Never touches
    the DB itself; the caller supplies the already-fetched plan dict (as
    generate_pair_plan()/generate_plan_for_real_pair() already produce) and
    one conflict dict from plan["author_content_conflicts"] (as
    build_author_conflict_report()'s Phase 4Y annotation already produces).

    Refuses to build a review display for anything that is not actually
    case-only -- this function must never be the thing that lets a
    genuine conflict quietly look reviewable (Task F item 8's guard)."""
    conflict_type = conflict.get("author_conflict_type")
    if conflict_type != "CASE_ONLY_FORMATTING_DIFFERENCE":
        raise ValueError(
            f"build_case_only_review_display() called with a non-case-only conflict "
            f"(author_conflict_type={conflict_type!r}) -- refusing to represent a "
            f"genuine conflict as a case-only review item"
        )
    return {
        "survivor_paper_id": plan["survivor"],
        "loser_paper_id": plan["loser"],
        "survivor_author_name_raw": conflict["winner_author_name_raw"],
        "loser_author_name_raw": conflict["loser_author_name_raw"],
        "case_only_comparison_result": conflict_type,
        "case_only_comparison_reason": conflict.get("author_conflict_type_reason"),
        "explanation": (
            "Survivor and loser AuthorNameRaw differ ONLY in letter case; every "
            "other character, punctuation mark, whitespace position, and token "
            "is identical."
        ),
        "journal_state": plan.get("journal_state"),
        "tenant_id": tenant_id,
        "doi_state": plan.get("doi_state"),
        "plan_fingerprint": plan.get("plan_fingerprint"),
        "plan_classification": plan.get("classification"),
        "disclaimer": CASE_ONLY_REVIEW_DISCLAIMER,
    }


def advance_case_only_review(current_state, decision=None):
    """Pure state machine -- Phase 4Z, Task B/D. No DB access, no side
    effects, no import of merge_approval/merge_executor anywhere in this
    function's body.

    PLAN_GENERATED -> CASE_ONLY_FORMATTING_DIFFERENCE -> HUMAN_REVIEW_REQUIRED
        -> REVIEWED_APPROVED or REVIEWED_REJECTED (terminal, either way)

    decision, only consulted from HUMAN_REVIEW_REQUIRED: 'APPROVE' or
    'REJECT'. REVIEWED_APPROVED is a decision/evidence state only -- Task D's
    central rule: a positive decision here must NEVER, by itself, change
    MergeApproval.Status, executor authorization, AUTO_MATCH, or
    execution_permitted anywhere else in the codebase. Bridging this state
    to an actual MergeApproval write would require a separate, explicitly
    authorized future change (Task H) -- nothing in this function performs
    or implies one.

    Returns (new_state, reason). Raises ValueError on any invalid
    transition or missing/invalid decision -- there is no silent
    fallthrough state."""
    if current_state == REVIEW_STATE_PLAN_GENERATED:
        return REVIEW_STATE_CASE_ONLY_FORMATTING_DIFFERENCE, \
            "case-only AuthorNameRaw conflict detected in the plan"
    if current_state == REVIEW_STATE_CASE_ONLY_FORMATTING_DIFFERENCE:
        return REVIEW_STATE_HUMAN_REVIEW_REQUIRED, \
            "case-only conflicts always require human review; Phase 4Y Policy B permits no AUTO_MATCH path"
    if current_state == REVIEW_STATE_HUMAN_REVIEW_REQUIRED:
        if decision == "APPROVE":
            return REVIEW_STATE_REVIEWED_APPROVED, (
                "human reviewer recorded APPROVE -- this is a decision/evidence record "
                "only; it does NOT authorize merge execution and does not modify "
                "MergeApproval, ResearchPaper, Authors, or DOI"
            )
        if decision == "REJECT":
            return REVIEW_STATE_REVIEWED_REJECTED, \
                "human reviewer recorded REJECT -- progression stops here, no further state exists"
        raise ValueError(
            f"a decision of 'APPROVE' or 'REJECT' is required to leave "
            f"HUMAN_REVIEW_REQUIRED, got {decision!r}"
        )
    raise ValueError(f"no further transition defined from state {current_state!r} (terminal or unrecognized)")


PAIRS_PHASE4B1 = [
    (5481, 5207), (5482, 5232), (5549, 5548), (6088, 6086), (6189, 6153),
    (5434, 5329), (6091, 3875), (7572, 6645), (5289, 5392), (6107, 6109),
]


def main():
    from litrix_db import db
    conn = db()
    cur = conn.cursor()

    plans = []
    for a, b in PAIRS_PHASE4B1:
        plans.append(generate_plan_for_real_pair(cur, a, b))

    # Nonzero-child validation fixture -- SYNTHETIC, NOT a claimed real
    # duplicate. See Phase 4C report §7 for the full explanation: PaperID
    # 3898 is a real row with 25 real ExternalAuthors rows (a genuine
    # multi-institution co-authorship list) and a real DOI of its own; it is
    # paired here against 3875 (a real winner from the pairs above) SOLELY
    # to exercise the child_table_actions code path against real nonzero
    # DB data. No merge is implied, suggested, or performed.
    fixture_plan = generate_plan_for_real_pair(cur, 3875, 3898)
    fixture_plan["_fixture_note"] = (
        "SYNTHETIC VALIDATION FIXTURE -- 3898 is not a suspected duplicate "
        "of 3875 (both have real, different DOIs). Paired here only to "
        "prove child_table_actions reports real nonzero ExternalAuthors "
        "rows correctly. Not part of the 10 forensic pairs. No merge implied."
    )
    plans.append(fixture_plan)

    conn.close()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / "phase4c_merge_plans.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "execution_permitted": False,
            "note": "READ-ONLY plan output. No DB writes were performed to produce this file.",
            "plans": plans,
        }, f, ensure_ascii=False, indent=2, default=str)

    print(f"Wrote {len(plans)} plans to {out_path}", file=sys.stderr)
    for p in plans:
        print(p["pair"], p["classification"], "human_approval=", p["requires_human_approval"], file=sys.stderr)


if __name__ == "__main__":
    main()
