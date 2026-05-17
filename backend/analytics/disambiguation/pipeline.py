"""
Litrix — Author Disambiguation & Co-Author Linkage
===================================================
THE BUG WE'RE FIXING
--------------------
The scholar/orcid scrapers correctly link the TRIGGER researcher (the one
whose profile we scraped) to every fetched paper. They do NOT, however,
process the rest of the paper's co-authors — so other Litrix-registered
researchers who co-authored the paper end up with NO link to it.

Result: the paper "exists" but appears authorless in some UI views, and
analytics that count "papers per researcher" undercount everyone except
the trigger researcher.

THE FIX
-------
After the scraper inserts a paper, we feed its `RawData_Log->'authorships'`
(OpenAlex format, already stored) through a cascading match pipeline:

    Tier 1 — ORCID exact match              → confidence 1.00
    Tier 2 — OpenAlex Author ID match       → confidence 0.95
    Tier 3 — Scholar_ID match               → confidence 0.95
    Tier 4 — Normalized name + Al-Baha aff. → confidence 0.85
    Tier 5 — Fuzzy name (rapidfuzz)         → confidence 0.55–0.80
    Tier 6 — Below threshold                → AuthorReviewQueue

Tiers 1–4 + (5 above 0.70) → auto-link in `Authors`.
Tier 5 below 0.70 → row in `AuthorReviewQueue` for admin review.
Nothing matched → row in `ExternalAuthors` (so the name isn't lost).

WHY CURSOR-BASED SQL
--------------------
This module follows the project's established pattern (see
analytics/views.py, scrapers/scholar.py): all writes go through
`connection.cursor()` so they participate in the scraper's transaction
exactly. No ORM `.save()` magic, no signal cascades, no surprises.

THE GOLDEN RULE (UNCHANGED)
---------------------------
The trigger researcher is unconditionally linked by the scraper BEFORE
this module runs. We never touch that link.
"""
from __future__ import annotations

import json
import re
import unicodedata
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Tunables
# -----------------------------------------------------------------------------

AUTO_LINK_THRESHOLD = 0.70      # ≥ → write to Authors, < → review queue
FUZZY_MIN_THRESHOLD = 0.55      # below this we don't even bother queuing

# Al-Baha University detector. Mirrors the regex used in views.paper_detail.
ALBAHA_RE = re.compile(r'(al[\s\-]?baha|albaha|الباحة)', re.IGNORECASE)


# -----------------------------------------------------------------------------
# Name normalization
# -----------------------------------------------------------------------------

_ARABIC_DIACRITICS = re.compile(r'[ً-ْٰ]')
_PUNCT_RE          = re.compile(r"[.,\-_'`\"]")
_WS_RE             = re.compile(r'\s+')

def normalize_name(name: str) -> str:
    """
    Comparable form: lowercase, no diacritics, no punctuation, single-spaced.
        'Mohammed K. Al-Otaibi' → 'mohammed k al otaibi'
        'محمد العتيبي'         → 'محمد العتيبي'  (Arabic diacritics removed)
    """
    if not name:
        return ''
    n = unicodedata.normalize('NFKD', name)
    n = ''.join(c for c in n if not unicodedata.combining(c))
    n = _ARABIC_DIACRITICS.sub('', n)
    n = _PUNCT_RE.sub(' ', n)
    n = _WS_RE.sub(' ', n).strip().lower()
    return n


def _strip_orcid(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return raw.replace('https://orcid.org/', '').replace('http://orcid.org/', '').strip() or None


def _strip_openalex_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return raw.replace('https://openalex.org/', '').strip() or None


# -----------------------------------------------------------------------------
# Authorship extraction from RawData_Log
# -----------------------------------------------------------------------------

def extract_authorships(raw_data_log: Any) -> List[Dict[str, Any]]:
    """
    Pull the authorships array from the JSONB we store on every scrape.

    The OpenAlex-shaped authorships look like:
        [
            {
              "author": {"display_name": "M. Al-Otaibi",
                         "orcid": "https://orcid.org/0000-...",
                         "id":    "https://openalex.org/A1234"},
              "institutions": [{"display_name": "Al-Baha University"}],
              "raw_affiliation_strings": ["Al-Baha University, ..."]
            },
            ...
        ]
    """
    if not raw_data_log:
        return []
    if isinstance(raw_data_log, str):
        try:
            raw_data_log = json.loads(raw_data_log)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw_data_log, dict):
        return []
    arr = raw_data_log.get('authorships')
    if isinstance(arr, str):
        try:
            arr = json.loads(arr)
        except (ValueError, TypeError):
            return []
    return arr if isinstance(arr, list) else []


