"""Publisher/DOI-pattern venue classification, independent of the API-based
verifier (verify_venue_authoritative.py calls into this, not the other way
around).

Each entry here is a deterministic, zero-API-call rule that is unambiguous by
construction (a publisher's own DOI minting convention), as opposed to the
verifier's Crossref/OpenAlex/DBLP opinion-gathering for cases with no such
signal. Add new publisher rules to publishers.py's PUBLISHER_DOI_RULES list -
never as another `if` branch in the verifier itself, so the rule count can
grow without turning classify() into a wall of conditionals.

    from venue_classifiers import classify_from_doi, BOOK, BOOK_CHAPTER
    verdict = classify_from_doi(doi)   # BOOK | BOOK_CHAPTER | JOURNAL | UNKNOWN
"""
from .doi_patterns import BOOK, BOOK_CHAPTER, CONFERENCE, JOURNAL, UNKNOWN, classify_from_doi

__all__ = [
    'BOOK', 'BOOK_CHAPTER', 'CONFERENCE', 'JOURNAL', 'UNKNOWN',
    'classify_from_doi',
]
