"""Phase 2A -- pure multi-signal DOI resolver.

No I/O of any kind. Every function here takes plain data in and returns
plain data out. Fetching "our" paper metadata (DB) and candidate metadata
(Crossref/OpenAlex) is entirely the responsibility of a caller OUTSIDE this
module, in a later phase. This module never imports psycopg2, Django, or
requests/httpx, and never will -- that is the whole point of Phase 2A.

DECISION CONTRACT
------------------
resolve(paper, candidates, known_dois) -> ResolverResult with
`.decision` in {AUTO_MATCH, REVIEW, REJECT}. AUTO_MATCH is the ONLY
decision a future phase may treat as eligible for a DB write, and even
then only after independent DOI re-validation (Phase 2C) and a duplicate
check against the REAL database (Phase 2A's check_duplicate_doi() takes an
in-memory dict on purpose -- see its docstring).

The guiding principle throughout, stated explicitly because the audit that
preceded this module found two production write-paths that violated it:
    False Negative (DOI left NULL) is acceptable.
    False Positive (wrong DOI written) is not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

try:
    from .normalize import (
        author_surname,
        normalize_doi,
        normalize_pages,
        normalize_venue,
        title_token_jaccard,
    )
except ImportError:  # pragma: no cover -- direct script/top-level import
    # (e.g. `python backend/doi_pipeline/benchmark.py`, which puts this
    # file's own directory on sys.path and imports it as a bare top-level
    # module, outside the `doi_pipeline` package context).
    from normalize import (  # type: ignore[no-redef]
        author_surname,
        normalize_doi,
        normalize_pages,
        normalize_venue,
        title_token_jaccard,
    )

# -----------------------------------------------------------------------------
# Decision constants
# -----------------------------------------------------------------------------

AUTO_MATCH = 'AUTO_MATCH'
REVIEW = 'REVIEW'
REJECT = 'REJECT'

# AUTO_MATCH_THRESHOLD mirrors the two most conservative DOI-recovery paths
# already trusted in this codebase rather than inventing a new number:
#   - find_missing_dois.py: TITLE_SIM_MIN = 0.90
#   - affiliation_verifier.py::resolve_doi_by_title: Jaccard >= 0.9
# Both were audited (Phase 1) and found to have zero known false positives
# in production. 0.90 keeps this resolver no more permissive than either.
AUTO_MATCH_THRESHOLD = 0.90

# Below this, evidence is too thin to be worth a human reviewer's time --
# routed straight to REJECT instead of flooding a REVIEW queue with noise
# that no reasonable reviewer would ever approve. This is a queue-quality
# choice, not a safety one: nothing below this line was ever going to reach
# AUTO_MATCH regardless of where REVIEW_THRESHOLD is drawn.
REVIEW_THRESHOLD = 0.75

# A separate, lower floor used ONLY when sources disagree or one source has
# no DOI (source_agreement in {'disagree', 'one_has_doi_one_no_doi'}).
# AUTO_MATCH is already structurally impossible on both those paths
# regardless of score (see resolve()) -- the only question left is whether
# the disagreement is worth a human's time (REVIEW) or the underlying
# evidence is so weak the disagreement is obviously noise (REJECT). That is
# a different question from "is this good enough to auto-write", so it
# gets its own threshold rather than reusing REVIEW_THRESHOLD. Calibrated
# against the Phase 2 benchmark: PaperID 7620 (score 0.643, a plausible
# Crossref candidate contradicted by an irrelevant OpenAlex result) must
# reach REVIEW; PaperID 7598 (score 0.214, both sources return unrelated,
# low-quality candidates) must not.
DISAGREEMENT_REVIEW_FLOOR = 0.50

# Signal weights. Author (0.35) intentionally outweighs title (0.30):
# empirically, in every real false-candidate case found during this
# pipeline's audit (PaperID 5645, 7618, 7615), a strong title score
# co-occurred with a wrong or absent author match -- title alone was never
# a reliable enough signal to justify equal or greater weight. Year/venue/
# bibliographic (0.35 combined) are corroborating-only: Volume/Issue/Pages
# are populated on only ~14.6% of ResearchPaper rows (measured on the live
# DB during the Phase 2 audit), so they can never be required, and venue
# matching here is a heuristic text comparison, not a unique key.
SIGNAL_WEIGHTS: Dict[str, float] = {
    'title': 0.30,
    'author': 0.35,
    'year': 0.10,
    'venue': 0.15,
    'bibliographic': 0.10,
}


# -----------------------------------------------------------------------------
# Input data classes (populated by a caller outside this package)
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class PaperInput:
    """'Our' paper's metadata, as already stored in Litrix. `authors` is a
    sequence of raw Latin-script name strings (e.g. from
    Authors.AuthorNameRaw) -- NOT Users.FullName_Ar (Arabic); see
    normalize.normalize_author()'s docstring. Leave a field None/empty
    when the data genuinely isn't available -- never invent a placeholder.
    """
    title: str
    authors: Sequence[str] = field(default_factory=tuple)
    year: Optional[int] = None
    venue: Optional[str] = None
    publication_type: Optional[str] = None  # ResearchPaper.PublicationType value
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None


@dataclass(frozen=True)
class CandidateInput:
    """A single candidate work from one external source (Crossref or
    OpenAlex). `doi` should be passed as-received (raw) -- this module
    normalizes it internally; do not pre-normalize before constructing.
    """
    source: str  # 'crossref' | 'openalex' | any caller-defined label
    doi: Optional[str]
    title: Optional[str]
    authors: Sequence[str] = field(default_factory=tuple)
    year: Optional[int] = None
    venue: Optional[str] = None
    publisher: Optional[str] = None
    doc_type: Optional[str] = None  # e.g. crossref 'type' / openalex 'type'
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None
    article_number: Optional[str] = None  # candidate-side only; no DB column exists for this


@dataclass(frozen=True)
class ResolverResult:
    decision: str
    doi: Optional[str]
    source: Optional[str]
    score: float
    signals: Dict[str, Optional[float]]
    reasons: List[str]
    warnings: List[str]
    source_agreement: str

    def to_dict(self) -> dict:
        return {
            'decision': self.decision,
            'doi': self.doi,
            'source': self.source,
            'score': round(self.score, 3),
            'signals': self.signals,
            'reasons': list(self.reasons),
            'warnings': list(self.warnings),
            'source_agreement': self.source_agreement,
        }


@dataclass(frozen=True)
class _CandidateScore:
    candidate: CandidateInput
    score: float
    signals: Dict[str, Optional[float]]
    gate_failure: Optional[str]


# -----------------------------------------------------------------------------
# Individual signals
# -----------------------------------------------------------------------------

def author_signal(our_authors: Sequence[str], cand_authors: Sequence[str]
                   ) -> tuple[Optional[float], Optional[float]]:
    """Returns (first_author_match, author_overlap), each in [0.0, 1.0] or
    None. Both are None ("missing evidence") if EITHER side has no usable
    surname -- missing evidence must never be scored as a mismatch (Phase 2
    requirement). `author_overlap` is |our_surnames ∩ cand_surnames| /
    |our_surnames| (the fraction of OUR authors the candidate accounts
    for, not a symmetric Jaccard -- our own author list is the ground
    truth we're validating against).
    """
    our_sur = {s for s in (author_surname(a) for a in (our_authors or ())) if s}
    cand_sur = {s for s in (author_surname(a) for a in (cand_authors or ())) if s}
    if not our_sur or not cand_sur:
        return None, None
    overlap = len(our_sur & cand_sur) / len(our_sur)
    our_first = author_surname(our_authors[0]) if our_authors else ''
    first_match = 1.0 if (our_first and our_first in cand_sur) else 0.0
    return first_match, overlap


def year_score(our_year: Optional[int], cand_year: Optional[int]) -> Optional[float]:
    """1.0 exact, 0.6 for a 1-year gap (online-first vs issue-year drift),
    0.0 for any gap > 1. None if either year is unknown -- missing year
    must not be treated as a contradiction (Phase 2 requirement)."""
    if our_year is None or cand_year is None:
        return None
    diff = abs(int(our_year) - int(cand_year))
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.6
    return 0.0


_VENUE_NOISE_WORDS = frozenset({
    'proceedings', 'of', 'the', 'conference', 'on', 'international', 'ieee',
    'acm', 'annual', 'symposium', 'workshop',
})


def _venue_core_tokens(text: Optional[str]) -> frozenset:
    norm = normalize_venue(text)
    return frozenset(
        w for w in norm.split() if w and w not in _VENUE_NOISE_WORDS and len(w) > 1
    )


def venue_similarity(our_venue: Optional[str], cand_venue: Optional[str]
                      ) -> Optional[float]:
    """Token-Jaccard over venue names with generic conference/publisher
    noise words removed. None if either side has no venue text -- venue is
    supporting evidence only (Phase 2 requirement), never a standalone
    match, so its absence must not count against a candidate."""
    if not our_venue or not cand_venue:
        return None
    ours, cand = _venue_core_tokens(our_venue), _venue_core_tokens(cand_venue)
    if not ours or not cand:
        return None
    return len(ours & cand) / len(ours | cand)


def bibliographic_score(paper: PaperInput, candidate: CandidateInput
                         ) -> Optional[float]:
    """Average of volume/issue/pages exact-match indicators, using only the
    fields present on BOTH sides. None if none of the three are comparable
    (our own ResearchPaper.Volume/Issue/Pages are populated on only ~14.6%
    of rows -- this will be None far more often than not, by design, and
    that must not penalize the candidate)."""
    parts: List[float] = []
    if paper.volume and candidate.volume:
        parts.append(1.0 if paper.volume.strip() == candidate.volume.strip() else 0.0)
    if paper.issue and candidate.issue:
        parts.append(1.0 if paper.issue.strip() == candidate.issue.strip() else 0.0)
    our_pages = normalize_pages(paper.pages)
    cand_pages = normalize_pages(candidate.pages)
    if our_pages and cand_pages:
        parts.append(1.0 if our_pages == cand_pages else 0.0)
    if not parts:
        return None
    return sum(parts) / len(parts)


# -----------------------------------------------------------------------------
# Publication-type gate
# -----------------------------------------------------------------------------

# Title-prefix patterns for special publication types that must never
# silently inherit the DOI of the "plain" work they relate to. Matched
# against the START of the (unnormalized) title, case-insensitive.
_SPECIAL_TYPE_PREFIX_PATTERNS: tuple[tuple[str, 're.Pattern'], ...] = (
    ('retraction_note', re.compile(r'^\s*retraction note', re.IGNORECASE)),
    ('retraction', re.compile(r'^\s*retraction\b', re.IGNORECASE)),
    ('corrigendum', re.compile(r'^\s*corrigendum', re.IGNORECASE)),
    ('correction', re.compile(r'^\s*correction(?:\s+to)?\s*[:\-]', re.IGNORECASE)),
    ('erratum', re.compile(r'^\s*erratum', re.IGNORECASE)),
    ('editorial', re.compile(r'^\s*editorial\b', re.IGNORECASE)),
    ('acknowledgment', re.compile(r'^\s*acknowledg', re.IGNORECASE)),
)

# Phase 2C -- thesis/dissertation indicators. Unlike the prefix patterns
# above, these are matched ANYWHERE in the title (re.search, not
# re.match): real thesis/dissertation titles vary in structure --
# "A Dissertation on X", "X: A PhD Thesis", "X (Doctoral Thesis)" are all
# real-world patterns, not just leading prefixes.
#
# Strict \b word boundaries are load-bearing here, not cosmetic: "thesis"
# is a substring of "synthesis", "hypothesis", "parenthesis", "antithesis"
# -- a naive substring check would misfire on ordinary research-paper
# titles (Phase 2C constraint: no generic blacklist that creates false
# positives). \bthesis\b matches none of those words.
#
# Deliberately EXCLUDES generic phrases like "contributions to" -- per
# the Phase 2C brief, that phrasing is too ambiguous to gate on by itself
# (it appears in ordinary paper titles too). This means the PaperID
# 5241/5439 boundary case (title: "Contributions to model-based testing
# of...", which never says "thesis" or "dissertation") is NOT caught by
# this gate -- see resolve()'s module-level notes / the Phase 2C report
# for why that is an accepted, documented residual risk rather than an
# oversight.
_THESIS_DISSERTATION_PATTERNS: tuple[tuple[str, 're.Pattern'], ...] = (
    ('thesis', re.compile(r'\bthesis\b', re.IGNORECASE)),
    ('dissertation', re.compile(r'\bdissertation\b', re.IGNORECASE)),
)

# Values from ResearchPaper.PublicationType (measured live on the DB during
# the Phase 2 audit: Research Article, Conference Paper, Book Chapter,
# Other, Review, Correction, Acknowledgment, Editorial) that count as
# "special" on our own side even if the title itself has no matching prefix.
OUR_SPECIAL_PUBLICATION_TYPES = frozenset({'Correction', 'Editorial', 'Acknowledgment'})

# Maps a ResearchPaper.PublicationType value to the SAME lowercase label
# vocabulary produced by detect_special_type(), so a DB-sourced type and a
# title-prefix-detected type are directly comparable (bug caught by
# tests/test_resolver.py: comparing 'Correction' (DB casing) against
# 'correction' (detect_special_type()'s label) as if they were the same
# string would silently and incorrectly flag every legitimate
# Correction-to-Correction match as a conflict).
_OUR_TYPE_TO_SPECIAL_LABEL: Dict[str, str] = {
    'Correction': 'correction',
    'Editorial': 'editorial',
    'Acknowledgment': 'acknowledgment',
}


def detect_special_type(title: Optional[str]) -> Optional[str]:
    """Returns a special-type label if `title` starts with a recognizable
    corrigendum/correction/erratum/retraction/editorial/acknowledgment
    prefix, OR contains an explicit thesis/dissertation indicator anywhere
    (Phase 2C), else None. Pure string matching -- no relation-metadata
    lookup (Crossref 'relation' field) is available at this offline stage;
    a later phase with network access may supplement this."""
    t = (title or '').strip()
    for name, pattern in _SPECIAL_TYPE_PREFIX_PATTERNS:
        if pattern.match(t):
            return name
    for name, pattern in _THESIS_DISSERTATION_PATTERNS:
        if pattern.search(t):
            return name
    return None


def publication_type_conflict(paper: PaperInput, candidate: CandidateInput
                               ) -> Optional[str]:
    """Hard-gate check: returns a reason string if `paper` and `candidate`
    are plausibly different publication classes (e.g. a Research Article
    being matched to a Corrigendum), else None.

    Deliberately conservative in scope: fires ONLY on the explicit
    correction/erratum/retraction/editorial/acknowledgment class (Phase 2
    section 8's explicit requirement) plus, as of Phase 2C, an explicit
    thesis/dissertation class -- not on ordinary Conference-Paper-vs-
    Journal-Article differences, and not on ambiguous phrasing like
    "contributions to" (too common in ordinary paper titles to gate on
    safely). A paper legitimately has both a conference version and a
    later journal version with different DOIs, so that distinction is
    left as soft evidence (via bibliographic/venue scoring), not a hard
    type gate.
    """
    our_special = (
        _OUR_TYPE_TO_SPECIAL_LABEL.get(paper.publication_type)
        or detect_special_type(paper.title)
    )
    cand_special = detect_special_type(candidate.title)

    if our_special and not cand_special:
        return f'type_conflict:our={our_special}_candidate=plain'
    if cand_special and not our_special:
        return f'type_conflict:our=plain_candidate={cand_special}'
    if our_special and cand_special and our_special != cand_special:
        return f'type_conflict:our={our_special}_candidate={cand_special}'
    return None


# -----------------------------------------------------------------------------
# Duplicate-DOI protection (abstract, in-memory interface)
# -----------------------------------------------------------------------------

def check_duplicate_doi(candidate_doi: Optional[str],
                         known_dois: Mapping[str, int]) -> Optional[int]:
    """Pure, in-memory duplicate check.

    `known_dois` maps a NORMALIZED doi string -> the PaperID that already
    owns it. Returns that PaperID if `candidate_doi` (normalized) is
    already taken, else None.

    This is an ABSTRACT interface on purpose. Phase 2A callers (including
    benchmark.py) pass a small, hardcoded, in-memory dict or {}. A later
    phase is responsible for populating `known_dois` from a real, read-only
    DB query (`SELECT lower(trim("DOI")), "PaperID" FROM "ResearchPaper"
    WHERE "DOI" IS NOT NULL`) immediately before resolving a batch -- this
    function itself never queries anything and never will.
    """
    nd = normalize_doi(candidate_doi)
    if nd is None:
        return None
    return known_dois.get(nd)


# -----------------------------------------------------------------------------
# Per-candidate scoring
# -----------------------------------------------------------------------------

def _weighted_score(signals: Dict[str, Optional[float]]) -> float:
    """Weighted sum over whichever signals are present (not None), with
    weights renormalized proportionally over the present subset -- a
    missing signal is excluded from the denominator, never treated as 0
    (Phase 2 requirement: "Do NOT treat missing evidence as negative
    evidence.")."""
    present: Dict[str, float] = {'title': signals['title']}  # title is always present

    fa, ao = signals.get('first_author'), signals.get('author_overlap')
    if fa is not None or ao is not None:
        present['author'] = ((fa or 0.0) + (ao or 0.0)) / 2
    if signals.get('year') is not None:
        present['year'] = signals['year']
    if signals.get('venue') is not None:
        present['venue'] = signals['venue']
    if signals.get('bibliographic') is not None:
        present['bibliographic'] = signals['bibliographic']

    weight_sum = sum(SIGNAL_WEIGHTS[k] for k in present)
    if weight_sum <= 0:
        return 0.0
    return sum(present[k] * SIGNAL_WEIGHTS[k] for k in present) / weight_sum


def _score_candidate(paper: PaperInput, candidate: CandidateInput,
                      known_dois: Mapping[str, int]) -> _CandidateScore:
    # Gate: malformed DOI claimed by the candidate itself.
    if candidate.doi and normalize_doi(candidate.doi) is None:
        return _CandidateScore(candidate, 0.0, {}, 'malformed_candidate_doi')

    # Gate: publication-type conflict (checked before scoring -- a
    # corrigendum must never even get a chance to accumulate a high score).
    type_conflict = publication_type_conflict(paper, candidate)
    if type_conflict:
        return _CandidateScore(candidate, 0.0, {}, type_conflict)

    title = title_token_jaccard(paper.title, candidate.title)
    fa, ao = author_signal(paper.authors, candidate.authors)
    yr = year_score(paper.year, candidate.year)
    ven = venue_similarity(paper.venue, candidate.venue)
    bib = bibliographic_score(paper, candidate)

    signals: Dict[str, Optional[float]] = {
        'title': round(title, 3),
        'first_author': fa,
        'author_overlap': (round(ao, 3) if ao is not None else None),
        'year': yr,
        'venue': (round(ven, 3) if ven is not None else None),
        'bibliographic': (round(bib, 3) if bib is not None else None),
    }

    # Gate: year gap > 1 when both years are known.
    if yr is not None and yr == 0.0:
        return _CandidateScore(candidate, 0.0, signals, 'year_gap_gt_1')

    # Gate: zero author overlap when both sides have usable author evidence.
    if ao is not None and ao == 0.0:
        return _CandidateScore(candidate, 0.0, signals, 'author_overlap_zero')

    # Gate: this specific candidate DOI is already owned by another paper.
    dup_owner = check_duplicate_doi(candidate.doi, known_dois)
    if dup_owner is not None:
        return _CandidateScore(candidate, 0.0, signals,
                                f'duplicate_doi_owned_by_paper_{dup_owner}')

    score = _weighted_score(signals)
    return _CandidateScore(candidate, score, signals, None)


# -----------------------------------------------------------------------------
# Source agreement
# -----------------------------------------------------------------------------

def source_agreement_state(candidates: Sequence[CandidateInput]) -> str:
    """Classifies the relationship between multiple independent candidates
    (Crossref, OpenAlex, ...) BEFORE any comparison against our paper.
    Crossref and OpenAlex are evaluated independently -- this resolver
    never uses a "Crossref first, OpenAlex only on failure" fallback (that
    exact pattern was identified as unsafe during the Phase 0/1 audit:
    backend/backfill_missing_dois.py's real production behavior only ever
    saw OpenAlex when the Crossref HTTP request itself failed, so it never
    got the chance to catch a case like PaperID 7618, where OpenAlex held
    the correct answer and Crossref held a confidently-wrong one).

    Returns one of:
      'no_candidates'        -- no source produced anything
      'single_source'        -- exactly one source produced a candidate,
                                 OR multiple sources produced candidates but
                                 none carries a usable DOI
      'agree'                -- >=2 candidates, all carry the same
                                 normalized DOI
      'disagree'             -- >=2 candidates carry >=2 different
                                 normalized DOIs
      'one_has_doi_one_no_doi' -- >=2 candidates, at least one has a DOI
                                 and at least one other does not
    """
    if not candidates:
        return 'no_candidates'
    if len(candidates) == 1:
        return 'single_source'

    normalized = [normalize_doi(c.doi) for c in candidates]
    with_doi = [d for d in normalized if d is not None]

    if not with_doi:
        return 'single_source'
    if len(with_doi) < len(candidates):
        return 'one_has_doi_one_no_doi'
    if len(set(with_doi)) == 1:
        return 'agree'
    return 'disagree'


# -----------------------------------------------------------------------------
# Top-level resolve()
# -----------------------------------------------------------------------------

def resolve(paper: PaperInput,
            candidates: Sequence[Optional[CandidateInput]],
            known_dois: Optional[Mapping[str, int]] = None,
            sources_attempted: Optional[int] = None) -> ResolverResult:
    """Pure decision function. `candidates` is typically [crossref_or_None,
    openalex_or_None] but any number/order is accepted; None entries are
    filtered out. `known_dois` defaults to {} (see check_duplicate_doi()'s
    docstring -- this function never queries a real database).

    `sources_attempted` (Phase 2F, optional, default None = unchanged
    behavior): the number of independent external sources the CALLER
    actually queried, regardless of how many produced a usable candidate.
    Without it, resolve() cannot tell apart two situations that otherwise
    look identical -- both arrive as a single CandidateInput:
      (a) only one source was ever relevant (e.g. re-validating an already-
          stored DOI against its own record -- there is nothing to attempt
          a second source against). Omit this parameter; AUTO_MATCH remains
          reachable exactly as before Phase 2F.
      (b) multiple sources were queried but only one responded (network
          failure, rate limit, empty result). Pass the true attempted
          count so a LOST independent source is never silently scored as
          if it had never existed -- see the Phase 2F safety invariant
          below and the PaperID 7545 regression test.
    Safety invariant (Phase 2F): loss of an independent external evidence
    source must never increase confidence in an AUTO_MATCH decision. This
    was found to be violated for case (b) when `sources_attempted` is not
    supplied -- a single surviving candidate reached AUTO_MATCH with the
    exact same evidence that produced REVIEW when a second (irrelevant,
    correctly-rejected) candidate was also present, purely because the
    second source's HTTP request failed. Passing `sources_attempted`
    restores the invariant by routing that specific case through the same
    conservative ceiling (DISAGREEMENT_REVIEW_FLOOR) already used for
    genuine source disagreement -- no new threshold, no new penalty.

    Returns a ResolverResult whose `.decision` is one of AUTO_MATCH /
    REVIEW / REJECT. AUTO_MATCH additionally guarantees:
      - no hard gate fired on the winning candidate
      - the candidate's DOI is present and well-formed
      - all present sources agree on that DOI (source_agreement == 'agree')
        OR only one source produced a candidate AND (no `sources_attempted`
        was supplied OR it confirms no other source was lost)
      - the candidate DOI is not already owned by another paper (given the
        known_dois the caller supplied)
      - the weighted score >= AUTO_MATCH_THRESHOLD
    Any single one of these failing forces REVIEW or REJECT -- there is no
    override path in this module that can force AUTO_MATCH despite a
    failure (Phase 2 requirement: no "overwhelming evidence" bypass was
    implemented, since defining "overwhelming" safely was explicitly out of
    scope for this phase).
    """
    known_dois = known_dois or {}
    present = [c for c in candidates if c is not None]
    reasons: List[str] = []
    warnings: List[str] = []

    if not present:
        return ResolverResult(REJECT, None, None, 0.0, {}, ['no_candidates'],
                               warnings, 'no_candidates')

    agreement = source_agreement_state(present)
    if (sources_attempted is not None and sources_attempted > len(present)
            and agreement == 'single_source'):
        # Case (b): a second (or later) source was attempted and lost --
        # do not let this look like an untroubled single-source case.
        agreement = 'single_source_after_failure'

    scored = [_score_candidate(paper, c, known_dois) for c in present]

    passing = [s for s in scored if s.gate_failure is None]
    best = max(passing, key=lambda s: s.score) if passing else \
        max(scored, key=lambda s: s.score)
    # Report the NORMALIZED DOI throughout -- this is the form suitable for
    # duplicate comparison and, later, for an actual DB write. The raw,
    # as-received candidate.doi is still available via result reconstruction
    # from the candidate objects if a caller needs it for display.
    best_doi = normalize_doi(best.candidate.doi)

    if best.gate_failure:
        reasons.append(f'hard_gate:{best.gate_failure}')
        return ResolverResult(REJECT, best_doi, best.candidate.source,
                               best.score, best.signals, reasons, warnings, agreement)

    if agreement == 'disagree':
        reasons.append('source_disagreement')
        decision = REVIEW if best.score >= DISAGREEMENT_REVIEW_FLOOR else REJECT
        return ResolverResult(decision, best_doi, best.candidate.source,
                               best.score, best.signals, reasons, warnings, agreement)

    if agreement == 'one_has_doi_one_no_doi':
        reasons.append('source_partial_doi_evidence')
        decision = REVIEW if best.score >= DISAGREEMENT_REVIEW_FLOOR else REJECT
        return ResolverResult(decision, best_doi, best.candidate.source,
                               best.score, best.signals, reasons, warnings, agreement)

    if agreement == 'single_source_after_failure':
        reasons.append('source_unavailable_reduced_confidence')
        decision = REVIEW if best.score >= DISAGREEMENT_REVIEW_FLOOR else REJECT
        return ResolverResult(decision, best_doi, best.candidate.source,
                               best.score, best.signals, reasons, warnings, agreement)

    if best_doi is None:
        reasons.append('best_candidate_has_no_usable_doi')
        decision = REVIEW if best.score >= REVIEW_THRESHOLD else REJECT
        return ResolverResult(decision, None, best.candidate.source,
                               best.score, best.signals, reasons, warnings, agreement)

    dup_owner = check_duplicate_doi(best_doi, known_dois)
    if dup_owner is not None:
        reasons.append(f'duplicate_doi_owned_by_paper_{dup_owner}')
        return ResolverResult(REJECT, best_doi, best.candidate.source,
                               best.score, best.signals, reasons, warnings, agreement)

    if best.score >= AUTO_MATCH_THRESHOLD:
        decision = AUTO_MATCH
    elif best.score >= REVIEW_THRESHOLD:
        decision = REVIEW
        reasons.append('score_below_auto_match_threshold')
    else:
        decision = REJECT
        reasons.append('score_below_review_threshold')

    return ResolverResult(decision, best_doi, best.candidate.source,
                           best.score, best.signals, reasons, warnings, agreement)