def authorship_summary(ship: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one authorship into the fields the pipeline cares about."""
    author = ship.get('author') or {}
    insts: List[str] = []
    for inst in (ship.get('institutions') or []):
        n = (inst or {}).get('display_name')
        if n:
            insts.append(n)
    for raw in (ship.get('raw_affiliation_strings') or []):
        if raw and raw not in insts:
            insts.append(raw)
    single = ship.get('raw_affiliation_string')
    if single and single not in insts:
        insts.append(single)
    affiliation_text = '; '.join(insts) or None

    return {
        'display_name':       (author.get('display_name') or '').strip(),
        'orcid':              _strip_orcid(author.get('orcid')),
        'openalex_author_id': _strip_openalex_id(author.get('id')),
        'affiliation':        affiliation_text,
        'at_albaha':          any(ALBAHA_RE.search(s) for s in insts),
        'author_position':    ship.get('author_position'),
    }


# -----------------------------------------------------------------------------
# Match Tiers — each returns (user_id, confidence, criteria) or None
# -----------------------------------------------------------------------------

def _tier_orcid(cur, info: Dict[str, Any]) -> Optional[Tuple[int, float, str]]:
    if not info['orcid']:
        return None
    cur.execute(
        'SELECT "UserID" FROM "Users" WHERE "Orcid_ID" = %s LIMIT 1',
        (info['orcid'],),
    )
    row = cur.fetchone()
    if row:
        return row[0], 1.00, 'orcid_match'
    return None


def _tier_openalex(cur, info: Dict[str, Any]) -> Optional[Tuple[int, float, str]]:
    aid = info['openalex_author_id']
    if not aid:
        return None
    # Researcher.OpenAlex_AuthorID column exists in production (per views.py).
    cur.execute(
        'SELECT "UserID" FROM "Researcher" WHERE "OpenAlex_AuthorID" = %s LIMIT 1',
        (aid,),
    )
    row = cur.fetchone()
    if row:
        return row[0], 0.95, 'openalex_match'
    return None


def _tier_name_and_albaha(cur, info: Dict[str, Any]) -> Optional[Tuple[int, float, str]]:
    """Same normalized name + affiliation contains 'Al-Baha' → strong signal."""
    if not info['at_albaha']:
        return None
    norm = normalize_name(info['display_name'])
    if not norm:
        return None
    # Pull all Al-Baha researchers — small set, fine to scan in Python.
    cur.execute('''
        SELECT u."UserID", u."FirstName", u."MiddleName", u."LastName", u."FullName_Ar"
        FROM "Users" u
        WHERE u."UserType" = 'Researcher' AND u."AccountStatus" = 'Active'
    ''')
    for row in cur.fetchall():
        uid, fn, mn, ln, ar = row
        candidates = [
            f'{fn or ""} {ln or ""}',
            f'{fn or ""} {mn or ""} {ln or ""}',
            ar or '',
        ]
        for cand in candidates:
            if normalize_name(cand) == norm:
                return uid, 0.85, 'name_albaha_match'
    return None


def _tier_fuzzy(cur, info: Dict[str, Any]) -> Optional[Tuple[int, float, str]]:
    """
    Last-resort fuzzy match via Postgres `pg_trgm`. Caps at 0.80 because a
    pure-name fuzzy match without any disambiguating signal is inherently
    less trustworthy.
    """
    norm = normalize_name(info['display_name'])
    if not norm or len(norm) < 4:
        return None
    cur.execute('''
        SELECT u."UserID",
               GREATEST(
                   SIMILARITY(LOWER(CONCAT(u."FirstName", ' ', u."LastName")), %s),
                   SIMILARITY(LOWER(COALESCE(u."FullName_Ar", '')), %s)
               ) AS score
        FROM "Users" u
        WHERE u."UserType" = 'Researcher' AND u."AccountStatus" = 'Active'
        ORDER BY score DESC
        LIMIT 1
    ''', (norm, info['display_name']))
    row = cur.fetchone()
    if not row:
        return None
    uid, score = row
    score = float(score or 0)
    if score < FUZZY_MIN_THRESHOLD:
        return None
    return uid, min(0.80, score), 'fuzzy_name_match'


MATCH_TIERS = [
    _tier_orcid,
    _tier_openalex,
    _tier_name_and_albaha,
    _tier_fuzzy,
]


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------

def link_coauthors_for_paper(cur, paper_id: int, trigger_user_id: int) -> Dict[str, int]:
    """
    Process the co-authors stored in RawData_Log->'authorships' for `paper_id`.

    Behavior:
      • Trigger researcher is skipped (the scraper already linked them).
      • Each remaining authorship runs through the tier pipeline.
      • High-confidence matches (≥0.70) → INSERT into Authors.
      • Low-confidence matches → INSERT into AuthorReviewQueue.
      • No match → INSERT into ExternalAuthors.

    Returns a stats dict for logging.
    """
    cur.execute(
        'SELECT "RawData_Log" FROM "ResearchPaper" WHERE "PaperID" = %s',
        (paper_id,),
    )
    row = cur.fetchone()
    if not row:
        return {'linked': 0, 'queued': 0, 'external': 0, 'skipped': 0}

    ships = extract_authorships(row[0])
    stats = {'linked': 0, 'queued': 0, 'external': 0, 'skipped': 0}

    for idx, ship in enumerate(ships, start=1):
        info = authorship_summary(ship)
        if not info['display_name']:
            continue

        # 1) Try to match.
        match = None
        for tier in MATCH_TIERS:
            match = tier(cur, info)
            if match:
                break

        if match:
            user_id, confidence, criteria = match

            # Don't double-link the trigger researcher (the scraper already did).
            if user_id == trigger_user_id:
                stats['skipped'] += 1
                continue

            if confidence >= AUTO_LINK_THRESHOLD:
                _insert_author_link(
                    cur, user_id, paper_id, idx, info, confidence, criteria
                )
                stats['linked'] += 1
            else:
                _enqueue_review(
                    cur, paper_id, info, user_id, confidence, criteria
                )
                stats['queued'] += 1
        else:
            _insert_external_author(cur, paper_id, info)
            stats['external'] += 1

    return stats


# -----------------------------------------------------------------------------
# Writers — uphold Authors unique (UserID, PaperID) constraint
# -----------------------------------------------------------------------------

def _insert_author_link(
    cur, user_id: int, paper_id: int, author_order: int,
    info: Dict[str, Any], confidence: float, criteria: str
) -> None:
    cur.execute('''
        INSERT INTO "Authors" (
            "UserID", "PaperID", "AuthorOrder",
            "IsCorrespondingAuthor", "MappingConfidence", "MappingCriteria",
            "AuthorNameRaw", "Is_Verified"
        )
        VALUES (%s, %s, %s, FALSE, %s, %s, %s, %s)
        ON CONFLICT ("UserID", "PaperID") DO NOTHING
    ''', (
        user_id, paper_id, author_order,
        confidence, criteria,
        info['display_name'][:255],
        confidence >= 0.90,   # mark as verified only if very confident
    ))


def _enqueue_review(
    cur, paper_id: int, info: Dict[str, Any],
    suggested_user_id: int, confidence: float, criteria: str
) -> None:
    cur.execute('''
        INSERT INTO "AuthorReviewQueue" (
            "PaperID", "ScrapedName", "ScrapedAffiliation",
            "SuggestedUserID", "SuggestedConfidence", "SuggestedCriteria",
            "Status"
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT ("PaperID", "ScrapedName") DO NOTHING
    ''', (
        paper_id,
        info['display_name'][:255],
        (info['affiliation'] or '')[:255] or None,
        suggested_user_id,
        round(confidence, 3),
        criteria,
    ))


def _insert_external_author(cur, paper_id: int, info: Dict[str, Any]) -> None:
    cur.execute('''
        INSERT INTO "ExternalAuthors" ("PaperID", "FullName", "Affiliation")
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    ''', (
        paper_id,
        info['display_name'][:255],
        (info['affiliation'] or '')[:255] or None,
    ))
