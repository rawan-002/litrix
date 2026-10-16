"""Phase 2A -- pure, offline, deterministic DOI resolver.

This package performs NO I/O of any kind:
  - no database access (no psycopg2, no Django ORM)
  - no network calls (no requests/httpx)
  - no file writes
  - no global mutable state

Every public function is a pure function of its arguments. Candidate
metadata (from Crossref/OpenAlex) and "our" paper metadata (from
ResearchPaper/Authors/Journals) must be fetched and normalized into the
dataclasses in `resolver.py` by a caller OUTSIDE this package -- that
caller (a later phase, not built here) is the only place network/DB access
may happen.

See resolver.py for the decision pipeline (AUTO_MATCH / REVIEW / REJECT)
and normalize.py for the string-normalization primitives it is built on.
"""
from .normalize import (
    normalize_author,
    normalize_doi,
    normalize_pages,
    normalize_title,
    normalize_venue,
    title_token_jaccard,
    title_tokens,
)
from .resolver import (
    AUTO_MATCH,
    AUTO_MATCH_THRESHOLD,
    DISAGREEMENT_REVIEW_FLOOR,
    REJECT,
    REVIEW,
    REVIEW_THRESHOLD,
    CandidateInput,
    PaperInput,
    ResolverResult,
    check_duplicate_doi,
    resolve,
)

__all__ = [
    "normalize_title", "normalize_author", "normalize_doi", "normalize_venue",
    "normalize_pages", "title_tokens", "title_token_jaccard",
    "PaperInput", "CandidateInput", "ResolverResult",
    "resolve", "check_duplicate_doi",
    "AUTO_MATCH", "REVIEW", "REJECT",
    "AUTO_MATCH_THRESHOLD", "REVIEW_THRESHOLD", "DISAGREEMENT_REVIEW_FLOOR",
]
